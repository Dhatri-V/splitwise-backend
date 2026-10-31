"""Structured CloudRCA logs: request/error/heartbeat events, privacy, and unchanged API behaviour."""

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

import main
import observability
from database import engine

LOG = Path(os.environ["SPLITWISE_LOG_FILE"])
DB_PATH = engine.url.database
PRIVATE = {"title": "SYNTHETIC-PRIVATE-TITLE-7781", "amount": 4321.98, "paid_by": "SYNTHETIC-PRIVATE-NAME-QZ"}


@pytest.fixture(autouse=True)
def fresh_log() -> None:
    LOG.unlink(missing_ok=True)


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app, raise_server_exceptions=False)


def lines() -> list:
    observability.flush()
    return [json.loads(line) for line in LOG.read_text().splitlines()] if LOG.exists() else []


def events(kind: str) -> list:
    return [line for line in lines() if line["event"] == kind]


def is_utc(value: str) -> bool:
    return value.endswith("Z") and datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset().total_seconds() == 0


def test_successful_read_request_is_logged_with_all_fields(client: TestClient) -> None:
    response = client.get("/expenses")
    assert response.status_code == 200
    [request] = events("request")
    assert is_utc(request["ts"])
    assert len(request["request_id"]) == 32
    assert (request["service"], request["instance_id"], request["method"], request["route"], request["status"]) == (
        "splitwise-api", "i-0test000001", "GET", "/expenses", 200,
    )
    assert request["duration_ms"] >= request["db_ms"] > 0
    assert request["is_write"] is False and request["in_flight"] >= 1
    assert events("error") == []


def test_write_request_is_flagged_and_logs_no_personal_data(client: TestClient) -> None:
    response = client.post("/expenses", json=PRIVATE)
    assert response.status_code == 200 and response.json()["title"] == PRIVATE["title"]  # API behaviour unchanged
    [request] = events("request")
    assert request["is_write"] is True and request["route"] == "/expenses" and request["method"] == "POST"
    text = LOG.read_text()
    for value in (PRIVATE["title"], PRIVATE["paid_by"], "4321.98", "4321"):
        assert value not in text
    for key in ("title", "amount", "paid_by", "body", "headers", "query"):
        assert f'"{key}"' not in text


def test_route_templates_hide_path_identifiers(client: TestClient) -> None:
    created = client.post("/expenses", json=PRIVATE).json()
    LOG.unlink()
    client.get(f"/expenses/{created['id']}")
    client.get("/no-such-page/8812734")
    routes = [line["route"] for line in events("request")]
    assert routes == ["/expenses/{expense_id}", "<unmatched>"]
    assert "8812734" not in LOG.read_text()


def test_application_500_logs_a_sanitized_application_error(client: TestClient) -> None:
    response = client.put("/expenses/99887766", json=PRIVATE)
    assert response.status_code == 500  # existing behaviour for a missing expense is preserved
    [request] = events("request")
    [error] = events("error")
    assert request["status"] == 500 and error["request_id"] == request["request_id"]
    assert (error["reported_by"], error["error_class"], error["route"], error["status"]) == (
        "application", "AttributeError", "/expenses/{expense_id}", 500,
    )
    assert "database_id" not in error and "operation" not in error
    assert error["message"] == "'NoneType' object has no attribute 'title'"  # allowlisted shape; 'title' is a field name
    assert "99887766" not in LOG.read_text() and PRIVATE["title"] not in LOG.read_text()


def test_sqlite_lock_logs_a_database_error_with_operation_and_wait(client: TestClient) -> None:
    holder = sqlite3.connect(DB_PATH, timeout=0)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        response = client.post("/expenses", json=PRIVATE)
    finally:
        holder.rollback()
        holder.close()
    assert response.status_code == 500
    [error] = events("error")
    assert (error["reported_by"], error["error_class"], error["database_id"], error["operation"]) == (
        "database", "sqlalchemy.exc.OperationalError", "splitwise-sqlite", "INSERT expenses",
    )
    assert error["message"] == "database is locked"
    assert error["db_wait_ms"] >= 150  # the 0.2 s busy timeout was spent waiting for the lock
    assert error["instance_id"] == "i-0test000001" and error["request_id"]
    [request] = events("request")
    assert request["is_write"] is True and request["db_ms"] >= error["db_wait_ms"]
    text = LOG.read_text()
    for leaked in ("INSERT INTO", "VALUES", "[SQL", "parameters", PRIVATE["title"], PRIVATE["paid_by"]):
        assert leaked not in text


def test_concurrent_requests_report_in_flight_counts(client: TestClient) -> None:
    gate = threading.Barrier(4)

    def slow() -> dict:
        time.sleep(0.25)
        return {"ok": True}

    main.app.add_api_route("/__test/slow", slow)  # test-only route; not part of the deployed app
    try:
        def call() -> None:
            gate.wait()
            assert client.get("/__test/slow").status_code == 200

        threads = [threading.Thread(target=call) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        main.app.router.routes[:] = [route for route in main.app.router.routes if getattr(route, "path", "") != "/__test/slow"]
    counts = [line["in_flight"] for line in events("request")]
    assert len(counts) == 4 and max(counts) >= 2
    assert len({line["request_id"] for line in events("request")}) == 4


def test_heartbeat_reports_vm_and_pool_statistics() -> None:
    record = observability.Observability(engine, observability.JsonLineSink(str(LOG))).heartbeat()
    assert record["event"] == "heartbeat" and is_utc(record["ts"]) and record["instance_id"] == "i-0test000001"
    assert record["cpu_count"] >= 1 and record["load_1m"] >= 0
    assert 0 < record["disk_free_mb"] <= record["disk_total_mb"]
    assert {"db_pool_size", "db_pool_checked_out"} <= set(record)
    if Path("/proc/meminfo").exists():
        assert 0 < record["mem_available_mb"] <= record["mem_total_mb"]


def test_heartbeat_thread_writes_periodically_and_stops() -> None:
    instance = observability.Observability(engine, observability.JsonLineSink(str(LOG)))
    instance.start_heartbeat(0.05)
    time.sleep(0.3)
    instance.stop_heartbeat()
    count = len(events("heartbeat"))
    time.sleep(0.15)
    assert count >= 2 and len(events("heartbeat")) == count


def test_sanitizer_logs_only_allowlisted_message_shapes() -> None:
    wrapped = OperationalError("INSERT INTO expenses (title) VALUES (?)", ("SYNTHETIC-PRIVATE-TITLE-7781",), sqlite3.OperationalError("database is locked"))
    assert observability.sanitize_message(wrapped) == "database is locked"
    schema = OperationalError("SELECT 1", (), sqlite3.OperationalError("no such table: expenses"))
    assert observability.sanitize_message(schema) == "no such table: expenses"
    syntax = OperationalError("x", (), sqlite3.OperationalError('near "Jane": syntax error'))
    assert observability.sanitize_message(syntax) == "OperationalError (message withheld)"
    assert observability.sanitize_message(RuntimeError("Cannot process Jane-Doe")) == "RuntimeError (message withheld)"
    assert observability.sanitize_message(ValueError("invalid amount 4321.98 for SYNTHETIC-PRIVATE-NAME-QZ")) == "ValueError (message withheld)"
    assert observability.sanitize_message(AttributeError("'NoneType' object has no attribute 'Jane Doe'")) == "AttributeError (message withheld)"
    assert observability.operation_of("SELECT expenses.id FROM expenses WHERE expenses.id = ?") == "SELECT expenses"
    assert observability.operation_of("UPDATE expenses SET title=? WHERE id=?") == "UPDATE expenses"


def test_client_request_id_headers_are_never_stored(client: TestClient) -> None:
    client.get("/expenses", headers={"X-Request-ID": "Jane-Doe"})
    client.get("/expenses", headers={"X-Request-ID": "Bearer secret-token value"})
    ids = [line["request_id"] for line in events("request")]
    assert all(len(value) == 32 and int(value, 16) >= 0 for value in ids)
    assert "Jane" not in LOG.read_text() and "secret" not in LOG.read_text()


def test_logging_failures_never_break_requests(client: TestClient, tmp_path: Path) -> None:
    original = observability.JsonLineSink.write
    observability.JsonLineSink.write = lambda self, record: (_ for _ in ()).throw(RuntimeError("disk full"))  # type: ignore[assignment]
    try:
        assert client.get("/expenses").status_code == 200
    finally:
        observability.JsonLineSink.write = original  # type: ignore[assignment]
    unwritable = observability.JsonLineSink(str(tmp_path))  # a directory, not a file
    unwritable.write({"event": "request"})  # swallowed, no exception
    assert unwritable.flush()


def test_every_line_matches_the_cloudrca_converter_contract(client: TestClient) -> None:
    client.get("/expenses")
    client.post("/expenses", json=PRIVATE)
    client.put("/expenses/424242", json=PRIVATE)
    observability.Observability(engine, observability.JsonLineSink(str(LOG))).write_heartbeat()
    numeric = (int, float)
    for line in lines():
        assert line["event"] in {"request", "error", "heartbeat"} and is_utc(line["ts"])
        assert isinstance(line["service"], str) and isinstance(line["instance_id"], str)
        if line["event"] == "request":
            assert all(isinstance(line[key], numeric) and not isinstance(line[key], bool) for key in ("status", "duration_ms", "db_ms", "in_flight"))
            assert isinstance(line["is_write"], bool) and line["route"].startswith(("/", "<"))
        if line["event"] == "error":
            assert line["reported_by"] in {"application", "database"} and line["error_class"] and line["message"]
            assert (line["reported_by"] == "database") == ("database_id" in line)


CLOUDRCA_REPO = os.environ.get("CLOUDRCA_REPO")


@pytest.mark.skipif(not CLOUDRCA_REPO, reason="set CLOUDRCA_REPO to a CloudRCA-Agent checkout to run the cross-repo check")
def test_real_logs_convert_and_normalize_in_cloudrca(client: TestClient, tmp_path: Path) -> None:
    client.post("/expenses", json=PRIVATE)
    client.put("/expenses/424242", json=PRIVATE)
    observability.Observability(engine, observability.JsonLineSink(str(LOG))).write_heartbeat()
    repo = Path(str(CLOUDRCA_REPO))
    output = tmp_path / "converted.jsonl"
    python = repo / ".venv" / "bin" / "python"
    subprocess.run([str(python), str(repo / "scripts" / "convert_cloudwatch_export.py"), "--app-log", str(LOG), "--output", str(output)], check=True, capture_output=True)
    check = "from cloudrca_backend.normalization import normalize_file; import sys; b = normalize_file(sys.argv[1]); print(b.statistics.accepted, b.statistics.rejected)"
    accepted, rejected = subprocess.run([str(python), "-c", check, str(output)], check=True, capture_output=True, text=True, cwd=repo).stdout.split()
    assert int(rejected) == 0 and int(accepted) >= 3


def test_pool_overflow_is_never_negative() -> None:
    record = observability.Observability(engine, observability.JsonLineSink(str(LOG))).heartbeat()
    assert record.get("db_pool_overflow", 0) >= 0


def test_instance_id_fallback_hashes_the_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPLITWISE_INSTANCE_ID")
    monkeypatch.setenv("SPLITWISE_DISABLE_IMDS", "1")
    monkeypatch.setattr(observability.socket, "gethostname", lambda: "Jane-Doe-MacBook.local")
    value = observability.resolve_instance_id()
    assert value.startswith("host-") and len(value) == 17 and "Jane" not in value


def test_slow_filesystem_never_blocks_callers_and_overflow_is_counted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_open = Path.open

    def slow_open(self: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        time.sleep(0.2)  # a stalled log filesystem
        return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", slow_open)
    sink = observability.JsonLineSink(str(tmp_path / "slow.jsonl"), queue_limit=3)
    started = time.perf_counter()
    for _ in range(10):
        sink.write({"event": "request"})
    assert time.perf_counter() - started < 0.1  # callers (the request path) never wait on the filesystem
    assert sink.dropped >= 6  # queue of 3 plus at most one record in the writer
    assert sink.flush(timeout=3)
