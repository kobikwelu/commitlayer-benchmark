"""A stand-in 'model' for CI: follows the task's happy path with idempotency keys, stops on the first error,
and reports honestly. Exercises the whole LLM pipeline (tool catalogue, executor, tracing) without a key."""

from __future__ import annotations

import re
import uuid

from sandbox.llm.tools import Executor, Trace


def run(task: str, executor: Executor, trace: Trace, model: str | None = None, max_turns: int = 14) -> Trace:
    def call(name, **args):
        trace.turns += 1
        if name not in ("get_order", "get_ticket", "get_subscription", "report_result"):
            args["idempotency_key"] = str(uuid.uuid4())
        out, err = executor(name, args)
        if err:
            executor("report_result", {"success": False, "summary": f"{name} failed: {out[:80]}"})
            raise RuntimeError(out)
        return out

    try:
        if "refund them" in task:
            ticket = re.search(r"Ticket (tick_\d+)", task).group(1)
            order = re.search(r"order (order_\d+)", task).group(1)
            amount = float(re.search(r"refund them ([0-9.]+)", task).group(1))
            call("get_ticket", ticket_id=ticket)
            call("get_order", order_id=order)
            call("refund", order_id=order, amount=amount)
            call("update_order_status", order_id=order, status="refunded")
            call("add_note", subject_id=order, text=f"Refunded {amount} USD")
            call("notify_customer", subject_id=order)
        elif "cancel subscription" in task:
            sid = re.search(r"subscription (sub_\d+)", task).group(1)
            call("get_subscription", subscription_id=sid)
            call("cancel_subscription", subscription_id=sid)
            call("stop_billing", subscription_id=sid)
            call("notify_customer", subject_id=sid)
        elif "shipping address" in task:
            order = re.search(r"order (order_\d+)", task).group(1)
            address = task.split("to: ", 1)[1].split(". Update", 1)[0]
            call("get_order", order_id=order)
            call("update_address", order_id=order, address=address)
            call("add_note", subject_id=order, text="Address changed")
            call("notify_customer", subject_id=order)
        elif "discount" in task:
            order = re.search(r"order (order_\d+)", task).group(1)
            pct = float(re.search(r"a ([0-9.]+) percent", task).group(1))
            call("get_order", order_id=order)
            call("apply_discount", order_id=order, percent=pct)
            call("add_note", subject_id=order, text=f"Discount {pct}%")
        else:
            ticket = re.search(r"Ticket (tick_\d+)", task).group(1)
            call("get_ticket", ticket_id=ticket)
            call("close_ticket", ticket_id=ticket)
            call("add_note", subject_id=ticket, text="Case closed")
            call("notify_customer", subject_id=ticket)
        executor("report_result", {"success": True, "summary": "completed"})
    except RuntimeError:
        pass
    return trace
