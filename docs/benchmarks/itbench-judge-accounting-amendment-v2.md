# ITBench Luna judge accounting amendment v2

The E4 Luna rescore batch transmitted and was charged for 35 one-attempt
cases, but the repository wrapper passed a relative result path to the
ephemeral evaluator working directory. The pinned evaluator completed its
judge calls and then failed to persist the aggregate with `FileNotFoundError`.
No score was recovered and no calls are refunded.

The wrapper now resolves all evaluator input/output paths before launch. To
complete the already-authorized frozen E4/E5/E6/E7 Luna score series, the
judge lifetime cap is amended from 142 to 177 while preserving `consumed=37`.
The extra 35 calls are the single recovery execution of the failed E4 batch;
the remaining E5, E6, and E7 batches remain one attempt per case. This is a
persistence recovery, not a score-driven rerun or an agent rerun.
