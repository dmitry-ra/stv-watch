"""Every test, and every process a test starts, sees a config directory that
does not exist: the password store of the machine running the tests is never
read, and nothing is made to point there."""

import os

os.environ["XDG_CONFIG_HOME"] = os.path.join(os.path.dirname(__file__), "no-such-config-dir")
