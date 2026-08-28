# macOS + VS Code Development Setup

## 1. Create the repository

```bash
mkdir -p ~/Projects/linuxlab
cd ~/Projects/linuxlab
git init -b main
code .
```

Copy the LinuxLab Development Kit into this repository root before asking Codex to implement anything.

## 2. Check local tools

```bash
xcode-select -p
python3.12 --version
psql --version
pg_isready
code --version
git --version
```

Expected database major version for this Mac: PostgreSQL 18.

If Python 3.12 is missing and Homebrew is available:

```bash
brew install python@3.12
```

If PostgreSQL 18 is installed with Homebrew but not running:

```bash
brew services start postgresql@18
```

If PostgreSQL came from Postgres.app, start it from the app instead. Do not install a second PostgreSQL distribution unnecessarily.

## 3. Create the Python virtual environment

```bash
cd ~/Projects/linuxlab
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
```

In VS Code:

1. Command Palette → **Python: Select Interpreter**
2. Select `~/Projects/linuxlab/.venv/bin/python`

Do not install Django/Wagtail globally.

## 4. Prepare PostgreSQL 18

Use the database setup in `POSTGRESQL18_LOCAL.md`.

Recommended local databases:

- `linuxlab_central_dev`
- `linuxlab_node_dev`
- `linuxlab_test`

Using separate Central and Node databases locally helps expose accidental database coupling early.

## 5. Initial Git hygiene

Before implementation, ensure `.gitignore` will include at least:

```text
.venv/
.env
.env.*
!.env.example
__pycache__/
*.py[cod]
.pytest_cache/
.ruff_cache/
.DS_Store
var/
media/
staticfiles/
.coverage
htmlcov/
```

Commit the documentation baseline first:

```bash
git add AGENTS.md START_HERE.md docs
git commit -m "docs: add LinuxLab architecture and development baseline"
```

## 6. Start Codex

Open `docs/codex/CODEX_MASTER_PROMPT.md`, paste its instruction into Codex, and tell it to execute **Task 1 only**.

After Task 1 finishes, review the report and Git diff before giving Task 2.
