.PHONY: install lint typecheck test check cluster-up build-images deploy load status cluster-down release-check

install:
	python3.12 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -e '.[dev]'

lint:
	.venv/bin/python -m ruff check .
	.venv/bin/python -m ruff format --check .

typecheck:
	.venv/bin/python -m mypy apps packages tests

test:
	.venv/bin/python -m pytest

check: lint typecheck test

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
	kubectl apply -f infra/kubernetes/namespace.yaml
	kubectl apply -f infra/kubernetes/workload.yaml
	kubectl apply -f infra/kubernetes/control-plane.yaml
	kubectl apply -f infra/kubernetes/dependencies.yaml
	kubectl rollout restart deployment/order-service deployment/payment-service deployment/order-worker deployment/control-plane -n sre-demo
	kubectl rollout status deployment/order-service -n sre-demo --timeout=120s
	kubectl rollout status deployment/payment-service -n sre-demo --timeout=120s
	kubectl rollout status deployment/order-worker -n sre-demo --timeout=120s
	kubectl rollout status deployment/control-plane -n sre-demo --timeout=120s
	kubectl delete job db-migration -n sre-demo --ignore-not-found
	kubectl apply -f infra/kubernetes/db-migration.yaml
	kubectl wait --for=condition=complete job/db-migration -n sre-demo --timeout=120s
	kubectl apply -f infra/kubernetes/tools-rbac.yaml

load:
	kubectl port-forward -n sre-demo svc/order-service 8000:8000 >/tmp/agentic-sre-port-forward.log 2>&1 & port_pid=$$!; trap 'kill "$$port_pid" 2>/dev/null || true' EXIT; sleep 2; .venv/bin/python -m workload.load_generator --base-url http://localhost:8000 --rate 10 --duration 1 --seed 42

status:
	kubectl get pods,svc -n sre-demo

cluster-down:
	kind delete cluster --name agentic-sre

release-check: check
	.venv/bin/python -m pytest tests/e2e
	.venv/bin/python -m pytest tests/unit/test_state_machine.py --cov=packages.incident.state_machine --cov-report=term-missing --cov-fail-under=100
