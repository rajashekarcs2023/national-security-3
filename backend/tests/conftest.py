"""Pytest fixtures shared by the Phase E test suite.

These tests exercise the individual pipeline modules directly. We do NOT
import ``app.main`` — that would kick off the FastAPI lifespan (classifier
load, Foundry reference push, etc.) which is slow and unnecessary for
module-scoped unit tests.

Keep fixtures small, deterministic, and close to the code they test.
"""

from __future__ import annotations

import pathlib
import sys

# Make ``import app.*`` work even when pytest is launched from repo root
# with the collection happening inside ``backend/tests``. The backend
# directory is two levels up from this file.
_BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
