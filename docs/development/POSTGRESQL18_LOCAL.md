# PostgreSQL 18 Local Development

LinuxLab uses PostgreSQL for both Central and Node. This Mac already has PostgreSQL 18, so local development should use it rather than installing an older major version.

## Verify the server

```bash
psql --version
pg_isready
```

If `pg_isready` reports no response, first determine how PostgreSQL was installed:

```bash
brew services list | grep postgres || true
which psql
```

Start only the existing installation.

## Create a dedicated development role

Open PostgreSQL as a local superuser. Depending on installation this is commonly:

```bash
psql postgres
```

Then run:

```sql
CREATE ROLE linuxlab WITH LOGIN PASSWORD 'CHANGE_THIS_LOCAL_PASSWORD';
CREATE DATABASE linuxlab_central_dev OWNER linuxlab;
CREATE DATABASE linuxlab_node_dev OWNER linuxlab;
CREATE DATABASE linuxlab_test OWNER linuxlab;
\q
```

If the role or databases already exist, inspect them rather than recreating or dropping them.

## Environment convention

Do not commit real passwords. Use local environment files or shell exports.

Example Central values:

```text
POSTGRES_DB=linuxlab_central_dev
POSTGRES_USER=linuxlab
POSTGRES_PASSWORD=CHANGE_THIS_LOCAL_PASSWORD
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
```

Example Node values:

```text
POSTGRES_DB=linuxlab_node_dev
POSTGRES_USER=linuxlab
POSTGRES_PASSWORD=CHANGE_THIS_LOCAL_PASSWORD
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
```

## Connectivity check

```bash
PGPASSWORD='CHANGE_THIS_LOCAL_PASSWORD' \
psql -h 127.0.0.1 -U linuxlab -d linuxlab_central_dev -c 'select version();'
```

Repeat for `linuxlab_node_dev`.

## Important architecture rule

Central and Node are allowed to use the same PostgreSQL **software version**, but they must be treated as separate application database instances. Do not copy application rows directly from one database into the other. LinuxLab synchronization uses signed packages and events.
