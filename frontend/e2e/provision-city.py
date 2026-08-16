"""Register Valencia in this browser suite's own database, as an operator would.

The product ships no city: since the startup seeding was removed, an isolated
install reports every input as missing, so the Files screen has nothing active
and `Run preflight` stays disabled.  The Python suite already answers this with
`conftest.provision_reference_city`; the browser suite needs the same rows.

This runs from the web-server command rather than from `globalSetup` for one
measured reason: Playwright starts the web server *before* `globalSetup`, so a
database written there arrives after the server has already read an empty one.
Chaining it ahead of `python -m workbench` also guarantees it inherits exactly
the same isolated `WORKBENCH_*` environment, instead of a second copy of the
config re-deriving a different run id and writing to a different root.

It imports `reference_city` and never `conftest`: importing the latter re-points
every root at a throwaway pytest directory, which is how the first version of
this provisioned a database nobody would ever read.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "tests"))

from reference_city import provision_reference_city  # noqa: E402

if __name__ == "__main__":
    print("provisioned:", provision_reference_city(), flush=True)
