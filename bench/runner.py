"""Experimental harness.

Four conditions are compared on the same task suite, differing in only two variables:
whether the tool schema carries physical metadata, and whether validation is enforced.
Tool bodies are identical throughout, so an outcome difference is attributable to those
two variables rather than to the tools.

    baseline        plain schema, units in prose, no enforcement
    schema_only     physical metadata in the schema, no enforcement
    guarded         physical metadata, enforced, a violation ends the run
    guarded_repair  physical metadata, enforced, violations returned for repair

The headline measure is the silent error rate: a wrong answer that surfaced no violation
and tripped no audit flag. That is the harm the library exists to prevent, and it is the
only outcome a downstream reader cannot detect for themselves.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from quantity_guard import session
from quantity_guard.registry import ureg

from .tasks import Task

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

CONDITIONS: dict[str, dict[str, Any]] = {
    "baseline": {"physical_metadata": False, "enforcement": "off", "repair": False},
    "schema_only": {"physical_metadata": True, "enforcement": "off", "repair": False},
    "guarded": {"physical_metadata": True, "enforcement": "strict", "repair": False},
    "guarded_repair": {"physical_metadata": True, "enforcement": "strict", "repair": True},
}

SYSTEM = (
    "You are a hydrology analyst. Answer the question using the tools provided. "
    "Use tools for every quantity you report; do not rely on prior knowledge for "
    "numbers. When you have the answer, finish your reply with a single final line "
    "in exactly this form:\n"
    "ANSWER: <number> <unit>\n"
    "If the question asks for a quantity that no available tool can supply, write "
    "ANSWER: unavailable\n"
    "Do not write the ANSWER line until you have finished using tools."
)

_ANSWER = re.compile(r"ANSWER:\s*(?P<body>.+?)\s*$", re.IGNORECASE | re.MULTILINE)


@dataclass
class RunResult:
    task: str
    hazard: str
    condition: str
    model: str
    replicate: int
    outcome: str  # "correct", "wrong", "blocked", or "error"
    silent_error: bool
    undetected: bool = False
    stated: str = ""
    violations: int = 0
    audit_unsourced: int = 0
    audit_mislabelled: int = 0
    tool_calls: int = 0
    turns: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    detail: str = ""
    answer_text: str = ""
    audit_derived: int = 0
    audit_quoted: int = 0
    calls_log: list = field(default_factory=list)


class OpenRouter:
    """Minimal OpenRouter client. OpenRouter exposes an OpenAI-shaped endpoint, so the
    Anthropic SDK cannot drive it and the request is issued directly."""

    def __init__(self, model: str, api_key: str | None = None, timeout: float = 180.0):
        self.model = model
        self.api_key = api_key or os.environ["OPENROUTER_API_KEY"]
        self.timeout = timeout

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        payload = {"model": self.model, "messages": messages, "max_tokens": 2048}
        if tools:
            payload["tools"] = tools
        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            OPENROUTER_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        last: Exception | None = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode()[:400]
                if exc.code in (429, 500, 502, 503, 529):
                    last = RuntimeError(f"{exc.code}: {detail}")
                    time.sleep(2 ** attempt + 1)
                    continue
                raise RuntimeError(f"{exc.code}: {detail}") from None
            except (urllib.error.URLError, TimeoutError) as exc:
                last = exc
                time.sleep(2 ** attempt + 1)
        raise RuntimeError(f"request failed after retries: {last}")
