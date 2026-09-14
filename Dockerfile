# RepoAtlas container image (V1_SPEC §18).
#
# Repositories are untrusted input and are never executed (V1_SPEC §17), so this
# image installs nothing from them, runs as a non-root user, and needs no network
# at runtime for any command. Reasoning is not in the image and not in the stack:
# an external agent connects over `repoatlas mcp`. Embeddings are ONNX-only, so
# there is no CUDA and no PyTorch.

# -- build stage ---------------------------------------------------------------
# Dependencies resolve from the committed uv.lock, so an image build is
# reproducible and never picks up a version the test suite has not seen.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first: they change far less often than the source, so an edit to
# repoatlas/ does not re-resolve or re-download the environment.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY repoatlas ./repoatlas
RUN uv sync --frozen --no-dev


# -- runtime stage -------------------------------------------------------------
FROM python:3.13-slim-bookworm AS runtime

# git is the only external binary RepoAtlas invokes, and only through the
# read-only plumbing allowlist in ARCHITECTURE.md §8.
RUN apt-get update \
 && apt-get install --no-install-recommends -y git \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin repoatlas

COPY --from=build --chown=repoatlas:repoatlas /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # State lives on its own volume, never beside the repositories (V1_SPEC §18).
    REPOATLAS_CONFIG=/state/config.yaml \
    REPOATLAS_DB=/state/atlas.db \
    # The embedding model cache belongs to the state volume too, so `sync --embed`
    # is paid once per deployment rather than once per container start.
    HF_HOME=/state/models \
    FASTEMBED_CACHE_PATH=/state/models/fastembed

# /repos is the read-only mount point; /state is the writable volume.
RUN mkdir -p /repos /state && chown repoatlas:repoatlas /state

USER repoatlas
WORKDIR /repos

# Reports "index stale or absent" (exit 3) until `init` has run, which is a true
# statement about the container rather than a synthetic health probe.
HEALTHCHECK --interval=1m --timeout=10s --start-period=5s --retries=3 \
    CMD ["repoatlas", "status", "--json"]

ENTRYPOINT ["repoatlas"]
CMD ["--help"]
