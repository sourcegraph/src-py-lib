"""Structured wide events with explicit sinks, decoupled from stdlib logging.

Events are plain dicts shaped after the OpenTelemetry Logs Data Model:
`time_unix_nano`, `severity_text`, `severity_number`, `event_name`, `body`,
`trace_id` / `span_id` / `parent_span_id`, and `attributes`. Run-level
metadata (`resource`) is stamped once per run on the run-start event, not
repeated on every line.

Sinks receive every event as a Mapping. The active sink is ambient run
context, set only by `observability_context()` in `logging.py`; the default
is `NullEventSink`, so importing this library never writes anywhere.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Protocol, Self, cast

# OpenTelemetry Logs Data Model field names, pinned here so an upstream
# rename is a one-file change.
TIME_UNIX_NANO: Final[str] = "time_unix_nano"
SEVERITY_TEXT: Final[str] = "severity_text"
SEVERITY_NUMBER: Final[str] = "severity_number"
EVENT_NAME: Final[str] = "event_name"
BODY: Final[str] = "body"
TRACE_ID: Final[str] = "trace_id"
SPAN_ID: Final[str] = "span_id"
PARENT_SPAN_ID: Final[str] = "parent_span_id"
ATTRIBUTES: Final[str] = "attributes"
RESOURCE: Final[str] = "resource"

# OpenTelemetry semantic-convention attribute names used by this library.
ERROR_TYPE: Final[str] = "error.type"
SERVICE_NAME: Final[str] = "service.name"
SERVICE_VERSION: Final[str] = "service.version"
PROCESS_PID: Final[str] = "process.pid"
PROCESS_RUNTIME_NAME: Final[str] = "process.runtime.name"
PROCESS_RUNTIME_VERSION: Final[str] = "process.runtime.version"

EVENT_FIELD_ORDER: Final[tuple[str, ...]] = (
    TIME_UNIX_NANO,
    SEVERITY_TEXT,
    SEVERITY_NUMBER,
    EVENT_NAME,
    TRACE_ID,
    SPAN_ID,
    PARENT_SPAN_ID,
    BODY,
    RESOURCE,
    ATTRIBUTES,
)

# Python logging level -> OTel severity number for the level's lower bound.
_SEVERITY_NUMBER_BY_LEVEL: Final[tuple[tuple[int, str, int], ...]] = (
    (logging.CRITICAL, "FATAL", 21),
    (logging.ERROR, "ERROR", 17),
    (logging.WARNING, "WARN", 13),
    (logging.INFO, "INFO", 9),
    (logging.DEBUG, "DEBUG", 5),
    (0, "TRACE", 1),
)


def severity_fields(level: int) -> tuple[str, int]:
    """Return OTel `(severity_text, severity_number)` for a Python log level."""
    for threshold, severity_text, severity_number in _SEVERITY_NUMBER_BY_LEVEL:
        if level >= threshold:
            return severity_text, severity_number
    return "TRACE", 1


def ordered_event_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the payload with model fields first and attributes sorted."""
    ordered: dict[str, Any] = {}
    for key in EVENT_FIELD_ORDER:
        if key in payload:
            ordered[key] = payload[key]
    for key in sorted(key for key in payload if key not in ordered):
        ordered[key] = payload[key]
    attributes = ordered.get(ATTRIBUTES)
    if isinstance(attributes, Mapping):
        typed_attributes = cast(Mapping[str, Any], attributes)
        ordered[ATTRIBUTES] = {key: typed_attributes[key] for key in sorted(typed_attributes)}
    return ordered


class EventSink(Protocol):
    """Receives structured wide events as plain mappings."""

    def emit(self, event: Mapping[str, Any]) -> None: ...


class NullEventSink:
    """Discard every event; the default sink outside any run context."""

    def emit(self, event: Mapping[str, Any]) -> None:
        return


class InMemoryEventSink:
    """Collect events in memory for tests and module callers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[dict[str, Any]] = []

    def emit(self, event: Mapping[str, Any]) -> None:
        with self._lock:
            self.events.append(dict(event))


class CallbackEventSink:
    """Pass a defensive copy of every event to a caller-supplied function."""

    def __init__(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self._callback = callback

    def emit(self, event: Mapping[str, Any]) -> None:
        self._callback(dict(event))


class CompositeEventSink:
    """Fan one event out to several sinks."""

    def __init__(self, sinks: tuple[EventSink, ...]) -> None:
        self.sinks = sinks

    def emit(self, event: Mapping[str, Any]) -> None:
        for sink in self.sinks:
            sink.emit(event)


class JSONLEventSink:
    """Write each event as one JSON line; thread-safe; usable as a context manager."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._file = path.open("w", encoding="utf-8", buffering=1)

    def emit(self, event: Mapping[str, Any]) -> None:
        line = json.dumps(ordered_event_payload(event), default=str)
        with self._lock, contextlib.suppress(ValueError):
            self._file.write(line + "\n")

    def flush(self) -> None:
        with self._lock, contextlib.suppress(ValueError):
            self._file.flush()

    def close(self) -> None:
        with self._lock, contextlib.suppress(Exception):
            self._file.flush()
            self._file.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def flush_sink(sink: EventSink) -> None:
    """Flush a sink if it supports flushing."""
    flush = getattr(sink, "flush", None)
    if callable(flush):
        flush()


@dataclass(frozen=True)
class EventRuntime:
    """Ambient per-run observability scope: where events go and what rides along."""

    run: str = ""
    sink: EventSink = field(default_factory=NullEventSink)
    min_level: int = logging.DEBUG


_EVENT_RUNTIME: ContextVar[EventRuntime | None] = ContextVar(
    "src_py_lib_event_runtime",
    default=None,
)
_DEFAULT_RUNTIME: Final[EventRuntime] = EventRuntime()


def current_event_runtime() -> EventRuntime:
    """Return the active event runtime; outside a run this is a null runtime."""
    return _EVENT_RUNTIME.get() or _DEFAULT_RUNTIME


def set_event_runtime(runtime: EventRuntime) -> Any:
    """Install an event runtime; returns the reset token."""
    return _EVENT_RUNTIME.set(runtime)


def reset_event_runtime(token: Any) -> None:
    """Restore the previous event runtime."""
    _EVENT_RUNTIME.reset(token)
