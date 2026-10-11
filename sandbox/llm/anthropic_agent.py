"""Claude tool-calling loop for the sandbox (Anthropic SDK, manual loop so every call is traced)."""

from __future__ import annotations

import os
from typing import Any

import anthropic

from sandbox.llm.tools import SYSTEM_PROMPT, TOOLS, Executor, Trace, json_schema

DEFAULT_MODEL = "claude-opus-5-5"


def _tools() -> list[dict[str, Any]]:
    return [
        {"name": t["name"], "description": t["description"], "input_schema": json_schema(t), "strict": True}
        for t in TOOLS
    ]


def run(task: str, executor: Executor, trace: Trace, model: str | None = None, max_turns: int = 14) -> Trace:
    client = anthropic.Anthropic(api_key=os.environ.get("LLM_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"))
    model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
    tools = _tools()
    for _ in range(max_turns):
        trace.turns += 1
        try:
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=messages,
            )
        except anthropic.RateLimitError as e:
            trace.notes.append(f"rate limited: {e.message}")
            raise
        except anthropic.APIStatusError as e:
            trace.notes.append(f"api error {e.status_code}: {e.message}")
            raise
        if response.stop_reason == "refusal":
            trace.notes.append("model refused")
            break
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        messages.append({"role": "assistant", "content": response.content})
        if not tool_uses:
            text = " ".join(b.text for b in response.content if b.type == "text")
            if text:
                trace.notes.append(f"final text: {text[:200]}")
            break
        results = []
        for tu in tool_uses:
            out, is_err = executor(tu.name, dict(tu.input))
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out, "is_error": is_err})
        messages.append({"role": "user", "content": results})
        if trace.final is not None:
            break
    else:
        trace.notes.append("max turns reached")
    return trace
