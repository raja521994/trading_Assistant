"""Test configuration: isolated temporary SQLite DB, no background jobs."""

import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="atd-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_tmp / 'test.db').as_posix()}"
os.environ["BACKGROUND_JOBS"] = "false"
os.environ["DATA_PROVIDER"] = "sample"
os.environ["LLM_PROVIDER"] = "template"
os.environ["SAMPLE_INTRADAY_SESSIONS"] = "6"
# Never touch real broker/data APIs from tests, even if backend/.env holds credentials.
os.environ["UPSTOX_ACCESS_TOKEN"] = ""
os.environ["KITE_ACCESS_TOKEN"] = ""
