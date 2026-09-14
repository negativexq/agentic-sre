# Post-A1 Kafka lag note

Status: **POST-A1**, not part of ITB-E2.

The internal A1 worker-lag evidence currently uses the bounded proxy
`increase(produced) - increase(consumed)`. A future implementation could use
Kafka consumer-group offsets as true lag. That would provide direct backlog
semantics and avoid proxy assumptions, but it requires stable group/partition
provenance, offset retention policy, and new tool/evaluator compatibility.
Changing it during A1 recovery would alter evidence semantics and invalidate
comparisons, so the current proxy remains frozen.
