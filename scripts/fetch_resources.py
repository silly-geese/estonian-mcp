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

import io
import os
import shutil
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


def _md5(path: Path) -> str:
    """Streaming MD5. `usedforsecurity=False` because this guards against
    truncation and corruption over TLS from our own release asset, not
    against a chosen-prefix attacker — and without it this raises on
    FIPS-enabled builds (RHEL/UBI)."""
    import hashlib

    try:
        h = hashlib.md5(usedforsecurity=False)
    except TypeError:      # Python < 3.9 signature
        h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_fasttext() -> bool:
    """Download the compressed fastText model, verifying its checksum."""

    target = _fasttext_target()
    if target.exists():
        # "Exists" is not "good". A truncated leftover from an interrupted
        # run would otherwise be accepted forever, and the server then fails
        # to load it with an opaque error. Hashing 33 MB costs ~0.1 s.
        if _md5(target) == FASTTEXT_MD5:
            print(f"  fasttext: already present at {target}, skipping")
            return True
        print(f"  fasttext: existing file at {target} is corrupt, re-downloading")
        target.unlink()

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"  fasttext: FAILED (cannot create {target.parent}: {e})")
        return False
    print(f"  fasttext: downloading (~33 MB) -> {target}")
    # Append rather than with_suffix(): with_suffix REPLACES an existing
    # suffix, so an operator-supplied ESTNLTK_MCP_FASTTEXT_PATH ending in
    # e.g. `.bin` would produce a temp name derived from a different stem.
    tmp = target.parent / (target.name + ".partial")
    try:
        with urllib.request.urlopen(FASTTEXT_URL, timeout=300) as r:
            tmp.write_bytes(r.read())
    except Exception as e:
        tmp.unlink(missing_ok=True)
        print(f"  fasttext: FAILED ({type(e).__name__}: {e})")
        return False

    digest = _md5(tmp)
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


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract with zip-slip protection.

    `ZipFile.extractall` will happily follow `../` in a member name and
    write outside `dest`. This is a downloaded archive, so resolve every
    member and refuse anything that escapes the destination.
    """
    dest_root = dest.resolve()
    for member in zf.infolist():
        out = (dest_root / member.filename).resolve()
        if not out.is_relative_to(dest_root):
            raise ValueError(f"unsafe path in archive: {member.filename!r}")
    zf.extractall(dest)


def _punkt_usable() -> bool:
    """Validate by USE: the data is only good if it actually tokenises.

    A directory-exists check accepts a partially-extracted tree — the zip
    can fail after creating `punkt_tab/estonian/` but before writing all
    its tables — and that partial state would then be skipped as
    "already present" on every subsequent run.
    """
    try:
        import nltk
        nltk.data.find("tokenizers/punkt_tab/estonian/")
        from nltk.tokenize.punkt import PunktTokenizer
        tok = PunktTokenizer(lang="estonian")
        return len(tok.tokenize("Kooli maja on suur. Teine lause siin.")) == 2
    except Exception:
        return False


def fetch_punkt_tab() -> bool:
    """Download NLTK punkt_tab, extracting atomically after validation."""
    import nltk

    if _punkt_usable():
        print("  punkt_tab: already present and usable, skipping")
        return True

    # Venv-local, so the fetch stays with the checkout it belongs to rather
    # than polluting the user's home. <sys.prefix>/nltk_data is on NLTK's
    # default search path.
    target = Path(sys.prefix) / "nltk_data"
    tokenizers = target / "tokenizers"
    try:
        tokenizers.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"  punkt_tab: FAILED (cannot create {tokenizers}: {e})")
        print("             is sys.prefix writable? run inside the venv (uv run ...)")
        return False
    if str(target) not in nltk.data.path:
        nltk.data.path.insert(0, str(target))

    print(f"  punkt_tab: downloading -> {tokenizers}")
    # Extract into a staging dir first, validate, then swap into place, so a
    # failure part-way through can never leave a half-installed tree that
    # later runs mistake for a good one.
    staging = tokenizers / ".punkt_tab.staging"
    final = tokenizers / "punkt_tab"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    try:
        with urllib.request.urlopen(PUNKT_TAB_URL, timeout=120) as r:
            payload = r.read()
        staging.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            _safe_extract(zf, staging)
    except Exception as e:
        shutil.rmtree(staging, ignore_errors=True)
        print(f"  punkt_tab: FAILED ({type(e).__name__}: {e})")
        return False

    extracted = staging / "punkt_tab"
    if not (extracted / "estonian").is_dir():
        shutil.rmtree(staging, ignore_errors=True)
        print("  punkt_tab: archive did not contain an Estonian model")
        return False

    backup = tokenizers / ".punkt_tab.previous"
    shutil.rmtree(backup, ignore_errors=True)
    if final.exists():
        final.rename(backup)
    extracted.rename(final)
    shutil.rmtree(staging, ignore_errors=True)

    if not _punkt_usable():
        # Roll back rather than leave the checkout worse than we found it.
        shutil.rmtree(final, ignore_errors=True)
        if backup.exists():
            backup.rename(final)
        print("  punkt_tab: downloaded data does not tokenise; rolled back")
        return False
    shutil.rmtree(backup, ignore_errors=True)
    print("  punkt_tab: OK")
    return True


def _wordnet_usable() -> bool:
    """Validate by USE. An interrupted extraction can leave the version
    directory in place with an incomplete set of sqlite files, which an
    index/exists check would accept forever."""
    try:
        from estnltk.wordnet import Wordnet
        return bool(Wordnet()["kasutama"])
    except Exception:
        return False


def fetch_wordnet() -> bool:
    """Download Estonian WordNet via EstNLTK's own resource downloader."""
    if _wordnet_usable():
        print("  wordnet: already present and usable, skipping")
        return True

    print("  wordnet: downloading (~26 MB)")
    try:
        from estnltk.downloader import download
        download("wordnet")
    except Exception as e:
        print(f"  wordnet: FAILED ({type(e).__name__}: {e})")
        return False

    if not _wordnet_usable():
        print("  wordnet: download reported success but the data does not load")
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
