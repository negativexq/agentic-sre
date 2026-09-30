.PHONY: install lock lint typecheck test test-pg check demo serve-local \
	itbench-setup itbench-index eval-dev eval-test benchmark-qualify \
	images cluster-up build-images deploy load status ui inject-bad-rollout recover rbac-check \
	lab-images chaos-mesh-install chaos-mesh-uninstall lab-up lab-check lab-check-once lab-pki connector-deploy connector-check cp-up cp-stop cp-down cp-reset cluster-down precommit offline-demo e2e-kind e2e-kind-clean m18a-live-validate release-check \
	verify-release-provenance product-bench-dev

PY := .venv/bin/python
CLI := .venv/bin/agentic-sre
UV ?= uv
RUN_ID := $(shell date -u +%Y%m%dT%H%M%SZ)
LOCAL_DB ?= sqlite:///.local/agentic-sre.db
NAMESPACE := sre-demo

# --- development -------------------------------------------------------------

install:
	$(UV) sync --locked --extra dev

lock:
	$(UV) lock

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

typecheck:
	$(PY) -m mypy apps packages tests scripts

test:
	$(PY) -m pytest

check: lint typecheck test

# Opt-in PostgreSQL integration tests on a throwaway container.
PG_TEST_IMAGE ?= postgres:16-alpine
PG_TEST_CONTAINER := agentic-sre-test-pg
PG_TEST_PORT ?= 55432

test-pg:
	@docker rm -f $(PG_TEST_CONTAINER) >/dev/null 2>&1 || true
	@trap 'docker rm -f $(PG_TEST_CONTAINER) >/dev/null 2>&1' EXIT; \
	docker run -d --rm --name $(PG_TEST_CONTAINER) -e POSTGRES_PASSWORD=postgres \
		-p 127.0.0.1:$(PG_TEST_PORT):5432 $(PG_TEST_IMAGE) >/dev/null; \
	for _ in $$(seq 1 60); do \
		docker exec $(PG_TEST_CONTAINER) pg_isready -h 127.0.0.1 -U postgres >/dev/null 2>&1 && break; \
		sleep 1; \
	done; \
	TEST_POSTGRES_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:$(PG_TEST_PORT)/postgres \
		$(PY) -m pytest -m postgres

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

# --- operator console (apps/web) ---------------------------------------------

web-install:
	cd apps/web && npm install

web-build:
	cd apps/web && npm run build

web-dev:
	cd apps/web && npm run dev

# Build the console, seed a realistic incident mix, and serve everything from
# the control plane at http://localhost:8000/app — no cluster required.
console: web-build
	mkdir -p .local
	DATABASE_URL=$(LOCAL_DB) $(PY) -m alembic upgrade head
	DATABASE_URL=$(LOCAL_DB) $(PY) scripts/seed_console_demo.py
	@echo "Operator console: http://localhost:8000/app"
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

benchmark-qualify:
	$(CLI) benchmark-qualify --split dev --out .local/diagnostics/qualification-$(RUN_ID)

verify-release-provenance:
	$(PY) scripts/verify_release_provenance.py evals/results/$$(git describe --tags --abbrev=0)/test

# --- live demo on kind ---------------------------------------------------------

cluster-up:
	kind create cluster --config infra/kubernetes/kind-config.yaml

IMAGES := control-plane migrator connector order-service payment-service order-worker

# Chaos Mesh for the lab (infra/kubernetes/chaos-mesh/pins.yaml): the vendored chart, containerd
# values, one controller. `lab-images` has the node pull the pinned images and refuses any whose digest
# differs from pins.yaml, so that a run never waits on a registry. (`kind load` cannot import these
# multi-platform images from the local Docker store: "content digest not found".)
CHAOS_DIR := infra/kubernetes/chaos-mesh
CHAOS_TAG := v2.8.4

lab-images:
	$(PY) -c "import sys, yaml; [print(i['name'], i['tag'], i['digest']) for i in yaml.safe_load(open(sys.argv[1]))['images']]" $(CHAOS_DIR)/pins.yaml | while read image tag digest; do \
		docker exec agentic-sre-control-plane crictl pull $$image:$$tag >/dev/null || exit 1; \
		docker exec agentic-sre-control-plane crictl inspecti $$image:$$tag | grep -q "$$digest" \
			|| { echo "digest mismatch for $$image:$$tag (pinned $$digest)"; exit 1; }; \
	done

chaos-mesh-install:
	helm upgrade --install chaos-mesh $(CHAOS_DIR)/chaos-mesh-$(CHAOS_TAG:v%=%).tgz \
		--namespace chaos-mesh --create-namespace -f $(CHAOS_DIR)/values.yaml --wait

chaos-mesh-uninstall:
	helm uninstall chaos-mesh --namespace chaos-mesh

# The testbed lab (docs/architecture/testbed-lab-design.md section 8): everything except the control
# plane, which runs outside it. Destructive only in that `cluster-up` creates the cluster; it never
# deletes one. Postgres converges its own schema (the `migrate` sidecar), Kafka its own topic.
# `KUBECTL` selects the context: make lab-check KUBECTL="kubectl --context kind-agentic-sre".
KUBECTL ?= kubectl

lab-up: cluster-up build-images lab-images
	$(KUBECTL) apply -f infra/kubernetes/namespace.yaml
	$(KUBECTL) apply -f infra/kubernetes/observability.yaml
	$(KUBECTL) apply -f infra/kubernetes/tools-rbac.yaml
	$(KUBECTL) apply -f infra/kubernetes/workload.yaml
	$(KUBECTL) apply -f infra/kubernetes/dependencies.yaml
	$(KUBECTL) apply -f infra/kubernetes/lab-control.yaml
	for name in kafka postgres redis order-service payment-service order-worker; do \
		$(KUBECTL) rollout status deployment/$$name -n $(NAMESPACE) --timeout=300s || exit 1; \
	done
	$(KUBECTL) rollout status deployment/isolated-echo -n lab-control --timeout=180s
	for name in otel-collector prometheus kube-state-metrics loki tempo alertmanager grafana; do \
		$(KUBECTL) rollout status deployment/$$name -n observability --timeout=180s || exit 1; \
	done
	$(MAKE) chaos-mesh-install
	$(MAKE) lab-check

# The scriptable part of the bring-up gate; the chaos smoke, the alert path and the node restart are
# run by hand and recorded (design section 8, step 5). The topic and the schema converge on their own
# (sidecars), so the gate retries for up to two minutes and reports the last failure; every single
# check still fails if kubectl itself fails.
lab-check:
	@for attempt in $$(seq 1 24); do \
		out=$$($(MAKE) --no-print-directory lab-check-once 2>&1) && { echo "$$out"; exit 0; }; \
		sleep 5; \
	done; echo "$$out"; echo "lab check: FAIL after 2 minutes"; exit 1

lab-check-once:
	@pods=$$($(KUBECTL) get pods -A --no-headers) || { echo "kubectl could not list pods"; exit 1; }; \
	bad=$$(echo "$$pods" | awk '$$4 != "Running" && $$4 != "Completed"'); \
	test -n "$$pods" || { echo "no pods found"; exit 1; }; \
	test -z "$$bad" || { echo "pods not running:"; echo "$$bad"; exit 1; }
	@topics=$$($(KUBECTL) exec -n $(NAMESPACE) deployment/kafka -c kafka -- /opt/kafka/bin/kafka-topics.sh --list --bootstrap-server localhost:9092) \
		|| { echo "could not list Kafka topics"; exit 1; }; \
	echo "$$topics" | grep -qx orders.created || { echo "topic orders.created is missing"; exit 1; }
	@schema=$$($(KUBECTL) exec -n $(NAMESPACE) deployment/postgres -c postgres -- psql -U postgres -d agentic_sre -Atc "select to_regclass('public.alembic_version') is not null") \
		|| { echo "could not query the database"; exit 1; }; \
	test "$$schema" = t || { echo "the database schema has not been migrated"; exit 1; }
	@crds=$$($(KUBECTL) get crd -o name) || { echo "could not list CRDs"; exit 1; }; \
	test "$$(echo "$$crds" | grep -c chaos-mesh.org)" = 23 || { echo "expected 23 Chaos Mesh CRDs"; exit 1; }
	@if $(KUBECTL) exec -n $(NAMESPACE) deployment/order-service -- python -c "import urllib.request; urllib.request.urlopen('http://isolated-echo.lab-control:8080', timeout=3)" >/dev/null 2>&1; then \
		echo "the isolated workload is reachable from $(NAMESPACE)"; exit 1; fi
	@echo "lab check: PASS"

# --- the control plane outside the lab (docs/architecture/testbed-control-plane-design.md) ---------
#
# The control plane and its own Postgres run on the host; the Connector runs in the lab and dials
# host.docker.internal:8443. Certificates and the webhook token are generated under .local/lab and never
# committed; only the connector's material enters the cluster. `cp-down` keeps the Postgres volume.
LAB_DIR := .local/lab
CP_PG := agentic-sre-cp-pg
CP_PG_VOLUME := agentic-sre-cp-pgdata
CP_PG_PORT ?= 55433
CP_DB_NAME ?= agentic_sre
CP_DB := postgresql+psycopg://postgres:postgres@127.0.0.1:$(CP_PG_PORT)/$(CP_DB_NAME)
CONNECTOR_SA := system:serviceaccount:connector:connector

lab-pki:
	$(PY) -m packages.connector.lab pki --out $(LAB_DIR)/pki

connector-deploy: lab-pki
	$(KUBECTL) apply -f infra/kubernetes/chaos-mesh-rbac.yaml
	$(KUBECTL) apply -f infra/kubernetes/connector.yaml
	test -f $(LAB_DIR)/webhook-token || { umask 077; head -c 24 /dev/urandom | base64 | tr -d '/+=\n' > $(LAB_DIR)/webhook-token; }
	$(KUBECTL) -n connector create secret generic connector-tls \
		--from-file=client.crt=$(LAB_DIR)/pki/client.crt --from-file=client.key=$(LAB_DIR)/pki/client.key \
		--from-file=ca.crt=$(LAB_DIR)/pki/ca.crt --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) -n connector create secret generic connector-webhook \
		--from-file=token=$(LAB_DIR)/webhook-token --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(PY) -m packages.connector.lab alertmanager --source infra/observability/alertmanager.yml \
		--url http://connector-webhook.connector.svc.cluster.local:9095/webhook \
		--token-file $(LAB_DIR)/webhook-token > $(LAB_DIR)/alertmanager.yml
	$(KUBECTL) -n observability create configmap alertmanager-config \
		--from-file=alertmanager.yml=$(LAB_DIR)/alertmanager.yml --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) -n observability rollout restart deployment/alertmanager
	$(KUBECTL) -n connector rollout restart deployment/connector
	$(KUBECTL) -n connector rollout status deployment/connector --timeout=180s
	$(KUBECTL) -n observability rollout status deployment/alertmanager --timeout=180s

# The connector can read but never write, never reads Secrets and can never inject a fault, in every
# namespace it watches. What it reads differs: workloads in `sre-demo` and `lab-control`, only Chaos
# Mesh objects in `chaos-mesh` (the evidence namespace).
connector-check:
	@for ns in sre-demo lab-control; do \
		test "$$($(KUBECTL) auth can-i list pods --as=$(CONNECTOR_SA) -n $$ns)" = yes \
			|| { echo "the connector cannot list pods in $$ns"; exit 1; }; \
	done; \
	test "$$($(KUBECTL) auth can-i list networkchaos.chaos-mesh.org --as=$(CONNECTOR_SA) -n chaos-mesh)" = yes \
		|| { echo "the connector cannot list chaos objects in chaos-mesh"; exit 1; }; \
	for ns in sre-demo lab-control chaos-mesh; do \
		for verb in "create pods" "patch deployments" "delete pods" "get secrets" "list secrets" \
			"create networkchaos.chaos-mesh.org" "delete networkchaos.chaos-mesh.org"; do \
			test "$$($(KUBECTL) auth can-i $$verb --as=$(CONNECTOR_SA) -n $$ns)" = no \
				|| { echo "the connector is not denied: $$verb in $$ns"; exit 1; }; \
		done; \
	done; echo "connector RBAC: read-only PASS"

cp-up: lab-pki
	@docker start $(CP_PG) >/dev/null 2>&1 || docker run -d --name $(CP_PG) \
		-e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=agentic_sre -p 127.0.0.1:$(CP_PG_PORT):5432 \
		-v $(CP_PG_VOLUME):/var/lib/postgresql/data postgres:16.4-alpine >/dev/null
	@for attempt in $$(seq 1 60); do docker exec $(CP_PG) pg_isready -U postgres -d agentic_sre >/dev/null 2>&1 && break; sleep 1; done
	@docker exec $(CP_PG) psql -U postgres -Atc "select 1 from pg_database where datname = '$(CP_DB_NAME)'" | grep -q 1 \
		|| docker exec $(CP_PG) createdb -U postgres $(CP_DB_NAME)
	DATABASE_URL=$(CP_DB) $(PY) -m alembic upgrade head
	@if [ -f $(LAB_DIR)/cp.pid ] && kill -0 $$(cat $(LAB_DIR)/cp.pid) 2>/dev/null; then \
		echo "the control plane is already running (pid $$(cat $(LAB_DIR)/cp.pid))"; \
	else \
		KUBECONFIG=/dev/null DATABASE_URL=$(CP_DB) SRE_CONNECTOR_MODE=remote \
		SRE_CONNECTOR_LISTEN=0.0.0.0:8443 SRE_CONNECTOR_ALLOWED=lab \
		SRE_CONNECTOR_TLS_CERT=$(LAB_DIR)/pki/server.crt SRE_CONNECTOR_TLS_KEY=$(LAB_DIR)/pki/server.key \
		SRE_CONNECTOR_TLS_CLIENT_CA=$(LAB_DIR)/pki/ca.crt SRE_AUTO_DIAGNOSE=true \
		SRE_WATCH_NAMESPACES=sre-demo,lab-control SRE_WATCH_INTERVAL_SECONDS=15 \
		nohup $(CLI) serve --host 127.0.0.1 --port 8080 > $(LAB_DIR)/cp.log 2>&1 & echo $$! > $(LAB_DIR)/cp.pid; \
		echo "control plane started (pid $$(cat $(LAB_DIR)/cp.pid)); log $(LAB_DIR)/cp.log; console http://127.0.0.1:8080/app"; \
	fi

# Stops only the control plane process (its Postgres keeps running), so a run can restart it on another
# database (`make cp-up CP_DB_NAME=...`).
cp-stop:
	@if [ -f $(LAB_DIR)/cp.pid ]; then kill $$(cat $(LAB_DIR)/cp.pid) 2>/dev/null || true; rm -f $(LAB_DIR)/cp.pid; fi

# Stops the control plane and its Postgres; the volume (the diagnosis history) is kept.
cp-down:
	@if [ -f $(LAB_DIR)/cp.pid ]; then kill $$(cat $(LAB_DIR)/cp.pid) 2>/dev/null || true; rm -f $(LAB_DIR)/cp.pid; fi
	@docker stop $(CP_PG) >/dev/null 2>&1 || true

# Deletes the control plane's history. Explicit on purpose.
cp-reset: cp-down
	docker rm -f $(CP_PG) >/dev/null 2>&1 || true
	docker volume rm $(CP_PG_VOLUME) >/dev/null 2>&1 || true

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
	for name in otel-collector prometheus kube-state-metrics loki tempo alertmanager grafana; do \
		kubectl rollout status deployment/$$name -n observability --timeout=180s || exit 1; \
	done

load:
	kubectl port-forward -n $(NAMESPACE) svc/order-service 8000:8000 >/tmp/agentic-sre-order.log 2>&1 & pid=$$!; \
	trap 'kill "$$pid" 2>/dev/null || true' EXIT; sleep 2; \
	$(PY) -m workload.load_generator --base-url http://localhost:8000 --rate 10 --duration 300 --seed 42

ui:
	@echo "Operator console: http://localhost:8080/app   (legacy pages at http://localhost:8080/)"
	kubectl port-forward -n $(NAMESPACE) svc/control-plane 8080:8000

# A bad rollout: payment requests start taking 2.5 s. The control plane records
# the change, Alertmanager fires on latency, and the incident is diagnosed.
inject-bad-rollout:
	kubectl set env deployment/payment-service -n $(NAMESPACE) FAULT_PAYMENT_DELAY_MS=2500
	kubectl rollout status deployment/payment-service -n $(NAMESPACE) --timeout=120s

recover:
	kubectl rollout undo deployment/payment-service -n $(NAMESPACE)
	kubectl rollout status deployment/payment-service -n $(NAMESPACE) --timeout=120s

# The product-resolution harness (M19), separate from the legacy live suite.
# Offline until M19-6.7 adds fresh-cluster execution: lists the product scenarios.
PRODUCT := $(PY) scripts/product_benchmark.py

product-bench-dev:
	$(PRODUCT) --list

# The internal live scenario suite: real faults, the real alerting path, and a
# graded answer. Needs a deployed cluster (make deploy). SCENARIO=<id> selects one.
LIVE := $(PY) scripts/live_benchmark.py

live-scenarios:
	$(LIVE) --list

live-bench:
	$(LIVE) --json .local/live-bench/results.json

live-bench-dev:
	$(LIVE) --tier DEV --json .local/live-bench/dev.json

live-bench-holdout:
	$(LIVE) --tier HOLDOUT --json .local/live-bench/holdout.json

# Stage one scenario and leave the fault in place so the incident and its
# diagnosis stay visible in the UI. Run `make live-restore` when finished.
live-demo:
	@test -n "$(SCENARIO)" || { echo "usage: make live-demo SCENARIO=<id>  (see make live-scenarios)"; exit 2; }
	$(LIVE) --scenario $(SCENARIO) --keep-fault

live-restore:
	$(LIVE) --restore

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

# Bounded CLASS 0 evidence-path proof against an already deployed Kind stack.
m18a-live-validate:
	$(PY) scripts/m18a_live_validate.py --out .local/m18a/live-validation.json

e2e-kind-clean:
	kind delete cluster --name agentic-sre

release-check: check precommit offline-demo verify-release-provenance e2e-kind
