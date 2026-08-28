from .base import *

LINUXLAB_PROFILE = "test"
LINUXLAB_CAN_AUTHOR = False
LINUXLAB_CAN_IMPORT_PACKAGES = False

DATABASES = {"default": database_config("linuxlab_test")}
