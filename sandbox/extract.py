"""Read-only extraction: pull each system's event log over HTTP (through the same chaos layer a real
connector would face) and turn it into CommitLayer facts. Returns the facts, the subject links a connector
can see (a ticket and the order it is about), and the systems that could not be reached, so the harness can
report coverage honestly."""

from __future__ import annotations

from typing import Any

import httpx

SYSTEMS = ("payments", "orders", "crm", "tickets", "ledger", "platform")


def extract_facts(client: httpx.Client) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    facts: list[dict[str, Any]] = []
    links: dict[tuple[str, str], dict[str, Any]] = {}
    unreachable: list[str] = []
    for system in SYSTEMS:
        try:
            r = client.get(f"/{system}/events", params={"since": 0}, timeout=5.0)
            r.raise_for_status()
        except (httpx.HTTPError, ValueError):
            unreachable.append(system)
            continue
        for e in r.json().get("events", []):
            attrs = e.get("attributes") or {}
            facts.append(
                {
                    "source": system,
                    "subject_id": e["subject_id"],
                    "subject_kind": "record",
                    "fact_type": e["type"],
                    "occurred_at": e["occurred_at"],
                    "attributes": attrs,
                    "raw_ref": f"sbx:{system}:{e['seq']}",
                }
            )
            # the tickets system stamps every ticket event with the order the ticket is about
            oid = attrs.get("order_id")
            if system == "tickets" and oid and oid != e["subject_id"]:
                links.setdefault(
                    (e["subject_id"], oid),
                    {
                        "subject_id": e["subject_id"],
                        "source": "tickets",
                        "linked_subject_id": oid,
                        "linked_source": "orders",
                    },
                )
    return facts, list(links.values()), unreachable
