# Post-A1 note: Kafka lag semantics

Status: POST-A1 / not implemented in the A1 evaluation harness

The current A1 compatibility alert and `kafka_consumer_lag` evidence path use a
bounded proxy based on the two-minute increase in produced messages minus the
two-minute increase in consumed messages. This keeps the frozen benchmark
semantics aligned with the existing alert rule and avoids changing the meaning
of recorded A1 evidence during recovery work.

A future engineering implementation may use Kafka consumer-group offsets and
compute true lag directly. That would better represent backlog across
partitions and would make consumer-group identity explicit, but it requires
stable offset inspection, partition aggregation, reset/replay policy, and a
new qualification of alert and tool semantics. It must therefore be a
versioned post-A1 change, not a fixture reliability patch.
