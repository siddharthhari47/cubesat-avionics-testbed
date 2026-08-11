"""
Repo-root conftest: puts the project root and simulator/ on sys.path for tests.

This exists so test modules (and the packages they import) resolve without each
file repeating its own sys.path bootstrap. `simulator/` and `ground-station/`
are directories of scripts rather than installable packages -- `ground-station`
cannot even be one, since a hyphen is not a legal identifier -- so a plain
`pip install -e .` would not fix this on its own, and V0 does not need a
packaging step to run its own tests.

Entry-point scripts executed directly (simulator/run_simulator.py,
ground-station/dashboard.py) still carry their own one-line bootstrap, because
pytest is not involved when they run.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent

for _p in (_ROOT, _ROOT / "simulator"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
