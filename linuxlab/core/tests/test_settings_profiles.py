from django.conf import settings


def test_settings_profile_is_test():
    assert settings.LINUXLAB_PROFILE == "test"


def test_settings_use_postgresql_engine():
    assert settings.DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql"
