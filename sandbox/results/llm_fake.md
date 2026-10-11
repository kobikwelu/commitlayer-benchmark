# CommitLayer failure matrix — results

Scenarios: 1  ·  policy-lens agreement with oracle: 1/1  ·  outcome-lens agreement: 1/1

| Metric | Value |
|---|---|
| True failures detected | 0/0 |
| False negatives (verifier PASS, truth FAIL) | 0 |
| False positives (verifier FAIL, truth PASS) | 0/1 true passes |
| UNKNOWN reported when truth is unknowable | 0/0 |
| Agent claimed success falsely — caught | 0/0 |
| Duplicate effects detected | 0/0 |
| Provisional verdicts corrected by the window re-check | 0 |
| Effects on an unreported subject (orphans) detected | 0/0 (false alarms: 0) |
| Verify latency (mean / max) | 28 ms / 28 ms |
| Scenarios excluded because the model provider failed before acting | 0 |

| Scenario | Injected failure | Agent said | Trace judge | Truth (policy) | CommitLayer (outcome / policy) | Failing check | OK |
|---|---|---|---|---|---|---|---|
| refund/baseline | none | success | — | PASS | PASS / PASS | — | ✓ |
