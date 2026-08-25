#!/bin/sh
# End-to-end test for the deploy/ stack: builds the nginx image, renders
# the config, and drives the running proxy with curl.
#
#   ./tests/deploy_e2e.sh
#
# Needs docker and openssl. Nothing else, and it touches nothing outside
# a scratch directory and its own containers.
#
# The upstream is a stub that echoes the headers it received, which is
# how the header-rewriting assertions can be made at all: a spoofed
# X-MCP-Client is only interesting if you can see what the app would
# have seen.

set -eu

IMAGE=estonian-mcp-nginx:e2e
NET=estonian-mcp-e2e
PROXY=estonian-mcp-e2e-proxy
STUB=estonian-mcp-e2e-app
HTTPS_PORT=${E2E_HTTPS_PORT:-18443}
HTTP_PORT=${E2E_HTTP_PORT:-18080}
DOMAIN=e2e.example.test
INTERNAL_TOKEN=internal-token-0123456789
CLIENT_TOKEN=e2eClientToken0123456789abcdefghijklmnop
CLIENT_ID=e2e-client
CLIENT_SECRET=e2eClientSecret0123456789abcdefghijklmno
OTHER_ID=other-client
OTHER_SECRET=otherClientSecret0123456789abcdefghijkl
OAUTH_TOKEN=e2eOauthToken0123456789abcdefghijklmnopq

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
WORK=$(mktemp -d)

passed=0
failed=0

ok()  { printf '  PASS %s\n' "$1"; passed=$((passed + 1)); }
bad() { printf '  FAIL %s -- %s\n' "$1" "$2"; failed=$((failed + 1)); }

expect_eq() {
    if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected [$3], got [$2]"; fi
}
expect_contains() {
    case "$2" in *"$3"*) ok "$1" ;; *) bad "$1" "[$3] missing from [$2]" ;; esac
}
expect_missing() {
    case "$2" in *"$3"*) bad "$1" "[$3] should not appear in [$2]" ;; *) ok "$1" ;; esac
}

cleanup() {
    docker rm -f "$PROXY" "$STUB" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------
mkdir -p "$WORK/secrets" "$WORK/certs" "$WORK/stub"
chmod 700 "$WORK/secrets"

digest() { printf '%s' "$1" | openssl dgst -sha256 | awk '{print $NF}'; }
b64url_sha256() { printf '%s' "$1" | openssl dgst -sha256 -binary | openssl base64 | tr '+/' '-_' | tr -d '=\n'; }

cat > "$WORK/secrets/tokens.map" <<MAP
"$CLIENT_TOKEN"   $CLIENT_ID;
"$OAUTH_TOKEN"    oauth-client;
MAP
cat > "$WORK/secrets/oauth_secrets.map" <<MAP
"$CLIENT_ID"   "$(digest "$CLIENT_SECRET")";
"$OTHER_ID"    "$(digest "$OTHER_SECRET")";
MAP
cat > "$WORK/secrets/oauth_tokens.map" <<MAP
"$CLIENT_ID"   "$OAUTH_TOKEN";
"$OTHER_ID"    "$OAUTH_TOKEN";
MAP

mkdir -p "$WORK/certs/live/$DOMAIN"
openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
    -keyout "$WORK/certs/live/$DOMAIN/privkey.pem" \
    -out    "$WORK/certs/live/$DOMAIN/fullchain.pem" \
    -subj "/CN=$DOMAIN" >/dev/null 2>&1

# The stub upstream: same image, so the test pulls nothing extra, and
# njs is already there to render the echo.
cat > "$WORK/stub/stub.js" <<'JS'
function echo(r) {
    var headers = {};
    for (var name in r.headersIn) {
        headers[name.toLowerCase()] = r.headersIn[name];
    }
    r.headersOut['Content-Type'] = 'application/json';
    r.return(200, JSON.stringify({uri: r.uri, args: r.variables.args, headers: headers}));
}
export default { echo };
JS
cat > "$WORK/stub/stub.conf" <<'CONF'
js_path "/etc/nginx/njs/";
js_import stub from stub.js;
server {
    listen 8081;
    location / { js_content stub.echo; }
}
CONF

# ---------------------------------------------------------------------
# Build and start
# ---------------------------------------------------------------------
echo "==> Building $IMAGE"
docker build -q -t "$IMAGE" "$ROOT/deploy/nginx" >/dev/null

docker rm -f "$PROXY" "$STUB" >/dev/null 2>&1 || true
docker network rm "$NET" >/dev/null 2>&1 || true
docker network create "$NET" >/dev/null

docker run -d --name "$STUB" --network "$NET" --network-alias app \
    -v "$WORK/stub/stub.conf:/etc/nginx/conf.d/stub.conf:ro" \
    -v "$WORK/stub/stub.js:/etc/nginx/njs/stub.js:ro" \
    "$IMAGE" >/dev/null

start_proxy() {
    docker rm -f "$PROXY" >/dev/null 2>&1 || true
    docker run -d --name "$PROXY" --network "$NET" \
        -p "$HTTP_PORT:80" -p "$HTTPS_PORT:443" \
        -e DOMAIN="$DOMAIN" \
        -e INTERNAL_TOKEN="$INTERNAL_TOKEN" \
        -e HTTPS_PORT="$HTTPS_PORT" \
        -e ACCESS_LOG="$1" \
        -e NGINX_ENVSUBST_FILTER='^(DOMAIN|INTERNAL_TOKEN|HTTPS_PORT_SUFFIX|ACCESS_LOG)$' \
        -v "$ROOT/deploy/nginx/templates:/etc/nginx/templates:ro" \
        -v "$WORK/secrets:/etc/nginx/secrets:ro" \
        -v "$WORK/certs:/etc/letsencrypt:ro" \
        "$IMAGE" >/dev/null

    i=0
    while [ "$i" -lt 40 ]; do
        if curl -sk -o /dev/null "https://localhost:$HTTPS_PORT/health" 2>/dev/null; then
            return 0
        fi
        i=$((i + 1))
        sleep 0.5
    done
    echo "proxy did not come up:" >&2
    docker logs "$PROXY" >&2 || true
    exit 1
}

echo "==> Starting the proxy with access logging OFF"
start_proxy 0

BASE="https://localhost:$HTTPS_PORT"
CURL="curl -sk --max-time 20"

# Every request below is paced. The shipped per-IP limit is 300r/m, so a
# test that fires as fast as curl can would rate-limit itself and report
# a config bug that is not there. 4 requests a second stays under the
# refill rate; the flood test at the end is the one that goes flat out.
pace()    { sleep 0.25; }
status()  { pace; $CURL -o /dev/null -w '%{http_code}' "$@"; }
body()    { pace; $CURL "$@"; }
headers() { pace; $CURL -D - -o /dev/null "$@"; }

# ---------------------------------------------------------------------
# The config nginx actually rendered
# ---------------------------------------------------------------------
echo "rendered configuration"
RENDERED=$(docker exec "$PROXY" cat /etc/nginx/conf.d/mcp.conf)
expect_contains "envsubst filled DOMAIN" "$RENDERED" "server_name $DOMAIN;"
expect_contains "envsubst filled the internal token" "$RENDERED" "Bearer $INTERNAL_TOKEN"
expect_contains "ACCESS_LOG rendered as 0" "$RENDERED" "default 0;"
# shellcheck disable=SC2016  # the literal ${ is the point
expect_missing "no placeholder survived" "$RENDERED" '${'
expect_eq "nginx -t accepts it" "$(docker exec "$PROXY" nginx -t >/dev/null 2>&1 && echo ok)" "ok"

# ---------------------------------------------------------------------
# Public paths and header rewriting
# ---------------------------------------------------------------------
echo "public paths"
expect_eq "GET /health is 200" "$(status "$BASE/health")" "200"
HEALTH=$(body "$BASE/health")
expect_contains "the app receives the internal token" "$HEALTH" "Bearer $INTERNAL_TOKEN"
expect_eq "unknown path is 404 JSON" "$(body "$BASE/nope")" '{"error":"not_found"}'
expect_eq "POST / is 405" "$(status -X POST "$BASE/")" "405"
expect_contains "the 405 names /mcp" "$(body -X POST "$BASE/")" "/mcp, not /"
expect_eq "HTTP redirects to HTTPS" \
    "$($CURL -o /dev/null -w '%{redirect_url}' "http://localhost:$HTTP_PORT/mcp")" \
    "https://localhost:$HTTPS_PORT/mcp"

echo "header rewriting"
SPOOF=$(body -H "X-MCP-Client: admin" -H "X-Forwarded-For: 9.9.9.9" "$BASE/health")
expect_missing "X-MCP-Client is dropped on public paths" "$SPOOF" "admin"
expect_missing "a client-supplied X-Forwarded-For is not appended" "$SPOOF" "9.9.9.9"

# ---------------------------------------------------------------------
# Bearer auth on /mcp
# ---------------------------------------------------------------------
echo "bearer auth"
expect_eq "no token is 401" "$(status -X POST "$BASE/mcp")" "401"
expect_eq "the 401 body is JSON" "$(body -X POST "$BASE/mcp")" '{"error":"unauthorized"}'
expect_contains "the 401 points at the resource metadata" \
    "$(headers -X POST "$BASE/mcp")" "resource_metadata="
expect_eq "a wrong token is 401" "$(status -X POST -H "Authorization: Bearer nope" "$BASE/mcp")" "401"
expect_eq "a valid token is 200" \
    "$(status -X POST -H "Authorization: Bearer $CLIENT_TOKEN" "$BASE/mcp")" "200"

MCP=$(body -X POST -H "Authorization: Bearer $CLIENT_TOKEN" \
      -H "X-MCP-Client: admin" -H "X-Forwarded-For: 9.9.9.9" "$BASE/mcp")
expect_contains "the client id reaches the app" "$MCP" "\"x-mcp-client\":\"$CLIENT_ID\""
expect_missing "a spoofed client id does not" "$MCP" "admin"
expect_missing "a spoofed X-Forwarded-For does not" "$MCP" "9.9.9.9"
expect_missing "the client's own token never reaches the app" "$MCP" "$CLIENT_TOKEN"
expect_contains "the app sees the internal token instead" "$MCP" "Bearer $INTERNAL_TOKEN"
expect_eq "an unsupported method is 405" \
    "$(status -X PUT -H "Authorization: Bearer $CLIENT_TOKEN" "$BASE/mcp")" "405"
expect_eq "the 405 body is JSON" \
    "$(body -X PUT -H "Authorization: Bearer $CLIENT_TOKEN" "$BASE/mcp")" \
    '{"error":"method_not_allowed"}'

echo "body size limit"
head -c 5000000 /dev/zero | tr '\0' 'a' > "$WORK/big.txt"
expect_eq "an oversized body is 413" \
    "$(status -X POST -H "Authorization: Bearer $CLIENT_TOKEN" --data-binary "@$WORK/big.txt" "$BASE/mcp")" \
    "413"
expect_eq "the 413 body is JSON" \
    "$(body -X POST -H "Authorization: Bearer $CLIENT_TOKEN" --data-binary "@$WORK/big.txt" "$BASE/mcp")" \
    '{"error":"payload_too_large"}'

# ---------------------------------------------------------------------
# OAuth metadata
# ---------------------------------------------------------------------
echo "oauth metadata"
PR=$(body "$BASE/.well-known/oauth-protected-resource")
expect_contains "protected-resource names /mcp" "$PR" "\"resource\":\"https://$DOMAIN:$HTTPS_PORT/mcp\""
expect_eq "the /mcp-suffixed variant answers too" \
    "$(status "$BASE/.well-known/oauth-protected-resource/mcp")" "200"
AS=$(body "$BASE/.well-known/oauth-authorization-server")
expect_contains "authorization-server advertises the token endpoint" "$AS" "/oauth/token"
expect_contains "S256 is advertised" "$AS" '"code_challenge_methods_supported":["S256"]'
expect_missing "DCR is not advertised" "$AS" "registration_endpoint"
expect_eq "an unimplemented /register is 404" "$(status -X POST "$BASE/register")" "404"

# ---------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------
echo "token endpoint"
expect_eq "GET is 405" "$(status "$BASE/oauth/token")" "405"
expect_contains "no credentials is 401 invalid_client" \
    "$(body -X POST "$BASE/oauth/token")" '"error":"invalid_client"'
expect_contains "an unknown client is invalid_client" \
    "$(body -X POST -d "client_id=ghost&client_secret=x" "$BASE/oauth/token")" '"error":"invalid_client"'
expect_contains "a wrong secret is invalid_client" \
    "$(body -X POST -d "client_id=$CLIENT_ID&client_secret=wrong" "$BASE/oauth/token")" '"error":"invalid_client"'
POSTED=$(body -X POST -d "client_id=$CLIENT_ID&client_secret=$CLIENT_SECRET" "$BASE/oauth/token")
expect_contains "client_secret_post gets the client's token" "$POSTED" "\"access_token\":\"$OAUTH_TOKEN\""
BASIC=$(body -X POST -u "$CLIENT_ID:$CLIENT_SECRET" -d "grant_type=client_credentials" "$BASE/oauth/token")
expect_contains "client_secret_basic works too" "$BASIC" "\"access_token\":\"$OAUTH_TOKEN\""
expect_missing "the token response allows no cross-origin reader" \
    "$(headers -X POST -d "client_id=$CLIENT_ID&client_secret=$CLIENT_SECRET" "$BASE/oauth/token")" \
    "Access-Control-Allow-Origin"
expect_contains "an unsupported grant is refused" \
    "$(body -X POST -d "client_id=$CLIENT_ID&client_secret=$CLIENT_SECRET&grant_type=password" "$BASE/oauth/token")" \
    '"error":"unsupported_grant_type"'
expect_contains "the issued token opens /mcp" \
    "$(body -X POST -H "Authorization: Bearer $OAUTH_TOKEN" "$BASE/mcp")" '"x-mcp-client":"oauth-client"'

# ---------------------------------------------------------------------
# Authorization endpoint and PKCE
# ---------------------------------------------------------------------
echo "authorize endpoint"
CALLBACK="http://localhost:8080/callback"
expect_contains "a callback outside the allowlist is refused" \
    "$(body "$BASE/oauth/authorize?client_id=$CLIENT_ID&redirect_uri=https://evil.example/cb")" \
    '"error":"invalid_request"'
expect_contains "an unknown client gets no code" \
    "$(body "$BASE/oauth/authorize?client_id=ghost&redirect_uri=$CALLBACK")" \
    '"error":"invalid_client"'
expect_contains "plain PKCE is refused" \
    "$(body "$BASE/oauth/authorize?client_id=$CLIENT_ID&redirect_uri=$CALLBACK&code_challenge=abc&code_challenge_method=plain")" \
    '"error":"invalid_request"'

location_of() { headers "$1" | tr -d '\r' | sed -n 's/^[Ll]ocation: //p'; }
code_from() { echo "$1" | sed -n 's/.*[?&]code=\([^&]*\).*/\1/p'; }
# A missing code would otherwise surface three assertions later as a
# puzzling "code is required", so name it here.
mint_code() {
    _code=$(code_from "$(location_of "$BASE/oauth/authorize?client_id=$CLIENT_ID&redirect_uri=$CALLBACK$1")")
    [ -n "$_code" ] || _code=NO-CODE-WAS-MINTED
    printf '%s' "$_code"
}

LOC=$(location_of "$BASE/oauth/authorize?client_id=$CLIENT_ID&redirect_uri=$CALLBACK&state=xyz%20123")
expect_contains "the browser is sent to the allowlisted callback" "$LOC" "$CALLBACK?code="
expect_contains "state is handed back, re-encoded" "$LOC" "state=xyz%20123"
CODE=$(code_from "$LOC")
expect_eq "the code is a 32-character request id" "$(printf '%s' "$CODE" | wc -c | tr -d ' ')" "32"

echo "authorization_code grant"
expect_contains "a code exchanges for the token" \
    "$(body -X POST -d "client_id=$CLIENT_ID&client_secret=$CLIENT_SECRET&grant_type=authorization_code&code=$CODE&redirect_uri=$CALLBACK" "$BASE/oauth/token")" \
    "\"access_token\":\"$OAUTH_TOKEN\""
expect_contains "the same code cannot be used twice" \
    "$(body -X POST -d "client_id=$CLIENT_ID&client_secret=$CLIENT_SECRET&grant_type=authorization_code&code=$CODE&redirect_uri=$CALLBACK" "$BASE/oauth/token")" \
    '"error":"invalid_grant"'
expect_contains "an invented code is refused" \
    "$(body -X POST -d "client_id=$CLIENT_ID&client_secret=$CLIENT_SECRET&grant_type=authorization_code&code=deadbeef" "$BASE/oauth/token")" \
    '"error":"invalid_grant"'
expect_contains "the grant needs a code at all" \
    "$(body -X POST -d "client_id=$CLIENT_ID&client_secret=$CLIENT_SECRET&grant_type=authorization_code" "$BASE/oauth/token")" \
    '"error":"invalid_request"'

CODE2=$(mint_code "")
expect_contains "another client cannot redeem it" \
    "$(body -X POST -d "client_id=$OTHER_ID&client_secret=$OTHER_SECRET&grant_type=authorization_code&code=$CODE2" "$BASE/oauth/token")" \
    '"error":"invalid_grant"'
expect_contains "a redirect_uri that changed mid-flow is refused" \
    "$(body -X POST -d "client_id=$CLIENT_ID&client_secret=$CLIENT_SECRET&grant_type=authorization_code&code=$CODE2&redirect_uri=https://claude.ai/api/mcp/auth_callback" "$BASE/oauth/token")" \
    '"error":"invalid_grant"'

echo "PKCE"
VERIFIER=pkce-verifier-0123456789-abcdefghijklmnopqrstuvwxyz
CHALLENGE=$(b64url_sha256 "$VERIFIER")
CODE3=$(mint_code "&code_challenge=$CHALLENGE&code_challenge_method=S256")
expect_contains "a bound code needs a verifier" \
    "$(body -X POST -d "client_id=$CLIENT_ID&client_secret=$CLIENT_SECRET&grant_type=authorization_code&code=$CODE3" "$BASE/oauth/token")" \
    '"error":"invalid_grant"'
CODE4=$(mint_code "&code_challenge=$CHALLENGE&code_challenge_method=S256")
expect_contains "a wrong verifier is refused" \
    "$(body -X POST -d "client_id=$CLIENT_ID&client_secret=$CLIENT_SECRET&grant_type=authorization_code&code=$CODE4&code_verifier=wrong" "$BASE/oauth/token")" \
    '"error":"invalid_grant"'
CODE5=$(mint_code "&code_challenge=$CHALLENGE&code_challenge_method=S256")
expect_contains "the right verifier is accepted" \
    "$(body -X POST -d "client_id=$CLIENT_ID&client_secret=$CLIENT_SECRET&grant_type=authorization_code&code=$CODE5&code_verifier=$VERIFIER" "$BASE/oauth/token")" \
    "\"access_token\":\"$OAUTH_TOKEN\""

# ---------------------------------------------------------------------
# Forward secrecy
# ---------------------------------------------------------------------
echo "TLS"
# The nginx image carries no openssl CLI, so the handshake is driven from
# the host. SECLEVEL=0 is what lets a modern OpenSSL even OFFER a
# static-RSA suite; without it the client would refuse first and the test
# would pass for the wrong reason.
tls_cipher() {
    echo | openssl s_client -connect "localhost:$HTTPS_PORT" -servername "$DOMAIN" \
        -tls1_2 -cipher "$1" 2>/dev/null | sed -n 's/^ *Cipher *: *//p' | head -1
}
if [ -n "$(tls_cipher 'ECDHE-RSA-AES128-GCM-SHA256')" ]; then
    expect_eq "an ECDHE suite is accepted" \
        "$(tls_cipher 'ECDHE-RSA-AES128-GCM-SHA256')" "ECDHE-RSA-AES128-GCM-SHA256"
    STATIC=$(tls_cipher 'AES128-SHA:AES256-SHA:AES128-SHA256@SECLEVEL=0')
    case "$STATIC" in
        ""|"(NONE)"|"0000") ok "a static-RSA suite is refused, so sessions stay forward-secret" ;;
        *) bad "a static-RSA suite is refused" "negotiated $STATIC" ;;
    esac
else
    echo "  SKIP forward-secrecy checks (this openssl cannot drive the handshake)"
fi

# ---------------------------------------------------------------------
# Logging: off by default, and never carrying a credential
# ---------------------------------------------------------------------
echo "logging with ACCESS_LOG=0"
$CURL -o /dev/null -X POST -H "Authorization: Bearer $CLIENT_TOKEN" "$BASE/mcp?config=SUPERSECRETCONFIG" || true
$CURL -o /dev/null -X PUT "$BASE/mcp?config=SUPERSECRETCONFIG" || true
$CURL -o /dev/null -X POST -d "client_id=ghost&client_secret=x" "$BASE/oauth/token" || true
sleep 1
LOGS=$(docker logs "$PROXY" 2>&1 | grep -v '/docker-entrypoint' | grep -v 'Configuration complete' || true)
expect_missing "no request line is logged at all" "$LOGS" '"POST /mcp"'
expect_missing "the query-string credential never reaches a log" "$LOGS" "SUPERSECRETCONFIG"
expect_missing "no client token in the logs" "$LOGS" "$CLIENT_TOKEN"

echo "logging with ACCESS_LOG=1"
start_proxy 1
$CURL -o /dev/null -X POST -H "Authorization: Bearer $CLIENT_TOKEN" "$BASE/mcp?config=SUPERSECRETCONFIG" || true
$CURL -o /dev/null -X PUT "$BASE/mcp?config=SUPERSECRETCONFIG" || true
# A forged log line, percent-encoded, in the one field that is echoed.
$CURL -o /dev/null -X POST \
    --data-raw 'client_id=zz%0A2026%2F01%2F01+00%3A00%3A00+%5Berror%5D+FORGED&client_secret=x' \
    "$BASE/oauth/token" || true
sleep 1
LOGS=$(docker logs "$PROXY" 2>&1 | grep -v '/docker-entrypoint' | grep -v 'Configuration complete' || true)
expect_contains "requests are logged when asked for" "$LOGS" '"POST /mcp" 200 client='
expect_contains "the client id is named" "$LOGS" "client=$CLIENT_ID"
expect_missing "the query string is still not logged" "$LOGS" "SUPERSECRETCONFIG"
expect_missing "nor is the bearer token" "$LOGS" "$CLIENT_TOKEN"
expect_contains "an oauth failure gives its reason" "$LOGS" 'diag="unknown client: zz.'
FORGED_LINES=$(printf '%s\n' "$LOGS" | grep -c '^2026/01/01' || true)
expect_eq "the injected newline forges no log line" "$FORGED_LINES" "0"

# ---------------------------------------------------------------------
# Rate limiting applies to REJECTED traffic, which is the whole point of
# keeping the 401 out of the rewrite phase.
# ---------------------------------------------------------------------
echo "rate limiting"
i=0
URLS=""
while [ "$i" -lt 200 ]; do
    URLS="$URLS $BASE/mcp"
    i=$((i + 1))
done
# shellcheck disable=SC2086
CODES=$($CURL -o /dev/null -w '%{http_code}\n' -X POST $URLS | sort | uniq -c | tr '\n' ' ')
expect_contains "unauthenticated floods hit the per-IP limit" "$CODES" "429"


echo
if [ "$failed" -gt 0 ]; then
    printf '%s passed, %s FAILED\n' "$passed" "$failed"
    exit 1
fi
printf 'all %s deploy end-to-end checks passed\n' "$passed"
