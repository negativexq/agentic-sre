.PHONY: install lint typecheck test check demo serve-local \
	itbench-setup itbench-index eval-dev eval-test \
	images cluster-up build-images deploy load status ui inject-bad-rollout recover rbac-check \
	cluster-down precommit offline-demo e2e-kind e2e-kind-clean release-check

PY := .venv/bin/python
CLI := .venv/bin/agentic-sre
RUN_ID := $(shell date -u +%Y%m%dT%H%M%SZ)
LOCAL_DB ?= sqlite:///.local/agentic-sre.db
NAMESPACE := sre-demo

# --- development -------------------------------------------------------------

install:
	python3.12 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e '.[dev]'

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

typecheck:
	$(PY) -m mypy apps packages tests scripts

test:
	$(PY) -m pytest

check: lint typecheck test

precommit:
	MYPY_CACHE_DIR=/tmp/agentic-sre-mypy-cache $(PY) -m pre_commit run --all-files

offline-demo:
	$(CLI) demo --json > /dev/null

# --- product without a cluster -----------------------------------------------

demo:
	$(CLI) demo --html .local/demo/diagnosis.html

serve-local:
	mkdir -p .local
	DATABASE_URL=$(LOCAL_DB) $(PY) -m alembic upgrade head
	DATABASE_URL=$(LOCAL_DB) $(CLI) serve

# --- benchmark (ITBench-Lite, offline) ---------------------------------------

ITBENCH_WORKERS ?= 16

itbench-setup:
	$(PY) scripts/itbench_lite_setup.py --workers $(ITBENCH_WORKERS)

itbench-index:
	$(PY) scripts/itbench_lite_build_index.py

# EVAL_FLAGS=--llm adds the LLM investigator (needs SRE_LLM_* and OPENAI_API_KEY).
EVAL_FLAGS ?=

eval-dev:
	$(CLI) eval --split dev $(EVAL_FLAGS) --out .local/runs/dev-$(RUN_ID)

# Test-split numbers are published once per tagged release (see evals/README.md).
eval-test:
	test -z "$$(git status --porcelain)"
	git describe --exact-match --tags HEAD
	$(CLI) eval --split test --confirm-test $(EVAL_FLAGS) --out .local/runs/test-$$(git describe --tags)$(if $(EVAL_FLAGS),-llm)-$(RUN_ID)

# --- live demo on kind ---------------------------------------------------------

cluster-up:
	kind create cluster --config infra/kubernetes/kind-config.yaml

IMAGES := control-plane migrator order-service payment-service order-worker

images:
	for image in $(IMAGES); do \
		docker build -f infra/docker/Dockerfile --target $$image -t agentic-sre/$$image:dev . || exit 1; \
	done

build-images: images
	for image in $(IMAGES); do \
		kind load docker-image agentic-sre/$$image:dev --name agentic-sre || exit 1; \
	done

deploy: build-images
	kubectl apply -f infra/kubernetes/namespace.yaml
	kubectl apply -f infra/kubernetes/observability.yaml
	kubectl apply -f infra/kubernetes/tools-rbac.yaml
	kubectl apply -f infra/kubernetes/workload.yaml
	kubectl apply -f infra/kubernetes/dependencies.yaml
	kubectl rollout status deployment/kafka -n $(NAMESPACE) --timeout=300s
	kubectl rollout status deployment/postgres -n $(NAMESPACE) --timeout=180s
	ready=no; for attempt in $$(seq 1 60); do if kubectl exec -n $(NAMESPACE) deployment/kafka -- /opt/kafka/bin/kafka-topics.sh --list --bootstrap-server localhost:9092 >/dev/null 2>&1; then ready=yes; break; fi; sleep 2; done; test "$$ready" = yes
	kubectl exec -n $(NAMESPACE) deployment/kafka -- /opt/kafka/bin/kafka-topics.sh --create --if-not-exists --topic orders.created --bootstrap-server localhost:9092
	kubectl delete job db-migration -n $(NAMESPACE) --ignore-not-found
	kubectl apply -f infra/kubernetes/db-migration.yaml
	kubectl wait --for=condition=complete job/db-migration -n $(NAMESPACE) --timeout=180s
	kubectl apply -f infra/kubernetes/control-plane.yaml
	# Restart only the app images; restarting Kafka would drop the in-memory topic.
	kubectl rollout restart deployment/order-service deployment/payment-service deployment/order-worker deployment/control-plane -n $(NAMESPACE)
	for name in order-service payment-service order-worker control-plane; do \
		kubectl rollout status deployment/$$name -n $(NAMESPACE) --timeout=180s || exit 1; \
	done
	for name in otel-collector prometheus loki tempo alertmanager grafana; do \
		kubectl rollout status deployment/$$name -n observability --timeout=180s || exit 1; \
	done

load:
	kubectl port-forward -n $(NAMESPACE) svc/order-service 8000:8000 >/tmp/agentic-sre-order.log 2>&1 & pid=$$!; \
	trap 'kill "$$pid" 2>/dev/null || true' EXIT; sleep 2; \
	$(PY) -m workload.load_generator --base-url http://localhost:8000 --rate 10 --duration 300 --seed 42

ui:
	@echo "Agentic SRE UI: http://localhost:8080"
	kubectl port-forward -n $(NAMESPACE) svc/control-plane 8080:8000

# A bad rollout: payment requests start taking 2.5 s. The control plane records
# the change, Alertmanager fires on latency, and the incident is diagnosed.
inject-bad-rollout:
	kubectl set env deployment/payment-service -n $(NAMESPACE) FAULT_PAYMENT_DELAY_MS=2500
	kubectl rollout status deployment/payment-service -n $(NAMESPACE) --timeout=120s

recover:
	kubectl rollout undo deployment/payment-service -n $(NAMESPACE)
	kubectl rollout status deployment/payment-service -n $(NAMESPACE) --timeout=120s

rbac-check:
	sa=system:serviceaccount:$(NAMESPACE):agentic-sre-reader; \
	test "$$(kubectl auth can-i list deployments --as=$$sa -n $(NAMESPACE))" = yes && \
	test "$$(kubectl auth can-i watch configmaps --as=$$sa -n $(NAMESPACE))" = yes && \
	test "$$(kubectl auth can-i get secrets --as=$$sa -n $(NAMESPACE))" = no && \
	test "$$(kubectl auth can-i patch deployments --as=$$sa -n $(NAMESPACE))" = no && \
	test "$$(kubectl auth can-i delete pods --as=$$sa -n $(NAMESPACE))" = no && \
	echo "control plane RBAC: read-only PASS"

status:
	kubectl get pods,svc -n $(NAMESPACE)
	kubectl get pods,svc -n observability

cluster-down:
	kind delete cluster --name agentic-sre

e2e-kind:
	$(PY) scripts/kind_e2e.py

e2e-kind-clean:
	kind delete cluster --name agentic-sre

release-check: check precommit offline-demo e2e-kind
