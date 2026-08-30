from .base import *

LINUXLAB_PROFILE = "node"
LINUXLAB_CAN_AUTHOR = False
LINUXLAB_CAN_IMPORT_PACKAGES = True
LINUXLAB_CAN_SERVE_LEARNER_CONTENT = True

DATABASES = {"default": database_config("linuxlab_node_dev")}
