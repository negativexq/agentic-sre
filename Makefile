.PHONY: install lint typecheck test check cluster-up build-images deploy load status cluster-down

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
	kind load docker-image agentic-sre/order-service:dev --name agentic-sre
	kind load docker-image agentic-sre/payment-service:dev --name agentic-sre
	kind load docker-image agentic-sre/order-worker:dev --name agentic-sre

deploy: build-images
	kubectl apply -f infra/kubernetes/namespace.yaml
	kubectl apply -f infra/kubernetes/workload.yaml
	kubectl apply -f infra/kubernetes/dependencies.yaml

load:
	.venv/bin/python -m workload.load_generator --base-url http://localhost:8000 --rate 10 --duration 1 --seed 42

status:
	kubectl get pods,svc -n sre-demo

cluster-down:
	kind delete cluster --name agentic-sre
