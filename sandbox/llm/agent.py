"""LLMAgent: the same workflow surface as the scripted Agent, driven by a model through a provider adapter."""

from __future__ import annotations

from typing import Any

import httpx

from sandbox.agent import Report
from sandbox.llm.tools import TASKS, Executor, Trace

PROVIDERS = ("anthropic", "openai", "google", "fake")


def _runner(provider: str):
    if provider == "anthropic":
        from sandbox.llm.anthropic_agent import run
    elif provider == "openai":
        from sandbox.llm.openai_agent import run
    elif provider == "google":
        from sandbox.llm.google_agent import run
    elif provider == "fake":
        from sandbox.llm.fake_agent import run
    else:
        raise ValueError(f"unknown provider {provider}")
    return run


class LLMAgent:
    def __init__(
        self, client: httpx.Client, provider: str, model: str | None = None, agent_id: str | None = None
    ):
        self.c, self.provider, self.model = client, provider, model
        self.agent_id = agent_id or f"{provider}-agent"
        self.last_trace: Trace | None = None

    def _run(
        self,
        contract_id: str,
        subject_id: str,
        subject_kind: str,
        action_type: str,
        params: dict[str, Any],
        requested_at: str,
        task: str,
    ) -> Report:
        rep = Report(contract_id, subject_id, subject_kind, action_type, params, requested_at)
        trace = Trace()
        executor = Executor(self.c, trace)
        try:
            self._drive(task, executor, trace)
        except Exception as e:  # provider/API failure: recorded, never hidden
            trace.notes.append(f"provider error: {type(e).__name__}: {str(e)[:160]}")
        self.last_trace = trace
        rep.steps = [
            {"tool": c["tool"], "status": c["status"]} for c in trace.calls if c["tool"] != "report_result"
        ]
        rep.claimed_success = bool(trace.final and trace.final.get("success"))
        if trace.final is None:
            rep.notes.append("agent ended without report_result")
        else:
            rep.notes.append(f"agent said: {trace.final.get('summary', '')[:160]}")
        rep.notes.extend(trace.notes)
        return rep

    def _drive(self, task: str, executor: Executor, trace: Trace) -> None:
        _runner(self.provider)(task, executor, trace, self.model)

    # --- same surface as sandbox.agent.Agent ---
    def refund_customer(
        self, order_id: str, amount: float, ticket_id: str | None, requested_at: str, skip_calls: bool = False
    ) -> Report:
        task = TASKS["sbx_refund_customer"].format(
            ticket_id=ticket_id or "tick_1", order_id=order_id, amount=amount
        )
        return self._run(
            "sbx_refund_customer",
            order_id,
            "order",
            "refund_order",
            {"amount": amount, "currency": "USD"},
            requested_at,
            task,
        )

    def cancel_subscription_wf(self, sid: str, requested_at: str) -> Report:
        return self._run(
            "sbx_cancel_subscription",
            sid,
            "subscription",
            "cancel_subscription",
            {"reason": "customer_request"},
            requested_at,
            TASKS["sbx_cancel_subscription"].format(subscription_id=sid),
        )

    def change_shipping_address(self, order_id: str, address: str, requested_at: str) -> Report:
        return self._run(
            "sbx_change_shipping_address",
            order_id,
            "order",
            "change_address",
            {"address": address},
            requested_at,
            TASKS["sbx_change_shipping_address"].format(order_id=order_id, address=address),
        )

    def approve_discount(self, order_id: str, percent: float, requested_at: str) -> Report:
        return self._run(
            "sbx_approve_discount",
            order_id,
            "order",
            "approve_discount",
            {"percent": percent},
            requested_at,
            TASKS["sbx_approve_discount"].format(order_id=order_id, percent=percent),
        )

    def close_support_case(self, ticket_id: str, requested_at: str) -> Report:
        return self._run(
            "sbx_close_support_case",
            ticket_id,
            "ticket",
            "close_case",
            {"resolution": "answered"},
            requested_at,
            TASKS["sbx_close_support_case"].format(ticket_id=ticket_id),
        )

    def send_payout_wf(
        self, account_id: str, amount: float, requested_at: str, sent_amount: float | None = None
    ) -> Report:
        # a model is asked for the requested amount; the scripted agent's unit error is not reproducible here
        return self._run(
            "sbx_send_payout",
            account_id,
            "account",
            "send_payout",
            {"amount": amount, "currency": "USD"},
            requested_at,
            TASKS["sbx_send_payout"].format(account_id=account_id, amount=amount),
        )

    def deploy_service_wf(
        self, service_id: str, version: str, requested_at: str, environment: str = "production"
    ) -> Report:
        return self._run(
            "sbx_deploy_service",
            service_id,
            "service",
            "deploy_service",
            {"version": version, "environment": environment},
            requested_at,
            TASKS["sbx_deploy_service"].format(
                service_id=service_id, version=version, environment=environment
            ),
        )


class ReplayAgent(LLMAgent):
    """Re-executes the tool calls a model made in a recorded run, through the same executor and chaos rules,
    so a recorded run can be re-verified by a newer engine without calling the model again. The recorded
    verdict of the trace-only judge is carried over by the harness (the trace is the same)."""

    def __init__(self, client: httpx.Client, provider: str, model: str | None = None):
        super().__init__(client, provider, model)
        self.calls: list[dict[str, Any]] = []

    def _drive(self, task: str, executor: Executor, trace: Trace) -> None:
        mismatches = 0
        for c in self.calls:
            executor(c["tool"], dict(c.get("args") or {}))
            if trace.calls and trace.calls[-1]["status"] != c.get("status"):
                mismatches += 1
        note = f"replayed {len(self.calls)} recorded tool calls"
        if mismatches:
            note += f"; {mismatches} responded differently from the recording"
        trace.notes.append(note)
