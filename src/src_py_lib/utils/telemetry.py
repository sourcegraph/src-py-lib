"""OpenTelemetry bootstrap and small instrumentation helpers."""

from __future__ import annotations

import importlib
import json
import os
import urllib.parse
from collections.abc import Mapping, MutableMapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Final, cast

from opentelemetry import metrics, propagate, trace
from opentelemetry.trace import Status, StatusCode, format_span_id, format_trace_id

from src_py_lib.utils.config import Config, config_field

OPEN_TELEMETRY_HELP_GROUP: Final[str] = "OpenTelemetry"
OTEL_ENABLED: Final[str] = "OTEL_ENABLED"
OTEL_SDK_DISABLED: Final[str] = "OTEL_SDK_DISABLED"
OTEL_SERVICE_NAME: Final[str] = "OTEL_SERVICE_NAME"
OTEL_RESOURCE_ATTRIBUTES: Final[str] = "OTEL_RESOURCE_ATTRIBUTES"
OTEL_EXPORTER_OTLP_ENDPOINT: Final[str] = "OTEL_EXPORTER_OTLP_ENDPOINT"
OTEL_EXPORTER_OTLP_HEADERS: Final[str] = "OTEL_EXPORTER_OTLP_HEADERS"
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: Final[str] = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
OTEL_EXPORTER_OTLP_METRICS_ENDPOINT: Final[str] = "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"

_TRACER_NAME: Final[str] = "src_py_lib"
_METER_NAME: Final[str] = "src_py_lib"
_TRACEPARENT_HEADER: Final[str] = "traceparent"


class OpenTelemetryConfig(Config):
    """Config fields for OpenTelemetry CLI and environment options."""

    open_telemetry: bool = config_field(
        default=False,
        env_var=OTEL_ENABLED,
        cli_flag="--otel",
        cli_action="store_true",
        help="Enable OpenTelemetry OTLP/HTTP traces and metrics",
        help_group=OPEN_TELEMETRY_HELP_GROUP,
    )
    open_telemetry_service_name: str | None = config_field(
        default=None,
        env_var=OTEL_SERVICE_NAME,
        cli_flag="--otel-service-name",
        metavar="NAME",
        help="OpenTelemetry service name; maps to OTEL_SERVICE_NAME",
        help_group=OPEN_TELEMETRY_HELP_GROUP,
    )
    open_telemetry_resource_attributes: str | None = config_field(
        default=None,
        env_var=OTEL_RESOURCE_ATTRIBUTES,
        cli_flag="--otel-resource-attributes",
        metavar="KEY=VALUE,...",
        help="Resource attributes; maps to OTEL_RESOURCE_ATTRIBUTES",
        help_group=OPEN_TELEMETRY_HELP_GROUP,
    )
    open_telemetry_exporter_otlp_endpoint: str | None = config_field(
        default=None,
        env_var=OTEL_EXPORTER_OTLP_ENDPOINT,
        cli_flag="--otel-exporter-otlp-endpoint",
        metavar="URL",
        help="OTLP/HTTP endpoint; maps to OTEL_EXPORTER_OTLP_ENDPOINT",
        help_group=OPEN_TELEMETRY_HELP_GROUP,
    )
    open_telemetry_exporter_otlp_headers: str | None = config_field(
        default=None,
        env_var=OTEL_EXPORTER_OTLP_HEADERS,
        cli_flag="--otel-exporter-otlp-headers",
        metavar="KEY=VALUE,...",
        help="OTLP headers; maps to OTEL_EXPORTER_OTLP_HEADERS",
        help_group=OPEN_TELEMETRY_HELP_GROUP,
        secret=True,
    )
    open_telemetry_exporter_otlp_traces_endpoint: str | None = config_field(
        default=None,
        env_var=OTEL_EXPORTER_OTLP_TRACES_ENDPOINT,
        cli_flag="--otel-exporter-otlp-traces-endpoint",
        metavar="URL",
        help="OTLP/HTTP traces endpoint; maps to OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        help_group=OPEN_TELEMETRY_HELP_GROUP,
    )
    open_telemetry_exporter_otlp_metrics_endpoint: str | None = config_field(
        default=None,
        env_var=OTEL_EXPORTER_OTLP_METRICS_ENDPOINT,
        cli_flag="--otel-exporter-otlp-metrics-endpoint",
        metavar="URL",
        help="OTLP/HTTP metrics endpoint; maps to OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        help_group=OPEN_TELEMETRY_HELP_GROUP,
    )


@dataclass(frozen=True)
class OpenTelemetrySettings:
    """Runtime OpenTelemetry settings resolved from config and call-site needs."""

    enabled: bool = False
    force_traces: bool = False
    service_name: str | None = None
    resource_attributes: str | None = None
    exporter_otlp_endpoint: str | None = None
    exporter_otlp_headers: str | None = None
    exporter_otlp_traces_endpoint: str | None = None
    exporter_otlp_metrics_endpoint: str | None = None


@dataclass(frozen=True)
class OpenTelemetryRuntime:
    """OpenTelemetry provider handles configured for the current process."""

    configured: bool
    exporting: bool
    tracer_provider: object | None = None
    meter_provider: object | None = None

    def force_flush(self, timeout_millis: int = 30_000) -> None:
        """Flush configured providers if they expose a force_flush method."""
        for provider in (self.tracer_provider, self.meter_provider):
            force_flush = getattr(provider, "force_flush", None)
            if callable(force_flush):
                force_flush(timeout_millis=timeout_millis)


class OpenTelemetrySetupError(RuntimeError):
    """Raised when OpenTelemetry was requested but optional packages are missing."""


def open_telemetry_settings_from_config(
    config: object | None = None,
    *,
    enabled: bool | None = None,
    force_traces: bool = False,
    service_name: str | None = None,
) -> OpenTelemetrySettings:
    """Return OpenTelemetry settings from shared config fields."""
    resolved_enabled = (
        _config_value(config, "open_telemetry", False) if enabled is None else enabled
    )
    return OpenTelemetrySettings(
        enabled=bool(resolved_enabled),
        force_traces=force_traces,
        service_name=service_name or _optional_string(config, "open_telemetry_service_name"),
        resource_attributes=_optional_string(config, "open_telemetry_resource_attributes"),
        exporter_otlp_endpoint=_optional_string(config, "open_telemetry_exporter_otlp_endpoint"),
        exporter_otlp_headers=_optional_string(config, "open_telemetry_exporter_otlp_headers"),
        exporter_otlp_traces_endpoint=_optional_string(
            config, "open_telemetry_exporter_otlp_traces_endpoint"
        ),
        exporter_otlp_metrics_endpoint=_optional_string(
            config, "open_telemetry_exporter_otlp_metrics_endpoint"
        ),
    )


def configure_open_telemetry(settings: OpenTelemetrySettings) -> OpenTelemetryRuntime:
    """Configure OpenTelemetry providers for traces and metrics.

    `enabled=True` exports OTLP/HTTP traces and metrics. `force_traces=True`
    installs SDK providers without exporters so W3C propagation still has real
    trace/span identifiers, useful for Sourcegraph debug trace capture.
    """
    if _otel_sdk_disabled() or not (settings.enabled or settings.force_traces):
        return OpenTelemetryRuntime(configured=False, exporting=False)

    resource = _resource(settings)
    tracer_provider = _configure_traces(settings, resource)
    meter_provider = _configure_metrics(settings, resource)
    return OpenTelemetryRuntime(
        configured=True,
        exporting=settings.enabled,
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
    )


def inject_current_trace_context(headers: MutableMapping[str, str]) -> None:
    """Inject the active W3C trace context into outbound request headers."""
    if any(name.lower() == _TRACEPARENT_HEADER for name in headers):
        return
    propagate.inject(headers)


def current_traceparent_header() -> str | None:
    """Return the active W3C traceparent header value, if a valid span exists."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier.get(_TRACEPARENT_HEADER)


def traceparent_fields(value: str | None) -> dict[str, str]:
    """Return trace/span identifiers extracted from a W3C traceparent header."""
    if not value:
        return {}
    context = propagate.extract({_TRACEPARENT_HEADER: value})
    span_context = trace.get_current_span(context).get_span_context()
    if not span_context.is_valid:
        return {}
    return {
        "trace_id": format_trace_id(span_context.trace_id),
        "span_id": format_span_id(span_context.span_id),
    }


@contextmanager
def open_telemetry_span(name: str, fields: Mapping[str, object] | None = None) -> Any:
    """Start an OpenTelemetry span and attach safe attributes."""
    with trace.get_tracer(_TRACER_NAME).start_as_current_span(name) as span:
        if fields:
            for key, value in span_attributes(fields).items():
                span.set_attribute(key, value)
        yield span


def current_trace_fields(parent_span_id: str | None = None) -> dict[str, str]:
    """Return log-friendly trace/span identifiers for the active span."""
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return {}
    fields = {
        "trace": format_trace_id(span_context.trace_id),
        "span": format_span_id(span_context.span_id),
    }
    if parent_span_id:
        fields["parent_span"] = parent_span_id
    return fields


def current_span_id() -> str | None:
    """Return the current span identifier, if there is a valid active span."""
    span_context = trace.get_current_span().get_span_context()
    return format_span_id(span_context.span_id) if span_context.is_valid else None


def set_current_span_attributes(fields: Mapping[str, object]) -> None:
    """Set log-safe attributes on the active span."""
    span = trace.get_current_span()
    if not span.is_recording():
        return
    for key, value in span_attributes(fields).items():
        span.set_attribute(key, value)


def add_current_span_event(name: str, fields: Mapping[str, object]) -> None:
    """Add a log event to the active span when recording is enabled."""
    span = trace.get_current_span()
    if span.is_recording():
        span.add_event(name, attributes=span_attributes(fields))


def mark_current_span_error(description: str) -> None:
    """Mark the active span as failed."""
    span = trace.get_current_span()
    if span.is_recording():
        span.set_status(Status(StatusCode.ERROR, description))


def record_http_client_metrics(
    *,
    method: str,
    url: str,
    duration_seconds: float,
    request_bytes: int,
    response_bytes: int = 0,
    status_code: int | None = None,
    transport_error: bool = False,
) -> None:
    """Record one HTTP client attempt with OpenTelemetry metrics."""
    attributes = _http_metric_attributes(
        method=method,
        url=url,
        status_code=status_code,
        transport_error=transport_error,
    )
    instruments = _http_metric_instruments()
    instruments["request_count"].add(1, attributes)
    instruments["duration"].record(max(duration_seconds, 0.0), attributes)
    instruments["request_bytes"].record(max(request_bytes, 0), attributes)
    instruments["response_bytes"].record(max(response_bytes, 0), attributes)
    if transport_error:
        instruments["transport_error_count"].add(1, attributes)


def record_http_client_retry(*, method: str, url: str, status_code: int | None = None) -> None:
    """Record one HTTP client retry with OpenTelemetry metrics."""
    _http_metric_instruments()["retry_count"].add(
        1,
        _http_metric_attributes(method=method, url=url, status_code=status_code),
    )


def span_attributes(fields: Mapping[str, object]) -> dict[str, Any]:
    """Return OpenTelemetry-safe attributes from structured log fields."""
    attributes: dict[str, Any] = {}
    for key, value in fields.items():
        attribute_value = _attribute_value(value)
        if attribute_value is not None:
            attributes[key] = attribute_value
    return attributes


def _configure_traces(settings: OpenTelemetrySettings, resource: object) -> object:
    provider = trace.get_tracer_provider()
    if not _is_default_provider(provider):
        return provider

    tracer_provider_class = _required_symbol(
        "opentelemetry.sdk.trace",
        "TracerProvider",
    )
    tracer_provider = tracer_provider_class(resource=resource)
    if settings.enabled:
        exporter_class = _required_symbol(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter",
            "OTLPSpanExporter",
        )
        processor_class = _required_symbol(
            "opentelemetry.sdk.trace.export",
            "BatchSpanProcessor",
        )
        exporter = exporter_class(
            endpoint=settings.exporter_otlp_traces_endpoint or settings.exporter_otlp_endpoint,
            headers=_headers(settings.exporter_otlp_headers),
        )
        tracer_provider.add_span_processor(processor_class(exporter))
    trace.set_tracer_provider(tracer_provider)
    return tracer_provider


def _configure_metrics(settings: OpenTelemetrySettings, resource: object) -> object:
    provider = metrics.get_meter_provider()
    if not _is_default_provider(provider):
        return provider

    meter_provider_class = _required_symbol(
        "opentelemetry.sdk.metrics",
        "MeterProvider",
    )
    readers: list[object] = []
    if settings.enabled:
        exporter_class = _required_symbol(
            "opentelemetry.exporter.otlp.proto.http.metric_exporter",
            "OTLPMetricExporter",
        )
        reader_class = _required_symbol(
            "opentelemetry.sdk.metrics.export",
            "PeriodicExportingMetricReader",
        )
        exporter = exporter_class(
            endpoint=settings.exporter_otlp_metrics_endpoint or settings.exporter_otlp_endpoint,
            headers=_headers(settings.exporter_otlp_headers),
        )
        readers.append(reader_class(exporter))
    meter_provider = meter_provider_class(resource=resource, metric_readers=readers)
    metrics.set_meter_provider(meter_provider)
    return meter_provider


def _resource(settings: OpenTelemetrySettings) -> object:
    resource_class = _required_symbol("opentelemetry.sdk.resources", "Resource")
    attributes = _resource_attributes(settings.resource_attributes)
    if settings.service_name:
        attributes["service.name"] = settings.service_name
    return resource_class.create(attributes)


def _http_metric_instruments() -> dict[str, Any]:
    return _http_metric_instruments_for_provider(id(metrics.get_meter_provider()))


@cache
def _http_metric_instruments_for_provider(_provider_id: int) -> dict[str, Any]:
    meter = metrics.get_meter(_METER_NAME)
    return {
        "request_count": meter.create_counter(
            "http.client.request.count",
            unit="{request}",
            description="HTTP client attempts",
        ),
        "retry_count": meter.create_counter(
            "src_py_lib.http.client.retry.count",
            unit="{retry}",
            description="HTTP client retries",
        ),
        "transport_error_count": meter.create_counter(
            "src_py_lib.http.client.transport_error.count",
            unit="{error}",
            description="HTTP client transport errors",
        ),
        "duration": meter.create_histogram(
            "http.client.request.duration",
            unit="s",
            description="HTTP client attempt duration",
        ),
        "request_bytes": meter.create_histogram(
            "http.client.request.body.size",
            unit="By",
            description="HTTP request body size",
        ),
        "response_bytes": meter.create_histogram(
            "http.client.response.body.size",
            unit="By",
            description="HTTP response body size",
        ),
    }


def _http_metric_attributes(
    *,
    method: str,
    url: str,
    status_code: int | None = None,
    transport_error: bool = False,
) -> dict[str, object]:
    split_url = urllib.parse.urlsplit(url)
    attributes: dict[str, object] = {
        "http.request.method": method.upper(),
        "server.address": split_url.hostname or "",
        "url.scheme": split_url.scheme,
        "error.type": "transport" if transport_error else "",
    }
    if split_url.port is not None:
        attributes["server.port"] = split_url.port
    if status_code is not None:
        attributes["http.response.status_code"] = status_code
    return attributes


def _attribute_value(value: object) -> Any:
    if value is None:
        return None
    if isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray | str):
        values = [_attribute_value(entry) for entry in cast(Sequence[object], value)]
        if all(isinstance(entry, str | bool | int | float) for entry in values):
            return values
    if isinstance(value, Mapping):
        return json.dumps(value, default=str, sort_keys=True)
    return str(cast(object, value))


def _resource_attributes(value: str | None) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for part in (value or "").split(","):
        key, separator, raw_value = part.strip().partition("=")
        if separator and key:
            attributes[key] = raw_value
    return attributes


def _headers(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    headers: dict[str, str] = {}
    for part in value.split(","):
        key, separator, raw_value = part.strip().partition("=")
        if separator and key:
            headers[key] = urllib.parse.unquote(raw_value)
    return headers


def _required_symbol(module_name: str, symbol_name: str) -> Any:
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exception:
        raise OpenTelemetrySetupError(
            "OpenTelemetry export requires the src-py-lib[otel] extra. "
            "Install it or disable --otel."
        ) from exception
    return getattr(module, symbol_name)


def _is_default_provider(provider: object) -> bool:
    return provider.__class__.__name__.startswith("Proxy")


def _otel_sdk_disabled() -> bool:
    return os.environ.get(OTEL_SDK_DISABLED, "").strip().lower() in {"1", "true", "yes"}


def _config_value(config: object | None, name: str, default: object) -> object:
    return getattr(config, name, default) if config is not None else default


def _optional_string(config: object | None, name: str) -> str | None:
    value = _config_value(config, name, None)
    return value if isinstance(value, str) and value else None
