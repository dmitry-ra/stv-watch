"""Every test, and every process a test starts, sees an empty config
directory: the password store of the machine running the tests is never read."""

import os
import tempfile

os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="stv-watch-test-config-")
