"""The sandbox's payments system backed by Stripe test mode.

The in-memory fake proves CommitLayer's verdicts against a payments system that does exactly what the sandbox
tells it. Backing the same system with Stripe answers a different question: do the verdicts hold when the
payment objects, their ids, amounts, statuses and idempotency semantics are a real provider's? Only the
*timing* stays synthetic: every event is stamped from the virtual clock, because the settlement window the
verifier reasons about must close deterministically and a scenario is placed 20 minutes in the past on purpose.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException

from sandbox.systems import Payments, VirtualClock

TEST_KEY_PREFIX = "sk_test_"


def require_test_key(api_key: str) -> None:
    """A live key would move real money; the sandbox never gets to find out whether it would have."""
    if not isinstance(api_key, str) or not api_key.startswith(TEST_KEY_PREFIX):
        raise ValueError(
            "the payments backend needs a Stripe *test* secret key (starting with 'sk_test_'); "
            "live keys are refused"
        )


def _cents(amount: float) -> int:
    return round(float(amount) * 100)


def _major(cents: int) -> float:
    return round(int(cents) / 100, 2)


class StripePayments(Payments):
    """Charges and refunds live in Stripe; the local tables hold the event log the extractor and the oracle
    read, with Stripe's ids and amounts in it. The local log is a cache of Stripe's facts, never the source of
    them: `events()` and `count()` reconcile with Stripe first, so a refund made without going through the
    sandbox endpoint is still reported, exactly as a connector pulling the provider would see it."""

    def __init__(
        self,
        name: str,
        path: Path,
        clock: VirtualClock,
        api_key: str,
        http_client: httpx.Client | None = None,
        base_url: str = "https://api.stripe.com",
    ):
        require_test_key(api_key)
        super().__init__(name, path, clock)
        # the key lives only inside the auth object httpx signs requests with: nothing else on this object
        # names it, so a repr, a trace or a results file cannot carry it
        self._auth = httpx.BasicAuth(api_key, "")
        self._client = http_client or httpx.Client(base_url=base_url, timeout=30.0)

    # --- Stripe calls ---
    def _call(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        """Stripe's error message is the only part of a failure worth passing on; the request is not, because it
        carries the auth header. A 4xx is Stripe refusing the operation (the sandbox endpoint answers the same
        status, as a real payments API would); anything else is the provider being unavailable."""
        try:
            r = self._client.request(method, path, auth=self._auth, **kw)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"stripe unavailable: {type(e).__name__}") from e
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code >= 400:
            msg = (body.get("error") or {}).get("message") or r.reason_phrase or "error"
            raise HTTPException(r.status_code if r.status_code < 500 else 502, f"stripe: {msg}")
        return body

    def _post(self, path: str, data: dict[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        return self._call("POST", path, data=data, headers=headers)

    # --- the payments interface ---
    def seed_charge(self, order_id: str, amount: float) -> None:
        """A confirmed PaymentIntent on Stripe's test card is the captured charge the refund scenarios act on.
        Redirect-based methods are excluded so confirmation completes in this one call."""
        pi = self._post(
            "/v1/payment_intents",
            {
                "amount": _cents(amount),
                "currency": "usd",
                "payment_method": "pm_card_visa",
                "confirm": "true",
                "automatic_payment_methods[enabled]": "true",
                "automatic_payment_methods[allow_redirects]": "never",
                "metadata[order_id]": order_id,
            },
        )
        if pi.get("status") != "succeeded":
            raise HTTPException(502, f"stripe: payment intent for {order_id} is {pi.get('status')!r}")
        self.put(
            "charge",
            order_id,
            {"order_id": order_id, "amount": amount, "captured": True, "payment_intent": pi["id"]},
        )
        self.emit(order_id, "charge_captured", {"amount": amount})

    def create_refund(self, order_id: str, amount: float, idempotency_key: str | None) -> dict[str, Any]:
        """The caller's idempotency key goes to Stripe, which is the one deciding whether this is a replay;
        the local log only records the outcome. A refund id already in the log is therefore a replay however
        it got there, and is answered without a second event."""
        ch = self.get("charge", order_id) or {}
        refund = self._post(
            "/v1/refunds",
            {"payment_intent": ch["payment_intent"], "amount": _cents(amount)},
            idempotency_key,
        )
        rid = refund["id"]
        if self.get("refund", rid) is not None:
            return {"ok": True, "refund_id": rid, "idempotent_replay": True}
        self._record_refund(order_id, refund, idempotency_key)
        return {"ok": True, "refund_id": rid, "amount": _major(refund["amount"])}

    def _record_refund(self, order_id: str, refund: dict[str, Any], idempotency_key: str | None) -> None:
        rid = refund["id"]
        self.put(
            "refund",
            rid,
            {
                "order_id": order_id,
                "amount": _major(refund["amount"]),
                "refund_id": rid,
                "payment_intent": refund.get("payment_intent"),
                "status": refund.get("status"),
            },
        )
        self.emit(
            order_id,
            "refund_created",
            {
                "amount": _major(refund["amount"]),
                "currency": str(refund.get("currency", "usd")).upper(),
                "refund_id": rid,
                "status": refund.get("status"),
            },
            idempotency_key,
        )

    # --- reconciliation ---
    def _intents(self) -> dict[str, str]:
        """payment intent -> order id, from the seeded charges (read back from the table rather than kept in
        memory so a system reopened on an existing database still knows what to reconcile)."""
        rows = self.db.execute("SELECT data FROM state WHERE kind = 'charge'").fetchall()
        out = {}
        for (data,) in rows:
            ch = json.loads(data)
            if ch.get("payment_intent"):
                out[ch["payment_intent"]] = ch["order_id"]
        return out

    def reconcile(self) -> int:
        """Append a `refund_created` event for every refund Stripe holds that the log does not. Refunds are
        keyed by Stripe's id, so a refund is appended once and its sequence number never moves afterwards:
        that is what keeps a stale-read snapshot (events up to a given seq) meaningful. Returns how many
        refunds were new."""
        new = 0
        for pi, order_id in self._intents().items():
            listing = self._call("GET", "/v1/refunds", params={"payment_intent": pi, "limit": 100})
            refunds = sorted(listing.get("data", []), key=lambda r: (r.get("created", 0), r["id"]))
            for refund in refunds:
                if self.get("refund", refund["id"]) is None:
                    self._record_refund(order_id, refund, None)
                    new += 1
        return new

    def events(self, since_seq: int = 0, until_seq: int | None = None) -> list[dict[str, Any]]:
        self.reconcile()
        return super().events(since_seq, until_seq)

    def count(self, subject_id: str, type_: str) -> int:
        if type_ == "refund_created":
            self.reconcile()
        return super().count(subject_id, type_)
