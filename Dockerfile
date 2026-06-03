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

# curl + unzip are needed at build time:
# - curl: fetch the fastText model from Zenodo (+ WordNet zip from our
#   GH Release mirror if upstream EstNLTK S3 is down).
# - unzip: extract the GH-mirrored WordNet zip into the resources dir.
# Builder stage only — discarded; runtime image doesn't get either.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl unzip \
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
# Wordnet's lazy-init path. If EstNLTK's upstream S3 (hpc.ut.ee) is
# down, fall back to fetching the same zip from our GH Release mirror
# and unpacking manually — same play as the Zenodo fastText fallback.
RUN set +e; \
    echo "y" | /opt/venv/bin/python -c "from estnltk.wordnet import Wordnet; Wordnet()"; \
    status=$?; \
    set -e; \
    if [ $status -ne 0 ]; then \
      echo "Upstream WordNet download failed; using GH Release mirror." ; \
      WN_DIR=$(/opt/venv/bin/python -c "from estnltk.resource_utils import get_resources_dir; print(get_resources_dir())") ; \
      mkdir -p "$WN_DIR/wordnet" ; \
      curl -fsSL --retry 3 --retry-delay 2 \
        -o /tmp/wn.zip \
        "https://github.com/silly-geese/estonian-mcp/releases/download/v0.1.0-models/wordnet_2026-02-13.zip" ; \
      unzip -q -o /tmp/wn.zip -d "$WN_DIR/wordnet/" ; \
      rm /tmp/wn.zip ; \
      /opt/venv/bin/python -c "from estnltk.wordnet import Wordnet; assert Wordnet()['kasutama'], 'wordnet still not loadable after mirror fallback'" ; \
    fi

# Estonian fastText word embeddings, compressed to ~33 MB with a 100K
# vocabulary. Used by find_related_words + check_compound_familiarity.
# Built locally from Facebook's cc.et.300.bin (Grave et al. 2018,
# CC-BY-SA-3.0) via compress-fasttext (Liebl 2021) and hosted on our
# GH Release. No Zenodo dependency — Facebook's upstream is the
# canonical source for the underlying vectors. MD5 verified after
# download.
RUN mkdir -p /opt/models \
 && curl -fsSL --retry 3 --retry-delay 2 \
      -o /opt/models/fasttext-et-medium \
      "https://github.com/silly-geese/estonian-mcp/releases/download/v0.1.0-models/fasttext-et-medium" \
 && echo "3690ee9983fc95740a61125fd58ed385  /opt/models/fasttext-et-medium" | md5sum -c -

COPY server.py logo.png ./

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
COPY --from=builder /app/logo.png /app/logo.png

# EstNLTK's WordNet opens its bundled SQLite DB read-write at query time
# (SQLite must create a journal file alongside the DB), so the resources
# dir has to be writable by the non-root runtime user. It's root-owned
# from the build stage, so hand it to `app` before dropping privileges —
# otherwise the `synonyms` tool fails at runtime with EACCES.
RUN chown -R app:app /opt/venv/lib/python3.13/site-packages/estnltk/estnltk_resources

USER app
EXPOSE 8081

# /health is public and unauthenticated; safe for liveness probes.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request, os, sys; \
    sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8081\")}/health', timeout=3).status == 200 else 1)"

CMD ["python", "server.py", "--transport", "http"]
