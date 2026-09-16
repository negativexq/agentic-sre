# OpenAI provider diagnostics readiness

This offline repair makes the next separately approved provider attempt diagnostically conclusive. It does not reinterpret or modify `ITB-CONTROL-LIVE-SMOKE-002`.

## Current limitation

SMOKE-002 ended at the provider boundary after one outbound attempt and no successful model response. Its old result contains `PROVIDER_UNAVAILABLE` and a generic runtime message because the provider discarded the SDK exception class, status, structured API error fields, and request ID. The exact historical cause is therefore unknown.

## Repaired contract

`ProviderFailureMetadata` is a strict, bounded contract separate from successful response-envelope metadata. The provider maps status and transport classes as follows:

| Condition | Stable code | Category |
| --- | --- | --- |
| 400 | `BAD_REQUEST` | `BAD_REQUEST` |
| 401 | `AUTHENTICATION_FAILED` | `AUTHENTICATION` |
| 403 | `PERMISSION_DENIED` | `PERMISSION_DENIED` |
| 404 | `RESOURCE_NOT_FOUND` | `NOT_FOUND` |
| 422 | `UNPROCESSABLE_REQUEST` | `BAD_REQUEST` |
| 429 | `RATE_LIMITED` | `RATE_LIMIT` |
| 500+ | `PROVIDER_UNAVAILABLE` | `SERVER_ERROR` |
| timeout | `PROVIDER_TIMEOUT` | `TIMEOUT` |
| connection | `PROVIDER_UNAVAILABLE` | `CONNECTION` |
| unknown | `PROVIDER_UNAVAILABLE` | `UNKNOWN` |

The safe whitelist is exactly:

```text
provider
exception_class
category
http_status_code
api_error_type
api_error_code
api_error_param
request_id
message_summary
```

`message_summary` uses an explicit SDK message field only, is capped at 500 characters, and redacts bearer-token and `sk-*` patterns. Raw exceptions, request/response bodies, headers, prompts, credentials, and tool payloads are never persisted.

## Propagation proof

Offline tests exercise the complete path:

```text
SDK-like exception
  → OpenAIProvider.complete()
  → ProviderError + ProviderFailureMetadata
  → E9 runtime turn trace
  → failure_artifact.json
  → final smoke summary
```

The request-ID and structured API fields survive this path, while the stable smoke classification remains `LIVE_SMOKE_PROVIDER_FAILURE`. This preserves the distinction between smoke classification and provider diagnostic cause. Provider retry and accounting semantics remain unchanged.

## Validation status

The targeted mapping, redaction, strict-contract, runtime propagation, and smoke-evidence tests pass offline. Full repository validation also passes: `500 passed`, Ruff check/format, mypy, `make check`, `make a1-eval-check`, and `pre-commit --all-files`. No CI result is claimed until the focused commits are pushed and CI completes.

No connectivity probe, OpenAI request, Luna call, judge, official scenario, or live smoke was run.

```text
live agent calls: 0
OpenAI outbound attempts: 0
Luna calls: 0
judge calls: 0
live smoke scenarios: 0
official benchmark scenarios: 0
```
