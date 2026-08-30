from .base import *

LINUXLAB_PROFILE = "central"
LINUXLAB_CAN_AUTHOR = True
LINUXLAB_CAN_IMPORT_PACKAGES = False
LINUXLAB_CAN_SERVE_LEARNER_CONTENT = False

DATABASES = {"default": database_config("linuxlab_central_dev")}
