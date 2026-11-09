"""Bounded, authenticated calls to the existing OpenAI-compatible DSRI gateway."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from threading import Event, local
from typing import Any, Callable, TypeVar

import requests
from pydantic import BaseModel, ValidationError

from datasight import config
from datasight.infrastructure.llm.tokenizer import count_tokens

MAX_TOKENS = 2048
CONTEXT_TOKENS = 16384
MAX_PROMPT_CHARS = 40000
TEMPLATE_RESERVE = 1024
R = TypeVar("R", bound=BaseModel)


class InferenceError(RuntimeError):
    pass


class InferenceUnavailable(InferenceError):
    pass


class RequestTooLarge(InferenceError):
    pass


class OutputTruncated(InferenceError):
    pass


class InvalidCompletion(InferenceError):
    pass


class ExtractionBudgetExceeded(InferenceUnavailable):
    pass


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            value.pop("default", None)
            if value.get("type") == "object":
                value["additionalProperties"] = False
                value["required"] = list(value.get("properties", {}))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)
    return schema


def payload_for(instructions: str, data: Any, model: type[BaseModel]) -> dict[str, Any]:
    schema = strict_schema(model)
    system = instructions + "\nReturn only JSON matching this schema:\n" + json.dumps(schema, separators=(",", ":"))
    user = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # Count model tokens in both messages; reserve chat-template overhead and output.
    prompt_tokens = count_tokens(system) + count_tokens(user)
    if len(system) + len(user) > MAX_PROMPT_CHARS or prompt_tokens + MAX_TOKENS + TEMPLATE_RESERVE > CONTEXT_TOKENS:
        raise RequestTooLarge("Request exceeds the conservative context budget")
    return {
        "model": config.LLM_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "stream": False,
        "max_tokens": MAX_TOKENS,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_schema", "json_schema": {"name": model.__name__, "strict": True, "schema": schema}},
    }


def retry_delay(value: str | None, attempt: int) -> float:
    if value:
        try:
            return max(0, float(value))
        except ValueError:
            try:
                return max(0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
            except (ValueError, TypeError):
                pass
    return float(2 ** attempt)


class GatewayClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self._worker = local()
        self.before_attempt: Callable[[], float] | None = None
        self.remaining_time: Callable[[], float] | None = None
        self.cancel_inference: Event | None = None

    def worker_session(self) -> requests.Session:
        # requests.Session carries mutable cookies and is owned by one inference worker.
        self._worker.session = requests.Session()
        return self._worker.session

    @property
    def attempt_is_retry(self) -> bool:
        return getattr(self._worker, "retry", False)

    @property
    def request_is_repair(self) -> bool:
        return getattr(self._worker, "repair", False)

    @request_is_repair.setter
    def request_is_repair(self, value: bool) -> None:
        self._worker.repair = value

    def close(self) -> None:
        self.session.close()

    def status(self) -> dict[str, Any]:
        result = {"configured": bool(config.LLM_BASE_URL and config.LLM_API_KEY), "available": False, "model": config.LLM_MODEL}
        if not result["configured"]:
            return {**result, "message": "Set LLM_BASE_URL and LLM_API_KEY on the backend."}
        try:
            response = self.session.get(config.LLM_BASE_URL + "/v1/models", headers=self._headers(), timeout=(5, 8))
            if response.status_code == 200:
                models = response.json().get("data", [])
                available = any(item.get("id") == config.LLM_MODEL and item.get("available", True) for item in models)
                return {**result, "available": available, "message": "Model available." if available else "Model unavailable."}
            return {**result, "message": f"Gateway returned HTTP {response.status_code}."}
        except (requests.RequestException, ValueError, AttributeError, TypeError):
            return {**result, "message": "Gateway could not be reached or returned an invalid model listing."}

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {config.LLM_API_KEY}", "Content-Type": "application/json"}

    def complete(
        self,
        payload: dict[str, Any],
        response_model: type[R],
        record_attempt: Callable[[dict[str, Any]], None],
        validate: Callable[[R], None],
    ) -> R:
        if not config.LLM_BASE_URL or not config.LLM_API_KEY:
            raise InferenceError("LLM_BASE_URL and LLM_API_KEY are required")
        for attempt in range(3):
            self._worker.retry = attempt > 0
            remaining = self.before_attempt() if self.before_attempt else config.LLM_TIMEOUT_SEC
            started = time.monotonic()
            response_data: Any = None
            status: int | None = None
            delay = float(2 ** attempt)
            error: InferenceError | None = None
            retry = False
            try:
                connect_timeout = min(5, remaining / 2)
                read_timeout = min(config.LLM_TIMEOUT_SEC, max(.001, remaining-connect_timeout))
                session = getattr(self._worker, "session", self.session)
                response = session.post(config.LLM_BASE_URL + "/v1/chat/completions", json=payload, headers=self._headers(), timeout=(connect_timeout, read_timeout))
                status = response.status_code
                try:
                    response_data = response.json()
                except ValueError:
                    response_data = {"invalid_json": True}
                if status == 503:
                    raise InferenceUnavailable("GPU inference is unavailable; resume during a GPU session")
                if status == 413:
                    raise RequestTooLarge("Gateway rejected request size")
                if status == 400:
                    match response_data:
                        case {"detail": {"error": {"code": "prompt_too_large"}}}:
                            raise RequestTooLarge("Gateway rejected prompt size")
                if status is not None and (status == 429 or status >= 500):
                    retry = True
                    delay = retry_delay(response.headers.get("Retry-After"), attempt)
                    raise InferenceError(f"Transient gateway HTTP {status}")
                if status != 200:
                    raise InferenceError(f"Gateway HTTP {status}; check server-side configuration")
                if response_data.get("model") != config.LLM_MODEL:
                    raise InferenceError("Gateway returned an unexpected model")
                choices = response_data["choices"]
                if len(choices) != 1:
                    raise InferenceError("Expected one completion choice")
                if choices[0].get("finish_reason") == "length":
                    raise OutputTruncated("Completion reached its output limit")
                if choices[0].get("finish_reason") != "stop":
                    raise InferenceError("Completion did not stop normally")
                parsed = response_model.model_validate_json(choices[0]["message"]["content"])
                validate(parsed)
            except requests.ReadTimeout:
                deadline_limited = self.remaining_time is not None and read_timeout < config.LLM_TIMEOUT_SEC
                retry = not deadline_limited
                error = (ExtractionBudgetExceeded('extraction_budget_exceeded: request read deadline; resume explicitly')
                         if deadline_limited else InferenceUnavailable('Gateway connection failed or timed out; extraction can be resumed'))
            except (requests.Timeout, requests.ConnectionError):
                retry = True
                error = InferenceUnavailable("Gateway connection failed or timed out; extraction can be resumed")
            except requests.RequestException:
                error = InferenceError("Gateway request failed")
            except InferenceError as exc:
                error = exc
            except (ValidationError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
                error = InvalidCompletion(f"Invalid completion: {type(exc).__name__}")
            else:
                record_attempt({"http_status": status, "response": response_data, "elapsed_seconds": time.monotonic() - started, "error": None})
                return parsed
            record_attempt({"http_status": status, "response": response_data, "elapsed_seconds": time.monotonic() - started, "error": str(error)})
            if not retry or attempt == 2:
                raise error or InferenceError("Inference failed")
            # Long server-requested waits require an explicit later resume.
            if delay > 60:
                raise InferenceUnavailable("Rate limit requires a later resume")
            if self.remaining_time is not None and delay >= self.remaining_time():
                raise ExtractionBudgetExceeded('extraction_budget_exceeded: retry wait exceeds remaining time; resume explicitly')
            if self.cancel_inference is None:
                time.sleep(delay)
            elif self.cancel_inference.wait(delay):
                raise ExtractionBudgetExceeded("Extraction stopped after another request failed")
        raise InferenceError("Inference attempts exhausted")
