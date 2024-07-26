import uuid
from functools import wraps
from typing import Any, Callable, Optional, Protocol, TypedDict, TypeVar, cast

import dbos_transact.utils as utils
from dbos_transact.error import DBOSWorkflowConflictUUIDError
from dbos_transact.transaction import TransactionContext
from dbos_transact.workflows import WorkflowContext

from .application_database import ApplicationDatabase, TransactionResultInternal
from .dbos_config import ConfigFile, load_config
from .logger import config_logger, dbos_logger
from .system_database import SystemDatabase, WorkflowInputs, WorkflowStatusInternal


class WorkflowProtocol(Protocol):
    __qualname__: str

    def __call__(self, ctx: WorkflowContext, *args: Any, **kwargs: Any) -> Any: ...


Workflow = TypeVar("Workflow", bound=WorkflowProtocol)


class TransactionProtocol(Protocol):
    __qualname__: str

    def __call__(self, ctx: TransactionContext, *args: Any, **kwargs: Any) -> Any: ...


Transaction = TypeVar("Transaction", bound=TransactionProtocol)


class WorkflowInputContext(TypedDict):
    workflow_uuid: str


class DBOS:
    def __init__(self, config: Optional[ConfigFile] = None) -> None:
        if config is None:
            config = load_config()
        config_logger(config)
        dbos_logger.info("Initializing DBOS!")
        self.config = config
        self.sys_db = SystemDatabase(config)
        self.app_db = ApplicationDatabase(config)

    def destroy(self) -> None:
        self.sys_db.destroy()
        self.app_db.destroy()

    def workflow(self) -> Callable[[Workflow], Workflow]:
        def decorator(func: Workflow) -> Workflow:
            @wraps(func)
            def wrapper(_ctxt: WorkflowContext, *args: Any, **kwargs: Any) -> Any:
                input_ctxt = cast(WorkflowInputContext, _ctxt)
                workflow_uuid = input_ctxt["workflow_uuid"]
                status: WorkflowStatusInternal = {
                    "workflow_uuid": workflow_uuid,
                    "status": "PENDING",
                    "name": func.__qualname__,
                    "output": None,
                    "error": None,
                }
                self.sys_db.update_workflow_status(status)

                inputs: WorkflowInputs = {
                    "args": args,
                    "kwargs": kwargs,
                }
                self.sys_db.update_workflow_inputs(
                    workflow_uuid, utils.serialize(inputs)
                )

                ctx = WorkflowContext(workflow_uuid)

                try:
                    output = func(ctx, *args, **kwargs)
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

            return cast(Workflow, wrapper)

        return decorator

    def wf_ctx(self, workflow_uuid: Optional[str] = None) -> WorkflowContext:
        workflow_uuid = workflow_uuid if workflow_uuid else str(uuid.uuid4())
        input_ctxt: WorkflowInputContext = {"workflow_uuid": workflow_uuid}
        return cast(WorkflowContext, input_ctxt)

    def transaction(self) -> Callable[[Transaction], Transaction]:
        def decorator(func: Transaction) -> Transaction:
            @wraps(func)
            def wrapper(_ctxt: TransactionContext, *args: Any, **kwargs: Any) -> Any:
                input_ctxt = cast(WorkflowContext, _ctxt)
                input_ctxt.function_id += 1
                with self.app_db.sessionmaker() as session:
                    txn_output: TransactionResultInternal = {
                        "workflow_uuid": input_ctxt.workflow_uuid,
                        "function_id": input_ctxt.function_id,
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
                                execution_options={"isolation_level": "REPEATABLE READ"}
                            )
                            txn_ctxt = TransactionContext(
                                session, input_ctxt.function_id
                            )
                            # Check recorded output for OAOO
                            recorded_output = (
                                ApplicationDatabase.check_transaction_execution(
                                    session,
                                    input_ctxt.workflow_uuid,
                                    txn_ctxt.function_id,
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
                                    return utils.deserialize(recorded_output["output"])
                                else:
                                    raise Exception("Output and error are both None")
                            output = func(txn_ctxt, *args, **kwargs)
                            txn_output["output"] = utils.serialize(output)
                            ApplicationDatabase.record_transaction_output(
                                txn_ctxt.session, txn_output
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
