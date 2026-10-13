import os
import sys

if os.name != "nt":
    sys.exit("Windows is required.")
if sys.version_info < (3, 12):
    sys.exit("Python 3.12 or later is required.")
