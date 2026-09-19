from django.apps import AppConfig


class SyncConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "linuxlab.sync"

    def ready(self):
        # Task 14: importing enqueue.py registers the one real
        # activity.completed syncable-event contract (T14-D20, T14-D21)
        # at Django startup, rather than relying on some code path
        # happening to import it first.
        #
        # Corrective B Task 10: register sync's diagnostic provider FACTORY
        # (the class). No settings snapshot, DB query, key read or network
        # call happens at startup -- only when a diagnosis constructs one.
        from linuxlab.core.diagnostics import register_diagnostic_provider

        from . import enqueue  # noqa: F401
        from .diagnostics import SyncDiagnosticProvider

        register_diagnostic_provider("sync", SyncDiagnosticProvider)
