# One image definition for every component. Build a component with
#   docker build -f infra/docker/Dockerfile --target <component> -t agentic-sre/<component>:dev .
# Targets: control-plane, migrator, order-service, payment-service, order-worker.

ARG PYTHON_IMAGE=python:3.12.13-slim-bookworm

FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# Dependencies first, so code edits do not reinstall them. The lock is the
# same resolution used by local development and CI; the project itself is
# installed separately below so the image remains a normal pip environment.
ARG UV_VERSION=0.12.15
COPY pyproject.toml uv.lock README.md ./
RUN pip install --no-cache-dir "uv==${UV_VERSION}" \
    && uv export --locked --no-dev --no-emit-project --format requirements-txt --output-file /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt

COPY apps ./apps
COPY packages ./packages
COPY workload ./workload
RUN pip install --no-deps . \
    && useradd --create-home --uid 10001 appuser
USER 10001

FROM base AS control-plane
EXPOSE 8000
CMD ["uvicorn", "apps.control_plane.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS migrator
COPY alembic.ini ./
COPY alembic ./alembic
CMD ["alembic", "upgrade", "head"]

FROM base AS order-service
EXPOSE 8000
CMD ["uvicorn", "workload.order_service.app:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS payment-service
EXPOSE 8000
CMD ["uvicorn", "workload.payment_service.app:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS order-worker
CMD ["python", "-m", "workload.order_worker.runner"]
