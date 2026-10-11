"""The artificial enterprise: five systems of record, each with its own SQLite database and HTTP API.

payments  — charges, refunds and payouts           (subject: order_id / account_id)
orders    — orders, status, shipping address       (subject: order_id)
crm       — customers, subscriptions, notes        (subject: order_id / subscription_id / ticket_id / account_id)
tickets   — support cases and customer notifications
ledger    — accounts, their status and balance     (subject: account_id)
platform  — services, deploys and health checks    (subject: service_id)

Every write appends an event row (subject_id, type, attributes, occurred_at) that the extractor turns into
CommitLayer facts. Timestamps come from a virtual clock so a whole scenario can be placed in the past and the
settlement window can be closed deterministically.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request

FMT = "%Y-%m-%d %H:%M:%S"


class VirtualClock:
    def __init__(self, start: datetime):
        self.t = start

    def now(self) -> str:
        return self.t.strftime(FMT)

    def tick(self, seconds: float = 1.0) -> str:
        self.t += timedelta(seconds=seconds)
        return self.now()


class System:
    """One system of record. Tables: state (kind, id, json) and events (append-only)."""

    def __init__(self, name: str, path: Path, clock: VirtualClock):
        self.name, self.clock = name, clock
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS state (kind TEXT, id TEXT, data TEXT, PRIMARY KEY (kind, id))"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, subject_id TEXT, type TEXT, "
            "attributes TEXT, occurred_at TEXT, idempotency_key TEXT)"
        )
        self.db.commit()

    # --- state ---
    def get(self, kind: str, id_: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT data FROM state WHERE kind = ? AND id = ?", (kind, id_)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, kind: str, id_: str, data: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO state (kind, id, data) VALUES (?, ?, ?) ON CONFLICT(kind, id) DO UPDATE SET data = excluded.data",
            (kind, id_, json.dumps(data)),
        )
        self.db.commit()

    # --- events ---
    def emit(
        self, subject_id: str, type_: str, attributes: dict[str, Any], idem: str | None = None
    ) -> dict[str, Any]:
        at = self.clock.tick(1)
        self.db.execute(
            "INSERT INTO events (subject_id, type, attributes, occurred_at, idempotency_key) VALUES (?, ?, ?, ?, ?)",
            (subject_id, type_, json.dumps(attributes), at, idem),
        )
        self.db.commit()
        return {"subject_id": subject_id, "type": type_, "attributes": attributes, "occurred_at": at}

    def seen_idempotency(self, key: str | None) -> bool:
        if not key:
            return False
        return (
            self.db.execute("SELECT 1 FROM events WHERE idempotency_key = ?", (key,)).fetchone() is not None
        )

    def events(self, since_seq: int = 0, until_seq: int | None = None) -> list[dict[str, Any]]:
        q = "SELECT seq, subject_id, type, attributes, occurred_at FROM events WHERE seq > ?"
        args: list[Any] = [since_seq]
        if until_seq is not None:
            q += " AND seq <= ?"
            args.append(until_seq)
        return [
            {
                "seq": r[0],
                "subject_id": r[1],
                "type": r[2],
                "attributes": json.loads(r[3]),
                "occurred_at": r[4],
            }
            for r in self.db.execute(q + " ORDER BY seq", args).fetchall()
        ]

    def count(self, subject_id: str, type_: str) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM events WHERE subject_id = ? AND type = ?", (subject_id, type_)
        ).fetchone()[0]

    def last_seq(self) -> int:
        return self.db.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]


class Payments(System):
    """The in-memory payments system. Charges and refunds are the two operations a payment provider owns, so
    they live behind methods a provider-backed subclass (`sandbox.stripe_payments.StripePayments`) can replace
    while the HTTP endpoint, the extractor and the oracle keep talking to one interface. Payouts and billing
    stops are not part of that interface: they stay on the generic state/event tables whichever backend runs."""

    def seed_charge(self, order_id: str, amount: float) -> None:
        self.put("charge", order_id, {"order_id": order_id, "amount": amount, "captured": True})
        self.emit(order_id, "charge_captured", {"amount": amount})

    def create_refund(self, order_id: str, amount: float, idempotency_key: str | None) -> dict[str, Any]:
        """The response shape is what the agent's tool sees; a replayed key answers without a second effect."""
        if self.seen_idempotency(idempotency_key):
            return {"ok": True, "refund_id": f"re_{order_id}", "idempotent_replay": True}
        self.put(
            "refund",
            f"re_{order_id}_{self.count(order_id, 'refund_created') + 1}",
            {"order_id": order_id, "amount": amount},
        )
        self.emit(order_id, "refund_created", {"amount": amount, "currency": "USD"}, idempotency_key)
        return {"ok": True, "refund_id": f"re_{order_id}", "amount": amount}


class Enterprise:
    """All four systems plus seed data. `chaos` is consulted by the HTTP layer only. `payments` lets a scenario
    run against a provider-backed payments system; the default is the in-memory fake."""

    def __init__(self, root: Path, clock: VirtualClock, payments: Payments | None = None):
        root.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.payments = payments or Payments("payments", root / "payments.db", clock)
        self.orders = System("orders", root / "orders.db", clock)
        self.crm = System("crm", root / "crm.db", clock)
        self.tickets = System("tickets", root / "tickets.db", clock)
        self.ledger = System("ledger", root / "ledger.db", clock)
        self.platform = System("platform", root / "platform.db", clock)
        self.systems = {
            s.name: s
            for s in (self.payments, self.orders, self.crm, self.tickets, self.ledger, self.platform)
        }

    def seed(self) -> None:
        """Known customers, orders, balances, subscriptions and tickets. Emits the *state* events the
        preconditions read (order_status, subscription_status, ticket_status)."""
        customers = [
            ("cust_1", "ada@example.com"),
            ("cust_2", "grace@example.com"),
            ("cust_3", "linus@example.com"),
        ]
        for cid, email in customers:
            self.crm.put("customer", cid, {"id": cid, "email": email, "tier": "standard"})
        orders = [
            ("order_1001", "cust_1", 180.0, "paid", "12 Rue A, Paris"),
            ("order_1002", "cust_2", 420.0, "paid", "5 Main St, Austin"),
            ("order_1003", "cust_3", 95.5, "shipped", "9 Bay St, Toronto"),
            ("order_1004", "cust_1", 60.0, "pending_payment", "12 Rue A, Paris"),
            ("order_1005", "cust_2", 230.0, "paid", "5 Main St, Austin"),
        ]
        for oid, cid, total, status, addr in orders:
            self.orders.put(
                "order",
                oid,
                {"id": oid, "customer_id": cid, "total": total, "status": status, "address": addr},
            )
            self.orders.emit(oid, "order_status", {"status": status, "total": total})
            if status in ("paid", "shipped"):
                self.payments.seed_charge(oid, total)
        for sid, cid, plan in (("sub_1", "cust_1", "pro"), ("sub_2", "cust_2", "team")):
            self.crm.put(
                "subscription", sid, {"id": sid, "customer_id": cid, "plan": plan, "status": "active"}
            )
            self.crm.emit(sid, "subscription_status", {"status": "active", "plan": plan})
        for tid, cid, oid in (
            ("tick_1", "cust_1", "order_1001"),
            ("tick_2", "cust_2", "order_1002"),
            ("tick_3", "cust_3", "order_1003"),
        ):
            self.tickets.put(
                "ticket",
                tid,
                {
                    "id": tid,
                    "customer_id": cid,
                    "order_id": oid,
                    "status": "open",
                    "body": "Please help with my order.",
                },
            )
            self.tickets.emit(tid, "ticket_status", {"status": "open", "order_id": oid})
        # payout accounts: acc_3 is frozen; acc_4..acc_6 exist for a batch of payouts in one day
        for aid, cid, balance, status in (
            ("acc_1", "cust_1", 10000.0, "active"),
            ("acc_2", "cust_2", 3000.0, "active"),
            ("acc_3", "cust_3", 5000.0, "frozen"),
            ("acc_4", "cust_1", 8000.0, "active"),
            ("acc_5", "cust_2", 8000.0, "active"),
            ("acc_6", "cust_3", 8000.0, "active"),
        ):
            self.ledger.put(
                "account", aid, {"id": aid, "customer_id": cid, "balance": balance, "status": status}
            )
            self.ledger.emit(aid, "account_status", {"status": status})
            self.ledger.emit(aid, "account_balance", {"balance": balance})
        # services: svc_batch is degraded; svc_a..svc_c exist for a burst of deploys in one hour
        for sid, version, health in (
            ("svc_api", "1.4.0", "healthy"),
            ("svc_web", "2.1.3", "healthy"),
            ("svc_batch", "0.9.1", "degraded"),
            ("svc_a", "1.0.0", "healthy"),
            ("svc_b", "1.0.0", "healthy"),
            ("svc_c", "1.0.0", "healthy"),
        ):
            self.platform.put("service", sid, {"id": sid, "version": version, "health": health})
            self.platform.emit(sid, "service_health", {"status": health, "version": version})


# ---------------- HTTP API (what the agent's tools call) ----------------


def build_app(ent: Enterprise) -> FastAPI:
    app = FastAPI(title="sandbox enterprise")

    def _idem(req: Request) -> str | None:
        return req.headers.get("Idempotency-Key")

    # what a read-only connector would pull (declared first: it must win over /orders/{oid} etc.)
    @app.get("/{system}/events")
    async def events(system: str, since: int = 0):
        if system not in ent.systems:
            raise HTTPException(404, "unknown system")
        return {"events": ent.systems[system].events(since)}

    # payments
    @app.post("/payments/refunds")
    async def create_refund(req: Request):
        body = await req.json()
        oid, amount = body["order_id"], float(body["amount"])
        if ent.payments.get("charge", oid) is None:
            raise HTTPException(404, "no captured charge for order")
        return ent.payments.create_refund(oid, amount, _idem(req))

    def _replay(system: System, req: Request) -> bool:
        return system.seen_idempotency(_idem(req))

    @app.post("/payments/payouts")
    async def create_payout(req: Request):
        body = await req.json()
        aid, amount = body["account_id"], float(body["amount"])
        if ent.ledger.get("account", aid) is None:
            raise HTTPException(404, "account not found")
        key = _idem(req)
        if ent.payments.seen_idempotency(key):
            return {"ok": True, "payout_id": f"po_{aid}", "idempotent_replay": True}
        n = ent.payments.count(aid, "payout_created") + 1
        ent.payments.put("payout", f"po_{aid}_{n}", {"account_id": aid, "amount": amount})
        ent.payments.emit(
            aid, "payout_created", {"amount": amount, "currency": "USD", "payout_id": f"po_{aid}_{n}"}, key
        )
        return {"ok": True, "payout_id": f"po_{aid}_{n}", "amount": amount}

    # platform
    @app.get("/platform/services/{sid}")
    async def get_service(sid: str):
        svc = ent.platform.get("service", sid)
        if svc is None:
            raise HTTPException(404, "service not found")
        return svc

    @app.post("/platform/services/{sid}/deploy")
    async def deploy_service(sid: str, req: Request):
        if _replay(ent.platform, req):
            return {"ok": True, "idempotent_replay": True}
        body = await req.json()
        svc = ent.platform.get("service", sid)
        if svc is None:
            raise HTTPException(404, "service not found")
        svc["version"] = body["version"]
        ent.platform.put("service", sid, svc)
        ent.platform.emit(
            sid,
            "deploy_created",
            {"version": body["version"], "environment": body.get("environment", "production")},
            _idem(req),
        )
        return {"ok": True, "version": svc["version"]}

    @app.post("/platform/services/{sid}/healthcheck")
    async def health_check(sid: str, req: Request):
        """Runs the service's health check and records the result (whatever the service currently reports)."""
        svc = ent.platform.get("service", sid)
        if svc is None:
            raise HTTPException(404, "service not found")
        status = "healthy" if svc.get("health") == "healthy" else "unhealthy"
        ent.platform.emit(sid, "health_check", {"status": status, "version": svc.get("version")}, _idem(req))
        return {"ok": True, "status": status}

    # ledger
    @app.get("/ledger/accounts/{aid}")
    async def get_account(aid: str):
        a = ent.ledger.get("account", aid)
        if a is None:
            raise HTTPException(404, "account not found")
        return a

    @app.post("/ledger/accounts/{aid}/post")
    async def post_entry(aid: str, req: Request):
        """Post a debit (positive amount) or credit (negative) and record the new balance."""
        if _replay(ent.ledger, req):
            return {"ok": True, "idempotent_replay": True}
        body = await req.json()
        a = ent.ledger.get("account", aid)
        if a is None:
            raise HTTPException(404, "account not found")
        a["balance"] = round(float(a["balance"]) - float(body["amount"]), 2)
        ent.ledger.put("account", aid, a)
        ent.ledger.emit(
            aid, "ledger_posted", {"amount": float(body["amount"]), "balance": a["balance"]}, _idem(req)
        )
        return {"ok": True, "balance": a["balance"]}

    @app.post("/payments/billing/stop")
    async def stop_billing(req: Request):
        body = await req.json()
        sid = body["subscription_id"]
        if _replay(ent.payments, req):
            return {"ok": True, "idempotent_replay": True}
        ent.payments.emit(sid, "billing_stopped", {}, _idem(req))
        return {"ok": True}

    # orders
    @app.get("/orders/{oid}")
    async def get_order(oid: str):
        o = ent.orders.get("order", oid)
        if o is None:
            raise HTTPException(404, "order not found")
        return o

    @app.post("/orders/{oid}/status")
    async def set_status(oid: str, req: Request):
        if _replay(ent.orders, req):
            return {"ok": True, "idempotent_replay": True}
        body = await req.json()
        o = ent.orders.get("order", oid)
        if o is None:
            raise HTTPException(404, "order not found")
        o["status"] = body["status"]
        ent.orders.put("order", oid, o)
        ent.orders.emit(oid, "order_status_changed", {"status": body["status"]}, _idem(req))
        return {"ok": True, "status": o["status"]}

    @app.post("/orders/{oid}/address")
    async def set_address(oid: str, req: Request):
        if _replay(ent.orders, req):
            return {"ok": True, "idempotent_replay": True}
        body = await req.json()
        o = ent.orders.get("order", oid)
        if o is None:
            raise HTTPException(404, "order not found")
        o["address"] = body["address"]
        ent.orders.put("order", oid, o)
        ent.orders.emit(oid, "address_updated", {"address": body["address"]}, _idem(req))
        return {"ok": True}

    @app.post("/orders/{oid}/discount")
    async def apply_discount(oid: str, req: Request):
        if _replay(ent.orders, req):
            return {"ok": True, "idempotent_replay": True}
        body = await req.json()
        o = ent.orders.get("order", oid)
        if o is None:
            raise HTTPException(404, "order not found")
        pct = float(body["percent"])
        o["discount_percent"] = pct
        ent.orders.put("order", oid, o)
        ent.orders.emit(oid, "discount_applied", {"percent": pct}, _idem(req))
        return {"ok": True}

    # crm
    @app.post("/crm/notes")
    async def add_note(req: Request):
        if _replay(ent.crm, req):
            return {"ok": True, "idempotent_replay": True}
        body = await req.json()
        ent.crm.emit(
            body["subject_id"],
            "note_added",
            {"author": body.get("author", "agent"), "text": body.get("text", "")[:80]},
            _idem(req),
        )
        return {"ok": True}

    @app.get("/crm/subscriptions/{sid}")
    async def get_sub(sid: str):
        s = ent.crm.get("subscription", sid)
        if s is None:
            raise HTTPException(404, "subscription not found")
        return s

    @app.post("/crm/subscriptions/{sid}/cancel")
    async def cancel_sub(sid: str, req: Request):
        if _replay(ent.crm, req):
            return {"ok": True, "idempotent_replay": True}
        s = ent.crm.get("subscription", sid)
        if s is None:
            raise HTTPException(404, "subscription not found")
        s["status"] = "cancelled"
        ent.crm.put("subscription", sid, s)
        ent.crm.emit(sid, "subscription_cancelled", {"plan": s["plan"]}, _idem(req))
        return {"ok": True}

    # tickets
    @app.get("/tickets/{tid}")
    async def get_ticket(tid: str):
        t = ent.tickets.get("ticket", tid)
        if t is None:
            raise HTTPException(404, "ticket not found")
        return t

    @app.post("/tickets/{tid}/close")
    async def close_ticket(tid: str, req: Request):
        if _replay(ent.tickets, req):
            return {"ok": True, "idempotent_replay": True}
        t = ent.tickets.get("ticket", tid)
        if t is None:
            raise HTTPException(404, "ticket not found")
        t["status"] = "closed"
        ent.tickets.put("ticket", tid, t)
        ent.tickets.emit(tid, "ticket_closed", {"order_id": t.get("order_id")}, _idem(req))
        return {"ok": True}

    @app.post("/tickets/notify")
    async def notify(req: Request):
        if _replay(ent.tickets, req):
            return {"ok": True, "idempotent_replay": True}
        body = await req.json()
        attrs: dict[str, Any] = {"channel": body.get("channel", "email")}
        t = ent.tickets.get("ticket", body["subject_id"])
        if t is not None:  # a notification sent on the ticket is about the ticket's order
            attrs["order_id"] = t.get("order_id")
        ent.tickets.emit(body["subject_id"], "customer_notified", attrs, _idem(req))
        return {"ok": True}

    return app


def start_clock(minutes_ago: int = 20) -> VirtualClock:
    return VirtualClock(datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=minutes_ago))
