"""Gemini tool-calling loop for the sandbox (google-genai function calling, manual loop)."""

from __future__ import annotations

import os
import time

from google import genai
from google.genai import types

from sandbox.llm.tools import SYSTEM_PROMPT, TOOLS, Executor, Trace, json_schema

DEFAULT_MODEL = "gemini-2.5-flash"


def _tools() -> list[types.Tool]:
    decls = [
        types.FunctionDeclaration(
            name=t["name"], description=t["description"], parameters=_params(json_schema(t))
        )
        for t in TOOLS
    ]
    return [types.Tool(function_declarations=decls)]


def _params(schema: dict) -> dict:
    # Gemini's schema dialect: drop additionalProperties, uppercase types are accepted as lowercase too.
    return {"type": "object", "properties": schema["properties"], "required": schema["required"]}


_resolved: dict[str, str] = {}


def _rank(name: str) -> tuple:
    # newest first, plain "flash" before lite/preview/8b variants, then everything else
    penalty = sum(k in name for k in ("lite", "preview", "8b", "exp"))
    return (
        0 if "flash" in name else 1,
        penalty,
        tuple(-int(x) for x in __import__("re").findall(r"\d+", name)),
    )


def resolve_model(client: genai.Client, requested: str | None) -> str:
    """The requested model when the account can actually generate with it, otherwise the first candidate that
    answers a one-token probe. Listing alone is not enough: retired models stay listed for existing users."""
    key = requested or "auto"
    if key in _resolved:
        return _resolved[key]
    names: list[str] = []
    for m in client.models.list():
        actions = getattr(m, "supported_actions", None) or []
        if actions and "generateContent" not in actions:
            continue
        n = (m.name or "").replace("models/", "")
        if n and not any(k in n for k in ("embedding", "image", "tts", "audio", "live", "veo", "imagen")):
            names.append(n)
    candidates = ([requested] if requested else []) + sorted(set(names) - {requested}, key=_rank)
    errors: list[str] = []
    for cand in candidates:
        try:
            client.models.generate_content(
                model=cand, contents="ping", config=types.GenerateContentConfig(max_output_tokens=5)
            )
            _resolved[key] = cand
            return cand
        except Exception as e:  # retired / unavailable for this account: try the next
            errors.append(f"{cand}: {str(e)[:60]}")
            continue
    raise RuntimeError("no usable Gemini model: " + " | ".join(errors[:5]))


def generate_with_backoff(client: genai.Client, attempts: int = 6, **kw):
    """Free-tier keys hit 429 (quota) and busy models return 503; wait and retry instead of failing the scenario."""
    from google.genai import errors

    delay = 15.0
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return client.models.generate_content(**kw)
        except errors.APIError as e:
            if getattr(e, "code", None) not in (429, 503):
                raise
            last = e
            time.sleep(delay)
            delay = min(delay * 2, 90.0)
    assert last is not None
    raise last


def run(task: str, executor: Executor, trace: Trace, model: str | None = None, max_turns: int = 14) -> Trace:
    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    model = resolve_model(client, model or os.environ.get("GOOGLE_MODEL") or DEFAULT_MODEL)
    trace.notes.append(f"model: {model}")
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=_tools(),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    contents: list[types.Content] = [types.Content(role="user", parts=[types.Part.from_text(text=task)])]
    for _ in range(max_turns):
        trace.turns += 1
        resp = generate_with_backoff(client, model=model, contents=contents, config=config)
        cand = resp.candidates[0] if resp.candidates else None
        if cand is None or cand.content is None:
            trace.notes.append("empty response")
            break
        contents.append(cand.content)
        calls = [p.function_call for p in (cand.content.parts or []) if p.function_call]
        if not calls:
            text = "".join(p.text or "" for p in (cand.content.parts or []))
            if text:
                trace.notes.append(f"final text: {text[:200]}")
            break
        parts = []
        for fc in calls:
            out, is_err = executor(fc.name, dict(fc.args or {}))
            parts.append(
                types.Part.from_function_response(name=fc.name, response={"result": out, "error": is_err})
            )
        contents.append(types.Content(role="user", parts=parts))
        if trace.final is not None:
            break
    else:
        trace.notes.append("max turns reached")
    return trace
