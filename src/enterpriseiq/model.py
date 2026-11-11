"""Small DeepSeek Chat Completions adapter; no credentials or HTTP bodies in errors."""

from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os

import httpx


_budget_grant = ContextVar('budget_grant', default=None)


def request_digest(messages, tools, model='deepseek-flash'):
    return hashlib.sha256(json.dumps([model, messages, tools], ensure_ascii=False,
                                    sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@contextmanager
def budget_grant(digest):
    """One HTTP attempt per committed reservation; not an authentication boundary."""
    token = _budget_grant.set(digest)
    try:
        yield
    finally:
        _budget_grant.reset(token)


class ModelError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class Completion:
    message: dict
    usage: dict
    model: str
    response_id: str
    finish_reason: str


class DeepSeekModel:
    def __init__(self):
        self.name = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
        self.base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
        self._key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not self._key:
            raise ModelError("missing_deepseek_key")
        if self.base_url not in ("https://api.deepseek.com", "https://api.deepseek.com/v1"):
            raise ModelError("unsupported_deepseek_endpoint")

    def complete(self, messages: list[dict], tools: list[dict], timeout: float) -> Completion:
        if _budget_grant.get() != request_digest(messages, tools, self.name):
            raise ModelError('model_budget_context_required')
        _budget_grant.set(None)
        try:
            # Non-thinking mode avoids storing or exporting hidden reasoning.
            with httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=False) as client:
                response = client.post(self.base_url + "/chat/completions",
                    headers={"Authorization": "Bearer " + self._key},
                    json={"model": self.name, "messages": messages, "tools": tools,
                          "tool_choice": "auto", "thinking": {"type": "disabled"},
                          "temperature": 0, "max_tokens": 4096, "stream": False})
            if response.status_code != 200:
                raise ModelError(f"model_http_{response.status_code}")
            payload = response.json()
            choice = payload["choices"][0]
            source = choice["message"]
            message = {"role": "assistant", "content": source.get("content") or ""}
            if source.get("tool_calls"):
                calls = source["tool_calls"]
                if not isinstance(calls, list) or any(
                    not isinstance(c.get("id"), str) or c.get("type") != "function"
                    or not isinstance(c.get("function", {}).get("name"), str)
                    or not isinstance(c.get("function", {}).get("arguments"), str) for c in calls
                ) or len({c["id"] for c in calls}) != len(calls):
                    raise ModelError("invalid_tool_call_protocol")
                message["tool_calls"] = calls
            return Completion(message, payload.get("usage", {}), payload.get("model", self.name),
                              payload.get("id", ""), choice.get("finish_reason", ""))
        except ModelError:
            raise
        except httpx.TimeoutException:
            raise ModelError("model_timeout") from None
        except httpx.HTTPError:
            raise ModelError("model_connection_error") from None
        except (KeyError, IndexError, TypeError, ValueError):
            raise ModelError("invalid_model_response") from None
