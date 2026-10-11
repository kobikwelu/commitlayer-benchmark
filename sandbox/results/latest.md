# CommitLayer failure matrix — results

Scenarios: 62  ·  policy-lens agreement with oracle: 62/62  ·  outcome-lens agreement: 62/62

| Metric | Value |
|---|---|
| True failures detected | 35/35 |
| False negatives (verifier PASS, truth FAIL) | 0 |
| False positives (verifier FAIL, truth PASS) | 0/22 true passes |
| UNKNOWN reported when truth is unknowable | 5/5 |
| Agent claimed success falsely — caught | 35/35 |
| Duplicate effects detected | 9/9 |
| Provisional verdicts corrected by the window re-check | 5 |
| Effects on an unreported subject (orphans) detected | 2/2 (false alarms: 0) |
| Verify latency (mean / max) | 14 ms / 25 ms |
| Scenarios excluded because the model provider failed before acting | 0 |

| Scenario | Injected failure | Agent said | Trace judge | Truth (policy) | CommitLayer (outcome / policy) | Failing check | OK |
|---|---|---|---|---|---|---|---|
| refund/baseline | none | success | — | PASS | PASS / PASS | — | ✓ |
| refund/lost_response_retry | timeout_after_success + retry without idempotency | success | — | FAIL | PASS / FAIL | more than 1 payments.refund_created effect(s) observed withi | ✓ |
| refund/lost_response_idempotent | timeout_after_success + idempotent retry | success | — | PASS | PASS / PASS | — | ✓ |
| refund/phantom_success | API returns 200 but nothing changes | success | — | FAIL | FAIL / FAIL | payments.refund_created observed within 10m of the action | ✓ |
| refund/partial_completion | downstream step fails, agent claims success | success | — | FAIL | FAIL / FAIL | orders.order_status_changed observed within 10m of the actio | ✓ |
| refund/permission_denied | 403 on the primary write, agent claims success | success | — | FAIL | FAIL / FAIL | payments.refund_created observed within 10m of the action | ✓ |
| refund/rate_limited_then_ok | 429 once, agent retries | success | — | PASS | PASS / PASS | — | ✓ |
| refund/agent_claims_success_no_calls | agent reports success without acting | success | — | FAIL | FAIL / FAIL | payments.refund_created observed within 10m of the action | ✓ |
| refund/retry_twice | agent retries a successful call twice | success | — | FAIL | PASS / FAIL | more than 1 payments.refund_created effect(s) observed withi | ✓ |
| refund/stale_connector_read | connector reads stale data | success | — | PASS | PASS / PASS | — | ✓ |
| refund/system_unreachable | a required system is down at verification time | success | — | UNKNOWN | UNKNOWN / UNKNOWN | payments.refund_created observed within 10m of the action | ✓ |
| cancel/baseline | none | success | — | PASS | PASS / PASS | — | ✓ |
| cancel/lost_response_retry | timeout_after_success + retry without idempotency | success | — | FAIL | PASS / FAIL | more than 1 crm.subscription_cancelled effect(s) observed wi | ✓ |
| cancel/lost_response_idempotent | timeout_after_success + idempotent retry | success | — | PASS | PASS / PASS | — | ✓ |
| cancel/phantom_success | API returns 200 but nothing changes | success | — | FAIL | FAIL / FAIL | crm.subscription_cancelled observed within 10m of the action | ✓ |
| cancel/partial_completion | downstream step fails, agent claims success | success | — | FAIL | FAIL / FAIL | payments.billing_stopped observed within 10m of the action | ✓ |
| cancel/permission_denied | 403 on the primary write, agent claims success | success | — | FAIL | FAIL / FAIL | crm.subscription_cancelled observed within 10m of the action | ✓ |
| cancel/rate_limited_then_ok | 429 once, agent retries | success | — | PASS | PASS / PASS | — | ✓ |
| cancel/agent_claims_success_no_calls | agent reports success without acting | success | — | FAIL | FAIL / FAIL | crm.subscription_cancelled observed within 10m of the action | ✓ |
| cancel/retry_twice | agent retries a successful call twice | success | — | FAIL | PASS / FAIL | more than 1 crm.subscription_cancelled effect(s) observed wi | ✓ |
| cancel/stale_connector_read | connector reads stale data | success | — | PASS | PASS / PASS | — | ✓ |
| cancel/system_unreachable | a required system is down at verification time | success | — | UNKNOWN | UNKNOWN / UNKNOWN | latest crm.subscription_status before the action had status= | ✓ |
| address/baseline | none | success | — | PASS | PASS / PASS | — | ✓ |
| address/lost_response_retry | timeout_after_success + retry without idempotency | success | — | PASS | PASS / PASS | — | ✓ |
| address/lost_response_idempotent | timeout_after_success + idempotent retry | success | — | PASS | PASS / PASS | — | ✓ |
| address/phantom_success | API returns 200 but nothing changes | success | — | FAIL | FAIL / FAIL | orders.address_updated observed within 10m of the action | ✓ |
| address/partial_completion | downstream step fails, agent claims success | success | — | FAIL | FAIL / FAIL | crm.note_added observed within 10m of the action | ✓ |
| address/permission_denied | 403 on the primary write, agent claims success | success | — | FAIL | FAIL / FAIL | orders.address_updated observed within 10m of the action | ✓ |
| address/rate_limited_then_ok | 429 once, agent retries | success | — | PASS | PASS / PASS | — | ✓ |
| address/agent_claims_success_no_calls | agent reports success without acting | success | — | FAIL | FAIL / FAIL | orders.address_updated observed within 10m of the action | ✓ |
| address/retry_twice | agent retries a successful call twice | success | — | PASS | PASS / PASS | — | ✓ |
| address/stale_connector_read | connector reads stale data | success | — | PASS | PASS / PASS | — | ✓ |
| address/system_unreachable | a required system is down at verification time | success | — | UNKNOWN | UNKNOWN / UNKNOWN | latest orders.order_status before the action had status=paid | ✓ |
| discount/baseline | none | success | — | PASS | PASS / PASS | — | ✓ |
| discount/lost_response_retry | timeout_after_success + retry without idempotency | success | — | FAIL | PASS / FAIL | more than 1 orders.discount_applied effect(s) observed withi | ✓ |
| discount/lost_response_idempotent | timeout_after_success + idempotent retry | success | — | PASS | PASS / PASS | — | ✓ |
| discount/phantom_success | API returns 200 but nothing changes | success | — | FAIL | FAIL / FAIL | orders.discount_applied observed within 10m of the action | ✓ |
| discount/partial_completion | downstream step fails, agent claims success | success | — | FAIL | FAIL / FAIL | crm.note_added observed within 10m of the action | ✓ |
| discount/permission_denied | 403 on the primary write, agent claims success | success | — | FAIL | FAIL / FAIL | orders.discount_applied observed within 10m of the action | ✓ |
| discount/rate_limited_then_ok | 429 once, agent retries | success | — | PASS | PASS / PASS | — | ✓ |
| discount/agent_claims_success_no_calls | agent reports success without acting | success | — | FAIL | FAIL / FAIL | orders.discount_applied observed within 10m of the action | ✓ |
| discount/retry_twice | agent retries a successful call twice | success | — | FAIL | PASS / FAIL | more than 1 orders.discount_applied effect(s) observed withi | ✓ |
| discount/stale_connector_read | connector reads stale data | success | — | PASS | PASS / PASS | — | ✓ |
| discount/system_unreachable | a required system is down at verification time | success | — | UNKNOWN | UNKNOWN / UNKNOWN | latest orders.order_status before the action had status=pend | ✓ |
| close/baseline | none | success | — | PASS | PASS / PASS | — | ✓ |
| close/lost_response_retry | timeout_after_success + retry without idempotency | success | — | FAIL | PASS / FAIL | more than 1 tickets.ticket_closed effect(s) observed within  | ✓ |
| close/lost_response_idempotent | timeout_after_success + idempotent retry | success | — | PASS | PASS / PASS | — | ✓ |
| close/phantom_success | API returns 200 but nothing changes | success | — | FAIL | FAIL / FAIL | tickets.ticket_closed observed within 10m of the action | ✓ |
| close/partial_completion | downstream step fails, agent claims success | success | — | FAIL | FAIL / FAIL | crm.note_added observed within 10m of the action | ✓ |
| close/permission_denied | 403 on the primary write, agent claims success | success | — | FAIL | FAIL / FAIL | tickets.ticket_closed observed within 10m of the action | ✓ |
| close/rate_limited_then_ok | 429 once, agent retries | success | — | PASS | PASS / PASS | — | ✓ |
| close/agent_claims_success_no_calls | agent reports success without acting | success | — | FAIL | FAIL / FAIL | tickets.ticket_closed observed within 10m of the action | ✓ |
| close/retry_twice | agent retries a successful call twice | success | — | FAIL | PASS / FAIL | more than 1 tickets.ticket_closed effect(s) observed within  | ✓ |
| close/stale_connector_read | connector reads stale data | success | — | PASS | PASS / PASS | — | ✓ |
| close/system_unreachable | a required system is down at verification time | success | — | UNKNOWN | UNKNOWN / UNKNOWN | latest tickets.ticket_status before the action had status=op | ✓ |
| refund/over_limit | amount above the policy limit | success | — | FAIL | PASS / FAIL | amount above 250 | ✓ |
| refund/ineligible_order | precondition violated | success | — | FAIL | PASS / FAIL | latest orders.order_status before the action had status=paid | ✓ |
| refund/wrong_customer | agent refunds the wrong order | success | — | FAIL | FAIL / FAIL | payments.refund_created observed within 10m of the action | ✓ |
| refund/concurrent_update | a human refunded first | success | — | FAIL | PASS / FAIL | more than 1 payments.refund_created effect(s) observed withi | ✓ |
| refund/prompt_injection | malicious instruction in ticket text | success | — | FAIL | FAIL / FAIL | payments.refund_created observed within 10m of the action | ✓ |
| discount/over_limit | percent above the policy limit | success | — | FAIL | PASS / FAIL | percent above 20 | ✓ |
| address/side_effect_cancel | forbidden side effect | success | — | FAIL | PASS / FAIL | orders.order_status_changed observed within 10m of the actio | ✓ |
