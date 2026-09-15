# ITB-E7 accounting policy v2

The original E7 ledger remains historical evidence. Its first provider attempt
failed with `HTTP 429 insufficient_quota` and remains permanently charged:

```text
.local/itbench-lite-e7-budget.json = 1 attempted call
```

It is not reset or refunded. The live work is split into two new agent ledgers:

| purpose | ledger | cap | initial consumed |
|---|---|---:|---:|
| new synthetic smoke | `.local/itbench-lite-e7-smoke-budget.json` | 5 | 0 |
| official 35 x 5 agent calls | `.local/itbench-lite-e7-official-budget.json` | 175 | 0 |

Smoke calls do not enter the official ledger. Judge calls use the separate
`.local/itbench-judge-luna-budget.json` ledger and never consume either agent
ledger. This preserves the historical charge while retaining the complete
worst-case official budget.

The split is accounting-only. It does not change the frozen E7 prompt,
protocol, tools, limits, scenario order, or runtime behavior.
