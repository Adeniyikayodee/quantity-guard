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


def openai_tools(tools, physical_metadata: bool) -> list[dict]:
    """Tool definitions in the shape the chat-completions endpoint expects."""
    out = []
    for tool in tools:
        schema = tool.json_schema(physical_metadata=physical_metadata)
        out.append(
            {
                "type": "function",
                "function": {
                    "name": schema["name"],
                    "description": schema["description"],
                    "parameters": schema["inputSchema"],
                },
            }
        )
    return out


def run_one(task: Task, condition: str, client: OpenRouter, replicate: int,
            max_turns: int = 8) -> RunResult:
    """Drive one task under one condition to a final answer or a terminal violation."""
    config = CONDITIONS[condition]
    tools = [t.clone(config["enforcement"]) for t in task.tools]
    by_name = {t.name: t for t in tools}
    schema = openai_tools(tools, config["physical_metadata"])

    result = RunResult(
        task=task.name, hazard=task.hazard, condition=condition,
        model=client.model, replicate=replicate, outcome="error", silent_error=False,
    )
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": task.prompt}]
    final_text = ""

    with session() as ledger:
        try:
            for _ in range(max_turns):
                response = client.complete(messages, schema)
                usage = response.get("usage") or {}
                result.prompt_tokens += usage.get("prompt_tokens", 0)
                result.completion_tokens += usage.get("completion_tokens", 0)
                result.turns += 1

                choice = response["choices"][0]
                message = choice["message"]
                messages.append({
                    "role": "assistant",
                    "content": message.get("content") or "",
                    **({"tool_calls": message["tool_calls"]} if message.get("tool_calls") else {}),
                })

                calls = message.get("tool_calls") or []
                if not calls:
                    final_text = message.get("content") or ""
                    break

                blocked = False
                for call in calls:
                    result.tool_calls += 1
                    name = call["function"]["name"]
                    try:
                        args = json.loads(call["function"]["arguments"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    tool = by_name.get(name)
                    if tool is None:
                        payload = {"isError": True,
                                   "content": [{"type": "text", "text": f"no tool named {name}"}]}
                    else:
                        payload = tool.invoke(args)
                    result.calls_log.append({"tool": name, "args": args})

                    if payload.get("isError") and payload.get("code"):
                        result.violations += 1
                        if not config["repair"]:
                            blocked = True

                    messages.append({
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(payload.get("result", payload.get("content"))),
                    })
                if blocked:
                    result.outcome = "blocked"
                    result.detail = "guard rejected a call and repair was disabled"
                    return result
        except Exception as exc:  # network or protocol failure, not a task outcome
            result.outcome = "error"
            result.detail = f"{type(exc).__name__}: {exc}"[:300]
            return result

        audit = ledger.audit_answer(final_text)

    result.audit_unsourced = len(audit.unsourced)
    result.audit_mislabelled = len(audit.mislabelled)
    result.audit_derived = len(audit.derived)
    result.audit_quoted = len(audit.quoted)
    result.answer_text = final_text[:2000]
    result.stated = _stated(final_text)
    grade, detail = score(task, final_text, audit)
    result.outcome = grade
    result.detail = detail
    # A silent error is a wrong *number* delivered with no signal. Declining to answer
    # is a visible failure and is deliberately excluded.
    wrong_number = (
        grade == "wrong" and bool(result.stated)
        and "unavailable" not in result.stated.lower()
    )
    # Enforcement alone, with the answer audit not counted as a detector.
    result.undetected = wrong_number and result.violations == 0
    result.silent_error = (
        grade == "wrong"
        and result.violations == 0
        and result.audit_unsourced == 0
        and result.audit_mislabelled == 0
    )
    return result


def _stated(text: str) -> str:
    match = None
    for match in _ANSWER.finditer(text):
        pass
    return match.group("body").strip() if match else ""


def _matches_at_stated_precision(stated: str, magnitude: float, reference: float) -> bool:
    digits = re.sub(r"[^0-9]", "", re.split(r"[eE]", stated.split()[0])[0].lstrip("-0."))
    figures = len(digits.rstrip("0")) or 1
    if figures > 6:
        return False
    from decimal import Decimal
    quantise = lambda v: float(f"%.{figures}g" % v)
    return quantise(reference) == quantise(magnitude)


def score(task: Task, text: str, audit) -> tuple[str, str]:
    """Grade a final answer against the task's known result."""
    stated = _stated(text)
    if not stated:
        return "wrong", "no ANSWER line"

    if task.expects_refusal:
        if "unavailable" in stated.lower():
            return "correct", "correctly reported the quantity as unavailable"
        if audit.unsourced or audit.mislabelled:
            return "wrong", f"stated an unsupported quantity: {stated}"
        return "wrong", f"expected unavailable, got {stated}"

    if "unavailable" in stated.lower():
        return "wrong", "reported unavailable for an answerable question"

    try:
        value = ureg.Quantity(stated.replace(",", ""))
        if value.dimensionless:
            raise ValueError("no unit")
        magnitude = value.to(task.answer.units).magnitude
    except Exception:
        return "wrong", f"could not read a quantity from {stated!r}"

    reference = task.answer.magnitude
    if abs(magnitude - reference) <= task.tolerance * abs(reference):
        return "correct", ""
    # A value rounded to the precision the model actually reported is correct at that
    # precision; grading it wrong would measure significant figures, not physics.
    if _matches_at_stated_precision(stated, magnitude, reference):
        return "correct", "correct at the stated precision"
    ratio = magnitude / reference if reference else float("inf")
    return "wrong", f"stated {stated} against {task.answer:~} (ratio {ratio:.3g})"
