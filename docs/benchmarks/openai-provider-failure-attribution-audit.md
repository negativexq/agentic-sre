# OpenAI provider failure attribution audit

This is an offline audit and repair record. Historical `ITB-CONTROL-LIVE-SMOKE-002` remains immutable: it made one provider invocation and one outbound attempt, received no model response, and recorded only `PROVIDER_UNAVAILABLE`.

## Source findings

| Path / symbol | Status | Finding and repair |
| --- | --- | --- |
| `packages/provider/openai.py` / `OpenAIProvider.complete` | CONFIRMED — FIXED | The old catch path collapsed final SDK/API failures to a generic `PROVIDER_UNAVAILABLE`. The provider now maps status, timeout, and connection classes to stable codes and creates bounded failure metadata. |
| `packages/provider/contracts.py` / `ProviderError` | CONFIRMED — FIXED | There was no separate transport/API failure contract. `ProviderFailureMetadata` now carries only safe, bounded diagnostic fields and remains separate from successful `ResponseEnvelopeMetadata`. |
| `packages/evals/itbench/e9_runtime.py` / `E9InvestigationRuntime.run` | CONFIRMED — FIXED | A provider-failed turn previously stored only the code string. It now stores the code plus sanitized typed metadata. |
| `packages/evals/itbench/live_smoke_execution.py` / failure summary path | CONFIRMED — FIXED | Summary and failure-artifact paths now preserve whitelisted provider diagnostics instead of only the generic runtime message. |
| `packages/provider/openai.py` / `_safe_error_message` | PARTIALLY CONFIRMED — FIXED | The old `str(error)` fallback was unsafe at a persistence boundary. The repaired helper accepts only an explicit SDK message, redacts bearer/API-key patterns, and bounds it to 500 characters; absent a safe field it records `null`. |

The historical result cannot reveal whether SMOKE-002 was a 400 schema/request error, an authentication/permission error, a missing resource, a transport error, or a service error. The old exception metadata was discarded before runtime and smoke persistence. This repair does not infer or rewrite that historical cause.

## Mapping contract

| Condition | Stable `ProviderErrorCode` | Metadata category |
| --- | --- | --- |
| HTTP 400 | `BAD_REQUEST` | `BAD_REQUEST` |
| HTTP 401 | `AUTHENTICATION_FAILED` | `AUTHENTICATION` |
| HTTP 403 | `PERMISSION_DENIED` | `PERMISSION_DENIED` |
| HTTP 404 | `RESOURCE_NOT_FOUND` | `NOT_FOUND` |
| HTTP 422 | `UNPROCESSABLE_REQUEST` | `BAD_REQUEST` |
| HTTP 429 | `RATE_LIMITED` | `RATE_LIMIT` |
| HTTP 500+ | `PROVIDER_UNAVAILABLE` | `SERVER_ERROR` |
| SDK timeout-like exception | `PROVIDER_TIMEOUT` | `TIMEOUT` |
| SDK connection-like exception | `PROVIDER_UNAVAILABLE` | `CONNECTION` |
| Other/statusless failure | `PROVIDER_UNAVAILABLE` | `API_STATUS_ERROR` or `UNKNOWN` |

The existing zero-retry and one-outbound-attempt accounting behavior is unchanged.

## Safe propagation

The only persisted provider-failure fields are:

`provider`, `exception_class`, `category`, `http_status_code`, `api_error_type`, `api_error_code`, `api_error_param`, `request_id`, and sanitized `message_summary`.

The raw exception, request/response payloads, headers, credentials, prompts, and raw body are excluded. The path is:

```text
SDK-like exception
  → OpenAIProvider failure mapping
  → ProviderError.failure_metadata
  → E9 turn trace provider_error
  → failure_artifact.json
  → final smoke result provider_failure
```

Offline tests cover all required HTTP status classes, timeout/connection classification, request IDs, API body fields, strict metadata shape, redaction, and end-to-end smoke evidence propagation.

## Scope and accounting

No connectivity probe, model request, judge, official scenario, or live smoke was run. The historical SMOKE-002 artifact was not modified.

```text
live agent calls: 0
OpenAI outbound attempts: 0
Luna calls: 0
judge calls: 0
live smoke scenarios: 0
official benchmark scenarios: 0
```
