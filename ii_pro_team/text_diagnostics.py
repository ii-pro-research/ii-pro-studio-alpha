"""Bounded public transport facts; no bodies, raw headers or exception text."""

import re
import time


API_ERROR_CODES = frozenset(
    {
        "subscription_sharing_user_not_eligible",
        "subscription_sharing_usage_limit_exceeded",
        "subscription_sharing_usage_unavailable",
        "subscription_sharing_unsupported_capability",
        "subscription_sharing_route_not_supported",
        "subscription_sharing_invalid_user",
        "subscription_sharing_user_unavailable",
        "chatpass_v2_scope_not_authorized",
        "chatpass_v2_invalid_authorization_context",
        "invalid_api_key",
        "invalid_request_error",
        "invalid_request",
        "unsupported_parameter",
        "model_not_found",
        "rate_limit_exceeded",
        "insufficient_quota",
        "server_error",
        "authentication_error",
        "permission_denied",
    }
)
EVENT_TYPES = frozenset(
    {
        "error",
        "response.created",
        "response.queued",
        "response.in_progress",
        "response.completed",
        "response.failed",
        "response.incomplete",
        "response.output_item.added",
        "response.output_item.done",
        "response.content_part.added",
        "response.content_part.done",
        "response.output_text.delta",
        "response.output_text.done",
        "response.output_text.annotation.added",
        "response.reasoning_text.delta",
        "response.reasoning_text.done",
        "response.reasoning_summary_text.delta",
        "response.reasoning_summary_text.done",
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_part.done",
        "response.refusal.delta",
        "response.refusal.done",
    }
)
MESSAGES = {
    "bridge_packet_quota": "Превышен предел числа пакетов между локальным мостом DevKit и Studio.",
    "devkit_failed": "Официальный DevKit не подтвердил завершение; автоматического повтора нет.",
    "serialize_failed": "Не удалось подготовить JSON запроса; отправки не было.",
    "connect_failed": "Не удалось установить соединение до отправки HTTP-запроса.",
    "send_failed": "Ошибка отправки; передача запроса могла начаться.",
    "headers_failed": "Ошибка получения HTTP-заголовков после отправки запроса.",
    "http_rejected": "Сервер вернул HTTP-отказ; автоматического повтора нет.",
    "error_body_failed": "Не удалось прочитать диагностику HTTP-отказа.",
    "unexpected_content_type": "Тип ответа не соответствует text/event-stream.",
    "stream_read_failed": "Соединение прервано при чтении SSE.",
    "utf8_invalid": "Ответ содержит некорректный UTF-8.",
    "sse_json_invalid": "Некорректный JSON в SSE-событии.",
    "sse_unterminated": "Поток оборвался внутри SSE-события.",
    "stream_quota": "Превышен предел размера строки или потока SSE.",
    "event_quota": "Превышен предел числа SSE-событий.",
    "deadline": "Истекло время ожидания; внешнее завершение не доказано.",
    "text_idle": "DevKit не передавал текст в течение заданного интервала; внешнее завершение не доказано.",
    "cancelled": "Ожидание отменено; внешнее завершение не доказано.",
    "unknown_event": "Получен неподдерживаемый тип события; поток остановлен.",
    "invalid_event": "Событие не соответствует протоколу Responses.",
    "provider_failed": "Провайдер сообщил об ошибке или незавершённом ответе.",
    "missing_completed": "Поток завершился без подтверждения response.completed.",
    "completion_invalid": "Проверка идентичности или содержимого итогового ответа не пройдена.",
    "adapter_failed": "Локальная проверка ответа не пройдена; результат не подтверждён.",
    "completed": "Получен и проверен response.completed.",
}
EXCEPTION_TYPES = frozenset(
    {
        "OSError",
        "TimeoutError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "BrokenPipeError",
        "ConnectionRefusedError",
        "RemoteDisconnected",
        "IncompleteRead",
        "HTTPException",
        "BadStatusLine",
        "SSLError",
        "SSLCertVerificationError",
        "UnicodeDecodeError",
        "JSONDecodeError",
        "ValueError",
        "TypeError",
        "AttributeError",
        "ValidationError",
        "Conflict",
        "TextUncertain",
    }
)


def exception_type(exc):
    name = type(exc).__name__
    return name if name in EXCEPTION_TYPES else "Exception"


def api_code(value):
    if value is None:
        return None
    return (
        value if isinstance(value, str) and value in API_ERROR_CODES else "unrecognized"
    )


class TransportDiagnostic:
    def __init__(self):
        self.started = time.monotonic()
        self.data = {
            "version": 1,
            "stage": "prepare_request",
            "outcome": "before_send",
            "request_transmission": "not_sent",
            "http_status": None,
            "content_type": None,
            "content_type_present": False,
            "request_id": None,
            "primary_exception_type": None,
            "os_error_number": None,
            "windows_error_number": None,
            "safe_message": None,
            "api_error_code": None,
            "error_body_shape": None,
            "received_bytes": 0,
            "sse_frames": 0,
            "sse_events": 0,
            "last_event_type": None,
            "response_created_received": False,
            "response_completed_received": False,
            "termination_reason": None,
        }

    def headers(self, response):
        self.data["http_status"] = response.status
        # HTTPResponse.getheader preserves absence as None. Do not use
        # response.headers.get_content_type(): email.message defaults to text/plain.
        raw = response.getheader("Content-Type")
        self.data["content_type_present"] = raw is not None
        media = raw.split(";", 1)[0].strip().lower() if raw is not None else None
        # Retain the actual normalized MIME, including unknown types, but never
        # raw parameters (which can contain credentials) or malformed header text.
        self.data["content_type"] = (
            media
            if media is None
            or media == ""
            or (
                len(media) <= 127
                and re.fullmatch(r"[a-z0-9!#$&^_.+\-]+/[a-z0-9!#$&^_.+\-]+", media)
            )
            else "invalid"
        )
        raw_id = response.getheader("x-request-id", "") or ""
        if re.fullmatch(
            r"(?:req_[A-Za-z0-9_-]{1,128}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})",
            raw_id,
        ):
            self.data["request_id"] = raw_id

    def observe(self, packet):
        self.data["sse_events"] += 1
        kind = packet.get("type") if isinstance(packet, dict) else None
        self.data["last_event_type"] = (
            kind if isinstance(kind, str) and kind in EVENT_TYPES else "unrecognized"
        )
        if kind == "response.created":
            self.data["response_created_received"] = True
        if kind == "response.completed":
            self.data["response_completed_received"] = True

    def fail(self, reason, exc=None):
        self.data["termination_reason"] = reason
        self.data["safe_message"] = MESSAGES[reason]
        if exc is not None:
            self.data["primary_exception_type"] = exception_type(exc)
            for field, attribute in (
                ("os_error_number", "errno"),
                ("windows_error_number", "winerror"),
            ):
                value = getattr(exc, attribute, None)
                if type(value) is int and 0 <= value < 2**31:
                    self.data[field] = value
        self.data["outcome"] = (
            "confirmed_http_rejection"
            if self.data["http_status"] and self.data["http_status"] != 200
            else "before_send"
            if self.data["request_transmission"] == "not_sent"
            else "uncertain_after_send"
        )

    def snapshot(self, secrets=()):
        result = {
            **self.data,
            "elapsed_seconds": round(time.monotonic() - self.started, 6),
        }
        # Header-like metadata is untrusted even after syntax validation.
        for key, value in result.items():
            if isinstance(value, str) and any(
                secret and secret in value for secret in secrets
            ):
                result[key] = "[скрыто]"
        return result
