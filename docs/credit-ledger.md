# Credit ledger

Credits are internal, non-withdrawable accounting units. One estimated credit represents one
reserved CPU core-minute:

```text
estimated = cpu_required × ceiling(timeout_seconds / 60)
```

`credit_transactions` is append-only by application policy. A balance is always reconciled as
the sum of its entries; there is no mutable balance field. Every entry contains its account,
signed integer amount, type, business reference, and globally unique idempotency key.

## Flows

| Event | Publisher | Worker owner | Platform |
|---|---:|---:|---:|
| Signup | `+bonus` | — | — |
| Submit | `-reserved` | — | — |
| Verified success | — | `+80%` | `+20%` |
| Infrastructure failure after retries | `+reserved` | — | — |
| User-code failure | `+unused` | `+80% charged` | `+20% charged` |

For a user-code failure, reliable elapsed time is rounded up to whole minutes, multiplied by the
reserved CPU count, and capped by the reservation. At least one CPU-minute is charged once a run
starts. This intentionally simple MVP policy is deterministic. Timeout is treated as user-code
failure; bundle/download/Docker-daemon/upload failures are infrastructure failures.

Ledger amounts use two decimal places, so even a one-credit job settles exactly at 80/20. Account
rows are locked before checking or changing value. Submission cannot make the publisher
negative. Settlement accounts are locked in stable account-ID order to avoid deadlocks. Unique
keys such as `task:<id>:worker-reward` prevent double payment, while the task/run row locks prevent
conflicting terminal transitions.
