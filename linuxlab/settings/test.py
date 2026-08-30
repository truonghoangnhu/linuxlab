from .base import *

LINUXLAB_PROFILE = "test"
LINUXLAB_CAN_AUTHOR = False
LINUXLAB_CAN_IMPORT_PACKAGES = False
LINUXLAB_CAN_SERVE_LEARNER_CONTENT = False

DATABASES = {"default": database_config("linuxlab_test")}
