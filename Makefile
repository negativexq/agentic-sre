.PHONY: install lint typecheck test check agent-check agent-smoke benchmark-offline a1-eval-check a1-generalization-check benchmark-harness-check model-smoke-live agent-smoke-live benchmark-live cluster-up build-images deploy load status cluster-down observability-check evidence-check rbac-check release-check release-check-live

LIVE_BUDGET_FILE ?= .local/v0.2.0-live-budget.json
HARNESS_SCENARIOS ?=

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
	( trap 'kill "$$child" 2>/dev/null || true; exit 0' TERM INT EXIT; while true; do kubectl port-forward -n observability svc/prometheus 19090:9090 & child=$$!; wait "$$child"; sleep 1; done ) >/tmp/agentic-sre-prometheus-forward.log 2>&1 & prom_pid=$$!; \
	kubectl port-forward -n observability svc/loki 19300:3100 >/tmp/agentic-sre-loki-forward.log 2>&1 & loki_pid=$$!; \
	kubectl port-forward -n observability svc/tempo 19320:3200 >/tmp/agentic-sre-tempo-forward.log 2>&1 & tempo_pid=$$!; \
	kubectl port-forward -n sre-demo svc/control-plane 18081:8000 >/tmp/agentic-sre-control-forward.log 2>&1 & control_pid=$$!; \
	( trap 'kill "$$child" 2>/dev/null || true; exit 0' TERM INT EXIT; while true; do kubectl port-forward -n sre-demo svc/order-service 18000:8000 & child=$$!; wait "$$child"; sleep 1; done ) >/tmp/agentic-sre-order-forward.log 2>&1 & order_pid=$$!; \
	( trap 'kill "$$child" 2>/dev/null || true; exit 0' TERM INT EXIT; while true; do kubectl port-forward -n sre-demo svc/payment-service 18001:8000 & child=$$!; wait "$$child"; sleep 1; done ) >/tmp/agentic-sre-payment-forward.log 2>&1 & payment_pid=$$!; \
	trap 'kill "$$prom_pid" "$$loki_pid" "$$tempo_pid" "$$control_pid" "$$order_pid" "$$payment_pid" 2>/dev/null || true' EXIT; \
	sleep 10; .venv/bin/python scripts/a1_generalization_check.py

benchmark-harness-check:
	kubectl port-forward -n observability svc/prometheus 19090:9090 >/tmp/agentic-sre-prometheus-forward.log 2>&1 & prom_pid=$$!; \
	kubectl port-forward -n observability svc/loki 19300:3100 >/tmp/agentic-sre-loki-forward.log 2>&1 & loki_pid=$$!; \
	kubectl port-forward -n observability svc/tempo 19320:3200 >/tmp/agentic-sre-tempo-forward.log 2>&1 & tempo_pid=$$!; \
	kubectl port-forward -n sre-demo svc/control-plane 18081:8000 >/tmp/agentic-sre-control-forward.log 2>&1 & control_pid=$$!; \
	( trap 'kill "$$child" 2>/dev/null || true; exit 0' TERM INT EXIT; while true; do kubectl port-forward -n sre-demo svc/order-service 18000:8000 & child=$$!; wait "$$child"; sleep 1; done ) >/tmp/agentic-sre-order-forward.log 2>&1 & order_pid=$$!; \
	( trap 'kill "$$child" 2>/dev/null || true; exit 0' TERM INT EXIT; while true; do kubectl port-forward -n sre-demo svc/payment-service 18001:8000 & child=$$!; wait "$$child"; sleep 1; done ) >/tmp/agentic-sre-payment-forward.log 2>&1 & payment_pid=$$!; \
	trap 'kill "$$prom_pid" "$$loki_pid" "$$tempo_pid" "$$control_pid" "$$order_pid" "$$payment_pid" 2>/dev/null || true' EXIT; \
	sleep 10; SRE_HARNESS_SCENARIOS="$(HARNESS_SCENARIOS)" .venv/bin/python scripts/benchmark_harness_check.py

model-smoke-live:
	test -n "$$OPENAI_API_KEY"
	SRE_LIVE_MODEL_ENABLED=true SRE_LIVE_MODEL_BUDGET_FILE="$(LIVE_BUDGET_FILE)" .venv/bin/python scripts/model_smoke_live.py

agent-smoke-live:
	test -n "$$OPENAI_API_KEY"
	kubectl port-forward -n observability svc/prometheus 19090:9090 >/tmp/agentic-sre-prometheus-forward.log 2>&1 & prom_pid=$$!; \
	kubectl port-forward -n observability svc/loki 19300:3100 >/tmp/agentic-sre-loki-forward.log 2>&1 & loki_pid=$$!; \
	kubectl port-forward -n observability svc/tempo 19320:3200 >/tmp/agentic-sre-tempo-forward.log 2>&1 & tempo_pid=$$!; \
	kubectl port-forward -n sre-demo svc/control-plane 18081:8000 >/tmp/agentic-sre-control-forward.log 2>&1 & control_pid=$$!; \
	trap 'kill "$$prom_pid" "$$loki_pid" "$$tempo_pid" "$$control_pid" 2>/dev/null || true' EXIT; \
	sleep 3; SRE_LIVE_MODEL_ENABLED=true SRE_LIVE_MODEL_BUDGET_FILE="$(LIVE_BUDGET_FILE)" .venv/bin/python scripts/live_agent_smoke.py

benchmark-live:
	test -n "$$OPENAI_API_KEY"
	kubectl port-forward -n observability svc/prometheus 19090:9090 >/tmp/agentic-sre-prometheus-forward.log 2>&1 & prom_pid=$$!; \
	kubectl port-forward -n observability svc/loki 19300:3100 >/tmp/agentic-sre-loki-forward.log 2>&1 & loki_pid=$$!; \
	kubectl port-forward -n observability svc/tempo 19320:3200 >/tmp/agentic-sre-tempo-forward.log 2>&1 & tempo_pid=$$!; \
	kubectl port-forward -n sre-demo svc/control-plane 18081:8000 >/tmp/agentic-sre-control-forward.log 2>&1 & control_pid=$$!; \
	( trap 'kill "$$child" 2>/dev/null || true; exit 0' TERM INT EXIT; while true; do kubectl port-forward -n sre-demo svc/order-service 18000:8000 & child=$$!; wait "$$child"; sleep 1; done ) >/tmp/agentic-sre-order-forward.log 2>&1 & order_pid=$$!; \
	( trap 'kill "$$child" 2>/dev/null || true; exit 0' TERM INT EXIT; while true; do kubectl port-forward -n sre-demo svc/payment-service 18001:8000 & child=$$!; wait "$$child"; sleep 1; done ) >/tmp/agentic-sre-payment-forward.log 2>&1 & payment_pid=$$!; \
	trap 'kill "$$prom_pid" "$$loki_pid" "$$tempo_pid" "$$control_pid" "$$order_pid" "$$payment_pid" 2>/dev/null || true' EXIT; \
	sleep 3; SRE_LIVE_MODEL_ENABLED=true SRE_LIVE_MODEL_BUDGET_FILE="$(LIVE_BUDGET_FILE)" .venv/bin/python scripts/live_benchmark.py

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
