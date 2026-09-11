FROM python:3.12.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY apps ./apps
COPY packages ./packages
COPY workload ./workload
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 appuser
USER 10001
CMD ["python", "-m", "workload.order_worker.runner"]
