FROM python:3.12.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY alembic ./alembic
COPY apps ./apps
COPY packages ./packages
COPY workload ./workload
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 appuser
USER 10001
CMD ["alembic", "upgrade", "head"]
