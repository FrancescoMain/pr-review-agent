# Multi-stage Dockerfile for the PR Review Agent.
#
# Stage 1 ("builder") installs uv and resolves all production dependencies
# into a frozen virtualenv. Stage 2 ("runtime") copies just the venv plus the
# source, keeping the final image small (~600MB with torch/sentence-transformers,
# ~250MB without — but we ship them because the convention-recall path needs
# them at runtime).
#
# The image includes `git` because RepoCheckout shells out to `git init` /
# `git fetch` / `git checkout` to materialise the head_sha of each PR.

# ---- builder ----
FROM python:3.12-slim AS builder

# uv via the official static binary — quicker than pip-installing it.
COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

# NOTE: we don't use `--mount=type=cache` here. Railway's BuildKit fork
# requires a non-standard `cacheKey/<id>` prefix that breaks portability
# on every other builder. `uv sync` against a frozen lockfile is already
# fast enough (~30s cold, bytecode pre-compiled via UV_COMPILE_BYTECODE)
# that the cache isn't worth the cross-builder fragility. Layer caching
# still kicks in when `uv.lock` doesn't change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Now copy the package + project metadata and install the project itself.
COPY src ./src
COPY README.md README.it.md ./
RUN uv sync --frozen --no-dev


# ---- runtime ----
FROM python:3.12-slim AS runtime

# Runtime needs git for RepoCheckout. ca-certificates is implied by python:slim
# but we make it explicit for clarity.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Sentence-transformers cache: persisted under /data on Railway via volume.
    HF_HOME=/data/hf-cache \
    XDG_CACHE_HOME=/data/cache

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY src ./src
COPY migrations ./migrations
COPY README.md README.it.md ./

# Railway / Fly inject the listening port through $PORT. Default to 8000 for
# local `docker run` smoke tests.
ENV PORT=8000
EXPOSE 8000

# Runtime entry point. The .venv is already on PATH (see ENV above), so we
# call uvicorn directly — no `uv run`, no extra binary needed in runtime.
# `$PORT` expansion needs sh, so we use the shell form.
CMD uvicorn pr_review_agent.main:app --host 0.0.0.0 --port ${PORT}
