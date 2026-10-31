# Structured logging for CloudRCA

`observability.py` writes JSON lines that CloudRCA-Agent's `scripts/convert_cloudwatch_export.py` reads for cross-layer root cause analysis:

- `request`: UTC `ts`, `request_id`, route template (never the raw path), method, status, `duration_ms`, `db_ms`, `is_write`, `in_flight`.
- `error`: one per unhandled exception, with `reported_by` (`database` when raised by the SQLite driver, otherwise `application`), `error_class`, a sanitized `message`, and for database errors `database_id`, `operation` (verb + table, e.g. `INSERT expenses`), and `db_wait_ms`.
- `heartbeat`: every 60 s, load average, CPU count, available/total memory (Linux), free/total disk for the database volume, and connection-pool size, checked-out, and overflow counts.

Never logged: request bodies, query strings, raw paths, headers (including client `X-Request-ID`; every request gets an opaque generated ID), SQL text or parameters, expense titles, amounts, names, credentials, or tokens. Exception messages are logged only when they match an allowlisted shape (for example `database is locked` or `'NoneType' object has no attribute '<field>'`); anything else is recorded as `<ErrorClass> (message withheld)`. Records are queued and written by a background thread, so a slow log filesystem never delays requests; if the queue is full, records are dropped and counted. Logging errors are swallowed and never affect responses.

| Setting | Default |
| --- | --- |
| `SPLITWISE_LOG_FILE` | `logs/app.jsonl` (gitignored) |
| `SPLITWISE_HEARTBEAT_SECONDS` | `60` (`0` disables) |
| `SPLITWISE_INSTANCE_ID` | EC2 instance ID from IMDSv2, else `host-<hash of hostname>` |
| `SPLITWISE_SERVICE_NAME` / `SPLITWISE_DATABASE_ID` | `splitwise-api` / `splitwise-sqlite` |
| `SPLITWISE_DATABASE_URL` / `SPLITWISE_SQLITE_TIMEOUT` | `sqlite:///./splitwise.db` / `5` (unchanged defaults) |

Tests: `pip install -r requirements-dev.txt && python -m pytest`. Set `CLOUDRCA_REPO=/path/to/CloudRCA-Agent` to also convert and normalize real logs with CloudRCA.
