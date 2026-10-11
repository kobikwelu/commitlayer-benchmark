"""OpenAI tool-calling loop for the sandbox (chat completions with function tools)."""

from __future__ import annotations

import json
import os
from typing import Any

from openai import OpenAI

from sandbox.llm.tools import SYSTEM_PROMPT, TOOLS, Executor, Trace, json_schema

DEFAULT_MODEL = "gpt-5-mini"


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": t["name"], "description": t["description"], "parameters": json_schema(t)},
        }
        for t in TOOLS
    ]


def run(task: str, executor: Executor, trace: Trace, model: str | None = None, max_turns: int = 14) -> Trace:
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    model = model or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]
    tools = _tools()
    for _ in range(max_turns):
        trace.turns += 1
        resp = client.chat.completions.create(model=model, messages=messages, tools=tools)
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))
        if not msg.tool_calls:
            if msg.content:
                trace.notes.append(f"final text: {msg.content[:200]}")
            break
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except ValueError:
                args = {}
            out, _ = executor(tc.function.name, args)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})
        if trace.final is not None:
            break
    else:
        trace.notes.append("max turns reached")
    return trace
