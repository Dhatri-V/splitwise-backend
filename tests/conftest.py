"""Isolated database and log file for every test session; set before the app modules are imported."""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = Path(tempfile.mkdtemp(prefix="splitwise-tests-"))
os.environ["SPLITWISE_DATABASE_URL"] = f"sqlite:///{WORK / 'test.db'}"
os.environ["SPLITWISE_LOG_FILE"] = str(WORK / "app.jsonl")
os.environ["SPLITWISE_INSTANCE_ID"] = "i-0test000001"
os.environ["SPLITWISE_HEARTBEAT_SECONDS"] = "0"
os.environ["SPLITWISE_SQLITE_TIMEOUT"] = "0.2"
sys.path.insert(0, str(ROOT))
