### Setting Up for Development

This package uses [`pdm`](https://pdm-project.org/en/latest/) for package and virtual environment management.
To install `pdm`, run:

```
curl -sSL https://pdm-project.org/install-pdm.py | python3 -
```

On Ubuntu, it may be necessary to do the following:
```
apt install python3.10-venv
```

To install dependencies:

```
pdm install
pdm run pre-commit install
```

To run unit tests:

```
pdm run pytest
```

NOTE: The tests need a Postgres database running on localhost:5432. To start one, run:

```bash
export PGPASSWORD=dbos
python3 dbos/templates/hello/start_postgres_docker.py
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

This command will add a new file under the `dbos/migrations/versions/` folder.
For more information, read [alembic tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html).

### Creating a Release

To cut a new release, run:

```shell
python3 make_release.py [--version_number <version>]
```

Version numbers follow [semver](https://semver.org/).
This command tags the latest commit with the version number and creates a release branch for it.
If a version number is not supplied, it automatically generated a version number by incrementing the last released minor version.

### Patching a release 

To patch a release, push the patch as a commit to the appropriate release branch.
Then, tag it with a version number:

```shell
git tag <version-number>
git push --tags
```

This version must follow semver: It should increment by one the patch number of the release branch.

### Preview Versions

Preview versions are [PEP440](https://peps.python.org/pep-0440/)-compliant alpha versions.
They can be published from `main`.
Their version number is `<next-release-version>a<number-of-git-commits-since-release>`.
You can install the latest preview version with `pip install --pre dbos`.

### Test Versions

Test versions are built from feature branches.
Their version number is `<next-release-version>a<number-of-git-commits-since-release>+<git-hash>`.

### Publishing

Run the [`Publish to PyPI`](./.github/workflows/publish.yml) GitHub action on the target branch.
