# Shared test setup.
#
# mon_utils imports boto3/psutil at module level; the pure functions under
# test need neither. Stub them ONLY when the real package is not installed,
# so environments that have them use the real imports and sys.modules is
# never poisoned for other test files (a later moto/psutil-based test would
# otherwise silently receive a MagicMock, depending on collection order).

import importlib.util
import sys
from unittest.mock import MagicMock

for _mod in ("boto3", "psutil"):
    if _mod not in sys.modules and importlib.util.find_spec(_mod) is None:
        sys.modules[_mod] = MagicMock()
