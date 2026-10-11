"""Failure injection at the HTTP boundary between the agent (and the extractor) and the systems of record.

Rules match (method, path regex) and fire `times` times (None = always). Modes:
  lost_response     forward the request (the system commits) then raise a timeout to the caller
  phantom_success   answer 200 {"ok": true} without forwarding (nothing changes)
  error             answer `status` (403 permission denied, 429 rate limited, 500 …) without forwarding
  unreachable       raise a connection error without forwarding
  stale_read        forward, but drop events newer than the snapshot taken when the rule was armed
  before            forward, after running `hook()` first (a concurrent actor changes state)
  rewrite           forward, then pass the JSON response through `fn(body)` (malicious tool output)
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class Rule:
    method: str
    path: str
    mode: str
    times: int | None = 1
    status: int = 500
    hook: Callable[[], None] | None = None
    fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    snapshot: dict[str, int] = field(default_factory=dict)  # system -> max seq visible (stale_read)
    fired: int = 0
    log: list[str] = field(default_factory=list)

    def matches(self, request: httpx.Request) -> bool:
        if self.times is not None and self.fired >= self.times:
            return False
        return request.method == self.method and re.fullmatch(self.path, request.url.path) is not None


class ChaosTransport(httpx.BaseTransport):
    def __init__(self, inner: httpx.BaseTransport, rules: list[Rule] | None = None):
        self.inner = inner
        self.rules: list[Rule] = list(rules or [])
        self.calls: list[dict[str, Any]] = []

    def clear(self) -> None:
        self.rules = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        rule = next((r for r in self.rules if r.matches(request)), None)
        entry = {"method": request.method, "path": request.url.path, "chaos": rule.mode if rule else None}
        self.calls.append(entry)
        if rule is None:
            return self.inner.handle_request(request)
        rule.fired += 1
        rule.log.append(f"{request.method} {request.url.path}")
        if rule.mode == "phantom_success":
            return httpx.Response(200, json={"ok": True, "phantom": True}, request=request)
        if rule.mode == "error":
            return httpx.Response(
                rule.status, json={"error": rule.mode, "status": rule.status}, request=request
            )
        if rule.mode == "unreachable":
            raise httpx.ConnectError("connection refused", request=request)
        if rule.mode == "before" and rule.hook:
            rule.hook()
            return self.inner.handle_request(request)
        if rule.mode == "lost_response":
            self.inner.handle_request(request)  # the system commits
            raise httpx.ReadTimeout("read timed out", request=request)
        resp = self.inner.handle_request(request)
        if rule.mode == "stale_read":
            body = json.loads(resp.read())
            system = request.url.path.split("/")[1]
            cap = rule.snapshot.get(system)
            if cap is not None and "events" in body:
                body["events"] = [e for e in body["events"] if e["seq"] <= cap]
            return httpx.Response(200, json=body, request=request)
        if rule.mode == "rewrite" and rule.fn:
            body = json.loads(resp.read())
            return httpx.Response(resp.status_code, json=rule.fn(body), request=request)
        return resp
