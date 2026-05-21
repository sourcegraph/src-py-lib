"""Central structured logging for small CLIs and scripts.

Use `configure_logging()` once near process startup. Other modules should use
`logging.getLogger(__name__)` for human-readable operator messages and
`event()` / `log()` for structured JSONL events.
"""

from __future__ import annotations

import ast
import contextlib
import contextvars
import datetime as _datetime
import json
import logging
import os
import secrets
import subprocess
import threading
import time
from collections.abc import Generator, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from src_py_lib.utils.config import Config, config_field, config_snapshot

RUN: Final[str] = secrets.token_hex(4)
DEFAULT_LOGS_DIR: Final[Path] = Path("logs")
DEFAULT_RETAIN_FILES: Final[int] = 50
DEFAULT_LOG_FILE_LEVEL: Final[str] = "debug"
SRC_LOG_LEVEL: Final[str] = "SRC_LOG_LEVEL"
TRACE_SPAN_BYTES: Final[int] = 4
SECRET_FIELD_FRAGMENTS: Final[tuple[str, ...]] = (
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
)
LOG_FIELD_ORDER: Final[tuple[str, ...]] = (
    "ts",
    "command",
    "level",
    "run",
    "trace",
    "span",
    "parent_span",
    "logger",
    "event",
    "phase",
    "message",
)

_STRUCTURED_EVENT_ATTR: Final[str] = "_src_py_lib_structured_event"
_STRUCTURED_FIELDS_ATTR: Final[str] = "_src_py_lib_structured_fields"
_HTTPCORE_RESPONSE_HEADERS_PREFIX: Final[str] = "receive_response_headers.complete return_value="
_HTTPX_REQUEST_PREFIX: Final[str] = "HTTP Request: "
_HTTP_DEPENDENCY_LOGGER_PREFIXES: Final[tuple[str, ...]] = ("httpx", "httpcore")
_CONTEXT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("src_py_lib_log_context")


@dataclass(frozen=True)
class LoggingSettings:
    """Logging destinations and levels."""

    logger_name: str = ""
    terminal_level: str = "info"
    log_file_level: str | None = None
    log_file: Path | None = None
    logs_dir: Path | None = DEFAULT_LOGS_DIR
    run: str = RUN
    retain_log_files: int = DEFAULT_RETAIN_FILES
    suppress_http_dependency_logs: bool = True


class LoggingConfig(Config):
    """Config fields for logging-related CLI and environment options."""

    src_log_level: str | None = config_field(
        None,
        env_var=SRC_LOG_LEVEL,
        cli_flag="--src-log-level",
        metavar="LEVEL",
        help="Minimum level for log events (default: DEBUG; e.g. INFO hides debug events).",
    )


@dataclass(frozen=True)
class _SpanContext:
    trace: str
    span: str
    parent_span: str | None = None


_SPAN_CONTEXT: contextvars.ContextVar[_SpanContext | None] = contextvars.ContextVar(
    "src_py_lib_span_context", default=None
)


class _DropStructuredEvents(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not hasattr(record, _STRUCTURED_EVENT_ATTR)


class _DropHTTPDependencyLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not any(
            record.name == prefix or record.name.startswith(f"{prefix}.")
            for prefix in _HTTP_DEPENDENCY_LOGGER_PREFIXES
        )


class JSONLogFileHandler(logging.Handler):
    """Write every log record as one JSON object line."""

    def __init__(self, path: Path, *, run: str, level: int) -> None:
        super().__init__(level=level)
        self.path = path
        self._run = run
        self._lock = threading.Lock()
        self._file = path.open("w", encoding="utf-8", buffering=1)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            timestamp = _datetime.datetime.now(_datetime.UTC).isoformat(timespec="milliseconds")
            structured_event = getattr(record, _STRUCTURED_EVENT_ATTR, None)
            if isinstance(structured_event, str):
                fields = getattr(record, _STRUCTURED_FIELDS_ATTR, {})
                structured_fields: dict[str, Any] = (
                    dict(cast(Mapping[str, Any], fields)) if isinstance(fields, Mapping) else {}
                )
                payload = {
                    "ts": timestamp,
                    "run": self._run,
                    "level": record.levelname,
                    "event": structured_event,
                    **structured_fields,
                }
            else:
                message, log_fields = _structured_log_fields(record)
                payload = {
                    "ts": timestamp,
                    "run": self._run,
                    "level": record.levelname,
                    "event": "log",
                    "logger": record.name,
                    "message": message,
                }
                payload.update(log_fields)
                payload.update(_current_log_fields(payload))
                if record.exc_info:
                    payload["exc_info"] = self.format(record)
            with self._lock:
                self._file.write(json.dumps(_ordered_payload(payload), default=str) + "\n")
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        with contextlib.suppress(Exception), self._lock:
            self._file.flush()
            self._file.close()
        super().close()


def configure_logging(config: LoggingSettings | None = None) -> Path | None:
    """Configure terminal logging and optional JSON log-file logging.

    Returns the JSON log-file path when file logging is enabled.
    """
    config = config or LoggingSettings()
    terminal_level = _log_level(config.terminal_level)
    log_file_level = _log_file_level(config.log_file_level)
    log_file = config.log_file
    if log_file is None and config.logs_dir is not None:
        log_file = default_log_file(config.logs_dir, run=config.run)
    root_or_package_logger = logging.getLogger(config.logger_name)
    root_or_package_logger.handlers.clear()
    root_or_package_logger.setLevel(
        min(
            terminal_level,
            log_file_level if log_file else terminal_level,
        )
    )
    root_or_package_logger.propagate = False

    terminal_handler = logging.StreamHandler()
    terminal_handler.setLevel(terminal_level)
    terminal_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    terminal_handler.addFilter(_DropStructuredEvents())
    if config.suppress_http_dependency_logs and config.logger_name == "":
        terminal_handler.addFilter(_DropHTTPDependencyLogs())
    root_or_package_logger.addHandler(terminal_handler)

    if log_file is None:
        return None

    log_file.parent.mkdir(parents=True, exist_ok=True)
    _prune_old_log_files(log_file.parent, config.retain_log_files)
    log_file_handler = JSONLogFileHandler(
        log_file,
        run=config.run,
        level=log_file_level,
    )
    if config.suppress_http_dependency_logs and config.logger_name == "":
        log_file_handler.addFilter(_DropHTTPDependencyLogs())
    root_or_package_logger.addHandler(log_file_handler)
    root_or_package_logger.info("Writing log events to %s.", log_file)
    return log_file


@contextlib.contextmanager
def logging_context(
    name: str,
    config: object | None = None,
    *,
    git_cwd: Path | str | None = None,
    logging_config: LoggingSettings | None = None,
) -> Generator[Path | None]:
    """Configure logging, install command context, and emit startup metadata."""
    resolved_logging_config = logging_config or LoggingSettings(
        log_file_level=_src_log_level_from_config(config)
    )
    log_file = configure_logging(resolved_logging_config)
    with log_context(command=name):
        startup_event(
            command=name,
            config=config,
            log_file=log_file,
            git_cwd=_git_cwd_path(git_cwd),
            logger_name=resolved_logging_config.logger_name,
        )
        yield log_file


def default_log_file(logs_dir: Path = DEFAULT_LOGS_DIR, *, run: str = RUN) -> Path:
    """Return a timestamped log-file path under `logs_dir`."""
    timestamp = _datetime.datetime.now(_datetime.UTC).strftime("%Y-%m-%d-%H-%M-%S-%z")
    timestamp = timestamp.replace("+", "", 1)
    return logs_dir / f"{timestamp}-{run}.json"


def log(level: str, key: str, *, logger_name: str = "", **fields: Any) -> None:
    """Log one structured event through the configured logger."""
    numeric_level = _log_level(level)
    logger = logging.getLogger(logger_name)
    if not logger.isEnabledFor(numeric_level):
        return
    logger.log(
        numeric_level,
        "event=%s",
        key,
        extra={
            _STRUCTURED_EVENT_ATTR: key,
            _STRUCTURED_FIELDS_ATTR: {**_current_log_fields(), **fields},
        },
    )


def debug(key: str, *, logger_name: str = "", **fields: Any) -> None:
    """Log a DEBUG structured event."""
    log("debug", key, logger_name=logger_name, **fields)


def info(key: str, *, logger_name: str = "", **fields: Any) -> None:
    """Log an INFO structured event."""
    log("info", key, logger_name=logger_name, **fields)


def warning(key: str, *, logger_name: str = "", **fields: Any) -> None:
    """Log a WARNING structured event."""
    log("warning", key, logger_name=logger_name, **fields)


def error(key: str, *, logger_name: str = "", **fields: Any) -> None:
    """Log an ERROR structured event."""
    log("error", key, logger_name=logger_name, **fields)


def critical(key: str, *, logger_name: str = "", **fields: Any) -> None:
    """Log a CRITICAL structured event."""
    log("critical", key, logger_name=logger_name, **fields)


@contextlib.contextmanager
def log_context(**fields: Any) -> Generator[None]:
    """Add inherited structured fields for nested `log()` calls."""
    reset_token = _CONTEXT.set({**_CONTEXT.get({}), **fields})
    try:
        yield
    finally:
        _CONTEXT.reset(reset_token)


@contextlib.contextmanager
def event(
    key: str, *, level: str = "info", logger_name: str = "", **fields: Any
) -> Generator[dict[str, Any]]:
    """Emit start/end structured events around a block of work."""
    parent = _SPAN_CONTEXT.get()
    span = _SpanContext(
        trace=parent.trace if parent else secrets.token_hex(TRACE_SPAN_BYTES),
        span=secrets.token_hex(TRACE_SPAN_BYTES),
        parent_span=parent.span if parent else None,
    )
    reset_token = _SPAN_CONTEXT.set(span)
    try:
        log(level, key, logger_name=logger_name, phase="start", **fields)
        started = time.perf_counter()
        extra: dict[str, Any] = {}
        error: BaseException | None = None
        try:
            yield extra
        except BaseException as exception:
            error = exception
            raise
        finally:
            end_fields = {
                **fields,
                **extra,
                "phase": "end",
                "duration_ms": round((time.perf_counter() - started) * 1000.0),
                "status": "error" if error else "ok",
                "error_type": type(error).__name__ if error else None,
            }
            log(
                "error" if error else level,
                key,
                logger_name=logger_name,
                **end_fields,
            )
    finally:
        _SPAN_CONTEXT.reset(reset_token)


def sanitized_config_snapshot(config: object) -> dict[str, Any]:
    """Return a log-safe snapshot of dataclass/object/dict config values."""
    if isinstance(config, Mapping):
        items: Iterable[tuple[object, object]] = cast(Mapping[object, object], config).items()
    else:
        object_items: list[tuple[object, object]] = []
        for name in dir(config):
            if name.startswith("_"):
                continue
            object_items.append((name, getattr(config, name)))
        items = object_items
    snapshot: dict[str, Any] = {}
    for key, value in items:
        if callable(value):
            continue
        key_text = str(key)
        if any(fragment in key_text.lower() for fragment in SECRET_FIELD_FRAGMENTS):
            snapshot[key_text] = _secret_state(value)
        elif isinstance(value, Path):
            snapshot[key_text] = str(value)
        elif isinstance(value, str | int | float | bool) or value is None:
            snapshot[key_text] = value
        else:
            snapshot[key_text] = str(value)
    return snapshot


def _current_log_fields(protected: Mapping[str, Any] | None = None) -> dict[str, Any]:
    protected_keys = set(protected or {})
    fields = {key: value for key, value in _CONTEXT.get({}).items() if key not in protected_keys}
    span = _SPAN_CONTEXT.get()
    if span is None:
        return fields
    if "parent_span" not in protected_keys and span.parent_span is not None:
        fields["parent_span"] = span.parent_span
    if "span" not in protected_keys:
        fields["span"] = span.span
    if "trace" not in protected_keys:
        fields["trace"] = span.trace
    return fields


def startup_event(
    *,
    command: str,
    config: object | None = None,
    log_file: Path | None = None,
    git_commit: str | None = None,
    git_cwd: Path | None = None,
    logger_name: str = "",
) -> None:
    """Emit standard startup metadata after logging is configured."""
    fields: dict[str, Any] = {
        "command": command,
        "log_file": str(log_file) if log_file else None,
    }
    commit = git_commit or git_short_hash(git_cwd)
    if commit:
        fields["git_commit"] = commit
    if config is not None:
        config_value = config_snapshot(config) if isinstance(config, Config) else config
        fields["config"] = sanitized_config_snapshot(config_value)
    info("startup", logger_name=logger_name, **fields)


def git_short_hash(cwd: Path | None = None) -> str | None:
    """Return the current git short hash, or None outside a git checkout."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except OSError:
        return None
    except subprocess.SubprocessError:
        return None
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and commit else None


def _ordered_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    ordered: dict[str, Any] = {}
    for key in LOG_FIELD_ORDER:
        if key in payload:
            ordered[key] = payload[key]
    for key in sorted(key for key in payload if key not in ordered):
        ordered[key] = payload[key]
    return ordered


def _log_file_level(configured_level: str | None) -> int:
    if configured_level is not None:
        return _log_level(configured_level)
    env_level = os.environ.get(SRC_LOG_LEVEL)
    if env_level:
        return _log_level(env_level)
    return _log_level(DEFAULT_LOG_FILE_LEVEL)


def _src_log_level_from_config(config: object | None) -> str | None:
    value = getattr(config, "src_log_level", None)
    return value if isinstance(value, str) else None


def _git_cwd_path(value: Path | str | None) -> Path | None:
    if value is None:
        return None
    path = Path(value)
    return path.parent if path.is_file() else path


def _log_level(value: int | str) -> int:
    if isinstance(value, int):
        return value
    normalized = value.strip().upper()
    if not normalized:
        return logging.DEBUG
    if normalized.isdecimal():
        return int(normalized)
    levels = logging.getLevelNamesMapping()
    level = levels.get(normalized)
    if level is None:
        return logging.DEBUG
    return level


def _structured_log_fields(record: logging.LogRecord) -> tuple[str, dict[str, Any]]:
    message = record.getMessage()
    fields: dict[str, Any] = {}
    if record.name == "httpx" and message.startswith(_HTTPX_REQUEST_PREFIX):
        fields["level"] = "DEBUG"
    if not message.startswith(_HTTPCORE_RESPONSE_HEADERS_PREFIX):
        return message, fields
    try:
        literal_value = cast(
            object,
            ast.literal_eval(message.removeprefix(_HTTPCORE_RESPONSE_HEADERS_PREFIX)),
        )
    except (SyntaxError, ValueError):
        return message, fields
    if not isinstance(literal_value, tuple):
        return message, fields

    return_value = cast(tuple[object, ...], literal_value)
    if len(return_value) != 4:
        return message, fields
    http_version, status_code, reason_phrase, raw_headers = return_value
    headers = _http_headers(raw_headers)
    if not headers:
        return message, fields

    fields["headers"] = headers
    decoded_version = _decode_http_bytes(http_version)
    if decoded_version is not None:
        fields["http_version"] = decoded_version
    if isinstance(status_code, int):
        fields["status_code"] = status_code
    decoded_reason = _decode_http_bytes(reason_phrase)
    if decoded_reason is not None:
        fields["reason_phrase"] = decoded_reason
    return "receive_response_headers.complete", fields


def _http_headers(raw_headers: object) -> dict[str, str | list[str]]:
    if not isinstance(raw_headers, list | tuple):
        return {}
    headers: dict[str, str | list[str]] = {}
    for item in cast(Iterable[object], raw_headers):
        if not isinstance(item, tuple):
            continue
        header = cast(tuple[object, ...], item)
        if len(header) != 2:
            continue
        raw_name, raw_value = header
        name = _decode_http_bytes(raw_name)
        value = _decode_http_bytes(raw_value)
        if name is None or value is None:
            continue
        key = name.lower()
        existing = headers.get(key)
        if existing is None:
            headers[key] = value
        elif isinstance(existing, list):
            existing.append(value)
        else:
            headers[key] = [existing, value]
    return {key: headers[key] for key in sorted(headers)}


def _decode_http_bytes(value: object) -> str | None:
    if isinstance(value, bytes):
        return value.decode("latin-1", errors="replace")
    if isinstance(value, str):
        return value
    return None


def _secret_state(value: object) -> str:
    if value is None or value == "":
        return "missing"
    return "reference" if isinstance(value, str) and value.startswith("op://") else "provided"


def _prune_old_log_files(logs_dir: Path, retain_files: int) -> None:
    if retain_files <= 0 or not logs_dir.exists():
        return
    log_files = sorted(
        [*logs_dir.glob("????-??-??-??-??-??-*.json"), *logs_dir.glob("events-*.json")],
        key=lambda path: path.stat().st_mtime,
    )
    for old_file in log_files[:-retain_files]:
        with contextlib.suppress(OSError):
            old_file.unlink()
