"""Safe command-line control for the LIVTRA NANOCORE."""

import logging

__version__ = "0.1.0"

# A library logs through the standard module and prints nothing until the application
# (the CLI with -v, or the server) installs a handler.
logging.getLogger("nanocore").addHandler(logging.NullHandler())
