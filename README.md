# CommitLayer agent-action benchmark

An agent's trace tells you what it attempted. The system of record tells you what happened. This repository is
the harness behind the numbers on [commitlayer.ai/benchmark](https://commitlayer.ai/benchmark), published so that
anyone can run it against their own agent, their own model and their own CommitLayer workspace, and try to break
the verifier.

What is in it:

- **An artificial enterprise** (`sandbox/systems.py`): six systems of record with their own databases and HTTP
  APIs (payments, orders, CRM, tickets, ledger, platform), a virtual clock, and an append-only event log per
  system that a read-only connector would pull.
- **A failure layer** (`sandbox/chaos.py`): lost responses, phantom successes, permission errors, rate limits,
  stale reads, unreachable systems, concurrent human edits, prompt injection through ticket text, side effects
  in another system.
- **94 scenarios across seven workflows** (`sandbox/scenarios.py`): refund, cancel a subscription, change a
  shipping address, approve a discount, close a support case, send a payout, deploy a service. Each carries an
  **oracle** that reads the sandbox databases directly after the run and decides the truth on a code path
  independent of the verifier.
- **Agents**: a scripted agent with selectable behaviours (`sandbox/agent.py`), and LLM agents for Anthropic,
  OpenAI and Google models with one shared tool catalogue and task set (`sandbox/llm/`), plus a trace-only
  judge as the baseline for what traces alone can show.
- **Seven action contracts** (`sandbox/contracts/`), the YAML the verifier evaluates.
- **Recorded runs** (`sandbox/results/`): the scripted-agent matrix and a run with the fake model adapter, in the
  format `--replay` takes. The three model runs behind the published report were recorded inside the
  production environment and are summarised row by row in `docs/benchmark.md`; they will be added here as
  they are exported.
- **Optional Stripe test mode** for the payments system, so the refund scenarios create and refund real
  PaymentIntents in a Stripe test account.

The verifier is measured, not the agent. A row counts as agreement when CommitLayer's verdict matches the
oracle's.

## Run it against your own workspace

You need Python 3.11+, a CommitLayer workspace (the free Developer plan is enough) and three API keys from
Settings → API keys, one per side of the line, the way a real integration is wired:

- a **Runtime recorder** key: records each scenario's action and can do nothing else;
- an **Evidence** key bound to the sandbox's six sources (`payments, orders, crm, tickets, ledger, platform`):
  pushes the facts the sandbox's systems of record emit and cannot record actions;
- a **Full access** key: creates the seven sandbox contracts on first run and asks for verdicts.

The key that records an action can never also supply the facts that verify it. Each scenario counts as one
verified action in your workspace; nothing else of yours is sent.

```bash
pip install -e .
export COMMITLAYER_API_URL=https://api.commitlayer.ai
export COMMITLAYER_API_KEY=spk_...          # Full access: contracts and verdicts
export COMMITLAYER_RECORDER_KEY=spk_...     # Runtime recorder: the agent's actions
export COMMITLAYER_EVIDENCE_KEY=spk_...     # Evidence: the facts, bound to the six sources above
python -m sandbox                            # scripted agent, every scenario
python -m sandbox --only refund/ deploy/     # a subset
```

With only the full key set, everything runs through it and the result says `credentials: single`; with the
three keys it says `credentials: split`.

The run prints one line per scenario and writes `results/latest.json` and `results/latest.md`. On its first run
the harness creates the seven sandbox contracts in your workspace (prefixed `sbx_`; like the library contracts
they count against no plan limit).

Records are namespaced per run, so repeated runs never share subjects. The orphan-effect report (effects with
no recorded action) is a Growth-plan feature of the product; on other plans the harness marks that column as
unavailable instead of failing.

## Run it with a model driving the agent

```bash
pip install -e ".[anthropic]"              # or openai, google
export ANTHROPIC_API_KEY=...               # the provider's key, read only by that provider's SDK
python -m sandbox --agent llm --provider anthropic --model claude-opus-5-5 --judge
```

`--judge` scores every trajectory with the trace-only judge as well, so the report shows what a judge that
only sees the agent's tool calls would have said. `--replay sandbox/results/llm_anthropic.json` re-executes a
recorded run's tool calls against the sandbox and re-verifies without calling a model; the report records
whether any tool call answered differently from the recording.

## Run the payments system against Stripe test mode

```bash
export STRIPE_TEST_SECRET_KEY=sk_test_...  # a test-mode key only; a live-shaped key is refused
python -m sandbox --payments stripe --only refund/
```

With Stripe backing the payments system the refund scenarios create a confirmed PaymentIntent per paid order
and refund it through Stripe's API, idempotency keys included. The PaymentIntent and refund ids, amounts and
statuses are Stripe's; timestamps are placed on the sandbox's clock so settlement windows stay deterministic.
Stripe refuses a second full refund of the same charge, so the lost-response retry does not produce a
duplicate there the way it does on the fake: the scenario ends with the agent stopping on Stripe's error and
the verifier naming the missing downstream effect. Payouts stay on the in-memory system.

## Add a scenario or plug in your own agent

- A scenario is a `Scenario(...)` in `sandbox/scenarios.py`: an id, a workflow, the failure to inject (a list
  of `chaos.Rule`), the agent behaviour to use, and the oracle learns the truth from the databases, not from
  the scenario's name. Add the oracle branch if the failure is of a new kind.
- Your own agent implements the small client surface in `sandbox/agent.py` (it talks to the enterprise over
  HTTP through the failure layer) and returns a `Report` naming the action it believes it performed. Pass a
  factory to `Matrix` or run `run_scenario` directly.
- A new system of record is a `System` with an event log and a few endpoints in `build_app`; the extractor
  (`sandbox/extract.py`) turns every event into a fact.

## Running in-process

Inside the CommitLayer API repository the same harness runs the verifier in-process (`InProcessVerifier`),
which is how the product's own sandbox page and the founder-run model benchmarks work. Outside it, only the API
mode is available; the harness tells you so if you run it without `--api-url`.

## What this does not show

One recorded run per model; agents are non-deterministic, so the agent-side counts move between runs while the
verifier-side results are deterministic given the facts. The systems of record are synthetic except for the
optional Stripe test mode. The verifier and the oracle read the same sandbox, so agreement shows that the
verifier reads systems of record correctly and that trace-only judging does not; it does not show behaviour
against real vendor APIs, under load, or against an agent that writes to the systems of record to cover its
tracks. All of this is on the published report as well.

## License

MIT. The verifier itself (the CommitLayer engine) is not in this repository; the harness talks to it through
the public API.
