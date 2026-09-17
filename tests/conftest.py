"""The modules under test are written without Home Assistant imports, so they run on their own.

`decode` falls back to a plain `from conversions import ...` when it has no parent package,
which is what makes this work: the integration directory goes on the path and the modules are
imported directly.
"""

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "optov")
)
