"""Structured JSON-lines logs for CloudRCA cross-layer root cause analysis.

Writes three event types in the format read by CloudRCA's convert_cloudwatch_export.py:

* ``request``   - one line per HTTP request: route template, method, status, duration, database time,
                  whether it wrote, and how many requests were in flight.
* ``error``     - one line per unhandled exception, stating who raised it (``reported_by``: database for
                  errors raised by the SQLite driver, application otherwise) with a sanitized message.
* ``heartbeat`` - a periodic VM/OS snapshot: load, memory, disk, and connection-pool statistics.

Never logged: request bodies, query strings, raw paths, SQL text or parameters, expense titles, amounts,
names, headers, credentials, or tokens. Logging problems are swallowed so they can never break a request.
Standard library only.
"""

from __future__ import annotations

import atexit
import contextvars
import hashlib
import json
import os
import queue
import re
import shutil
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.request import Request, urlopen

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

SERVICE = os.environ.get("SPLITWISE_SERVICE_NAME", "splitwise-api")
DATABASE_ID = os.environ.get("SPLITWISE_DATABASE_ID", "splitwise-sqlite")
WRITE_VERBS = ("INSERT", "UPDATE", "DELETE", "REPLACE")
_OPERATION = re.compile(r"^\s*(INSERT\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM|SELECT\b.*?\bFROM)\s+[\"`\[]?(\w+)", re.I | re.S)
_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_.]{0,63}"
# Only these message shapes are ever logged; captured parts are code/schema identifiers, never user data.
_SAFE_DATABASE_MESSAGES = tuple(
    re.compile(pattern)
    for pattern in (
        r"database is locked",
        r"database table is locked",
        r"disk I/O error",
        r"database or disk is full",
        r"unable to open database file",
        r"attempt to write a readonly database",
        r"database disk image is malformed",
        rf"no such table: {_IDENTIFIER}",
        rf"no such column: {_IDENTIFIER}",
        rf"(?:UNIQUE|NOT NULL|CHECK|FOREIGN KEY) constraint failed(?:: {_IDENTIFIER}(?:, {_IDENTIFIER})*)?",
    )
)
_SAFE_APPLICATION_MESSAGES = tuple(
    re.compile(pattern)
    for pattern in (
        rf"'NoneType' object has no attribute '{_IDENTIFIER}'",
        rf"Class '{_IDENTIFIER}' is not mapped",
        r"QueuePool limit of size \d+ overflow \d+ reached, connection timed out, timeout \d+(?:\.\d+)?",
    )
)
QUEUE_LIMIT = 10_000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sanitize_message(exc: BaseException) -> str:
    """An allowlisted message shape, or '<ErrorClass> (message withheld)'.

    Filtering arbitrary exception text cannot remove an unquoted name or title, so unknown messages are never logged.
    """
    database_error = isinstance(exc, DBAPIError)
    text = str(exc.orig) if database_error and exc.orig is not None else str(exc)
    text = " ".join(text.split("[SQL:")[0].split())
    for pattern in _SAFE_DATABASE_MESSAGES if database_error else _SAFE_APPLICATION_MESSAGES:
        if pattern.fullmatch(text):
            return text
    return f"{type(exc).__name__} (message withheld)"


def error_class(exc: BaseException) -> str:
    kind = type(exc)
    return kind.__qualname__ if kind.__module__ == "builtins" else f"{kind.__module__}.{kind.__qualname__}"


def operation_of(statement: str) -> Optional[str]:
    """Verb and table only, e.g. 'INSERT expenses'; never the statement text or parameters."""
    match = _OPERATION.match(statement or "")
    return f"{match.group(1).split()[0].upper()} {match.group(2)}" if match else None


class JsonLineSink:
    """Queue JSON lines for a background writer; requests never wait on the filesystem.

    When the queue is full, records are dropped and counted rather than blocking. Failures are swallowed.
    """

    _instances: "List[JsonLineSink]" = []

    def __init__(self, path: Optional[str] = None, queue_limit: int = QUEUE_LIMIT) -> None:
        self.path = path if path is not None else os.environ.get("SPLITWISE_LOG_FILE", "logs/app.jsonl")
        self.dropped = 0
        self._queue: "queue.Queue[str]" = queue.Queue(maxsize=queue_limit)
        self._writer = threading.Thread(target=self._drain, name="cloudrca-log-writer", daemon=True)
        self._writer.start()
        JsonLineSink._instances.append(self)

    def write(self, record: Dict[str, Any]) -> None:
        try:
            self._queue.put_nowait(json.dumps(record, sort_keys=True, separators=(",", ":")))
        except queue.Full:
            self.dropped += 1
        except Exception:  # noqa: BLE001 - logging must never raise into request handling
            pass

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait (up to ``timeout``) until queued lines are written; used at shutdown and in tests."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        return not self._queue.unfinished_tasks

    def _drain(self) -> None:
        while True:
            line = self._queue.get()
            try:
                target = Path(self.path)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._queue.task_done()


def flush(timeout: float = 5.0) -> None:
    for sink in list(JsonLineSink._instances):
        sink.flush(timeout)


atexit.register(flush)


def resolve_instance_id() -> str:
    """SPLITWISE_INSTANCE_ID, else the EC2 instance ID from IMDSv2, else 'host-<hash of hostname>'.

    The fallback is hashed because workstation hostnames often contain a person's name.
    """
    configured = os.environ.get("SPLITWISE_INSTANCE_ID", "").strip()
    if configured:
        return configured
    if os.environ.get("SPLITWISE_DISABLE_IMDS") != "1":
        try:
            token_request = Request(
                "http://169.254.169.254/latest/api/token", method="PUT",
                headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
            )
            token = urlopen(token_request, timeout=0.3).read().decode()  # noqa: S310 - fixed link-local metadata URL
            identity = Request("http://169.254.169.254/latest/meta-data/instance-id", headers={"X-aws-ec2-metadata-token": token})
            value = urlopen(identity, timeout=0.3).read().decode().strip()  # noqa: S310
            if value:
                return value
        except Exception:  # noqa: BLE001 - not on EC2 or metadata disabled
            pass
    return "host-" + hashlib.sha256(socket.gethostname().encode("utf-8")).hexdigest()[:12]


@dataclass
class _RequestState:
    request_id: str
    db_ms: float = 0.0
    is_write: bool = False
    failed_operation: Optional[str] = None
    failed_statement_ms: Optional[float] = None


_STATE: contextvars.ContextVar[Optional[_RequestState]] = contextvars.ContextVar("cloudrca_request_state", default=None)


class Observability:
    def __init__(self, engine: Engine, sink: Optional[JsonLineSink] = None, clock: Callable[[], float] = time.perf_counter) -> None:
        self.engine = engine
        self.sink = sink or JsonLineSink()
        self.clock = clock
        self.instance_id = resolve_instance_id()
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()
        self._stop = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None

    # -- database timing (SQLAlchemy engine events) ---------------------------------------------------------
    def _before_cursor(self, conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        conn.info.setdefault("cloudrca_started", []).append(self.clock())
        state = _STATE.get()
        if state is not None and (statement or "").lstrip().upper().startswith(WRITE_VERBS):
            state.is_write = True

    def _after_cursor(self, conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        started = conn.info.get("cloudrca_started")
        state = _STATE.get()
        if started:
            elapsed = (self.clock() - started.pop()) * 1000
            if state is not None:
                state.db_ms += elapsed

    def _handle_error(self, context: Any) -> None:
        state = _STATE.get()
        connection = getattr(context, "connection", None)
        started = connection.info.get("cloudrca_started") if connection is not None else None
        if started:
            elapsed = (self.clock() - started.pop()) * 1000
            if state is not None:
                state.db_ms += elapsed
                state.failed_statement_ms = elapsed
        if state is not None:
            state.failed_operation = operation_of(getattr(context, "statement", "") or "")

    # -- requests -------------------------------------------------------------------------------------------
    async def middleware(self, request: Any, call_next: Callable[[Any], Any]) -> Any:
        # Always an opaque generated ID: client-supplied X-Request-ID values could carry names, so they are never stored.
        state = _RequestState(request_id=uuid.uuid4().hex)
        token = _STATE.set(state)
        with self._in_flight_lock:
            self._in_flight += 1
            in_flight = self._in_flight
        started = self.clock()
        status = 500
        failure: Optional[BaseException] = None
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        except Exception as exc:
            failure = exc
            raise  # unchanged behaviour: Starlette still turns this into a 500 response
        finally:
            with self._in_flight_lock:
                self._in_flight -= 1
            try:
                self._log_request(request, state, status, (self.clock() - started) * 1000, in_flight, failure)
            except Exception:  # noqa: BLE001
                pass
            _STATE.reset(token)

    def _log_request(
        self, request: Any, state: _RequestState, status: int, duration_ms: float, in_flight: int, failure: Optional[BaseException]
    ) -> None:
        route = getattr(request.scope.get("route"), "path", None) or "<unmatched>"
        base = {"service": SERVICE, "instance_id": self.instance_id, "request_id": state.request_id, "method": request.method, "route": route}
        self.sink.write({
            "event": "request", "ts": utc_now(), **base, "status": status, "duration_ms": round(duration_ms, 3),
            "db_ms": round(state.db_ms, 3), "is_write": state.is_write, "in_flight": in_flight,
        })
        if failure is None:
            return
        raised_by_database = isinstance(failure, DBAPIError)
        record: Dict[str, Any] = {
            "event": "error", "ts": utc_now(), **base, "status": status,
            "reported_by": "database" if raised_by_database else "application",
            "error_class": error_class(failure), "message": sanitize_message(failure), "duration_ms": round(duration_ms, 3),
        }
        if raised_by_database:
            record["database_id"] = DATABASE_ID
            if state.failed_operation:
                record["operation"] = state.failed_operation
            if state.failed_statement_ms is not None:
                record["db_wait_ms"] = round(state.failed_statement_ms, 3)
        self.sink.write(record)

    # -- VM/OS heartbeat -------------------------------------------------------------------------------------
    def heartbeat(self) -> Dict[str, Any]:
        record: Dict[str, Any] = {"event": "heartbeat", "ts": utc_now(), "service": SERVICE, "instance_id": self.instance_id}
        try:
            load_1m, load_5m, _ = os.getloadavg()
            record.update(load_1m=round(load_1m, 3), load_5m=round(load_5m, 3))
        except (AttributeError, OSError):
            pass
        if os.cpu_count():
            record["cpu_count"] = os.cpu_count()
        record.update(_memory_mb())
        try:
            usage = shutil.disk_usage(_database_directory(self.engine))
            record.update(disk_free_mb=round(usage.free / 1_048_576, 1), disk_total_mb=round(usage.total / 1_048_576, 1))
        except OSError:
            pass
        pool = self.engine.pool
        for field, method in (("db_pool_size", "size"), ("db_pool_checked_out", "checkedout"), ("db_pool_overflow", "overflow")):
            if callable(getattr(pool, method, None)):
                try:
                    value = int(getattr(pool, method)())
                    # QueuePool counts overflow from -pool_size; report only connections opened beyond the pool size.
                    record[field] = max(0, value) if field == "db_pool_overflow" else value
                except Exception:  # noqa: BLE001 - not every pool implementation supports every statistic
                    pass
        return record

    def write_heartbeat(self) -> None:
        try:
            self.sink.write(self.heartbeat())
        except Exception:  # noqa: BLE001
            pass

    def start_heartbeat(self, interval_seconds: float) -> None:
        if interval_seconds <= 0 or self._heartbeat_thread is not None:
            return

        def loop() -> None:
            while not self._stop.is_set():
                self.write_heartbeat()
                self._stop.wait(interval_seconds)

        self._heartbeat_thread = threading.Thread(target=loop, name="cloudrca-heartbeat", daemon=True)
        self._heartbeat_thread.start()

    def stop_heartbeat(self) -> None:
        self._stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2)
            self._heartbeat_thread = None


def _memory_mb() -> Dict[str, float]:
    """MemAvailable/MemTotal from /proc/meminfo (Linux, e.g. EC2); omitted where unavailable."""
    values: Dict[str, float] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                if key in ("MemAvailable", "MemTotal"):
                    values["mem_available_mb" if key == "MemAvailable" else "mem_total_mb"] = round(int(rest.split()[0]) / 1024, 1)
    except (OSError, ValueError, IndexError):
        return {}
    return values


def _database_directory(engine: Engine) -> str:
    database = engine.url.database
    return str(Path(database).resolve().parent) if database and database != ":memory:" else "/"


def install(app: Any, engine: Engine, sink: Optional[JsonLineSink] = None) -> Observability:
    """Attach request logging, database timing, and the heartbeat to a FastAPI app."""
    observability = Observability(engine, sink)
    event.listen(engine, "before_cursor_execute", observability._before_cursor)
    event.listen(engine, "after_cursor_execute", observability._after_cursor)
    event.listen(engine, "handle_error", observability._handle_error)
    app.middleware("http")(observability.middleware)
    observability.start_heartbeat(float(os.environ.get("SPLITWISE_HEARTBEAT_SECONDS", "60")))
    return observability
