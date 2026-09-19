"""Core's own diagnostic provider (Corrective B Task 10, approved spec §4):
DB_CONNECTIVITY and SCHEMA_STATE, always required in every mode, over the
public Task-2 primitives check_database()/check_schema_state() -- no second
DB/schema implementation, no migration execution, no DB mutation, and fixed
safe messages/details only (never a DB name, password or exception text).
"""

from .diagnostics import DiagnosticCheck, DiagnosticContext
from .health import check_database, check_schema_state

_DB_UNAVAILABLE = {"error_code": "DB_UNAVAILABLE"}
_SCHEMA_INCOMPATIBLE = {"error_code": "SCHEMA_INCOMPATIBLE"}


class CoreDiagnosticProvider:
    codes = frozenset({"DB_CONNECTIVITY", "SCHEMA_STATE"})

    def required_codes(self, context: DiagnosticContext) -> frozenset[str]:
        del context
        return self.codes

    def checks(self, context: DiagnosticContext) -> tuple[DiagnosticCheck, ...]:
        del context
        if not check_database():
            # Schema compatibility cannot be established without the
            # database: SCHEMA_STATE is FAIL, never PASS or omitted.
            return (
                DiagnosticCheck(
                    "DB_CONNECTIVITY", "FAIL", "Local database is unreachable.", _DB_UNAVAILABLE
                ),
                DiagnosticCheck(
                    "SCHEMA_STATE",
                    "FAIL",
                    "Schema state cannot be established without database connectivity.",
                    _DB_UNAVAILABLE,
                ),
            )
        database = DiagnosticCheck("DB_CONNECTIVITY", "PASS", "Local database is reachable.")
        if check_schema_state():
            schema = DiagnosticCheck(
                "SCHEMA_STATE", "PASS", "Local profile configuration and schema are compatible."
            )
        else:
            schema = DiagnosticCheck(
                "SCHEMA_STATE",
                "FAIL",
                "Local profile configuration or migration state is not compatible with this code.",
                _SCHEMA_INCOMPATIBLE,
            )
        return (database, schema)
