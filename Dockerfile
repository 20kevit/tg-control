# Synthetic development image only. Pin the base image digest and review for release.
FROM python:3.11.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.lock pyproject.toml /app/
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock
COPY src /app/src
COPY alembic.ini /app/alembic.ini
COPY alembic /app/alembic
RUN python -m pip install --no-cache-dir --no-deps --no-build-isolation . \
    && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin tgcontrol
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -m tg_control.healthcheck || exit 1
CMD ["uvicorn", "tg_control.api:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--no-server-header", "--no-proxy-headers"]
