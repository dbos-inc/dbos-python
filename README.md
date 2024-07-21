# DBOS Transact Python

This package uses [`pdm`](https://pdm-project.org/en/latest/) for package and virtual environment management.
To install `pdm`, run:

```
curl -sSL https://pdm-project.org/install-pdm.py | python3 -
```

To install dependencies:

```
pdm install
```

To run unit tests:

```
pdm run pytest
```

To check types:

```
pdm run mypy .
```

We use alembic to manage system table schema migrations.
To generate a new migration, run:
```
pdm run alembic revision -m "<new migration name>"
```

This command will add a new file under the `dbos_transact/migrations/versions/` folder.
For more information, read [alembic tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html).