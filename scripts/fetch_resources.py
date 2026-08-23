#!/usr/bin/env python3
"""Fetch the NLP resources estonian-mcp needs, for SOURCE installs.

    uv run python scripts/fetch_resources.py

`uv sync` installs Python distributions. It cannot install NLTK corpus data
or EstNLTK resources, because those are not Python distributions and are not
in `uv.lock`. So a fresh `git clone && uv sync` leaves three gaps:

  * NLTK `punkt_tab` — the sentence tokenizer EstNLTK's `sentences` layer
    uses. Without it, tools that tag that layer raise `LookupError`.
  * Estonian WordNet (~26 MB) — needed by `synonyms`, and by the
    `shared-wordnet-synset` rule in `check_term_consistency`.
  * fastText embeddings (~33 MB) — needed by `find_related_words` and
    `check_compound_familiarity`. Served from this project's own GitHub
    release, the same artifact the Dockerfile uses, so no new third party
    is involved.

WHY THIS IS A SCRIPT AND NOT LAZY AUTO-DOWNLOAD
-----------------------------------------------
PRIVACY.md promises the running server makes **no outbound HTTP calls** and
uses no third-party processors. Fetching resources on demand from inside a
tool call would break that promise, and under stdio transport the download
prompt would be written to stdout — which is the MCP protocol channel — and
corrupt the stream.

So fetching is deliberately a separate, explicit step that you run: the
network access is yours, knowingly, at setup time, not the server's at
request time. The Docker image does the same thing at BUILD time; nothing
is ever fetched while serving.

CA TRUST STORE
--------------
uv-provisioned interpreters ship without a CA trust store, so NLTK's and
EstNLTK's downloads fail with CERTIFICATE_VERIFY_FAILED. `certifi` is
already present as a transitive dependency, so we point SSL_CERT_FILE at it
before importing anything that downloads. This must happen before those
imports, which is why it is at the top of main() rather than inline.

Safe to re-run: every fetch is skipped when the resource is already
present, so this is idempotent.
"""
from __future__ import annotations

import os
import sys
import urllib.request
import zipfile
from pathlib import Path

# NLTK's own mirror. Pinned to the gh-pages package path NLTK itself serves.
PUNKT_TAB_URL = (
    "https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/"
    "packages/tokenizers/punkt_tab.zip"
)

# Same artifact the Dockerfile pulls, from this project's own release, so
# a source install and the image end up on identical bytes.
FASTTEXT_URL = (
    "https://github.com/silly-geese/estonian-mcp/releases/download/"
    "v0.1.0-models/fasttext-et-medium"
)
# Matches the checksum the CI workflow verifies for the same file.
FASTTEXT_MD5 = "3690ee9983fc95740a61125fd58ed385"


def _fasttext_target() -> Path:
    """Where the server looks for the model (env override wins)."""
    env = os.environ.get("ESTNLTK_MCP_FASTTEXT_PATH")
    if env:
        return Path(env)
    return Path.home() / ".cache" / "estnltk-mcp" / "fasttext-et-medium"


def fetch_fasttext() -> bool:
    """Download the compressed fastText model, verifying its checksum."""
    import hashlib

    target = _fasttext_target()
    if target.exists():
        print(f"  fasttext: already present at {target}, skipping")
        return True

    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"  fasttext: downloading (~33 MB) -> {target}")
    tmp = target.with_suffix(".partial")
    try:
        with urllib.request.urlopen(FASTTEXT_URL, timeout=300) as r:
            tmp.write_bytes(r.read())
    except Exception as e:
        tmp.unlink(missing_ok=True)
        print(f"  fasttext: FAILED ({type(e).__name__}: {e})")
        return False

    digest = hashlib.md5(tmp.read_bytes()).hexdigest()
    if digest != FASTTEXT_MD5:
        tmp.unlink(missing_ok=True)
        print(f"  fasttext: CHECKSUM MISMATCH (got {digest}, want {FASTTEXT_MD5})")
        return False
    # Only move into place once verified, so an interrupted or corrupted
    # download can never masquerade as a good model.
    tmp.replace(target)
    print("  fasttext: OK")
    if not os.environ.get("ESTNLTK_MCP_FASTTEXT_PATH"):
        print(f"           set ESTNLTK_MCP_FASTTEXT_PATH={target} for the server")
    return True


def _use_certifi_trust_store() -> str | None:
    """Point SSL_CERT_FILE at certifi's bundle if it is not already set.

    Returns the path used, or None if certifi is unavailable (in which case
    we leave the environment alone and let the download fail loudly rather
    than silently disabling verification — we never set a permissive TLS
    mode)."""
    if os.environ.get("SSL_CERT_FILE"):
        return os.environ["SSL_CERT_FILE"]
    try:
        import certifi
    except ImportError:
        return None
    path = certifi.where()
    os.environ["SSL_CERT_FILE"] = path
    os.environ.setdefault("REQUESTS_CA_BUNDLE", path)
    return path


def fetch_punkt_tab() -> bool:
    """Download NLTK punkt_tab into the first writable nltk_data dir."""
    import nltk

    try:
        nltk.data.find("tokenizers/punkt_tab/estonian/")
        print("  punkt_tab: already present, skipping")
        return True
    except LookupError:
        pass

    # Prefer a venv-local dir so the fetch does not pollute the user's home
    # and stays with the checkout it belongs to.
    target = Path(sys.prefix) / "nltk_data"
    tokenizers = target / "tokenizers"
    tokenizers.mkdir(parents=True, exist_ok=True)
    if str(target) not in nltk.data.path:
        nltk.data.path.insert(0, str(target))

    print(f"  punkt_tab: downloading -> {tokenizers}")
    zip_path = tokenizers / "punkt_tab.zip"
    try:
        with urllib.request.urlopen(PUNKT_TAB_URL, timeout=60) as r:
            zip_path.write_bytes(r.read())
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tokenizers)
        zip_path.unlink(missing_ok=True)
    except Exception as e:
        print(f"  punkt_tab: FAILED ({type(e).__name__}: {e})")
        return False

    try:
        nltk.data.find("tokenizers/punkt_tab/estonian/")
    except LookupError:
        print("  punkt_tab: downloaded but Estonian model still not found")
        return False
    print("  punkt_tab: OK")
    return True


def fetch_wordnet() -> bool:
    """Download Estonian WordNet via EstNLTK's own resource downloader."""
    from estnltk.downloader import download, get_resource_paths

    if get_resource_paths("wordnet", only_latest=True, download_missing=False):
        print("  wordnet: already present, skipping")
        return True

    print("  wordnet: downloading (~26 MB)")
    try:
        download("wordnet")
    except Exception as e:
        print(f"  wordnet: FAILED ({type(e).__name__}: {e})")
        return False

    if not get_resource_paths("wordnet", only_latest=True, download_missing=False):
        print("  wordnet: download reported success but resource is not found")
        return False
    print("  wordnet: OK")
    return True


def main() -> int:
    trust = _use_certifi_trust_store()
    print("estonian-mcp resource fetch")
    if trust:
        print(f"  TLS trust store: {trust}")
    else:
        print("  TLS trust store: certifi not found; using system default")

    ok_punkt = fetch_punkt_tab()
    ok_wordnet = fetch_wordnet()
    ok_fasttext = fetch_fasttext()

    print()
    if ok_punkt and ok_wordnet and ok_fasttext:
        print("All resources present. estonian-mcp will run at full strength.")
        return 0
    # Partial success is still useful — say precisely what is degraded so the
    # operator knows which tools are affected rather than guessing.
    if not ok_punkt:
        print("punkt_tab missing: tools tagging the `sentences` layer will raise.")
    if not ok_wordnet:
        print(
            "wordnet missing: `synonyms` will raise, and "
            "`check_term_consistency` runs with only its compound-head rule "
            "(its output reports degraded: true)."
        )
    if not ok_fasttext:
        print(
            "fasttext missing: `find_related_words` and "
            "`check_compound_familiarity` will raise."
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
