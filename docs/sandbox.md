# Sandbox: the artificial enterprise and the failure matrix

`sandbox/` is how CommitLayer is tested without customers. It builds a small enterprise, lets an agent act in it under
injected failures, and scores CommitLayer's verdict against an oracle that reads the systems' databases directly.
CommitLayer is what is being measured; the agent is a scripted stand-in until a model key is configured.

```bash
.venv/bin/python -m sandbox                      # full matrix → sandbox/results/latest.{json,md}
.venv/bin/python -m sandbox --only refund/       # one workflow
.venv/bin/python -m pytest tests/test_sandbox.py # the regression subset (14 scenarios)
```

## What is in it

| Piece | File | Notes |
|---|---|---|
| Six systems of record | `sandbox/systems.py` | payments, orders, crm, tickets, ledger, platform. Each has its own SQLite file, an HTTP API the agent's tools call, and an append-only event log a read-only connector would pull (`GET /{system}/events`). A virtual clock places every scenario 20 minutes in the past so the 10-minute settlement window is closed at verification time. Writes honour `Idempotency-Key`. |
| Failure injection | `sandbox/chaos.py` | An httpx transport between the agent (and the extractor) and the systems. Modes: `lost_response` (commit, then time out), `phantom_success` (200, no write), `error` (403 / 429 / 500), `unreachable`, `stale_read` (connector sees a snapshot), `before` (a concurrent actor changes state first), `rewrite` (tool output altered). |
| Scripted agent | `sandbox/agent.py` | Ordinary tools (`refund`, `update_order_status`, `add_note`, `close_ticket`, `send_payout`, …) and behaviour profiles: retry without idempotency key, retry with one, stop on error, claim success on error, follow instructions found in ticket text. |
| Extractor | `sandbox/extract.py` | Pulls each system's events over HTTP through the same chaos layer and turns them into CommitLayer facts. Reports systems it could not reach. |
| Seven action contracts | `sandbox/contracts/*.yaml` | refund customer, cancel subscription, change shipping address, approve discount, close support case, send payout, deploy service. The payout and deploy contracts use the checks that look beyond one action: a per-agent total over a window (`aggregate_limits`), a value compared with the request (`effect_attr_differs`, payout only), and a later record that undoes the action (`reversed_by`: a returned payout, a rollback). |
| Scenarios and oracle | `sandbox/scenarios.py` | 94 scenarios: 11 injected failures × 7 workflows plus 17 workflow-specific cases (over limit, ineligible, wrong customer, concurrent human refund, prompt injection, forbidden side effect, frozen account, daily total exceeded, amount mismatch, payout returned after settlement, degraded service, wrong environment, too many deploys, unhealthy after deploy, deploy rolled back after settlement). A scenario may run earlier actions by the same agent first (`prior_runs`) and inject something after the verdict settled (`after_settlement`). The oracle reads the databases and never looks at CommitLayer. |
| Harness | `sandbox/harness.py` | Fresh enterprise and fresh tenant per scenario; record → extract → verify → compare; writes `results/latest.md`. |

## Results (run of 2026-09-28)

Scenarios: 62 · policy-lens agreement with the oracle: 62/62 · outcome-lens agreement: 62/62

| Metric | Value |
|---|---|
| True failures detected | 35/35 |
| False negatives (verifier PASS, truth FAIL) | 0 |
| False positives (verifier FAIL, truth PASS) | 0 of 22 true passes |
| UNKNOWN reported when the truth is unknowable (required system down) | 5/5 |
| Agent claimed success falsely — caught | 35/35 |
| Duplicate effects detected (retry without idempotency, human refunded first, retry twice) | 9/9 |
| Provisional verdicts corrected by the settlement-window re-check (stale connector reads) | 5/5 |
| Effects on a subject with no recorded action (wrong customer, prompt injection) | 2/2 flagged, 0 false alarms |
| Verify latency, mean / max | 16 ms / 68 ms |

The per-scenario table is in `sandbox/results/latest.md`.

## What the matrix changed in the engine

The first run scored 50/62. Both gaps were real and are fixed:

1. **"No structured signal" must not apply to action contracts.** Twelve scenarios where the agent claimed success and
   nothing happened (403 on the write, no tool calls at all, wrong customer, injected instruction) came back UNKNOWN.
   The outcome-billing engine treats "none of the relevant record types were observed" as unverifiable, which is right
   when a vendor's claim may simply be invisible to the connected sources. For an action the agent asserted, with the
   sources connected and the subject known from its pre-action state, no effect at all is a FAIL that names every
   missing effect. Action contracts now skip that fallback (`compiler.py`).
2. **Orphan effects.** When the agent acts on the wrong record, the reported action correctly FAILs (its effects are
   missing) but the damage happened somewhere else. `GET /v1/actions/orphans?contract_id=` lists effects of the kinds the
   contract expects or forbids on subjects that have no recorded action in the window. Both wrong-subject scenarios
   are flagged; the other 60 produce no false alarm.

A third finding was about the sandbox itself: three systems did not honour idempotency keys, so an idempotent retry
still duplicated. CommitLayer reported the duplicate correctly; the systems were fixed to behave like real ones.

## Limits of this evidence

- The agent is scripted. Its failure behaviours are the ones models are known to have, but a model may produce
  others. Swap in a real agent by giving `Agent` an LLM-driven tool loop; the harness does not change.
- Coverage is by "source ever reported". A system that reported before and is down now yields FAIL (effects missing),
  not UNKNOWN. Connector health should feed coverage; tracked as a follow-up.
- Stale connector reads are only corrected because the settlement window re-check exists. A verdict read before the
  window closes is provisional by design.
- No comparison against trace-based evaluators has been run. The scenario set is built so the same agent can be run
  through them: `phantom_success`, `permission_denied` and `agent_claims_success_no_calls` are the cases where the
  agent's trace looks correct and only the systems of record disagree.

## Running the payments system against Stripe test mode

By default the payments system is an in-memory fake that does exactly what the sandbox tells it. The refund
scenarios can instead run against **Stripe test mode**, so the verdicts are scored on a real payment API's
objects rather than on the sandbox's imitation of one (`sandbox/stripe_payments.py`).

```bash
export STRIPE_TEST_SECRET_KEY=sk_test_...        # a Stripe *test* secret key; never put it in a file
.venv/bin/python -m sandbox --payments stripe    # refund scenarios only → sandbox/results/latest_stripe.{json,md}
.venv/bin/python -m pytest tests/test_sandbox_stripe.py   # the same code against an in-process fake Stripe; no key
```

What is real when `--payments stripe` is set:

- **The objects.** Seeding creates one confirmed PaymentIntent per paid or shipped order (`pm_card_visa`, amount in
  cents, `metadata[order_id]`); the agent's `POST /payments/refunds` creates a Stripe refund against it. The
  `refund_created` facts carry Stripe's refund id (`re_…`), amount (converted back to major units), currency and
  status.
- **Idempotency.** The agent's `Idempotency-Key` is forwarded to Stripe, which decides what a replay is; the
  sandbox records the key locally so the existing replay behaviour is unchanged. A retry without a key after a
  lost response asks Stripe for a second refund: for a partial refund Stripe makes a duplicate, for a full one it
  refuses because the charge is spent, so that scenario ends in a tool error rather than a duplicate. Both are
  real outcomes, and the oracle and the verifier agree on them either way.
- **Reconciliation.** Before answering, `GET /payments/events` lists Stripe's refunds for every seeded
  PaymentIntent and appends a `refund_created` fact for any it has not seen, keyed by refund id. A refund made
  in the Stripe dashboard shows up on the next pull as a system-of-record fact, exactly as a connector would
  report it. A refund is appended once and keeps its sequence number, so the `stale_read` chaos mode still works.

What is not real:

- **Timestamps.** Every fact is placed on the sandbox's virtual clock (one second per write), not on Stripe's
  `created`, so a scenario still sits 20 minutes in the past and the settlement window closes deterministically.
- **Payouts and billing stops.** `/payments/payouts` and `/payments/billing/stop` stay on the local fake
  behaviour; nothing is sent to Stripe for them. That is why the matrix runs only the `refund` workflow in this
  mode and says so up front rather than failing midway.
- **The concurrent human refund.** `refund/concurrent_update` plants its first refund on the sandbox side (the
  scenario is shared with the fake); the agent's duplicate is the Stripe one.

The key is read from `STRIPE_TEST_SECRET_KEY` only, is never written anywhere, and must start with `sk_test_`:
anything else is refused before a single call is made. The results carry `payments_backend` so a JSON or
markdown file says which backend produced its numbers.

## Real-agent benchmark (Claude, OpenAI, Gemini)

`sandbox/llm/` swaps the scripted agent for a model driving the same tools through a provider adapter
(`anthropic_agent.py`, `openai_agent.py`, `google_agent.py`; `fake_agent.py` for CI). The model sees the task and
the tool results — including timeouts, 403s and injected ticket text — and decides for itself whether to retry, whether
to use an idempotency key, and what to report. 46 of the 62 scenarios apply (the four that script a specific agent
behaviour are skipped). Two baselines run alongside CommitLayer:

- **Trace-based judge** (`llm/judge.py`): a fixed Claude model that sees only the trajectory (calls, arguments,
  responses, the agent's own report) — what a trace/observability evaluator sees — and never the systems of record.
- **Braintrust / LangSmith sinks** (`llm/tracing.py`): every trajectory is logged with the oracle truth as the expected
  value and CommitLayer's verdict as a score, so the same runs can be inspected and re-scored in those platforms.

Run locally (keys in the environment):

```bash
.venv/bin/python -m sandbox --agent llm --provider anthropic --judge --sinks
.venv/bin/python -m sandbox --agent llm --provider openai --model gpt-5-mini --judge
.venv/bin/python -m sandbox --agent llm --provider google --model gemini-2.5-flash --judge
```

Run in production without handling keys (founder only; the keys stay in the Railway environment):

```bash
POST /v1/admin/benchmark   {"provider": "anthropic", "judge": true, "sinks": true, "only": ["refund/"]}
GET  /v1/admin/benchmark/{id}      # status, summary, per-scenario table, markdown, log tail
GET  /v1/admin/benchmark           # all runs
POST /v1/admin/benchmark   {"replay_of": "<run id>"}   # re-verify a recorded run on the current engine; no model calls
```

**Replay.** A recorded run keeps every tool call the model made (`agent_calls`). `--replay <llm_<provider>.json>`
(or `replay_of` on the admin endpoint) re-executes those calls through the same chaos rules against a fresh
enterprise, extracts, and verifies again, so a verifier change can be re-scored against real agent behaviour
without spending model quota. The trace-only judge's verdict is carried over unchanged (the trace is the same).
A replayed row notes how many recorded calls responded differently from the recording; zero means the replay is
faithful. The replay test in `tests/test_sandbox.py` checks that replaying a run reproduces its verdicts.

```
```

Each run is an isolated subprocess with its own SQLite database and object store under `/data/benchmarks/<id>`;
it never touches the production database. Results are `llm_<provider>.{json,md}` in that directory.

## Real-agent results (2026-09-29)

Full 46-scenario runs for Claude, GPT and Gemini, with the trace-only judge alongside, are in
[benchmark.md](benchmark.md). The runs found one verifier defect: a real agent notified the customer on the support ticket rather than on the
order, and both the oracle and the verifier counted that as a missing notification and as an orphan effect. Tickets
are linked to their orders, so effects on linked subjects now count for the action's subject and are never orphans
(`links` on `POST /v1/facts`; the sandbox extractor derives them from the ticket events). Every benchmark row was
then re-verified by replaying the recorded runs on the fixed verifier (see Replay above); no replayed call responded
differently from its recording.
