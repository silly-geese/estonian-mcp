"""Tests for the self-host stack in deploy/.

The nginx config, the compose file and the credential scripts carry
security and privacy decisions that no Python test would otherwise
notice going missing. This file pins the ones that are invariants rather
than opinions: logging off by default, no credential reachable from a
log, forward secrecy, connection limits keyed on something an
unauthenticated flood actually has, and secret files that are not
world-readable.

It needs no Docker and no dependencies. The behaviour of the running
proxy — auth, OAuth, PKCE, rate limits, TLS — is covered by
`tests/deploy_e2e.sh`, which builds the image and drives it with curl.

Run via:

    python3 tests/test_deploy.py
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = (ROOT / "deploy/nginx/templates/mcp.conf.template").read_text()
COMPOSE = (ROOT / "docker-compose.yaml").read_text()


def uncommented(text: str) -> str:
    """Directives only. Several checks below assert that a construct is
    absent, and both files discuss the constructs they avoid."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


TEMPLATE_CODE = uncommented(TEMPLATE)
COMPOSE_CODE = uncommented(COMPOSE)
OAUTH_JS = (ROOT / "deploy/nginx/njs/oauth.js").read_text()
ENV_EXAMPLE = (ROOT / ".env.example").read_text()
DEPLOY_README = (ROOT / "deploy/README.md").read_text()

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS {label}")
    else:
        failures.append(f"{label}: {detail}")
        print(f"  FAIL {label} {detail}")


def server_blocks(text: str) -> list[str]:
    """The body of each top-level `server { ... }` block, braces balanced."""
    blocks = []
    for match in re.finditer(r"^server \{", text, re.M):
        depth = 0
        for i in range(match.start(), len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(text[match.start():i + 1])
                    break
    return blocks


# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------
def logging_is_off_by_default() -> None:
    print("logging is off unless the operator asks for it")
    access_logs = re.findall(r"^\s*access_log\s+(.+);", TEMPLATE, re.M)
    check("every access_log is gated", bool(access_logs) and all("if=$mcp_log" in a for a in access_logs),
          str(access_logs))
    check("the gate is driven by the ACCESS_LOG knob",
          re.search(r'map "\$\{ACCESS_LOG\}:\$status" \$mcp_log \{', TEMPLATE) is not None)
    check("a 5xx is logged whatever the knob says",
          re.search(r'"~\^0:5"\s+1;', TEMPLATE) is not None,
          "with no access log and the error log at crit, a broken upstream is silent")
    check("ACCESS_LOG ships as 0", re.search(r"^ACCESS_LOG=0$", ENV_EXAMPLE, re.M) is not None)
    check("compose defaults it to 0 as well", "ACCESS_LOG: ${ACCESS_LOG:-0}" in COMPOSE)
    check("the value is normalised before it reaches the config",
          "ACCESS_LOG=1" in (ROOT / "deploy/nginx/render-vars.envsh").read_text())

    fmt = re.search(r"log_format mcp (.+?);", TEMPLATE, re.S)
    check("a log format exists", fmt is not None)
    if fmt:
        body = fmt.group(1)
        for forbidden, why in (
            ("$http_authorization", "the bearer token itself"),
            ("$query_string", "the Smithery ?config= credential"),
            ("$request_uri", "carries the query string too"),
            ("$remote_addr", "PRIVACY.md uses the address for limiting, not for a record"),
            ("$http_cookie", "not ours to record"),
        ):
            check(f"the access log excludes {forbidden}", forbidden not in body, why)

    # nginx stamps every error-log line with the full request line, query
    # string included, so anything below crit publishes credentials.
    for i, block in enumerate(server_blocks(TEMPLATE)):
        check(f"server block {i + 1} holds the error log at crit",
              re.search(r"^\s*error_log\s+\S+\s+crit;", block, re.M) is not None)


# ---------------------------------------------------------------------
# Transport security
# ---------------------------------------------------------------------
def tls_is_forward_secret() -> None:
    print("TLS keeps forward secrecy")
    ciphers = re.search(r"^\s*ssl_ciphers\s+(.+);", TEMPLATE, re.M)
    check("an explicit cipher list is set", ciphers is not None,
          "nginx's default negotiates static-RSA suites on TLS 1.2")
    if ciphers:
        suites = ciphers.group(1).split(":")
        check("every TLS 1.2 suite is ephemeral", all(s.startswith("ECDHE") for s in suites),
              str([s for s in suites if not s.startswith("ECDHE")]))
        check("no DHE suite without an ssl_dhparam file",
              not any(s.startswith("DHE") for s in suites) or "ssl_dhparam" in TEMPLATE)
    check("the HTTP redirect names the configured domain",
          re.search(r"return 301 https://\$\{DOMAIN\}", TEMPLATE_CODE) is not None
          and "return 301 https://$host" not in TEMPLATE_CODE,
          "a default_server takes any Host, so $host is an open redirect")
    check("TLS 1.1 and below are refused",
          re.search(r"^\s*ssl_protocols\s+TLSv1\.2 TLSv1\.3;", TEMPLATE, re.M) is not None)
    check("session tickets are off", "ssl_session_tickets off;" in TEMPLATE)
    # add_header does not merge across levels: a location that declares
    # any header of its own loses the server-level HSTS silently.
    server_level = 0
    for block in server_blocks(TEMPLATE_CODE):
        if re.search(r"^    add_header Strict-Transport-Security", block, re.M):
            server_level += 1
        for loc in re.finditer(r"location ([^\{]+)\{((?:[^{}]|\{[^{}]*\})*)\}", block):
            name, body = loc.group(1).strip(), loc.group(2)
            if "add_header" in body:
                check(f"{name} keeps HSTS while adding headers of its own",
                      "Strict-Transport-Security" in body)
    check("the TLS server sets HSTS at server level", server_level >= 1)


# ---------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------
def limits_meter_unauthenticated_traffic() -> None:
    print("the limits cover traffic that has no client id")
    check("connections are metered per IP",
          "limit_conn_zone $binary_remote_addr zone=conn_ip:10m;" in TEMPLATE,
          "$mcp_client is empty for exactly the flood this is meant to stop")

    # Per server block, not "somewhere in the file". Port 80 had none of
    # this, and it is the port certbot cannot lose: exhaust the workers
    # there and the certificate stops renewing.
    for block in server_blocks(TEMPLATE_CODE):
        listen = re.search(r"listen (\d+)", block)
        port = listen.group(1) if listen else "?"
        check(f"port {port} caps connections per IP",
              re.search(r"limit_conn conn_ip\s+\d+;", block) is not None)
        check(f"port {port} cuts off a slow client",
              "client_header_timeout" in block and "client_body_timeout" in block)
        for loc in re.finditer(r"location ([^\{]+)\{((?:[^{}]|\{[^{}]*\})*)\}", block):
            name, body = loc.group(1).strip(), loc.group(2)
            # Named locations are internal targets: their parent was
            # metered before the request reached them.
            if name.startswith("@") or "internal;" in body:
                continue
            check(f"port {port} meters {name}", "limit_req" in body)

    # limit_conn is inherited only when a level declares none of its own,
    # so a location that names conn_client must repeat conn_ip.
    for match in re.finditer(r"location ([^\{]+)\{((?:[^{}]|\{[^{}]*\})*)\}", TEMPLATE):
        body = match.group(2)
        if "limit_conn conn_client" in body:
            check(f"location {match.group(1).strip()} keeps the per-IP cap when it names its own",
                  "limit_conn conn_ip" in body)

    check("requests are metered per IP and per client",
          "limit_req_zone  $mcp_client" in TEMPLATE and "limit_req_zone  $binary_remote_addr" in TEMPLATE)
    # A `return` inside an `if` is answered in the rewrite phase, before
    # the preaccess phase where limit_req lives, so it would never be
    # metered. The one exception is the auth_request target, which is an
    # internal subrequest whose parent has already been charged.
    internal = re.search(r"location = /internal/mcp-auth \{(?:[^{}]|\{[^{}]*\})*\}", TEMPLATE_CODE)
    check("the auth_request target exists", internal is not None)
    if internal:
        outside = TEMPLATE_CODE.replace(internal.group(0), "")
        check("no refusal is made with `if` plus `return`", "if (" not in outside,
              "a rewrite-phase return skips the preaccess phase where limit_req lives")
    check("the body size is bounded", re.search(r"client_max_body_size\s+4m;", TEMPLATE) is not None)
    check("slow clients time out", "client_header_timeout" in TEMPLATE and "client_body_timeout" in TEMPLATE)


# ---------------------------------------------------------------------
# What the app is told about the caller
# ---------------------------------------------------------------------
def upstream_headers_are_ours() -> None:
    print("the app is told only what nginx knows")
    check("X-Forwarded-For is written, never appended",
          "$proxy_add_x_forwarded_for" not in TEMPLATE_CODE,
          "appending trusts a client-supplied header")
    xff = re.findall(r"proxy_set_header X-Forwarded-For\s+(\S+);", TEMPLATE)
    check("every X-Forwarded-For carries the peer address", bool(xff) and all(v == "$remote_addr" for v in xff),
          str(xff))
    check("a client-supplied X-MCP-Client is dropped",
          'proxy_set_header X-MCP-Client ""' in TEMPLATE,
          "otherwise it is forwarded verbatim on every public path")
    check("the client's own credential stops at nginx",
          'proxy_set_header Authorization "Bearer ${INTERNAL_TOKEN}"' in TEMPLATE)
    mcp = re.search(r"location /mcp \{(.*?)\n    \}", TEMPLATE, re.S)
    check("/mcp names the authenticated client", mcp is not None
          and "proxy_set_header X-MCP-Client      $mcp_client;" in mcp.group(1))


# ---------------------------------------------------------------------
# OAuth facade
# ---------------------------------------------------------------------
def oauth_metadata_matches_behaviour() -> None:
    print("the OAuth metadata describes what the code does")
    metadata = re.search(r"oauth-authorization-server.*?return 200 '(.*?)';", TEMPLATE, re.S)
    check("the authorization-server document exists", metadata is not None)
    if metadata:
        doc = metadata.group(1)
        # Every claim in the document has to be one the handler enforces.
        if "code_challenge_methods_supported" in doc:
            check("an advertised PKCE method is actually verified",
                  "code_verifier" in OAUTH_JS and "sha256Base64Url" in OAUTH_JS)
            check("only S256 is advertised", '"code_challenge_methods_supported":["S256"]' in doc)
            check("plain is refused rather than silently accepted",
                  "only the S256 code_challenge_method is supported" in OAUTH_JS)
        if "authorization_code" in doc:
            check("an advertised code grant redeems a real code",
                  "grant === 'authorization_code'" in OAUTH_JS and "function redeem" in OAUTH_JS)
        check("dynamic client registration is not advertised", "registration_endpoint" not in doc)

    check("codes are single use", ".delete(code)" in OAUTH_JS)
    check("codes expire", "timeout=" in TEMPLATE and "js_shared_dict_zone" in TEMPLATE)
    # njs 1.0.0 answers undefined from pop() on a zone that has a timeout,
    # which would refuse every code. get()+delete() is the working form and
    # is just as atomic for single use.
    check("the njs pop() trap is not reintroduced", ".pop(" not in OAUTH_JS)
    check("codes come from a cryptographic source",
          "crypto.getRandomValues" in OAUTH_JS,
          "$request_id is random() seeded from the pid and the clock, and every "
          "caller of /oauth/authorize is handed one of its outputs")
    check("the client is authenticated before a code is redeemed",
          OAUTH_JS.index("constantTimeEquals(expectedDigest") < OAUTH_JS.index("redeem(r, presented.id"))
    check("secrets are compared without an early exit", "function constantTimeEquals" in OAUTH_JS)
    check("the secret is never stored, only its digest", "sha256Hex" in OAUTH_JS)
    check("the token endpoint is not cross-origin readable",
          re.search(r"location = /oauth/token \{(?:[^{}]|\{[^{}]*\})*\}", TEMPLATE) is not None
          and "Access-Control-Allow-Origin"
          not in re.search(r"location = /oauth/token \{(?:[^{}]|\{[^{}]*\})*\}", TEMPLATE).group(0),
          "any page could otherwise drive credential guessing from its visitors")
    check("the redirect target is allowlisted", "$oauth_redirect_allowed" in TEMPLATE)
    # njs returns an ARRAY for a repeated query parameter, so a direct
    # r.args read plus a string method is a 500 waiting for the first
    # client that sends ?x=1&x=2.
    direct = [line.strip() for line in OAUTH_JS.splitlines()
              if "r.args" in line and "function arg(" not in line and "var value = r.args[name]" not in line]
    check("query parameters are read through the array-safe helper", not direct, str(direct))
    allowlist = re.search(r"map \$oauth_redirect_uri \$oauth_redirect_allowed \{(.*?)\}", TEMPLATE, re.S)
    check("the allowlist exists", allowlist is not None)
    if allowlist:
        entries = [line.strip() for line in allowlist.group(1).splitlines()
                   if line.strip() and not line.strip().startswith("#")
                   and not line.strip().startswith("default")]
        check("every allowed callback is matched case-sensitively",
              bool(entries) and all(e.startswith('"~^') for e in entries),
              str([e for e in entries if not e.startswith('"~^')]))
    check("a PKCE challenge is bounded before it is stored",
          "{43,128}" in OAUTH_JS,
          "an unbounded challenge is stored verbatim and evicts live codes")
    check("the callback binding is not optional at redemption",
          "entry.u && entry.u !== presentedRedirect" in OAUTH_JS,
          "requiring the client to supply the field is what makes it a binding")
    check("bodies large enough to spill to disk are bounded away",
          re.search(r"client_body_buffer_size\s+512k;", TEMPLATE) is not None,
          "nginx writes a body bigger than this to a temporary file")


def logged_values_are_sanitised() -> None:
    print("attacker-controlled values are sanitised before they are logged")
    check("a sanitiser exists", "function safe(" in OAUTH_JS)
    check("it bounds the length", "out.length < 100" in OAUTH_JS,
          "an unbounded redirect_uri is a slow disk-fill")
    check("it drops the characters that forge a line or break a field",
          all(c in OAUTH_JS for c in ("0x20", "0x7f", "0x22", "0x5c")))
    # Every diagnostic must go through diag(), which sanitises. A bare
    # r.error() with a concatenated value would not.
    raw_errors = [
        line.strip() for line in OAUTH_JS.splitlines()
        # diag() is the sanitiser's own call site: `text` is already safe().
        if "r.error(" in line and "safe(" not in line and "r.error('oauth: ' + text)" not in line
    ]
    check("no diagnostic bypasses the sanitiser", not raw_errors, str(raw_errors))
    check("the reason reaches the operator without the error log",
          "$oauth_diag" in TEMPLATE and "oauth_diag" in OAUTH_JS)


# ---------------------------------------------------------------------
# Compose
# ---------------------------------------------------------------------
def compose_is_pinned_and_bounded() -> None:
    print("compose pins its images and bounds its logs")
    images = re.findall(r"^\s+image:\s*(\S+)", COMPOSE, re.M)
    check("no image floats on latest", all(not i.endswith(":latest") for i in images), str(images))
    check("every image carries a tag", all(":" in i for i in images), str(images))
    check("certbot is pinned", any(i.startswith("certbot/certbot:v") for i in images), str(images))
    dependabot = (ROOT / ".github/dependabot.yml").read_text()
    check("Dependabot watches Dockerfiles", 'package-ecosystem: "docker"' in dependabot)
    check("and compose files, which the docker ecosystem does not read",
          'package-ecosystem: "docker-compose"' in dependabot,
          "the certbot pin lives in docker-compose.yaml")
    check("the proxy build context excludes the credential maps",
          "secrets/" in (ROOT / "deploy/nginx/.dockerignore").read_text(),
          "docker build ./deploy/nginx would otherwise ship every client token to the daemon")
    for dockerfile in ("Dockerfile", "deploy/nginx/Dockerfile"):
        froms = re.findall(r"^FROM (\S+)", (ROOT / dockerfile).read_text(), re.M)
        check(f"{dockerfile} pins its base images", bool(froms)
              and all(":" in f and not f.endswith(":latest") for f in froms), str(froms))
    check("log files are capped", 'max-size: "10m"' in COMPOSE and 'max-file: "3"' in COMPOSE)
    body = re.search(r"^services:\n(.*?)(?=^\w)", COMPOSE_CODE, re.S | re.M)
    check("a services block exists", body is not None)
    services = re.findall(r"^  (\w+):$", body.group(1), re.M) if body else []
    check("three services", sorted(services) == ["app", "certbot", "nginx"], str(services))
    check("every service caps its logs", COMPOSE_CODE.count("logging: *logging") == len(services))
    app = re.search(r"^  app:\n(.*?)(?=^  \w+:|\Z)", body.group(1), re.S | re.M) if body else None
    check("the app service is defined", app is not None)
    check("the app is not published to the host", app is not None and "ports:" not in app.group(1),
          "expose only; the internal token is what makes that safe on Linux")
    check("the app keeps its own bearer auth on the internal hop",
          "ESTNLTK_MCP_AUTH_TOKEN: ${INTERNAL_TOKEN" in COMPOSE)
    check("nginx does not wait for a healthy app",
          "service_healthy" not in COMPOSE_CODE,
          "it serves the ACME challenge, so it has to start even when the app is down")

    # Every ${PLACEHOLDER} in the template has to be in the envsubst
    # filter, or it arrives as an empty string with no error anywhere.
    placeholders = set(re.findall(r"\$\{(\w+)\}", TEMPLATE))
    envsubst = re.search(r'NGINX_ENVSUBST_FILTER: "\^\((.+?)\)\$', COMPOSE)
    check("the envsubst filter is declared", envsubst is not None)
    if envsubst:
        allowed = set(envsubst.group(1).split("|"))
        check("every template placeholder is substituted", placeholders <= allowed,
              f"missing from the filter: {sorted(placeholders - allowed)}")
        check("the filter lists nothing the template does not use", allowed <= placeholders,
              f"stale: {sorted(allowed - placeholders)}")


def ignores_keep_secrets_out() -> None:
    print("secrets stay out of git and out of the image")
    gitignore = (ROOT / ".gitignore").read_text()
    for pattern in (".env", "deploy/nginx/secrets/*.map", "deploy/letsencrypt/"):
        check(f"git ignores {pattern}", pattern in gitignore)
    check(".gitignore ends with a newline", gitignore.endswith("\n"))
    dockerignore = (ROOT / ".dockerignore").read_text()
    for pattern in ("deploy/", ".env", "docker-compose.yaml"):
        check(f"the app image excludes {pattern}", pattern in dockerignore)
    tracked = subprocess.run(["git", "ls-files", "deploy/nginx/secrets"],
                             cwd=ROOT, capture_output=True, text=True).stdout.split()
    check("no committed map file holds a real credential",
          all(f.endswith(".example") for f in tracked), str(tracked),
          )


# ---------------------------------------------------------------------
# The credential scripts, actually run
# ---------------------------------------------------------------------
def sandbox() -> tempfile.TemporaryDirectory:
    box = tempfile.TemporaryDirectory()
    # Not letsencrypt/, which docker creates root-owned on Linux, and not
    # anyone's real credential maps: a checkout that has actually been
    # deployed from should still be able to run this suite.
    shutil.copytree(ROOT / "deploy", Path(box.name) / "deploy",
                    ignore=shutil.ignore_patterns("letsencrypt", "*.map"))
    return box


def sh(script: str, cwd: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", "-c", script], cwd=cwd, capture_output=True, text=True, **kwargs)


def mode_of(path: Path) -> str:
    return oct(stat.S_IMODE(path.stat().st_mode))


def credential_scripts_work() -> None:
    print("the credential scripts do what they say")
    with sandbox() as box:
        run = sh("./deploy/new-token.sh my-laptop", box)
        check("new-token.sh succeeds", run.returncode == 0, run.stderr)
        token = re.search(r"Token: (\S+)", run.stdout)
        check("it prints a token", token is not None, run.stdout)
        tokens_map = Path(box) / "deploy/nginx/secrets/tokens.map"
        check("it writes the map file", tokens_map.exists())
        if token:
            check("the token is 40 alphanumeric characters",
                  re.fullmatch(r"[A-Za-z0-9]{40}", token.group(1)) is not None, token.group(1))
            check("the map maps it to the client id",
                  f'"{token.group(1)}"' in tokens_map.read_text() and "my-laptop;" in tokens_map.read_text())

        # On a shared host the default umask would hand every local account
        # every client's token.
        check("the map file is not readable by others", mode_of(tokens_map) == "0o600", mode_of(tokens_map))
        check("the secrets directory is not traversable by others",
              mode_of(tokens_map.parent) == "0o700", mode_of(tokens_map.parent))

        again = sh("./deploy/new-token.sh my-laptop", box)
        check("a duplicate client is refused", again.returncode != 0, again.stdout)
        check("the refusal names the way out", "revoke-client.sh" in again.stderr, again.stderr)

        bad = sh("./deploy/new-token.sh 'evil; rm -rf /'", box)
        check("a client id with shell metacharacters is refused", bad.returncode != 0, bad.stdout)

        oauth = sh("./deploy/new-oauth-client.sh partner-acme", box)
        check("new-oauth-client.sh succeeds", oauth.returncode == 0, oauth.stderr)
        secret = re.search(r"OAuth Client Secret: (\S+)", oauth.stdout)
        check("it prints a client secret", secret is not None, oauth.stdout)
        secrets_map = (Path(box) / "deploy/nginx/secrets/oauth_secrets.map").read_text()
        tokens_map_text = tokens_map.read_text()
        if secret:
            import hashlib
            digest = hashlib.sha256(secret.group(1).encode()).hexdigest()
            check("only the digest of the secret is stored", digest in secrets_map)
            check("the secret itself is nowhere in the files",
                  secret.group(1) not in secrets_map and secret.group(1) not in tokens_map_text)
        check("the access token is in both maps it needs to be in",
              "partner-acme;" in tokens_map_text
              and '"partner-acme"' in (Path(box) / "deploy/nginx/secrets/oauth_tokens.map").read_text())

        revoke = sh("./deploy/revoke-client.sh partner-acme", box)
        check("revoke-client.sh succeeds", revoke.returncode == 0, revoke.stderr)
        for name in ("tokens.map", "oauth_tokens.map", "oauth_secrets.map"):
            body = (Path(box) / "deploy/nginx/secrets" / name).read_text()
            check(f"revocation clears {name}", "partner-acme" not in body, body)
        check("the other client is untouched", "my-laptop;" in tokens_map.read_text())
        check("revocation keeps the file private", mode_of(tokens_map) == "0o600", mode_of(tokens_map))

        gone = sh("./deploy/revoke-client.sh partner-acme", box)
        check("revoking an unknown client fails loudly", gone.returncode != 0, gone.stdout)


def credentials_fail_closed() -> None:
    print("a credential that cannot be generated is not written")
    with sandbox() as box:
        # openssl missing or broken. A POSIX pipeline reports the status
        # of its last command, so this used to leave an EMPTY token in
        # the map - and an empty map key matches a request that carries
        # no Authorization header at all.
        fake = Path(box) / "fakebin"
        fake.mkdir()
        (fake / "openssl").write_text("#!/bin/sh\nexit 1\n")
        (fake / "openssl").chmod(0o755)
        run = sh(f'PATH="{fake}:$PATH" ./deploy/new-token.sh broken', box)
        check("new-token.sh fails when openssl does", run.returncode != 0, run.stdout)
        tokens_map = Path(box) / "deploy/nginx/secrets/tokens.map"
        body = tokens_map.read_text() if tokens_map.exists() else ""
        check("no empty key reaches the map", '""' not in body, body)
        check("no client line was written at all", "broken" not in body, body)

    print("client ids nginx would misread are refused")
    with sandbox() as box:
        for reserved in ("default", "include", "hostnames", "volatile", "DEFAULT"):
            run = sh(f"./deploy/new-token.sh {reserved}", box)
            check(f"'{reserved}' is refused", run.returncode != 0, run.stdout)
        first = sh("./deploy/new-oauth-client.sh Partner", box)
        check("a mixed-case client is created once", first.returncode == 0, first.stderr)
        # nginx lowercases map keys, so `partner` and `Partner` are one
        # client to it: writing both gives the OAuth maps a duplicate key
        # and the next reload fails.
        again = sh("printf 'n\n' | ./deploy/new-oauth-client.sh partner", box)
        check("its case variant is recognised as the same client",
              "already exists" in again.stdout, again.stdout + again.stderr)

    print("revocation covers every map nginx reads")
    with sandbox() as box:
        run = sh("./deploy/new-token.sh partner-acme", box)
        check("a client exists to revoke", run.returncode == 0, run.stderr)
        secrets = Path(box) / "deploy/nginx/secrets"
        # The README documents splitting clients across several files,
        # and the template includes tokens*.map, so revocation has to
        # look at all of them.
        (secrets / "tokens-partners.map").write_text('"someOtherToken0123456789abcdefghijklmnop"   split-client;\n')
        found = sh("./deploy/revoke-client.sh split-client", box)
        check("a client in a split map is found", found.returncode == 0, found.stdout + found.stderr)
        check("and removed from it",
              "split-client" not in (secrets / "tokens-partners.map").read_text())
        check("the canonical map is untouched", "partner-acme" in (secrets / "tokens.map").read_text())


def env_is_read_not_executed() -> None:
    print(".env is read, never executed")
    with sandbox() as box:
        canary = Path(box) / "canary"
        (Path(box) / ".env").write_text(
            "# a comment\n"
            f'DOMAIN=$(touch "{canary}")mcp.example.ee\n'
            "LETSENCRYPT_EMAIL = you@example.ee   # inline comment\n"
            "INTERNAL_TOKEN=\"abc+def/ghi==\"\n"
            "STAGING=1\n"
        )
        run = sh(". ./deploy/lib.sh; env_value DOMAIN; env_value LETSENCRYPT_EMAIL; "
                 "env_value INTERNAL_TOKEN; env_value STAGING; env_value MISSING", box)
        lines = run.stdout.splitlines()
        check("nothing in .env is executed", not canary.exists(),
              "a command substitution in .env ran as shell")
        check("a bare value is read literally", lines[0] == f'$(touch "{canary}")mcp.example.ee', str(lines))
        check("spaces around = are tolerated", lines[1] == "you@example.ee", str(lines))
        check("an inline comment is dropped", "#" not in lines[1], str(lines))
        check("a quoted value keeps its punctuation", lines[2] == "abc+def/ghi==", str(lines))
        check("a missing key reads empty", len(lines) == 4, str(lines))

        for script in ("init-letsencrypt.sh", "local-cert.sh"):
            body = (Path(box) / "deploy" / script).read_text()
            check(f"{script} no longer sources .env", ". ./.env" not in body)
            check(f"{script} takes its values from env_value", "env_value" in body)


def cert_scripts_are_careful() -> None:
    print("the certificate scripts do not burn the rate limit")
    body = (ROOT / "deploy/init-letsencrypt.sh").read_text()
    check("--force-renewal is gated", "--force-renewal" in body and 'FORCE:-0}" = "1"' in body,
          "Let's Encrypt allows 5 duplicate certificates per week")
    check("the certbot image is not named a second time", "certbot/certbot" not in body,
          "pinning it in compose only works if the scripts go through compose")
    check("an existing certbot lineage is renewed rather than deleted",
          'if [ -f "$CONF_DIR/renewal/$DOMAIN.conf" ]' in body,
          "deleting it first means a failed issuance leaves the host with no certificate")
    check("a failed issuance leaves something nginx can start from",
          "restoring a self-signed placeholder" in body)
    local = (ROOT / "deploy/local-cert.sh").read_text()
    check("local-cert.sh goes through compose too", "certbot/certbot" not in local)
    check("staging is the default", "STAGING=1" in ENV_EXAMPLE)


# ---------------------------------------------------------------------
# Documentation that has to stay true
# ---------------------------------------------------------------------
def docs_match_the_stack() -> None:
    print("the documents describe this stack")
    privacy = (ROOT / "PRIVACY.md").read_text()
    check("PRIVACY.md scopes its promises to the hosted service", "deploy/" in privacy,
          "a self-hosted stack has its own operator and its own trade-offs")
    security = (ROOT / "SECURITY.md").read_text()
    check("SECURITY.md points at the self-host stack", "deploy/" in security)
    check("the root README points at it too", "deploy/README.md" in (ROOT / "README.md").read_text())
    check("the deploy README documents the logging switch", "ACCESS_LOG" in DEPLOY_README)
    check("it documents the proxy-hop setting", "ESTNLTK_MCP_TRUSTED_PROXY_HOPS" in DEPLOY_README)
    check("it no longer claims the app container is unreachable",
          "You cannot connect to the `app` container" not in DEPLOY_README
          and "cannot connect to the app container" not in DEPLOY_README)
    check("token matching is described as case-insensitive",
          "case-INSENSITIVE" in (ROOT / "deploy/nginx/secrets/tokens.map.example").read_text())
    check("the OAuth limits section is honest about what a code proves",
          "7.5" in DEPLOY_README and "client secret" in DEPLOY_README)

    # Files the deploy README links to must exist.
    for link in re.findall(r"\]\((?!https?:)([^)#]+)\)", DEPLOY_README):
        target = (ROOT / "deploy" / link).resolve()
        check(f"deploy/README.md links to a real file: {link}", target.exists(), str(target))


def line_endings_are_lf() -> None:
    print("files that ride into a Linux container keep LF endings")
    attrs = (ROOT / ".gitattributes").read_text()
    for pattern in ("*.sh", "*.envsh", "*.template", "*.js"):
        check(f"{pattern} is pinned to LF", re.search(rf"^{re.escape(pattern)}\s+text eol=lf", attrs, re.M) is not None)
    for path in sorted(ROOT.glob("deploy/**/*")):
        if path.is_file() and path.suffix in (".sh", ".envsh", ".template", ".js", ".map", ".example"):
            check(f"{path.relative_to(ROOT)} has no CR", b"\r" not in path.read_bytes())


def scripts_are_executable() -> None:
    print("the scripts a reader is told to run are runnable")
    for path in sorted(ROOT.glob("deploy/*.sh")):
        if path.name == "lib.sh":
            check("lib.sh is sourced, not executed", not os.access(path, os.X_OK))
            continue
        check(f"{path.name} is executable", os.access(path, os.X_OK))
        check(f"{path.name} sets -eu", re.search(r"^set -eu$", path.read_text(), re.M) is not None)


logging_is_off_by_default()
tls_is_forward_secret()
limits_meter_unauthenticated_traffic()
upstream_headers_are_ours()
oauth_metadata_matches_behaviour()
logged_values_are_sanitised()
compose_is_pinned_and_bounded()
ignores_keep_secrets_out()
credential_scripts_work()
credentials_fail_closed()
env_is_read_not_executed()
cert_scripts_are_careful()
docs_match_the_stack()
line_endings_are_lf()
scripts_are_executable()

if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nall deploy-stack tests passed")
