"""Central structured logging and observability for CLIs and importable libraries.

Three decoupled channels:

- Human messages: plain `logging.getLogger(__name__)` on module loggers.
  Library code never installs handlers; CLI entrypoints opt in via
  `cli_logging_handlers()` (or the composed `cli_run_context()`).
- Structured wide events: `span()` / `log_event()` emit OTel-shaped dicts to
  the active `EventSink` (see `events.py`). Outside a run context the sink is
  null, so importing this library never writes anywhere.
- OpenTelemetry traces: opened by `span()` whenever a provider is configured.

`observability_context()` owns one run: event runtime, optional OTel setup,
run start/end events, resource sampling, and flush ordering.
"""

from __future__ import annotations

import ast
import contextlib
import contextvars
import datetime as _datetime
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from concurrent.futures import Executor, Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Self, cast

if sys.platform != "win32":
    import resource

from pydantic import model_validator

from src_py_lib.utils import events, telemetry
from src_py_lib.utils.config import Config, config_field, config_snapshot

RUN: Final[str] = secrets.token_hex(4)
DEFAULT_LOGS_DIR: Final[Path] = Path("logs")
DEFAULT_RETAIN_FILES: Final[int] = 50
DEFAULT_LOG_FILE_LEVEL: Final[str] = "debug"
SRC_LOG_LEVEL: Final[str] = "SRC_LOG_LEVEL"
SRC_LOG_VERBOSE: Final[str] = "SRC_LOG_VERBOSE"
SRC_LOG_QUIET: Final[str] = "SRC_LOG_QUIET"
SRC_LOG_SILENT: Final[str] = "SRC_LOG_SILENT"
MEBIBYTE: Final[int] = 1024 * 1024
REDACTED_LOG_VALUE: Final[str] = "[redacted]"
SECRET_FIELD_FRAGMENTS: Final[tuple[str, ...]] = (
    "api-key",
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
)
DEFAULT_LOGGER_NAMES: Final[tuple[str, ...]] = ("src_py_lib",)

_HTTPCORE_RESPONSE_HEADERS_PREFIX: Final[str] = "receive_response_headers.complete return_value="
_HTTPX_REQUEST_PREFIX: Final[str] = "HTTP Request: "
_HTTP_DEPENDENCY_LOGGER_NAMES: Final[tuple[str, ...]] = ("httpx", "httpcore")
_CONTEXT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("src_py_lib_log_context")
_PARENT_SPAN_CONTEXT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "src_py_lib_parent_span_id", default=None
)


@dataclass(frozen=True)
class LoggingSettings:
    """Logging destinations and levels for one CLI run."""

    logger_names: tuple[str, ...] = DEFAULT_LOGGER_NAMES
    terminal_level: str = "info"
    log_file_level: str | None = None
    log_file: Path | None = None
    logs_dir: Path | None = DEFAULT_LOGS_DIR
    run: str = RUN
    retain_log_files: int = DEFAULT_RETAIN_FILES
    suppress_http_dependency_logs: bool = True
    resource_sample_interval_seconds: float | None = None
    open_telemetry: telemetry.OpenTelemetrySettings | None = None


class LoggingConfig(Config):
    """Config fields for logging-related CLI and environment options."""

    src_log_level: str | None = config_field(
        default="INFO",
        env_var=SRC_LOG_LEVEL,
        cli_flag="--src-log-level",
        metavar="LEVEL",
        help="Log level (default: INFO)",
        help_group="Logging",
    )
    verbose: bool = config_field(
        default=False,
        env_var=SRC_LOG_VERBOSE,
        cli_flag="--verbose",
        cli_aliases=("-v",),
        cli_action="store_true",
        help="Alias for --src-log-level DEBUG",
        help_group="Logging",
    )
    quiet: bool = config_field(
        default=False,
        env_var=SRC_LOG_QUIET,
        cli_flag="--quiet",
        cli_aliases=("-q",),
        cli_action="store_true",
        help="Alias for --src-log-level WARNING",
        help_group="Logging",
    )
    silent: bool = config_field(
        default=False,
        env_var=SRC_LOG_SILENT,
        cli_flag="--silent",
        cli_aliases=("-s",),
        cli_action="store_true",
        help="Alias for --src-log-level ERROR",
        help_group="Logging",
    )

    @model_validator(mode="after")
    def validate_log_level_alias(self) -> Self:
        """Require at most one alias for the terminal/log-file level."""
        if sum((self.verbose, self.quiet, self.silent)) > 1:
            raise ValueError("choose only one of --verbose/-v, --quiet/-q, or --silent/-s")
        return self


def resolve_log_level_name(
    config: object | None = None,
    *,
    log_level: str | None = None,
    verbose: bool | None = None,
    quiet: bool | None = None,
    silent: bool | None = None,
) -> str | None:
    """Resolve common CLI log-level alias to a level name.

    Alias flags intentionally only map to strings. Explicit log-level
    values are returned unchanged so `configure_logging()` owns parsing
    and fallback behavior.
    """
    resolved_verbose = verbose if verbose is not None else bool(getattr(config, "verbose", False))
    resolved_quiet = quiet if quiet is not None else bool(getattr(config, "quiet", False))
    resolved_silent = silent if silent is not None else bool(getattr(config, "silent", False))
    if resolved_verbose:
        return "DEBUG"
    if resolved_quiet:
        return "WARNING"
    if resolved_silent:
        return "ERROR"
    if log_level is not None:
        return log_level
    return _src_log_level_from_config(config)


def logging_settings_from_config(
    config: object | None = None,
    *,
    terminal_default: str = "INFO",
    log_file_default: str | None = DEFAULT_LOG_FILE_LEVEL,
    logger_names: tuple[str, ...] = DEFAULT_LOGGER_NAMES,
    log_file: Path | None = None,
    logs_dir: Path | None = DEFAULT_LOGS_DIR,
    run: str = RUN,
    retain_log_files: int = DEFAULT_RETAIN_FILES,
    suppress_http_dependency_logs: bool = True,
    resource_sample_interval_seconds: float | None = None,
    open_telemetry: telemetry.OpenTelemetrySettings | None = None,
) -> LoggingSettings:
    """Return `LoggingSettings` using common CLI log-level alias."""
    explicit_level = resolve_log_level_name(config)
    return LoggingSettings(
        logger_names=logger_names,
        terminal_level=explicit_level or terminal_default,
        log_file_level=explicit_level or log_file_default,
        log_file=log_file,
        logs_dir=logs_dir,
        run=run,
        retain_log_files=retain_log_files,
        suppress_http_dependency_logs=suppress_http_dependency_logs,
        resource_sample_interval_seconds=resource_sample_interval_seconds,
        open_telemetry=open_telemetry,
    )


_HTTP_METRICS_LOCK: Final[threading.Lock] = threading.Lock()
_HTTP_METRICS: dict[str, int] = {
    "http.client.request.count": 0,
    "http.client.request.body.size.total": 0,
    "http.client.response.body.size.total": 0,
    "http.client.retry.count": 0,
    "http.client.response.2xx.count": 0,
    "http.client.response.3xx.count": 0,
    "http.client.response.4xx.count": 0,
    "http.client.response.429.count": 0,
    "http.client.response.5xx.count": 0,
    "http.client.transport_error.count": 0,
}


@dataclass
class ResourceSampler:
    """Emit optional process resource samples and summarize usage at run end."""

    interval_seconds: float
    _stop: threading.Event = field(init=False, default_factory=threading.Event)
    _thread: threading.Thread | None = field(init=False, default=None)
    _started_at: float = field(init=False, default_factory=time.perf_counter)
    _last_sample_at: float = field(init=False, default_factory=time.perf_counter)
    _last_cpu_seconds: float = field(init=False, default=0.0)
    _start_usage: Any = field(init=False, default=None)
    _peak_rss_bytes: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        if self.interval_seconds < 0:
            raise ValueError("resource_sample_interval_seconds must be >= 0")
        self._start_usage = _resource_usage()
        if self._start_usage is not None:
            self._last_cpu_seconds = _cpu_seconds(self._start_usage)

    def start(self) -> None:
        """Start periodic sampling, if enabled by a positive interval."""
        if self.interval_seconds <= 0:
            return
        context = contextvars.copy_context()
        self._thread = threading.Thread(
            target=context.run,
            args=(self._loop,),
            name="ResourceSampler",
            daemon=True,
        )
        self._thread.start()
        self.emit_sample()

    def emit_sample(self) -> None:
        """Emit one DEBUG `resource_sample` event."""
        log_event("debug", "resource_sample", **self._sample_fields())

    def stop_and_summary(self) -> dict[str, Any]:
        """Stop periodic sampling and return run-end resource fields."""
        if self.interval_seconds > 0:
            self.emit_sample()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        usage = _resource_usage()
        summary: dict[str, Any] = {
            "cpu_count_logical": os.cpu_count() or 0,
            "num_threads": threading.active_count(),
        }
        file_descriptors = _num_file_descriptors()
        if file_descriptors is not None:
            summary["num_fds"] = file_descriptors
        rss_bytes = _rss_bytes(usage)
        if rss_bytes is not None:
            self._peak_rss_bytes = max(self._peak_rss_bytes, rss_bytes)
        if self._peak_rss_bytes:
            summary["peak_rss_mb"] = _bytes_to_mib(self._peak_rss_bytes)
        if usage is not None and self._start_usage is not None:
            summary["cpu_user_seconds"] = round(
                float(usage.ru_utime) - float(self._start_usage.ru_utime), 3
            )
            summary["cpu_system_seconds"] = round(
                float(usage.ru_stime) - float(self._start_usage.ru_stime), 3
            )
            summary["io_read_count"] = int(usage.ru_inblock) - int(self._start_usage.ru_inblock)
            summary["io_write_count"] = int(usage.ru_oublock) - int(self._start_usage.ru_oublock)
        return summary

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.emit_sample()

    def _sample_fields(self) -> dict[str, Any]:
        now = time.perf_counter()
        usage = _resource_usage()
        fields: dict[str, Any] = {
            "num_threads": threading.active_count(),
        }
        rss_bytes = _rss_bytes(usage)
        if rss_bytes is not None:
            self._peak_rss_bytes = max(self._peak_rss_bytes, rss_bytes)
            fields["rss_mb"] = _bytes_to_mib(rss_bytes)
        file_descriptors = _num_file_descriptors()
        if file_descriptors is not None:
            fields["num_fds"] = file_descriptors
        if usage is not None:
            cpu_seconds = _cpu_seconds(usage)
            elapsed = max(now - self._last_sample_at, 0.001)
            fields["process_cpu_percent"] = round(
                max(cpu_seconds - self._last_cpu_seconds, 0.0) / elapsed * 100.0,
                1,
            )
            self._last_cpu_seconds = cpu_seconds
        self._last_sample_at = now
        return fields


def _event_payload(
    numeric_level: int,
    event_name: str,
    *,
    attributes: dict[str, Any],
    body: str | None = None,
) -> dict[str, Any]:
    """Build one OTel-shaped wide event payload."""
    severity_text, severity_number = events.severity_fields(numeric_level)
    runtime = events.current_event_runtime()
    if runtime.run:
        attributes.setdefault("run", runtime.run)
    resource = attributes.pop(events.RESOURCE, None)
    payload: dict[str, Any] = {
        events.TIME_UNIX_NANO: time.time_ns(),
        events.SEVERITY_TEXT: severity_text,
        events.SEVERITY_NUMBER: severity_number,
        events.EVENT_NAME: event_name,
        **telemetry.current_trace_fields(_PARENT_SPAN_CONTEXT.get()),
        events.ATTRIBUTES: attributes,
    }
    if resource is not None:
        payload[events.RESOURCE] = resource
    if body is not None:
        payload[events.BODY] = body
    return payload


class EventBridgeHandler(logging.Handler):
    """Forward human log records into the event sink as `event_name="log"` events.

    Carries the httpcore wire-debug mining and secret redaction from
    `_structured_log_fields()`, plus exception tracebacks via `exc_info`.
    Installed only by CLI entrypoints; never by library code.
    """

    def __init__(self, sink: events.EventSink, *, level: int | str = DEFAULT_LOG_FILE_LEVEL):
        super().__init__(level=_log_level(level))
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message, mined_fields = _structured_log_fields(record)
            numeric_level = record.levelno
            mined_level = mined_fields.pop("level", None)
            if isinstance(mined_level, str):
                numeric_level = _log_level(mined_level)
            attributes: dict[str, Any] = {
                **_CONTEXT.get({}),
                "logger": record.name,
                **mined_fields,
            }
            if record.exc_info:
                attributes["exc_info"] = self.format(record)
            self.sink.emit(
                _event_payload(numeric_level, "log", attributes=attributes, body=message)
            )
        except Exception:
            self.handleError(record)


@contextlib.contextmanager
def cli_logging_handlers(
    *,
    sink: events.EventSink | None = None,
    logger_names: Sequence[str] = DEFAULT_LOGGER_NAMES,
    terminal_level: int | str = "info",
    bridge_level: int | str = DEFAULT_LOG_FILE_LEVEL,
    suppress_http_dependency_logs: bool = True,
) -> Generator[None]:
    """Attach terminal (and optional bridge) handlers to the named loggers.

    Adds and removes only its own handlers, restores prior logger levels on
    exit, and never touches the root logger or other handlers — safe to
    compose with a host application's logging configuration.

    With `suppress_http_dependency_logs=False`, httpx/httpcore loggers are
    bridged too, restoring wire-level debugging in the event stream.
    """
    resolved_terminal_level = _log_level(terminal_level)
    resolved_bridge_level = _log_level(bridge_level)
    handler_level = (
        min(resolved_terminal_level, resolved_bridge_level)
        if sink is not None
        else resolved_terminal_level
    )

    terminal_handler = logging.StreamHandler()
    terminal_handler.setLevel(resolved_terminal_level)
    terminal_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    bridge_handler = EventBridgeHandler(sink, level=resolved_bridge_level) if sink else None

    names = tuple(logger_names)
    if not suppress_http_dependency_logs:
        names += _HTTP_DEPENDENCY_LOGGER_NAMES
    previous_levels: dict[str, int] = {}
    attached: list[tuple[logging.Logger, logging.Handler]] = []
    try:
        for name in names:
            logger = logging.getLogger(name)
            previous_levels[name] = logger.level
            if logger.level == logging.NOTSET or logger.level > handler_level:
                logger.setLevel(handler_level)
            if name not in _HTTP_DEPENDENCY_LOGGER_NAMES:
                logger.addHandler(terminal_handler)
                attached.append((logger, terminal_handler))
            if bridge_handler is not None:
                logger.addHandler(bridge_handler)
                attached.append((logger, bridge_handler))
        yield
    finally:
        for logger, handler in attached:
            logger.removeHandler(handler)
        for name, level in previous_levels.items():
            logging.getLogger(name).setLevel(level)


def reset_observability_metrics() -> None:
    """Reset process-wide HTTP counters used by `logging_context()` run summaries."""
    with _HTTP_METRICS_LOCK:
        for metric_name in _HTTP_METRICS:
            _HTTP_METRICS[metric_name] = 0


def record_http_attempt(
    *,
    request_bytes: int,
    response_bytes: int = 0,
    status_code: int | None = None,
    transport_error: bool = False,
) -> None:
    """Record one HTTP attempt for the current run summary."""
    with _HTTP_METRICS_LOCK:
        _HTTP_METRICS["http.client.request.count"] += 1
        _HTTP_METRICS["http.client.request.body.size.total"] += request_bytes
        _HTTP_METRICS["http.client.response.body.size.total"] += response_bytes
        if transport_error:
            _HTTP_METRICS["http.client.transport_error.count"] += 1
        if status_code is None:
            return
        status_group = 5 if status_code >= 500 else status_code // 100
        metric_name = {
            2: "http.client.response.2xx.count",
            3: "http.client.response.3xx.count",
            4: "http.client.response.4xx.count",
            5: "http.client.response.5xx.count",
        }.get(status_group)
        if metric_name is not None:
            _HTTP_METRICS[metric_name] += 1
        if status_code == 429:
            _HTTP_METRICS["http.client.response.429.count"] += 1


def record_http_retry() -> None:
    """Record that an HTTP attempt will be retried."""
    with _HTTP_METRICS_LOCK:
        _HTTP_METRICS["http.client.retry.count"] += 1


def observability_summary() -> dict[str, Any]:
    """Return process-wide counters accumulated since logging was configured."""
    with _HTTP_METRICS_LOCK:
        return dict(_HTTP_METRICS)


def _run_resource_fields(extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return OTel resource attributes stamped once on the run-start event."""
    fields: dict[str, Any] = {
        events.PROCESS_PID: os.getpid(),
        events.PROCESS_RUNTIME_NAME: sys.implementation.name,
        events.PROCESS_RUNTIME_VERSION: sys.version.split()[0],
    }
    fields.update(dict(extra or {}))
    return fields


@contextlib.contextmanager
def observability_context(
    name: str,
    config: object | None = None,
    *,
    sink: events.EventSink | None = None,
    run: str = RUN,
    min_level: int | str = DEFAULT_LOG_FILE_LEVEL,
    git_cwd: Path | str | None = None,
    run_fields: Mapping[str, Any] | None = None,
    run_summary: Callable[[], Mapping[str, Any]] | None = None,
    resource: Mapping[str, Any] | None = None,
    open_telemetry: telemetry.OpenTelemetrySettings | None = None,
    resource_sample_interval_seconds: float | None = None,
    log_file: Path | None = None,
) -> Generator[None]:
    """Own one run's observability without touching stdlib logging handlers.

    Installs the event runtime (sink, run id, level floor), configures
    OpenTelemetry only when explicitly requested, emits run start/end and
    startup events, runs the resource sampler, and tears down in order:
    sampler -> run-end event -> sink flush -> OTel flush (owned providers
    only). The run-end event is emitted even on exceptions and `SystemExit`.
    """
    reset_observability_metrics()
    open_telemetry_runtime = telemetry.configure_open_telemetry(
        open_telemetry or telemetry.OpenTelemetrySettings()
    )
    runtime = events.EventRuntime(
        run=run,
        sink=sink or events.NullEventSink(),
        min_level=_log_level(min_level),
    )
    runtime_token = events.set_event_runtime(runtime)
    sampler = (
        ResourceSampler(resource_sample_interval_seconds)
        if resource_sample_interval_seconds is not None
        else None
    )
    started = time.perf_counter()
    error: BaseException | None = None
    try:
        with (
            log_context(command=name),
            telemetry.open_telemetry_span(name, {"command": name, **dict(run_fields or {})}),
        ):
            if sampler is not None:
                sampler.start()
            start_fields: dict[str, Any] = {"phase": "start", **dict(run_fields or {})}
            start_fields[events.RESOURCE] = _run_resource_fields(resource)
            debug("run", **start_fields)
            try:
                startup_event(
                    command=name,
                    config=config,
                    log_file=log_file,
                    git_cwd=_git_cwd_path(git_cwd),
                )
                yield
            except BaseException as exception:
                error = exception
                raise
            finally:
                error_type = _run_error_type(error)
                summary: dict[str, Any] = {}
                if sampler is not None:
                    summary.update(sampler.stop_and_summary())
                summary.update(observability_summary())
                summary["exit_code"] = _run_exit_code(error)
                if run_summary is not None:
                    summary.update(dict(run_summary()))
                end_fields: dict[str, Any] = {
                    "phase": "end",
                    "duration_ms": round((time.perf_counter() - started) * 1000.0),
                    "status": "error" if error_type else "ok",
                    events.ERROR_TYPE: error_type,
                    **dict(run_fields or {}),
                    **summary,
                }
                telemetry.set_current_span_attributes(end_fields)
                if error_type:
                    telemetry.mark_current_span_error(error_type)
                log_event("error" if error_type else "info", "run", **end_fields)
    finally:
        if sink is not None:
            events.flush_sink(sink)
        events.reset_event_runtime(runtime_token)
        open_telemetry_runtime.force_flush()


@contextlib.contextmanager
def cli_run_context(
    name: str,
    config: object | None = None,
    *,
    git_cwd: Path | str | None = None,
    logging_config: LoggingSettings | None = None,
    run_fields: Mapping[str, Any] | None = None,
    run_summary: Callable[[], Mapping[str, Any]] | None = None,
    resource: Mapping[str, Any] | None = None,
) -> Generator[Path | None]:
    """Compose CLI-mode logging for one run: JSONL sink + handlers + observability.

    Yields the JSON event-log path (or None when file logging is disabled).
    Teardown order: run-end event, sink flush, OTel flush, handler removal,
    sink close.
    """
    settings = logging_config or LoggingSettings(log_file_level=_src_log_level_from_config(config))
    log_file = settings.log_file
    if log_file is None and settings.logs_dir is not None:
        log_file = default_log_file(settings.logs_dir, run=settings.run)
    with contextlib.ExitStack() as stack:
        sink: events.JSONLEventSink | None = None
        if log_file is not None:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            _prune_old_log_files(log_file.parent, settings.retain_log_files)
            sink = stack.enter_context(events.JSONLEventSink(log_file))
        stack.enter_context(
            cli_logging_handlers(
                sink=sink,
                logger_names=settings.logger_names,
                terminal_level=settings.terminal_level,
                bridge_level=_log_file_level(settings.log_file_level),
                suppress_http_dependency_logs=settings.suppress_http_dependency_logs,
            )
        )
        if log_file is not None:
            logging.getLogger(__name__).info("Writing log events to %s.", log_file)
        stack.enter_context(
            observability_context(
                name,
                config,
                sink=sink,
                run=settings.run,
                min_level=_log_file_level(settings.log_file_level),
                git_cwd=git_cwd,
                run_fields=run_fields,
                run_summary=run_summary,
                resource=resource,
                open_telemetry=settings.open_telemetry,
                resource_sample_interval_seconds=settings.resource_sample_interval_seconds,
                log_file=log_file,
            )
        )
        yield log_file


def default_log_file(logs_dir: Path = DEFAULT_LOGS_DIR, *, run: str = RUN) -> Path:
    """Return a timestamped log-file path under `logs_dir`."""
    timestamp = _datetime.datetime.now(_datetime.UTC).strftime("%Y-%m-%d-%H-%M-%S-%z")
    timestamp = timestamp.replace("+", "", 1)
    return logs_dir / f"{timestamp}-{run}.json"


def log_event(level: str, key: str, *, logger_name: str = "", **fields: Any) -> None:
    """Emit one structured wide event to the active sink and the current span.

    `logger_name` is accepted for signature compatibility; events no longer
    ride stdlib logging, so it is ignored.
    """
    del logger_name
    numeric_level = _log_level(level)
    telemetry.add_span_event(key, {"level": logging.getLevelName(numeric_level), **fields})
    runtime = events.current_event_runtime()
    if numeric_level < runtime.min_level:
        return
    attributes = {**_CONTEXT.get({}), **fields}
    runtime.sink.emit(_event_payload(numeric_level, key, attributes=attributes))


def debug(key: str, *, logger_name: str = "", **fields: Any) -> None:
    """Log a DEBUG structured event."""
    log_event("debug", key, logger_name=logger_name, **fields)


def info(key: str, *, logger_name: str = "", **fields: Any) -> None:
    """Log an INFO structured event."""
    log_event("info", key, logger_name=logger_name, **fields)


def warning(key: str, *, logger_name: str = "", **fields: Any) -> None:
    """Log a WARNING structured event."""
    log_event("warning", key, logger_name=logger_name, **fields)


def error(key: str, *, logger_name: str = "", **fields: Any) -> None:
    """Log an ERROR structured event."""
    log_event("error", key, logger_name=logger_name, **fields)


def critical(key: str, *, logger_name: str = "", **fields: Any) -> None:
    """Log a CRITICAL structured event."""
    log_event("critical", key, logger_name=logger_name, **fields)


@contextlib.contextmanager
def log_context(**fields: Any) -> Generator[None]:
    """Add inherited structured fields for nested `log_event()` calls."""
    reset_token = _CONTEXT.set({**_CONTEXT.get({}), **fields})
    try:
        yield
    finally:
        _CONTEXT.reset(reset_token)


@contextlib.contextmanager
def stage(name: str, **fields: Any) -> Generator[None]:
    """Add a workflow stage field for nested logs and structured events."""
    with log_context(stage=name, **fields):
        yield


@contextlib.contextmanager
def span(
    key: str,
    *,
    level: str = "info",
    start_level: str | None = None,
    omit_success_status: bool = False,
    logger_name: str = "",
    **fields: Any,
) -> Generator[dict[str, Any]]:
    """Open an observed span; the span-end event is the canonical wide event.

    Attributes accumulated onto the yielded dict during the work ride the end
    event alongside duration and status. The start event is demoted to debug
    (wide-event discipline: one informative event per unit of work).
    """
    parent_span_id = telemetry.current_span_id()
    parent_reset_token = _PARENT_SPAN_CONTEXT.set(parent_span_id)
    try:
        with telemetry.open_telemetry_span(key, fields):
            log_event(start_level or "debug", key, logger_name=logger_name, phase="start", **fields)
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
                }
                if error:
                    end_fields["status"] = "error"
                    end_fields[events.ERROR_TYPE] = type(error).__name__
                    telemetry.mark_current_span_error(type(error).__name__)
                elif not omit_success_status:
                    end_fields["status"] = "ok"
                    end_fields[events.ERROR_TYPE] = None
                telemetry.set_current_span_attributes(end_fields)
                log_event(
                    "error" if error else level,
                    key,
                    logger_name=logger_name,
                    **end_fields,
                )
    finally:
        _PARENT_SPAN_CONTEXT.reset(parent_reset_token)


def submit_with_log_context(
    executor: Executor,
    function: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Future[Any]:
    """Submit work to an executor with current logging ContextVars propagated."""
    context = contextvars.copy_context()
    return executor.submit(context.run, function, *args, **kwargs)


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
        if _is_sensitive_log_field(key_text):
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
        return logging.INFO
    if normalized.isdecimal():
        return int(normalized)
    levels = logging.getLevelNamesMapping()
    level = levels.get(normalized)
    if level is None:
        return logging.INFO
    return level


def _structured_log_fields(record: logging.LogRecord) -> tuple[str, dict[str, Any]]:
    message = record.getMessage()
    fields: dict[str, Any] = (
        {"level": "DEBUG"}
        if record.name == "httpx" and message.startswith(_HTTPX_REQUEST_PREFIX)
        else {}
    )
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
        logged_value = REDACTED_LOG_VALUE if _is_sensitive_log_field(key) else value
        existing = headers.get(key)
        if existing is None:
            headers[key] = logged_value
        elif isinstance(existing, list):
            existing.append(logged_value)
        else:
            headers[key] = [existing, logged_value]
    return {key: headers[key] for key in sorted(headers)}


def _decode_http_bytes(value: object) -> str | None:
    if isinstance(value, bytes):
        return value.decode("latin-1", errors="replace")
    if isinstance(value, str):
        return value
    return None


def _is_sensitive_log_field(name: str) -> bool:
    lowered = name.lower()
    return any(fragment in lowered for fragment in SECRET_FIELD_FRAGMENTS)


def _secret_state(value: object) -> str:
    if value is None or value == "":
        return "missing"
    return "reference" if isinstance(value, str) and value.startswith("op://") else "provided"


def _run_error_type(exception: BaseException | None) -> str | None:
    if exception is None:
        return None
    if isinstance(exception, SystemExit) and exception.code in (None, 0):
        return None
    return type(exception).__name__


def _run_exit_code(exception: BaseException | None) -> int:
    if exception is None:
        return 0
    if isinstance(exception, SystemExit):
        return exception.code if isinstance(exception.code, int) else 1
    return 1


def _resource_usage() -> Any | None:
    if sys.platform == "win32":
        return None
    return resource.getrusage(resource.RUSAGE_SELF)


def _cpu_seconds(usage: Any) -> float:
    return float(usage.ru_utime) + float(usage.ru_stime)


def _rss_bytes(usage: Any | None) -> int | None:
    current = _linux_current_rss_bytes()
    if current is not None:
        return current
    if usage is None:
        return None
    # Linux reports ru_maxrss in KiB; macOS reports bytes.
    max_rss = int(usage.ru_maxrss)
    return max_rss if sys.platform == "darwin" else max_rss * 1024


def _linux_current_rss_bytes() -> int | None:
    statm = Path("/proc/self/statm")
    if not statm.exists():
        return None
    try:
        fields = statm.read_text(encoding="utf-8").split()
        if len(fields) < 2:
            return None
        return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError):
        return None


def _num_file_descriptors() -> int | None:
    for directory in (Path("/proc/self/fd"), Path("/dev/fd")):
        if not directory.exists():
            continue
        try:
            return len(list(directory.iterdir()))
        except OSError:
            continue
    return None


def _bytes_to_mib(byte_count: int) -> float:
    return round(byte_count / MEBIBYTE, 2)


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
