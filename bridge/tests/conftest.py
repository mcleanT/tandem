"""Put `bridge/` (the parent of the `cmux_bridge` package) on sys.path for the test suite."""

import sys
from pathlib import Path

BRIDGE_ROOT = Path(__file__).resolve().parents[1]
if str(BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(BRIDGE_ROOT))
