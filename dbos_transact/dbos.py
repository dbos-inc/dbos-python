import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from logging import Logger
from typing import Any, Callable, List, Optional, Protocol, TypedDict, TypeVar, cast

from sqlalchemy.orm import Session

if sys.version_info < (3, 10):
    from typing_extensions import ParamSpec, TypeAlias
else:
    from typing import ParamSpec, TypeAlias

import dbos_transact.utils as utils
from dbos_transact.admin_sever import AdminServer
from dbos_transact.context import (
    DBOSContext,
    DBOSContextSwap,
    EnterDBOSCommunicator,
    EnterDBOSTransaction,
    EnterDBOSWorkflow,
    SetWorkflowUUID,
    assertCurrentDBOSContext,
    getLocalDBOSContext,
)
from dbos_transact.error import (
    DBOSRecoveryError,
    DBOSWorkflowConflictUUIDError,
    DBOSWorkflowFunctionNotFoundError,
)
from dbos_transact.workflow import WorkflowHandle

from .application_database import ApplicationDatabase, TransactionResultInternal
from .dbos_config import ConfigFile, load_config
from .logger import config_logger, dbos_logger
from .system_database import (
    OperationResultInternal,
    SystemDatabase,
    WorkflowInputs,
    WorkflowStatusInternal,
)

P = ParamSpec("P")
R = TypeVar("R", covariant=True)


class WorkflowProtocol(Protocol[P, R]):
    __qualname__: str

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R: ...


Workflow: TypeAlias = WorkflowProtocol[P, R]


class TransactionProtocol(Protocol):
    __qualname__: str

    def __call__(self, *args: Any, **kwargs: Any) -> Any: ...


Transaction = TypeVar("Transaction", bound=TransactionProtocol)


class CommunicatorProtocol(Protocol):
    __qualname__: str

    def __call__(self, *args: Any, **kwargs: Any) -> Any: ...


Communicator = TypeVar("Communicator", bound=CommunicatorProtocol)


class WorkflowInputContext(TypedDict):
    workflow_uuid: str


class ClassPropertyDescriptor:
    def __init__(self, fget: Optional[Any], fset: Optional[Any] = None) -> None:
        self.fget = fget
        self.fset = fset

    def __get__(self, obj: Any, objtype: Optional[Any] = None) -> Any:
        if objtype is None:
            objtype = type(obj)
        if self.fget is None:
            raise AttributeError("unreadable attribute")
        return self.fget(objtype)

    def __set__(self, obj: Any, value: Any) -> None:
        if not self.fset:
            raise AttributeError("can't set attribute")
        self.fset(type(obj), value)


def classproperty(func: Any) -> ClassPropertyDescriptor:
    return ClassPropertyDescriptor(func)


def classproperty_setter(func: Any) -> ClassPropertyDescriptor:
    return ClassPropertyDescriptor(None, func)


class DBOS:
    def __init__(self, config: Optional[ConfigFile] = None) -> None:
        if config is None:
            config = load_config()
        config_logger(config)
        dbos_logger.info("Initializing DBOS!")
        self.config = config
        self.sys_db = SystemDatabase(config)
        self.app_db = ApplicationDatabase(config)
        self.workflow_info_map: dict[str, WorkflowProtocol[Any, Any]] = {}
        self.executor = ThreadPoolExecutor(max_workers=64)
        self.admin_server = AdminServer(dbos=self)
        self._run_startup_recovery_thread = True
        if not os.environ.get("DBOS__VMID"):
            workflow_ids = self.sys_db.get_pending_workflows("local")
            self.executor.submit(self._startup_recovery_thread, workflow_ids)

    def destroy(self) -> None:
        self._run_startup_recovery_thread = False
        self.sys_db.destroy()
        self.app_db.destroy()
        self.admin_server.stop()
        self.executor.shutdown()

    def workflow(self) -> Callable[[Workflow[P, R]], Workflow[P, R]]:
        def decorator(func: Workflow[P, R]) -> Workflow[P, R]:
            func.__orig_func = func  # type: ignore

            @wraps(func)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                inputs: WorkflowInputs = {
                    "args": args,
                    "kwargs": kwargs,
                }
                with EnterDBOSWorkflow():
                    ctx = assertCurrentDBOSContext()
                    status = self._init_workflow(
                        ctx,
                        inputs=inputs,
                        wf_name=func.__qualname__,
                    )

                    return self._execute_workflow(status, func, *args, **kwargs)

            wrapped_func = cast(Workflow[P, R], wrapper)
            self.workflow_info_map[func.__qualname__] = wrapped_func
            return wrapped_func

        return decorator

    def start_workflow(
        self,
        func: Workflow[P, R],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> WorkflowHandle[R]:
        func = cast(Workflow[P, R], func.__orig_func)  # type: ignore
        inputs: WorkflowInputs = {
            "args": args,
            "kwargs": kwargs,
        }

        # TODO this is structured quite poorly
        cur_ctx = getLocalDBOSContext()
        new_wf_ctx = DBOSContext() if cur_ctx is None else cur_ctx.create_child()
        new_wf_ctx.id_assigned_for_next_workflow = new_wf_ctx.assign_workflow_id()

        new_wf_ctx.workflow_uuid = new_wf_ctx.id_assigned_for_next_workflow
        status = self._init_workflow(
            new_wf_ctx,
            inputs=inputs,
            wf_name=func.__qualname__,
        )
        new_wf_ctx.workflow_uuid = ""

        future = self.executor.submit(
            cast(Callable[..., R], self._execute_workflow_wthread),
            status,
            func,
            new_wf_ctx,
            *args,
            **kwargs,
        )
        return WorkflowHandle(new_wf_ctx.id_assigned_for_next_workflow, future)

    def _init_workflow(
        self, ctx: DBOSContext, inputs: WorkflowInputs, wf_name: str
    ) -> WorkflowStatusInternal:
        status: WorkflowStatusInternal = {
            "workflow_uuid": ctx.workflow_uuid,
            "status": "PENDING",
            "name": wf_name,
            "output": None,
            "error": None,
            "app_id": ctx.app_id,
            "app_version": ctx.app_version,
            "executor_id": ctx.executor_id,
        }
        self.sys_db.update_workflow_status(status)

        self.sys_db.update_workflow_inputs(ctx.workflow_uuid, utils.serialize(inputs))

        return status

    def _execute_workflow_wthread(
        self,
        status: WorkflowStatusInternal,
        func: Workflow[P, R],
        ctx: DBOSContext,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        with DBOSContextSwap(ctx):
            with EnterDBOSWorkflow():
                return self._execute_workflow(status, func, *args, **kwargs)

    def _execute_workflow(
        self,
        status: WorkflowStatusInternal,
        func: Workflow[P, R],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        try:
            output = func(*args, **kwargs)
        except DBOSWorkflowConflictUUIDError as wferror:
            # TODO: handle this properly by waiting/returning the output
            raise wferror
        except Exception as error:
            status["status"] = "ERROR"
            status["error"] = utils.serialize(error)
            self.sys_db.update_workflow_status(status)
            raise error

        status["status"] = "SUCCESS"
        status["output"] = utils.serialize(output)
        self.sys_db.update_workflow_status(status)
        return output

    def transaction(self) -> Callable[[Transaction], Transaction]:
        def decorator(func: Transaction) -> Transaction:
            @wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                with self.app_db.sessionmaker() as session:
                    with EnterDBOSTransaction(session) as ctx:
                        txn_output: TransactionResultInternal = {
                            "workflow_uuid": ctx.workflow_uuid,
                            "function_id": ctx.function_id,
                            "output": None,
                            "error": None,
                            "txn_snapshot": "",  # TODO: add actual snapshot
                            "executor_id": None,
                            "txn_id": None,
                        }
                        has_recorded_error = False
                        try:
                            # TODO: support multiple isolation levels
                            # TODO: handle serialization errors properly
                            with session.begin():
                                # This must be the first statement in the transaction!
                                session.connection(
                                    execution_options={
                                        "isolation_level": "REPEATABLE READ"
                                    }
                                )
                                # Check recorded output for OAOO
                                recorded_output = (
                                    ApplicationDatabase.check_transaction_execution(
                                        session,
                                        ctx.workflow_uuid,
                                        ctx.function_id,
                                    )
                                )
                                if recorded_output:
                                    if recorded_output["error"]:
                                        deserialized_error = utils.deserialize(
                                            recorded_output["error"]
                                        )
                                        has_recorded_error = True
                                        raise deserialized_error
                                    elif recorded_output["output"]:
                                        return utils.deserialize(
                                            recorded_output["output"]
                                        )
                                    else:
                                        raise Exception(
                                            "Output and error are both None"
                                        )
                                output = func(*args, **kwargs)
                                txn_output["output"] = utils.serialize(output)
                                assert ctx.sql_session is not None
                                ApplicationDatabase.record_transaction_output(
                                    ctx.sql_session, txn_output
                                )

                        except Exception as error:
                            # Don't record the error if it was already recorded
                            if not has_recorded_error:
                                txn_output["error"] = utils.serialize(error)
                                self.app_db.record_transaction_error(txn_output)
                            raise error
                return output

            return cast(Transaction, wrapper)

        return decorator

    def communicator(self) -> Callable[[Communicator], Communicator]:
        def decorator(func: Communicator) -> Communicator:
            @wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                with EnterDBOSCommunicator() as ctx:
                    comm_output: OperationResultInternal = {
                        "workflow_uuid": ctx.workflow_uuid,
                        "function_id": ctx.function_id,
                        "output": None,
                        "error": None,
                    }
                    recorded_output = self.sys_db.check_operation_execution(
                        ctx.workflow_uuid, ctx.function_id
                    )
                    if recorded_output:
                        if recorded_output["error"]:
                            deserialized_error = utils.deserialize(
                                recorded_output["error"]
                            )
                            raise deserialized_error
                        elif recorded_output["output"]:
                            return utils.deserialize(recorded_output["output"])
                        else:
                            raise Exception("Output and error are both None")
                    output = None
                    try:
                        # TODO: support configurable retries
                        output = func(*args, **kwargs)
                        comm_output["output"] = utils.serialize(output)
                    except Exception as error:
                        comm_output["error"] = utils.serialize(error)
                        raise error
                    finally:
                        self.sys_db.record_operation_result(comm_output)
                    return output

            return cast(Communicator, wrapper)

        return decorator

    def execute_workflow_uuid(self, workflow_uuid: str) -> WorkflowHandle[Any]:
        """
        This function is used to execute a workflow by a UUID for recovery.
        """
        status = self.sys_db.get_workflow_status(workflow_uuid)
        if not status:
            raise DBOSRecoveryError(workflow_uuid, "Workflow status not found")
        inputs = self.sys_db.get_workflow_inputs(workflow_uuid)
        if not inputs:
            raise DBOSRecoveryError(workflow_uuid, "Workflow inputs not found")
        wf_func = self.workflow_info_map.get(status["name"], None)
        if not wf_func:
            raise DBOSWorkflowFunctionNotFoundError(
                workflow_uuid, "Workflow function not found"
            )
        with SetWorkflowUUID(workflow_uuid):
            return self.start_workflow(wf_func, *inputs["args"], **inputs["kwargs"])

    def recover_pending_workflows(
        self, executor_ids: List[str] = ["local"]
    ) -> List[WorkflowHandle[Any]]:
        """
        Find all PENDING workflows and execute them.
        """
        workflow_handles: List[WorkflowHandle[Any]] = []
        for executor_id in executor_ids:
            if executor_id == "local" and os.environ.get("DBOS__VMID"):
                dbos_logger.debug(
                    f"Skip local recovery because it's running in a VM: {os.environ.get('DBOS__VMID')}"
                )
            dbos_logger.debug(
                f"Recovering pending workflows for executor: {executor_id}"
            )
            workflow_ids = self.sys_db.get_pending_workflows(executor_id)
            dbos_logger.debug(f"Pending workflows: {workflow_ids}")

            for workflowID in workflow_ids:
                handle = self.execute_workflow_uuid(workflowID)
                workflow_handles.append(handle)

        dbos_logger.info("Recovered pending workflows")
        return workflow_handles

    @classproperty
    def logger(cls) -> Logger:
        return dbos_logger  # TODO get from context if appropriate...

    @classproperty
    def sql_session(cls) -> Session:
        ctx = assertCurrentDBOSContext()
        assert ctx.is_transaction()
        rv = ctx.sql_session
        assert rv
        return rv

    @classproperty
    def workflow_id(cls) -> str:
        ctx = assertCurrentDBOSContext()
        assert ctx.is_in_workflow()
        return ctx.workflow_uuid

    def _startup_recovery_thread(self, workflow_ids: List[str]) -> None:
        """
        A background thread that attempts to recover local pending workflows on startup.
        """
        while self._run_startup_recovery_thread and len(workflow_ids) > 0:
            try:
                for workflowID in list(workflow_ids):
                    self.execute_workflow_uuid(workflowID)
                    workflow_ids.remove(workflowID)
            except DBOSWorkflowFunctionNotFoundError:
                time.sleep(1)
            except Exception as e:
                dbos_logger.error(
                    f"Exception encountered when recovering workflows: {repr(e)}"
                )
                raise e
