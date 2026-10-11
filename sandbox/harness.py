"""Run the failure matrix: for each scenario build a fresh enterprise, let the agent act under injected
failures, extract facts the way a connector would, verify with CommitLayer, and score the verdict against the
oracle. CommitLayer is measured, not the agent."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from sandbox.agent import Agent
from sandbox.chaos import ChaosTransport
from sandbox.extract import extract_facts
from sandbox.scenarios import Scenario, build_scenarios, oracle
from sandbox.systems import Enterprise, VirtualClock, build_app, start_clock

CONTRACT_DIR = Path(__file__).parent / "contracts"
PAYMENTS_BACKENDS = ("fake", "stripe")
# the workflows whose payments interactions go through the provider: the others write payouts or billing
# stops, which the provider-backed system keeps on its local tables, so running them against Stripe would
# measure nothing new while spending test-mode calls
STRIPE_WORKFLOWS = ("refund",)


def make_enterprise(
    root: Path, clock: VirtualClock, payments: str = "fake", http_client: httpx.Client | None = None
) -> Enterprise:
    """The one place a scenario's enterprise is built, so the choice of payments backend is made once. The
    Stripe key is read here, from the environment, and refused unless it is a test key; `http_client` exists so
    a test can stand a fake Stripe behind the real client code."""
    if payments == "fake":
        return Enterprise(root, clock)
    if payments != "stripe":
        raise ValueError(f"unknown payments backend {payments!r} (choose one of {PAYMENTS_BACKENDS})")
    from sandbox.stripe_payments import StripePayments, require_test_key

    key = os.environ.get("STRIPE_TEST_SECRET_KEY", "")
    if not key:
        raise RuntimeError(
            "payments=stripe needs STRIPE_TEST_SECRET_KEY in the environment (a Stripe test key)"
        )
    require_test_key(key)
    root.mkdir(parents=True, exist_ok=True)
    stripe = StripePayments("payments", root / "payments.db", clock, key, http_client=http_client)
    return Enterprise(root, clock, payments=stripe)


def _configure_env(root: Path) -> None:
    import base64

    os.environ.setdefault("DATABASE_URL", f"sqlite:///{root}/commitlayer.db")
    os.environ.setdefault("OBJECT_STORE_ROOT", str(root / "objects"))
    os.environ.setdefault("SEED_DEMO_ON_START", "false")
    os.environ.setdefault("APP_ENV", "test")
    os.environ.setdefault("MASTER_KEY", base64.b64encode(b"sandbox-master-key-0123456789abc").decode())
    os.environ.setdefault("SESSION_SECRET", "sandbox-session-secret-not-for-production")
    os.environ.setdefault("HMAC_PEPPER", "sandbox-pepper")
    os.environ.setdefault("CHROMIUM_PATH", "/nonexistent")


class TestClientTransport(httpx.BaseTransport):
    """Sync httpx transport over FastAPI's TestClient (so the chaos layer can wrap an in-process app)."""

    def __init__(self, app):
        from fastapi.testclient import TestClient

        self.tc = TestClient(app)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        r = self.tc.request(
            request.method,
            request.url.path + (f"?{request.url.query.decode()}" if request.url.query else ""),
            content=request.read(),
            headers=dict(request.headers),
        )
        return httpx.Response(r.status_code, content=r.content, headers=dict(r.headers), request=request)


class InProcessVerifier:
    """Verifies through the engine directly, inside the API process: what the product's own sandbox endpoint and
    the founder-run benchmark use."""

    def __init__(self, session, tenant):
        self.session, self.tenant = session, tenant

    def scope(self):
        from app.core.db import bind_tenant

        return bind_tenant(self.tenant.id)

    def ingest(self, facts, links) -> None:
        from app.engine.actions import ingest_facts, ingest_links

        ingest_facts(self.session, self.tenant, facts)
        ingest_links(self.session, self.tenant, links)

    def record(self, body: dict[str, Any]):
        from app.engine.actions import record_action

        return record_action(self.session, self.tenant, body)

    def verify(self, claim) -> dict[str, Any]:
        from app.engine.actions import verify_action

        return verify_action(self.session, self.tenant.id, claim, persist=True)

    def orphans(self, contract_id: str) -> dict[str, Any]:
        from app.engine.actions import orphan_effects

        return orphan_effects(self.session, self.tenant.id, contract_id)

    def commit(self) -> None:
        self.session.commit()


class _Recorded:
    """What the API verifier hands back for a recorded action: the two fields the harness reads from a claim."""

    def __init__(self, view: dict[str, Any]):
        self.claim_id = view["id"]
        self.attributes = {"action_id": view["action_id"]}


class ApiVerifier:
    """Verifies through the public API of a CommitLayer workspace, with an API key: how anyone outside the
    company runs the benchmark against the product, on their own agent, with nothing of ours in the loop but
    the verdict. The sandbox contracts are created in the workspace on first use."""

    def __init__(self, base_url: str, api_key: str, client: httpx.Client | None = None):
        self.client = client or httpx.Client(
            base_url=base_url.rstrip("/"), headers={"X-API-Key": api_key}, timeout=60.0
        )
        self._contracts_ready = False

    def _call(self, method: str, path: str, **kw) -> httpx.Response:
        r = self.client.request(method, path, **kw)
        if r.status_code >= 400 and r.status_code != 402:
            detail = r.json().get("error", {}).get("message", r.text) if r.content else r.text
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {detail}")
        return r

    def ensure_contracts(self, texts: dict[str, str]) -> None:
        """Every sandbox contract becomes a version in the workspace and is selected; a second run finds them."""
        if self._contracts_ready:
            return
        items = self._call("GET", "/v1/contracts").json().get("items", [])
        have = {c["id"] for c in items if c.get("owner") == "tenant"}
        for cid, text in texts.items():
            if cid not in have:  # a version is enough: verification resolves it; the product's own sandbox
                self._call("POST", "/v1/contracts", json={"yaml": text})  # contracts count against no limit
        self._contracts_ready = True

    def scope(self):
        from contextlib import nullcontext

        return nullcontext()

    def ingest(self, facts, links) -> None:
        for i in range(0, len(facts), 500):
            self._call(
                "POST", "/v1/facts", json={"facts": facts[i : i + 500], "links": links if i == 0 else []}
            )
        if not facts and links:
            self._call("POST", "/v1/facts", json={"facts": [], "links": links})

    def record(self, body: dict[str, Any]) -> _Recorded:
        return _Recorded(self._call("POST", "/v1/actions", json=body).json()["action"])

    def verify(self, claim: _Recorded) -> dict[str, Any]:
        return self._call(
            "POST", "/v1/actions/verify", json={"action_id": claim.attributes["action_id"]}
        ).json()

    def orphans(self, contract_id: str) -> dict[str, Any]:
        r = self._call("GET", "/v1/actions/orphans", params={"contract_id": contract_id})
        if r.status_code == 402:  # the orphan report is a Growth feature; the benchmark reports it as absent
            return {"items": [], "count": 0, "unavailable": "plan"}
        return r.json()

    def commit(self) -> None:
        return None


def run_scenario(
    session,
    tenant,
    sc: Scenario,
    ent_dir: Path,
    make_agent: Callable[[httpx.Client], Any],
    namespace: str = "",
    action_id: str | None = None,
    payments: str = "fake",
    verifier: Any = None,
) -> dict[str, Any]:
    """Run one scenario end to end inside `tenant`: build a fresh enterprise, let the agent act under the injected
    failures, extract facts the way a connector would, record and verify the action, and score it against the
    oracle. `namespace` prefixes every subject id and raw reference so repeated runs in one tenant never share
    records (the benchmark uses a fresh tenant per scenario and leaves it empty). `payments` picks the payments
    backend (see `make_enterprise`). `verifier` is an `InProcessVerifier` (default, built from `session` and
    `tenant`) or an `ApiVerifier` that goes through a workspace's public API."""
    verifier = verifier or InProcessVerifier(session, tenant)

    if ent_dir.exists():
        shutil.rmtree(ent_dir)
    ent = make_enterprise(ent_dir, start_clock(20), payments)
    ent.seed()
    if sc.ticket_injection:
        t = ent.tickets.get("ticket", "tick_1") or {}
        t["body"] = (t.get("body", "") + " " + sc.ticket_injection).strip()
        ent.tickets.put("ticket", "tick_1", t)
    transport = ChaosTransport(TestClientTransport(build_app(ent)), sc.rules(ent))
    client = httpx.Client(transport=transport, base_url="http://sandbox")
    agent = make_agent(client)
    priors = [mk(agent, ent.clock.tick(60)) for mk in sc.prior_runs]  # earlier today, same agent
    requested_at = ent.clock.tick(1)
    rep = sc.run(agent, requested_at)
    rep.prior = priors
    trace = getattr(agent, "last_trace", None)

    def pull():
        facts, links, unreachable = extract_facts(client)
        for f in facts:
            f["subject_id"] = namespace + f["subject_id"]
            f["raw_ref"] = namespace + f["raw_ref"]
        for ln in links:
            ln["subject_id"] = namespace + ln["subject_id"]
            ln["linked_subject_id"] = namespace + ln["linked_subject_id"]
        verifier.ingest(facts, links)
        return len(facts), unreachable

    def record(r, aid: str):
        return verifier.record(
            {
                "action_id": aid,
                "contract_id": r.contract_id,
                "workflow_id": sc.workflow,
                "agent_id": agent.agent_id,
                "action_type": r.action_type,
                "subject_id": namespace + r.subject_id,
                "subject_kind": r.subject_kind,
                "requested_at": r.requested_at,
                "params": r.params,
                # the product's own sandbox runs are marked and never metered; over the API a run is a
                # customer's and counts like any other action
                "channel": "sandbox" if namespace and isinstance(verifier, InProcessVerifier) else None,
            }
        )

    main_id = action_id or sc.id.replace("/", "_")
    with verifier.scope():
        ingested, unreachable = pull()
        for i, prior in enumerate(priors):  # recorded and verified, never scored: context for the aggregate
            verifier.verify(record(prior, f"{main_id}_prior{i + 1}"))
        claim = record(rep, main_id)
        t0 = time.perf_counter()
        provisional = verifier.verify(claim)
        verify_ms = int((time.perf_counter() - t0) * 1000)
        truth = oracle(ent, rep, unreachable)
        final, corrected = provisional, False
        if sc.recheck:  # settlement-window re-check with the chaos cleared
            transport.clear()
            if sc.after_settlement:
                sc.after_settlement(ent)
            more, unreachable = pull()
            ingested += more
            final = verifier.verify(claim)
            truth = oracle(ent, rep, unreachable)
            corrected = final["lenses"]["policy"]["verdict"] != provisional["lenses"]["policy"]["verdict"]
        orphans = verifier.orphans(rep.contract_id)
        if namespace:  # only this run's records are this run's orphans
            orphans["items"] = [o for o in orphans["items"] if o["subject_id"].startswith(namespace)]
            for o in orphans["items"]:
                o["subject_id"] = o["subject_id"][len(namespace) :]
            orphans["count"] = len(orphans["items"])
        verifier.commit()
    return {
        "report": rep,
        "trace": trace,
        "calls": transport.calls,
        "provisional": provisional,
        "final": final,
        "truth": truth,
        "orphans": orphans,
        "corrected": corrected,
        "verify_ms": verify_ms,
        "unreachable": unreachable,
        "facts_ingested": ingested,
        "action_id": claim.attributes.get("action_id") if claim.attributes else None,
        "claim": claim,
    }


class Matrix:
    def __init__(
        self,
        root: Path | None = None,
        agent: str = "scripted",
        provider: str = "fake",
        model: str | None = None,
        judge: bool = False,
        sinks: bool = False,
        replay: dict[str, Any] | None = None,
        payments: str = "fake",
        api_url: str | None = None,
        api_key: str | None = None,
        api_client: httpx.Client | None = None,
    ):
        self.agent_kind, self.provider, self.model, self.judge = agent, provider, model, judge
        # API mode: every scenario runs against one workspace through its public API, namespaced per run, so
        # the verifier under test is the deployed product and nothing in-process
        self.api = (
            ApiVerifier(api_url or "http://api", api_key or "", client=api_client)
            if (api_url and api_key) or api_client is not None
            else None
        )
        self.run_stamp = time.strftime("%Y%m%d%H%M%S")
        if payments not in PAYMENTS_BACKENDS:
            raise ValueError(f"unknown payments backend {payments!r} (choose one of {PAYMENTS_BACKENDS})")
        self.payments = payments
        # replay: a recorded LLM run (the llm_<provider>.json of an earlier run); its tool calls are re-executed
        # and re-verified, its judge verdicts carried over, no model is called
        self.replay = {r["id"]: r for r in (replay or {}).get("scenarios", [])}
        self.replay_of = (replay or {}).get("run_id")
        self.sinks = None
        if sinks:
            from sandbox.llm.tracing import Sinks

            self.sinks = Sinks()
        self.root = root or Path(tempfile.mkdtemp(prefix="commitlayer-sandbox-"))
        self.contract_texts = {p.stem: p.read_text() for p in sorted(CONTRACT_DIR.glob("*.yaml"))}
        self._n = 0
        self.session = None
        if self.api is None:
            _configure_env(self.root)
            try:
                from app.core.db import init_db, new_session
            except ImportError as e:  # the published harness: the engine is reached through the API only
                raise SystemExit(
                    "in-process verification needs the CommitLayer API repository; run against a workspace "
                    "with --api-url and --api-key (or COMMITLAYER_API_URL / COMMITLAYER_API_KEY)"
                ) from e

            init_db()
            self.session = new_session()

    def _tenant(self):
        if self.api is not None:
            self.api.ensure_contracts(self.contract_texts)
            return None
        from app.contracts.service import save_version, select_contract
        from app.core.db import bind_tenant
        from app.core.tenancy import create_tenant, get_or_create_user

        self._n += 1
        user = get_or_create_user(self.session, f"sandbox-{self._n}@example.com", "Sandbox")
        t = create_tenant(self.session, f"Sandbox {self._n}", user, plan="scale", synthetic=True, trial=False)
        self.session.flush()
        with bind_tenant(t.id):
            for cid, text in self.contract_texts.items():
                save_version(self.session, t.id, text)
                self.session.flush()
                select_contract(self.session, t.id, cid)
        self.session.flush()
        return t

    def run_one(self, sc: Scenario) -> dict[str, Any]:
        recorded = self.replay.get(sc.id)

        def make_agent(client: httpx.Client):
            if recorded is not None:
                from sandbox.llm.agent import ReplayAgent

                agent = ReplayAgent(client, self.provider, self.model)
                agent.calls = recorded.get("agent_calls", [])
                return agent
            if self.agent_kind == "llm":
                from sandbox.llm.agent import LLMAgent

                return LLMAgent(client, self.provider, self.model)
            return Agent(client, sc.behaviour)

        self._n += 1 if self.api is not None else 0
        out = run_scenario(
            self.session,
            self._tenant(),
            sc,
            self.root / "ent" / sc.id.replace("/", "_"),
            make_agent,
            namespace=f"bmk_{self.run_stamp}_{self._n}:" if self.api is not None else "",
            action_id=f"bmk_{self.run_stamp}_{sc.id.replace('/', '_')}" if self.api is not None else None,
            payments=self.payments,
            verifier=self.api,
        )
        rep, trace, provisional, final = out["report"], out["trace"], out["provisional"], out["final"]
        truth, orphans, corrected = out["truth"], out["orphans"], out["corrected"]
        verify_ms, unreachable = out["verify_ms"], out["unreachable"]

        failing = next(
            (
                c
                for k in (
                    "preconditions",
                    "limits",
                    "aggregate_limits",
                    "forbidden_effects",
                    "reversals",
                    "expected_effects",
                )
                for c in final["checks"][k]
                if not c["ok"]
            ),
            None,
        )
        cl_out, cl_pol = final["lenses"]["outcome"]["verdict"], final["lenses"]["policy"]["verdict"]
        row = {
            "id": sc.id,
            "provider": self.provider if self.agent_kind == "llm" else "scripted",
            "workflow": sc.workflow,
            "failure": sc.failure,
            "description": sc.description,
            "agent_claimed_success": rep.claimed_success,
            "agent_steps": len(rep.steps),
            "agent_notes": rep.notes,
            "unreachable": unreachable,
            "truth": truth,
            "commitlayer": {
                "provisional": {
                    "outcome": provisional["lenses"]["outcome"]["verdict"],
                    "policy": provisional["lenses"]["policy"]["verdict"],
                },
                "outcome": cl_out,
                "policy": cl_pol,
                "reason_codes": final["lenses"]["policy"]["reason_codes"],
                "headline": final["summary"]["headline"],
                "failing_check": failing["description"] if failing else None,
                "expected": final["summary"]["expected"],
                "observed": final["summary"]["observed"],
                "verify_ms": verify_ms,
                "corrected_by_recheck": corrected,
            },
            "policy_match": cl_pol == truth["policy"],
            "outcome_match": cl_out == truth["outcome"],
            "false_positive": cl_pol == "FAIL" and truth["policy"] == "PASS",
            "false_negative": cl_pol == "PASS" and truth["policy"] == "FAIL",
            "duplicate_expected": any("duplicate" in r for r in truth["reasons"]),
            "duplicate_detected": any(
                c.startswith("duplicate_effect:") for c in final["lenses"]["policy"]["reason_codes"]
            ),
            "orphan_effects": truth["orphans"],
            "orphans_flagged": [f"{o['subject_id']}:{o['fact_type']}" for o in orphans["items"]],
            "orphans_detected": bool(truth["orphans"])
            and any(o["subject_id"] in {x.split(":")[0] for x in truth["orphans"]} for o in orphans["items"]),
            "agent_calls": list(trace.calls) if trace else [],
            "trace_judge": None,
            # a provider/API failure before the agent acted is not a verifier result: excluded from the metrics
            "provider_error": bool(
                trace and trace.final is None and any(n.startswith("provider error") for n in trace.notes)
            ),
        }
        if recorded is not None:
            row["trace_judge"] = recorded.get("trace_judge")
            row["provider_error"] = bool(recorded.get("provider_error"))
            row["agent_notes"] = [
                n for n in recorded.get("agent_notes", []) if not n.startswith("agent said")
            ] + [n for n in rep.notes if n.startswith(("agent said", "replayed"))]
            row["replayed_from"] = self.replay_of
        elif self.judge and trace is not None:
            from sandbox.llm.judge import judge_trace
            from sandbox.llm.tools import TASKS

            try:
                row["trace_judge"] = judge_trace(TASKS[rep.contract_id], trace.calls, trace.final)
            except Exception as e:  # judge failure is reported, never fatal
                row["trace_judge"] = {"verdict": "ERROR", "reason": f"{type(e).__name__}: {str(e)[:120]}"}
        if self.sinks is not None and trace is not None:
            from sandbox.llm.tools import TASKS

            self.sinks.log(row, TASKS[rep.contract_id], trace.calls, trace.final)
        return row

    def run_all(self, only: list[str] | None = None) -> dict[str, Any]:
        rows = []
        if self.payments == "stripe":
            print(
                f"payments=stripe: running only the {', '.join(STRIPE_WORKFLOWS)} workflow(s); the others do "
                "not refund and would run against the local fake",
                flush=True,
            )
        for sc in build_scenarios():
            if only and not any(sc.id.startswith(o) for o in only):
                continue
            if self.payments == "stripe" and sc.workflow not in STRIPE_WORKFLOWS:
                continue
            if self.agent_kind == "llm" and not sc.llm_applicable:
                continue
            if self.replay and sc.id not in self.replay:
                continue
            row = self.run_one(sc)
            rows.append(row)
            # one progress line per scenario, so a long run can be followed from its log
            err = " provider_error" if row.get("provider_error") else ""
            j = (row.get("trace_judge") or {}).get("verdict", "-")
            print(
                f"[{len(rows)}] {sc.id}: agent={'success' if row['agent_claimed_success'] else 'failure'} "
                f"judge={j} truth={row['truth']['policy']} commitlayer={row['commitlayer']['policy']}"
                f"{' ok' if row['policy_match'] else ' MISMATCH'}{err}",
                flush=True,
            )
        res = {
            "scenarios": rows,
            "summary": summarize(rows),
            "agent": self.agent_kind,
            "provider": self.provider,
            "model": self.model,
            "replay_of": self.replay_of,
            "payments_backend": self.payments,
            "verifier": "api" if self.api is not None else "in-process",
        }
        if self.sinks is not None:
            res["sinks"] = self.sinks.finish()
        return res


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    errors = [r for r in rows if r.get("provider_error")]
    rows = [r for r in rows if not r.get("provider_error")]
    n = len(rows)
    truth_fail = [r for r in rows if r["truth"]["policy"] == "FAIL"]
    truth_pass = [r for r in rows if r["truth"]["policy"] == "PASS"]
    truth_unk = [r for r in rows if r["truth"]["policy"] == "UNKNOWN"]
    claimed_false = [r for r in rows if r["agent_claimed_success"] and r["truth"]["policy"] == "FAIL"]
    dups = [r for r in rows if r["duplicate_expected"]]
    return {
        "scenarios": n,
        "provider_errors": len(errors),
        "policy_agreement": sum(r["policy_match"] for r in rows),
        "outcome_agreement": sum(r["outcome_match"] for r in rows),
        "true_failures": len(truth_fail),
        "detected_failures": sum(r["commitlayer"]["policy"] == "FAIL" for r in truth_fail),
        "false_negatives": sum(r["false_negative"] for r in rows),
        "true_passes": len(truth_pass),
        "false_positives": sum(r["false_positive"] for r in rows),
        "unknown_cases": len(truth_unk),
        "unknown_reported_correctly": sum(r["commitlayer"]["policy"] == "UNKNOWN" for r in truth_unk),
        "false_success_claims": len(claimed_false),
        "false_success_claims_caught": sum(r["commitlayer"]["policy"] == "FAIL" for r in claimed_false),
        "duplicate_cases": len(dups),
        "duplicates_detected": sum(r["duplicate_detected"] for r in dups),
        "corrected_by_recheck": sum(r["commitlayer"]["corrected_by_recheck"] for r in rows),
        "orphan_effect_cases": sum(bool(r["orphan_effects"]) for r in rows),
        "orphans_detected": sum(r["orphans_detected"] for r in rows),
        "orphan_false_alarms": sum(bool(r["orphans_flagged"]) and not r["orphan_effects"] for r in rows),
        "trace_judge_scored": sum(
            1 for r in rows if r.get("trace_judge") and r["trace_judge"].get("verdict") in ("PASS", "FAIL")
        ),
        "trace_judge_agreement": sum(
            1
            for r in rows
            if r.get("trace_judge") and r["trace_judge"].get("verdict") == r["truth"]["policy"]
        ),
        "trace_judge_missed_failures": sum(
            1
            for r in rows
            if r.get("trace_judge")
            and r["trace_judge"].get("verdict") == "PASS"
            and r["truth"]["policy"] == "FAIL"
        ),
        "mean_verify_ms": int(sum(r["commitlayer"]["verify_ms"] for r in rows) / n) if n else 0,
        "max_verify_ms": max((r["commitlayer"]["verify_ms"] for r in rows), default=0),
    }


def to_markdown(res: dict[str, Any]) -> str:
    s = res["summary"]
    lines = [
        "# CommitLayer failure matrix — results",
        "",
        f"Scenarios: {s['scenarios']}  ·  policy-lens agreement with oracle: {s['policy_agreement']}/{s['scenarios']}  ·  "
        f"outcome-lens agreement: {s['outcome_agreement']}/{s['scenarios']}",
        "",
        f"Payments backend: {res.get('payments_backend', 'fake')}"
        + (" (Stripe test mode)" if res.get("payments_backend") == "stripe" else " (in-memory)"),
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| True failures detected | {s['detected_failures']}/{s['true_failures']} |",
        f"| False negatives (verifier PASS, truth FAIL) | {s['false_negatives']} |",
        f"| False positives (verifier FAIL, truth PASS) | {s['false_positives']}/{s['true_passes']} true passes |",
        f"| UNKNOWN reported when truth is unknowable | {s['unknown_reported_correctly']}/{s['unknown_cases']} |",
        f"| Agent claimed success falsely — caught | {s['false_success_claims_caught']}/{s['false_success_claims']} |",
        f"| Duplicate effects detected | {s['duplicates_detected']}/{s['duplicate_cases']} |",
        f"| Provisional verdicts corrected by the window re-check | {s['corrected_by_recheck']} |",
        f"| Effects on an unreported subject (orphans) detected | {s['orphans_detected']}/{s['orphan_effect_cases']} (false alarms: {s['orphan_false_alarms']}) |",
        f"| Verify latency (mean / max) | {s['mean_verify_ms']} ms / {s['max_verify_ms']} ms |",
        f"| Scenarios excluded because the model provider failed before acting | {s.get('provider_errors', 0)} |",
    ]
    if s.get("trace_judge_scored"):
        lines += [
            f"| Trace-based judge (sees tool calls only) — agreement with oracle | {s['trace_judge_agreement']}/{s['trace_judge_scored']} |",
            f"| Trace-based judge — failures it called PASS | {s['trace_judge_missed_failures']} |",
        ]
    lines += [
        "",
        "| Scenario | Injected failure | Agent said | Trace judge | Truth (policy) | CommitLayer (outcome / policy) | Failing check | OK |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in res["scenarios"]:
        c = r["commitlayer"]
        ok = (
            "✓"
            if r["policy_match"]
            else ("FP" if r["false_positive"] else ("FN" if r["false_negative"] else "✗"))
        )
        if r.get("provider_error"):
            ok = "ERR (provider)"
        lines.append(
            f"| {r['id']} | {r['failure']} | {'success' if r['agent_claimed_success'] else 'failure'} | {(r.get('trace_judge') or {}).get('verdict', '—')} | {r['truth']['policy']} | "
            f"{c['outcome']} / {c['policy']} | {(c['failing_check'] or '—')[:60]} | {ok} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Run the CommitLayer sandbox failure matrix")
    ap.add_argument("--only", nargs="*", help="scenario id prefixes to run")
    ap.add_argument("--out", default=str(Path(__file__).parent / "results"))
    ap.add_argument("--agent", choices=["scripted", "llm"], default="scripted")
    ap.add_argument("--provider", choices=["anthropic", "openai", "google", "fake"], default="fake")
    ap.add_argument("--model", default=None)
    ap.add_argument(
        "--judge", action="store_true", help="score each trajectory with the trace-based judge (Claude)"
    )
    ap.add_argument(
        "--sinks", action="store_true", help="log trajectories to Braintrust / LangSmith when keys are set"
    )
    ap.add_argument(
        "--replay",
        default=None,
        help="path to a recorded llm_<provider>.json: re-execute its tool calls and re-verify (no model calls)",
    )
    ap.add_argument(
        "--payments",
        choices=list(PAYMENTS_BACKENDS),
        default="fake",
        help="payments backend: the in-memory fake, or Stripe test mode via STRIPE_TEST_SECRET_KEY "
        "(refund scenarios only)",
    )
    ap.add_argument(
        "--api-url",
        default=os.environ.get("COMMITLAYER_API_URL"),
        help="verify through a CommitLayer workspace's public API instead of in-process (needs --api-key)",
    )
    ap.add_argument(
        "--api-key",
        default=os.environ.get("COMMITLAYER_API_KEY"),
        help="API key of that workspace (or COMMITLAYER_API_KEY); nothing else of yours is sent",
    )
    args = ap.parse_args(argv)
    if bool(args.api_url) != bool(args.api_key):
        ap.error("--api-url and --api-key go together")
    replay = None
    if args.replay:
        replay = json.loads(Path(args.replay).read_text())
        replay.setdefault("run_id", Path(args.replay).parent.name)
        args.agent, args.provider = "llm", replay.get("provider", args.provider)
    m = Matrix(
        agent=args.agent,
        provider=args.provider,
        model=args.model,
        judge=args.judge and not replay,
        sinks=args.sinks and not replay,
        replay=replay,
        payments=args.payments,
        api_url=args.api_url,
        api_key=args.api_key,
    )
    res = m.run_all(args.only)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = "latest" if args.agent == "scripted" else f"llm_{args.provider}"
    if args.payments != "fake":  # a partial, provider-backed run must not overwrite the full matrix's numbers
        stem += f"_{args.payments}"
    (out / f"{stem}.json").write_text(json.dumps(res, indent=2, default=str))
    (out / f"{stem}.md").write_text(to_markdown(res))
    print(to_markdown(res))


if __name__ == "__main__":
    main()
