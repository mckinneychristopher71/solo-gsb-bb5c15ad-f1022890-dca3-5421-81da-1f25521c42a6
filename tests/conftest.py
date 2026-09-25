"""Shared pytest setup: use a fresh throwaway database for every run."""
from __future__ import annotations

import os
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)
os.unlink(_db_path)  # the app creates the file itself

os.environ["ADDRESS_DB_PATH"] = _db_path
os.environ["SAMPLE_CSV"] = os.path.join(_ROOT, "sample_data", "standard_addresses.csv")
os.environ.setdefault("AUTO_THRESHOLD", "0.80")
os.environ.setdefault("MARGIN_THRESHOLD", "0.06")
