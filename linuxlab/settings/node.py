from .base import *

LINUXLAB_PROFILE = "node"
LINUXLAB_CAN_AUTHOR = False
LINUXLAB_CAN_IMPORT_PACKAGES = True

DATABASES = {"default": database_config("linuxlab_node_dev")}
