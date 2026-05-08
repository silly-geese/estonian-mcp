# syntax=docker/dockerfile:1.7

# ---- builder ----
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# uv produces reproducible installs from uv.lock with pinned hashes.
COPY --from=ghcr.io/astral-sh/uv:0.5.18 /uv /usr/local/bin/uv

# curl is needed to fetch the fastText model from Zenodo at build time.
# Builder stage only — discarded; runtime image doesn't get curl.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps from the lockfile only — the project itself isn't installed
# because CMD invokes `python server.py` directly rather than the console
# script entrypoint. This avoids Hatchling having to read README/LICENSE
# during the build.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Pre-download EstNLTK WordNet (~26 MB) into the venv's resources dir so
# the `synonyms` tool doesn't trigger an interactive download prompt on
# first call at runtime. The "y" pipes past the [Y/n] prompt baked into
# Wordnet's lazy-init path.
RUN echo "y" | /opt/venv/bin/python -c "from estnltk.wordnet import Wordnet; Wordnet()"

# Estonian fastText word embeddings (compressed, ~22 MB). Used by the
# find_related_words tool. Source: Liebl 2021 on Zenodo, vectors by
# Grave et al. 2018 (CC-BY-SA-3.0). MD5 verified at build time.
RUN mkdir -p /opt/models \
 && curl -fsSL --retry 3 --retry-delay 2 \
      -o /opt/models/fasttext-et-mini \
      "https://zenodo.org/records/4905385/files/fasttext-et-mini?download=1" \
 && echo "0904bf4e96e53a727069f783a3415869  /opt/models/fasttext-et-mini" | md5sum -c -

COPY server.py ./

# ---- runtime ----
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8081 \
    HOST=0.0.0.0 \
    ESTNLTK_MCP_TRANSPORT=http

# Drop privileges. EstNLTK has no need for root.
RUN groupadd --system --gid 1000 app \
 && useradd  --system --uid 1000 --gid app --home /home/app --create-home app

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/models /opt/models
COPY --from=builder /app/server.py /app/server.py

USER app
EXPOSE 8081

# /health is public and unauthenticated; safe for liveness probes.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request, os, sys; \
    sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8081\")}/health', timeout=3).status == 200 else 1)"

CMD ["python", "server.py", "--transport", "http"]
