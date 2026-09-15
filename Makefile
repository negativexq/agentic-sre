.PHONY: install lint typecheck test check agent-check agent-smoke benchmark-offline a1-eval-check a1-generalization-check benchmark-harness-check a1-r4-fixture-check a1-fixture-check a1-harness-check a1-harness-repeatability a1-order-independence-check a1-r4-smoke-fixture-check a1-r4-forward-reconnect-check itbench-lite-setup itbench-lite-index itbench-lite-check itbench-e2-check itbench-e2-request-build-check itbench-e5-check itbench-e7-check model-smoke-live agent-smoke-live benchmark-live a1-live-smoke a1-live-benchmark cluster-up build-images deploy load status cluster-down observability-check evidence-check rbac-check release-check release-check-live

LIVE_BUDGET_FILE ?= .local/v0.2.0-live-budget.json
HARNESS_SCENARIOS ?=
A1_LIVE_MANIFEST ?= docs/benchmarks/a1-r4-evaluation-manifest.json
A1_LIVE_SMOKE ?= docs/benchmarks/a1-r4-live-smoke.json
A1_LIVE_SMOKE_PHASE_LEDGER ?= .local/a1-r4-live-smoke/phases.jsonl
A1_LIVE_RESULT ?= docs/benchmarks/a1-r4-single-agent-live.json
A1_LIVE_RESULT_SHA ?= docs/benchmarks/a1-r4-single-agent-live.sha256
A1_LIVE_BUDGET_FILE ?= .local/a1-r4-single-agent-live-budget.json
A1_HARNESS_SUITE_REPEATS ?= 3
A1_R4_REPEATABILITY_MODE ?= all

install:
	python3.12 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -e '.[dev]'

lint:
	.venv/bin/python -m ruff check .
	.venv/bin/python -m ruff format --check .

typecheck:
	.venv/bin/python -m mypy apps packages tests scripts

test:
	.venv/bin/python -m pytest

check: lint typecheck test

agent-check:
	.venv/bin/python -m pytest tests/unit/test_provider.py tests/integration/test_investigation_runtime.py

agent-smoke: agent-check
	@echo "fake-provider agent smoke: PASS (live API calls: 0)"

benchmark-offline:
	.venv/bin/python scripts/offline_benchmark.py

a1-eval-check:
	.venv/bin/python scripts/a1_offline_check.py
	.venv/bin/python -m pytest tests/unit/test_a1_targets.py tests/unit/test_a1_graders.py

a1-generalization-check:
	.venv/bin/python scripts/live_forward_supervisor.py --profile a1 -- .venv/bin/python scripts/a1_generalization_check.py

benchmark-harness-check:
	.venv/bin/python scripts/live_forward_supervisor.py --profile a1 -- env SRE_HARNESS_SCENARIOS="$(HARNESS_SCENARIOS)" .venv/bin/python scripts/benchmark_harness_check.py

a1-r4-fixture-check:
	.venv/bin/python scripts/live_forward_supervisor.py --profile a1 -- env A1_R4_REPEATABILITY_MODE="$(A1_R4_REPEATABILITY_MODE)" .venv/bin/python scripts/a1_r4_fixture_repeatability.py

a1-fixture-check: A1_R4_REPEATABILITY_MODE=v007
a1-fixture-check: a1-r4-fixture-check

a1-harness-check: A1_HARNESS_SUITE_REPEATS=1
a1-harness-check: a1-harness-repeatability

a1-harness-repeatability:
	.venv/bin/python scripts/live_forward_supervisor.py --profile a1 -- env A1_HARNESS_SUITE_REPEATS="$(A1_HARNESS_SUITE_REPEATS)" .venv/bin/python scripts/a1_harness_repeatability.py

a1-order-independence-check:
	.venv/bin/python scripts/live_forward_supervisor.py --profile a1 -- .venv/bin/python scripts/a1_order_independence_check.py

a1-r4-smoke-fixture-check:
	.venv/bin/python scripts/live_forward_supervisor.py --profile a1 -- .venv/bin/python scripts/a1_r4_smoke_qualification.py

a1-r4-forward-reconnect-check:
	.venv/bin/python scripts/live_forward_supervisor.py --profile a1 -- .venv/bin/python scripts/a1_r4_forward_reconnect_check.py

itbench-lite-setup:
	.venv/bin/python scripts/itbench_lite_setup.py

itbench-lite-index:
	.venv/bin/python scripts/itbench_lite_build_index.py

itbench-lite-check:
	.venv/bin/python scripts/itbench_lite_check.py --report /tmp/itbench-lite-adapter-qualification-check.json

itbench-e2-check:
	.venv/bin/python scripts/itbench_e2_check.py

itbench-e2-request-build-check:
	.venv/bin/python scripts/itbench_e2_request_build_check.py

itbench-e5-check:
	.venv/bin/python scripts/itbench_e5_check.py

itbench-e6-check:
	.venv/bin/python scripts/itbench_e6_check.py

itbench-e7-check:
	.venv/bin/python scripts/itbench_e7_check.py

model-smoke-live:
	test -n "$$OPENAI_API_KEY"
	SRE_LIVE_MODEL_ENABLED=true SRE_LIVE_MODEL_BUDGET_FILE="$(LIVE_BUDGET_FILE)" .venv/bin/python scripts/model_smoke_live.py

agent-smoke-live:
	test -n "$$OPENAI_API_KEY"
	.venv/bin/python scripts/live_forward_supervisor.py --profile a1 -- env SRE_LIVE_MODEL_ENABLED=true SRE_LIVE_MODEL_BUDGET_FILE="$(LIVE_BUDGET_FILE)" .venv/bin/python scripts/live_agent_smoke.py

benchmark-live:
	test -n "$$OPENAI_API_KEY"
	.venv/bin/python scripts/live_forward_supervisor.py --profile a1 -- env SRE_LIVE_MODEL_ENABLED=true SRE_LIVE_MODEL_BUDGET_FILE="$(LIVE_BUDGET_FILE)" .venv/bin/python scripts/live_benchmark.py

a1-live-smoke:
	test -n "$$OPENAI_API_KEY"
	.venv/bin/python scripts/live_forward_supervisor.py --profile a1 -- env SRE_LIVE_MODEL_ENABLED=true SRE_MODEL=gpt-5.6-luna SRE_REASONING_EFFORT=none SRE_LIVE_MODEL_CALL_BUDGET=80 SRE_LIVE_MODEL_BUDGET_FILE="$(A1_LIVE_BUDGET_FILE)" SRE_A1_MANIFEST_PATH="$(A1_LIVE_MANIFEST)" SRE_A1_SMOKE_PATH="$(A1_LIVE_SMOKE)" SRE_A1_SMOKE_PHASE_LEDGER="$(A1_LIVE_SMOKE_PHASE_LEDGER)" SRE_A1_RESULT_PATH="$(A1_LIVE_RESULT)" SRE_A1_RESULT_SHA_PATH="$(A1_LIVE_RESULT_SHA)" SRE_A1_LEDGER_PATH="$(A1_LIVE_BUDGET_FILE)" .venv/bin/python scripts/a1_live_benchmark.py smoke

a1-live-benchmark:
	test -n "$$OPENAI_API_KEY"
	.venv/bin/python scripts/live_forward_supervisor.py --profile a1 -- env SRE_LIVE_MODEL_ENABLED=true SRE_MODEL=gpt-5.6-luna SRE_REASONING_EFFORT=none SRE_LIVE_MODEL_CALL_BUDGET=80 SRE_LIVE_MODEL_BUDGET_FILE="$(A1_LIVE_BUDGET_FILE)" SRE_A1_MANIFEST_PATH="$(A1_LIVE_MANIFEST)" SRE_A1_SMOKE_PATH="$(A1_LIVE_SMOKE)" SRE_A1_RESULT_PATH="$(A1_LIVE_RESULT)" SRE_A1_RESULT_SHA_PATH="$(A1_LIVE_RESULT_SHA)" SRE_A1_LEDGER_PATH="$(A1_LIVE_BUDGET_FILE)" .venv/bin/python scripts/a1_live_benchmark.py benchmark

cluster-up:
	kind create cluster --config infra/kubernetes/kind-config.yaml

build-images:
	docker build -f infra/docker/order-service.Dockerfile -t agentic-sre/order-service:dev .
	docker build -f infra/docker/payment-service.Dockerfile -t agentic-sre/payment-service:dev .
	docker build -f infra/docker/order-worker.Dockerfile -t agentic-sre/order-worker:dev .
	docker build -f infra/docker/control-plane.Dockerfile -t agentic-sre/control-plane:dev .
	docker build -f infra/docker/migrator.Dockerfile -t agentic-sre/migrator:dev .
	kind load docker-image agentic-sre/order-service:dev --name agentic-sre
	kind load docker-image agentic-sre/payment-service:dev --name agentic-sre
	kind load docker-image agentic-sre/order-worker:dev --name agentic-sre
	kind load docker-image agentic-sre/control-plane:dev --name agentic-sre
	kind load docker-image agentic-sre/migrator:dev --name agentic-sre

deploy: build-images
	rm -f /tmp/agentic-sre-existing-workloads
	if kubectl get deployment/order-service deployment/payment-service deployment/order-worker deployment/control-plane -n sre-demo >/dev/null 2>&1; then touch /tmp/agentic-sre-existing-workloads; fi
	kubectl apply -f infra/kubernetes/namespace.yaml
	kubectl apply -f infra/kubernetes/observability.yaml
	kubectl apply -f infra/kubernetes/workload.yaml
	kubectl apply -f infra/kubernetes/control-plane.yaml
	kubectl apply -f infra/kubernetes/dependencies.yaml
	kubectl rollout status deployment/kafka -n sre-demo --timeout=300s
	ready=no; for attempt in $$(seq 1 60); do if kubectl exec -n sre-demo deployment/kafka -- /opt/kafka/bin/kafka-topics.sh --list --bootstrap-server localhost:9092 >/dev/null 2>&1; then ready=yes; break; fi; sleep 2; done; test "$$ready" = yes
	kubectl exec -n sre-demo deployment/kafka -- /opt/kafka/bin/kafka-topics.sh --create --if-not-exists --topic orders.created --bootstrap-server localhost:9092
	if test -f /tmp/agentic-sre-existing-workloads; then kubectl rollout restart deployment/order-service deployment/payment-service deployment/order-worker deployment/control-plane -n sre-demo; fi
	rm -f /tmp/agentic-sre-existing-workloads
	kubectl rollout restart deployment/prometheus -n observability
	kubectl rollout status deployment/order-service -n sre-demo --timeout=120s
	kubectl rollout status deployment/payment-service -n sre-demo --timeout=120s
	kubectl rollout status deployment/order-worker -n sre-demo --timeout=120s
	kubectl rollout status deployment/control-plane -n sre-demo --timeout=120s
	kubectl delete job db-migration -n sre-demo --ignore-not-found
	kubectl apply -f infra/kubernetes/db-migration.yaml
	kubectl wait --for=condition=complete job/db-migration -n sre-demo --timeout=120s
	kubectl apply -f infra/kubernetes/tools-rbac.yaml
	kubectl rollout status deployment/otel-collector -n observability --timeout=180s
	kubectl rollout status deployment/prometheus -n observability --timeout=180s
	kubectl rollout status deployment/loki -n observability --timeout=180s
	kubectl rollout status deployment/tempo -n observability --timeout=180s
	kubectl rollout status deployment/alertmanager -n observability --timeout=180s
	kubectl rollout status deployment/grafana -n observability --timeout=180s

load:
	kubectl port-forward -n sre-demo svc/order-service 8000:8000 >/tmp/agentic-sre-port-forward.log 2>&1 & port_pid=$$!; trap 'kill "$$port_pid" 2>/dev/null || true' EXIT; sleep 2; .venv/bin/python -m workload.load_generator --base-url http://localhost:8000 --rate 10 --duration 1 --seed 42

observability-check:
	kubectl port-forward -n observability svc/prometheus 19090:9090 >/tmp/agentic-sre-prometheus-forward.log 2>&1 & prom_pid=$$!; \
	kubectl port-forward -n observability svc/loki 19300:3100 >/tmp/agentic-sre-loki-forward.log 2>&1 & loki_pid=$$!; \
	kubectl port-forward -n observability svc/tempo 19320:3200 >/tmp/agentic-sre-tempo-forward.log 2>&1 & tempo_pid=$$!; \
	kubectl port-forward -n observability svc/alertmanager 19093:9093 >/tmp/agentic-sre-alertmanager-forward.log 2>&1 & alertmanager_pid=$$!; \
	kubectl port-forward -n observability svc/grafana 13000:3000 >/tmp/agentic-sre-grafana-forward.log 2>&1 & grafana_pid=$$!; \
	kubectl port-forward -n sre-demo svc/order-service 18000:8000 >/tmp/agentic-sre-order-forward.log 2>&1 & order_pid=$$!; \
	trap 'kill "$$prom_pid" "$$loki_pid" "$$tempo_pid" "$$alertmanager_pid" "$$grafana_pid" "$$order_pid" 2>/dev/null || true' EXIT; \
	sleep 3; .venv/bin/python scripts/observability_check.py

evidence-check:
	kubectl port-forward -n observability svc/prometheus 19090:9090 >/tmp/agentic-sre-prometheus-forward.log 2>&1 & prom_pid=$$!; \
	kubectl port-forward -n sre-demo svc/control-plane 18081:8000 >/tmp/agentic-sre-control-forward.log 2>&1 & control_pid=$$!; \
	kubectl port-forward -n sre-demo svc/postgres 15432:5432 >/tmp/agentic-sre-postgres-forward.log 2>&1 & postgres_pid=$$!; \
	trap 'kill "$$prom_pid" "$$control_pid" "$$postgres_pid" 2>/dev/null || true' EXIT; \
	sleep 3; .venv/bin/python scripts/live_evidence_check.py

rbac-check:
	test "$$(kubectl auth can-i get pods --as=system:serviceaccount:sre-demo:investigation-tools -n sre-demo)" = yes
	test "$$(kubectl auth can-i list pods --as=system:serviceaccount:sre-demo:investigation-tools -n sre-demo)" = yes
	test "$$(kubectl auth can-i watch pods --as=system:serviceaccount:sre-demo:investigation-tools -n sre-demo)" = yes
	test "$$(kubectl auth can-i create deployments --as=system:serviceaccount:sre-demo:investigation-tools -n sre-demo)" = no
	test "$$(kubectl auth can-i patch deployments --as=system:serviceaccount:sre-demo:investigation-tools -n sre-demo)" = no
	test "$$(kubectl auth can-i delete pods --as=system:serviceaccount:sre-demo:investigation-tools -n sre-demo)" = no
	echo "investigation RBAC: read-only PASS"

status:
	kubectl get pods,svc -n sre-demo
	kubectl get pods,svc -n observability

cluster-down:
	kind delete cluster --name agentic-sre

release-check: check agent-check benchmark-offline observability-check evidence-check rbac-check
	.venv/bin/python -m pytest tests/e2e
	.venv/bin/python -m pytest tests/unit/test_state_machine.py --cov=packages.incident.state_machine --cov-report=term-missing --cov-fail-under=100

release-check-live:
	$(MAKE) release-check
	test -f docs/benchmarks/v0.2.0-live-smoke.json
	test -f docs/benchmarks/v0.2.0-single-agent-live.json
	.venv/bin/python scripts/release_check_live.py
