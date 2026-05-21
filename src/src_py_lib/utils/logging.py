"""Central structured logging for small CLIs and scripts.

Use `configure_logging()` once near process startup. Other modules should use
`logging.getLogger(__name__)` for human-readable operator messages and
`event()` / `emit_event()` for structured JSONL events.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime as _datetime
import json
import logging
import secrets
import subprocess
import threading
import time
from collections.abc import Generator, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

RUN_ID: Final[str] = secrets.token_hex(6)
DEFAULT_RETAIN_FILES: Final[int] = 50
SECRET_FIELD_FRAGMENTS: Final[tuple[str, ...]] = (
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
)

_STRUCTURED_EVENT_ATTR: Final[str] = "_src_py_lib_structured_event"
_STRUCTURED_FIELDS_ATTR: Final[str] = "_src_py_lib_structured_fields"
_CONTEXT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("src_py_lib_log_context")


@dataclass(frozen=True)
class LoggingConfig:
    """Logging destinations and levels."""

    logger_name: str = ""
    terminal_level: int = logging.INFO
    event_file_level: int = logging.DEBUG
    event_file: Path | None = None
    run_id: str = RUN_ID
    retain_event_files: int = DEFAULT_RETAIN_FILES


class _DropStructuredEvents(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not hasattr(record, _STRUCTURED_EVENT_ATTR)


class JSONLinesEventHandler(logging.Handler):
    """Write every log record as one JSON line."""

    def __init__(self, path: Path, *, run_id: str, level: int) -> None:
        super().__init__(level=level)
        self.path = path
        self._run_id = run_id
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
                    "run_id": self._run_id,
                    "level": record.levelname,
                    "event": structured_event,
                    **structured_fields,
                }
            else:
                payload = {
                    "ts": timestamp,
                    "run_id": self._run_id,
                    "level": record.levelname,
                    "event": "log",
                    "logger": record.name,
                    "message": record.getMessage(),
                }
                if record.exc_info:
                    payload["exc_info"] = self.format(record)
            with self._lock:
                self._file.write(json.dumps(payload, default=str, sort_keys=True) + "\n")
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        with contextlib.suppress(Exception), self._lock:
            self._file.flush()
            self._file.close()
        super().close()


def configure_logging(config: LoggingConfig | None = None) -> Path | None:
    """Configure terminal logging and optional JSONL event logging.

    Returns the JSONL event path when file logging is enabled.
    """
    config = config or LoggingConfig()
    root_or_package_logger = logging.getLogger(config.logger_name)
    root_or_package_logger.handlers.clear()
    root_or_package_logger.setLevel(
        min(
            config.terminal_level,
            config.event_file_level if config.event_file else config.terminal_level,
        )
    )
    root_or_package_logger.propagate = False

    terminal_handler = logging.StreamHandler()
    terminal_handler.setLevel(config.terminal_level)
    terminal_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    terminal_handler.addFilter(_DropStructuredEvents())
    root_or_package_logger.addHandler(terminal_handler)

    if config.event_file is None:
        return None

    config.event_file.parent.mkdir(parents=True, exist_ok=True)
    _prune_old_event_files(config.event_file.parent, config.retain_event_files)
    event_handler = JSONLinesEventHandler(
        config.event_file,
        run_id=config.run_id,
        level=config.event_file_level,
    )
    root_or_package_logger.addHandler(event_handler)
    root_or_package_logger.info(
        "Writing structured events to %s (run_id=%s).", config.event_file, config.run_id
    )
    return config.event_file


def default_event_file(logs_dir: Path = Path("logs"), *, run_id: str = RUN_ID) -> Path:
    """Return a timestamped event-file path under `logs_dir`."""
    timestamp = _datetime.datetime.now(_datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    return logs_dir / f"events-{timestamp}-{run_id}.jsonl"


def emit_event(
    name: str, *, level: int = logging.INFO, logger_name: str = "", **fields: Any
) -> None:
    """Emit one structured event through the configured logger."""
    logger = logging.getLogger(logger_name)
    if not logger.isEnabledFor(level):
        return
    logger.log(
        level,
        "event=%s",
        name,
        extra={
            _STRUCTURED_EVENT_ATTR: name,
            _STRUCTURED_FIELDS_ATTR: {**_CONTEXT.get({}), **fields},
        },
    )


@contextlib.contextmanager
def log_context(**fields: Any) -> Generator[None]:
    """Add inherited structured fields for nested `emit_event()` calls."""
    reset_token = _CONTEXT.set({**_CONTEXT.get({}), **fields})
    try:
        yield
    finally:
        _CONTEXT.reset(reset_token)


@contextlib.contextmanager
def event(
    name: str, *, level: int = logging.INFO, logger_name: str = "", **fields: Any
) -> Generator[dict[str, Any]]:
    """Emit start/end structured events around a block of work."""
    emit_event(name, level=level, logger_name=logger_name, phase="start", **fields)
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
        emit_event(
            name,
            level=logging.ERROR if error else level,
            logger_name=logger_name,
            **end_fields,
        )


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


def startup_event(
    *,
    command: str,
    config: object | None = None,
    event_file: Path | None = None,
    git_commit: str | None = None,
    git_cwd: Path | None = None,
    logger_name: str = "",
) -> None:
    """Emit standard startup metadata after logging is configured."""
    fields: dict[str, Any] = {
        "command": command,
        "event_file": str(event_file) if event_file else None,
    }
    commit = git_commit or git_short_hash(git_cwd)
    if commit:
        fields["git_commit"] = commit
    if config is not None:
        fields["config"] = sanitized_config_snapshot(config)
    emit_event("startup", logger_name=logger_name, **fields)


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


def _secret_state(value: object) -> str:
    if value is None or value == "":
        return "missing"
    return "reference" if isinstance(value, str) and value.startswith("op://") else "provided"


def _prune_old_event_files(logs_dir: Path, retain_files: int) -> None:
    if retain_files <= 0 or not logs_dir.exists():
        return
    event_files = sorted(logs_dir.glob("events-*.jsonl"), key=lambda path: path.stat().st_mtime)
    for old_file in event_files[:-retain_files]:
        with contextlib.suppress(OSError):
            old_file.unlink()
