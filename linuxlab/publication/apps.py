from django.apps import AppConfig


class PublicationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "linuxlab.publication"

    def ready(self):
        # Corrective B Task 10: register publication's diagnostic provider
        # FACTORY (the class). No key file is touched at startup.
        from linuxlab.core.diagnostics import register_diagnostic_provider

        from .diagnostics import PublicationDiagnosticProvider

        register_diagnostic_provider("publication", PublicationDiagnosticProvider)
