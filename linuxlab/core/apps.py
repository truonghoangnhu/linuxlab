from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "linuxlab.core"

    def ready(self):
        # Corrective B Task 10: register core's diagnostic provider FACTORY
        # (the class itself). Nothing is instantiated, queried, read from
        # disk or sent over the network at startup.
        from .diagnostics import register_diagnostic_provider
        from .local_diagnostics import CoreDiagnosticProvider

        register_diagnostic_provider("core", CoreDiagnosticProvider)
