# Multi-stage image for the synthetic-only TG-Control gateway.
#
# Security properties (verified by tests/test_deployment.py and
# docs/DEPLOYMENT.md):
# - Dependencies install from the hash-locked requirements.lock in a builder
#   stage; the runtime image contains no compiler toolchain, no lock file and no
#   build cache.
# - The process runs as an unprivileged, fixed-UID user with no shell and no home
#   directory. The container needs no Linux capabilities.
# - No credential, token, session, database dump or `.env` file is copied into any
#   layer. Runtime secrets are mounted files (Docker/Kubernetes secrets), never
#   build arguments or environment literals.
# - The health check calls the readiness endpoint (which requires the owner
#   credential and a reachable database), so "healthy" cannot mean merely "the
#   process is running".
# - uvicorn is PID 1 and terminates gracefully on SIGTERM; Compose adds `init:
#   true` for signal forwarding and zombie reaping.
#
# The base tag is a patch-pinned Debian slim image. Pinning by digest requires
# registry access that the build environment does not have; `docs/DEPLOYMENT.md`
# records that as an open release-checklist item rather than claiming it is done.
ARG PYTHON_VERSION=3.11

FROM python:${PYTHON_VERSION}-slim-bookworm AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
# Only the dependency metadata is copied first so the layer is cached until the
# lock file changes. `--require-hashes` rejects an unpinned or tampered package.
COPY requirements.lock pyproject.toml ./
RUN python -m pip install --require-hashes --prefix=/install -r requirements.lock
# The project is built with build isolation disabled so no build backend is
# fetched implicitly. That requires the exact backends declared in
# [build-system] to exist in this stage; without `wheel` pip cannot produce a
# wheel and the build fails with exit code 2.
RUN python -m pip install --no-cache-dir "setuptools==84.2.1" "wheel==0.48.0"
COPY src ./src
RUN python -m pip install --no-build-isolation --no-deps --prefix=/install .

FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TG_LOG_LEVEL=INFO \
    TG_HEALTH_PORT=8000
ARG APP_UID=10001
ARG APP_GID=10001
# Fixed, unprivileged identity with no home directory and no login shell. The
# writable state directories are created and owned here so a bind mount or named
# volume inherits a usable owner; a host bind mount must still be chmod/chowned
# to APP_UID by the operator (documented).
RUN groupadd --system --gid "${APP_GID}" tgcontrol \
    && useradd --system --uid "${APP_UID}" --gid "${APP_GID}" \
       --no-create-home --shell /usr/sbin/nologin tgcontrol \
    && install -d -o "${APP_UID}" -g "${APP_GID}" -m 0700 \
       /var/lib/tg-control/synthetic /var/lib/tg-control/media /var/lib/tg-control/tmp
COPY --from=builder /install /usr/local
WORKDIR /app
COPY alembic.ini /app/alembic.ini
COPY alembic /app/alembic
USER ${APP_UID}:${APP_GID}
EXPOSE 8000
STOPSIGNAL SIGTERM
# Readiness-based health: unauthenticated liveness would only prove that a
# socket accepts connections. The probe never prints secret material.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-m", "tg_control.healthcheck"]
ENTRYPOINT ["uvicorn", "tg_control.api:app_factory", "--factory", \
            "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
            "--no-access-log", "--no-server-header", "--no-proxy-headers", \
            "--timeout-graceful-shutdown", "30"]
