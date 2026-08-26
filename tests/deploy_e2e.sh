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
CLIENT_ID=e2e-client
CLIENT_TOKEN=e2eClientToken0123456789abcdefghijklmnop
# Two OAuth clients, each with its OWN access token. Sharing one token
# between them would let a regression that collapses client identity,
# quota or revocation pass unnoticed.
OAUTH_ID=e2e-oauth
OAUTH_SECRET=e2eClientSecret0123456789abcdefghijklmno
OAUTH_TOKEN=e2eOauthToken0123456789abcdefghijklmnopq
OTHER_ID=e2e-other
OTHER_SECRET=otherClientSecret0123456789abcdefghijkl
OTHER_TOKEN=otherOauthToken0123456789abcdefghijklmno

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
"$OAUTH_TOKEN"    $OAUTH_ID;
"$OTHER_TOKEN"    $OTHER_ID;
MAP
cat > "$WORK/secrets/oauth_secrets.map" <<MAP
"$OAUTH_ID"   "$(digest "$OAUTH_SECRET")";
"$OTHER_ID"   "$(digest "$OTHER_SECRET")";
MAP
cat > "$WORK/secrets/oauth_tokens.map" <<MAP
"$OAUTH_ID"   "$OAUTH_TOKEN";
"$OTHER_ID"   "$OTHER_TOKEN";
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
        # -f, so a 502 from nginx while the stub is still starting does
        # not count as ready and race every assertion after it.
        if curl -fsk -o /dev/null "https://localhost:$HTTPS_PORT/health" 2>/dev/null; then
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
# HTTP/2 sends header names in lower case, so every header assertion
# compares against a lowercased copy. Without this, a check for the
# ABSENCE of a header passes whether or not the header is there.
lheaders() { headers "$@" | tr '[:upper:]' '[:lower:]'; }

# ---------------------------------------------------------------------
# The config nginx actually rendered
# ---------------------------------------------------------------------
echo "rendered configuration"
RENDERED=$(docker exec "$PROXY" cat /etc/nginx/conf.d/mcp.conf)
expect_contains "envsubst filled DOMAIN" "$RENDERED" "server_name $DOMAIN;"
expect_contains "envsubst filled the internal token" "$RENDERED" "Bearer $INTERNAL_TOKEN"
# shellcheck disable=SC2016  # matching the literal $status is the point
expect_contains "ACCESS_LOG rendered as 0" "$RENDERED" 'map "0:$status" $mcp_log' 
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
# nginx answers a malformed request line before it picks a server block,
# so it keeps the built-in page rather than becoming a 500 through an
# error_page redirect that has no context to run in.
expect_eq "a malformed request line is refused as a 400" \
    "$(status "$BASE/health%00")" "400"
expect_eq "POST / is 405" "$(status -X POST "$BASE/")" "405"
expect_contains "the 405 names /mcp" "$(body -X POST "$BASE/")" "/mcp, not /"
expect_eq "HTTP redirects to HTTPS at the configured domain" \
    "$($CURL -o /dev/null -w '%{redirect_url}' "http://localhost:$HTTP_PORT/mcp")" \
    "https://$DOMAIN:$HTTPS_PORT/mcp"
# A default_server takes any Host, so echoing it into the Location would
# be an open redirect carrying the original query string along.
expect_eq "a forged Host does not steer the redirect" \
    "$($CURL -o /dev/null -H "Host: evil.example" -w '%{redirect_url}' "http://localhost:$HTTP_PORT/mcp?config=SUPERSECRETCONFIG")" \
    "https://$DOMAIN:$HTTPS_PORT/mcp?config=SUPERSECRETCONFIG"

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
    "$(lheaders -X POST "$BASE/mcp")" "resource_metadata="
expect_contains "the 401 still carries HSTS" \
    "$(lheaders -X POST "$BASE/mcp")" "strict-transport-security"
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
expect_contains "discovery still carries HSTS" \
    "$(lheaders "$BASE/.well-known/oauth-protected-resource")" "strict-transport-security"
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
    "$(body -X POST -d "client_id=$OAUTH_ID&client_secret=wrong" "$BASE/oauth/token")" '"error":"invalid_client"'
POSTED=$(body -X POST -d "client_id=$OAUTH_ID&client_secret=$OAUTH_SECRET" "$BASE/oauth/token")
expect_contains "client_secret_post gets the client's token" "$POSTED" "\"access_token\":\"$OAUTH_TOKEN\""
BASIC=$(body -X POST -u "$OAUTH_ID:$OAUTH_SECRET" -d "grant_type=client_credentials" "$BASE/oauth/token")
expect_contains "client_secret_basic works too" "$BASIC" "\"access_token\":\"$OAUTH_TOKEN\""
expect_missing "the token response allows no cross-origin reader" \
    "$(lheaders -X POST -d "client_id=$OAUTH_ID&client_secret=$OAUTH_SECRET" "$BASE/oauth/token")" \
    "access-control-allow-origin"
expect_contains "an unsupported grant is refused" \
    "$(body -X POST -d "client_id=$OAUTH_ID&client_secret=$OAUTH_SECRET&grant_type=password" "$BASE/oauth/token")" \
    '"error":"unsupported_grant_type"'
expect_contains "the issued token opens /mcp under its own client id" \
    "$(body -X POST -H "Authorization: Bearer $OAUTH_TOKEN" "$BASE/mcp")" "\"x-mcp-client\":\"$OAUTH_ID\""
expect_contains "the second client has a token of its own" \
    "$(body -X POST -d "client_id=$OTHER_ID&client_secret=$OTHER_SECRET" "$BASE/oauth/token")" \
    "\"access_token\":\"$OTHER_TOKEN\""
expect_contains "and its own identity downstream" \
    "$(body -X POST -H "Authorization: Bearer $OTHER_TOKEN" "$BASE/mcp")" "\"x-mcp-client\":\"$OTHER_ID\""

# ---------------------------------------------------------------------
# Authorization endpoint and PKCE
# ---------------------------------------------------------------------
echo "authorize endpoint"
CALLBACK="http://localhost:8080/callback"
# Longer than RFC 7636 allows. Stored verbatim, thousands of these would
# evict the codes of clients still waiting to redeem.
LONG_CHALLENGE=$(printf 'a%.0s' $(seq 1 200))
VERIFIER=pkce-verifier-0123456789-abcdefghijklmnopqrstuvwxyz
CHALLENGE=$(b64url_sha256 "$VERIFIER")
expect_contains "a callback outside the allowlist is refused" \
    "$(body "$BASE/oauth/authorize?client_id=$OAUTH_ID&redirect_uri=https://evil.example/cb")" \
    '"error":"invalid_request"'
expect_contains "an unknown client gets no code" \
    "$(body "$BASE/oauth/authorize?client_id=ghost&redirect_uri=$CALLBACK")" \
    '"error":"invalid_client"'
expect_eq "the authorize endpoint takes GET only" \
    "$(status -X POST "$BASE/oauth/authorize?client_id=$OAUTH_ID&redirect_uri=$CALLBACK")" "405"
expect_contains "a case-variant callback is not the allowlisted one" \
    "$(body "$BASE/oauth/authorize?client_id=$OAUTH_ID&redirect_uri=http://localhost:8080/CALLBACK")" \
    '"error":"invalid_request"'
expect_contains "an oversized code_challenge is refused before it is stored" \
    "$(body "$BASE/oauth/authorize?client_id=$OAUTH_ID&redirect_uri=$CALLBACK&code_challenge=$LONG_CHALLENGE&code_challenge_method=S256")" \
    '"error":"invalid_request"'
expect_contains "a short code_challenge is refused too" \
    "$(body "$BASE/oauth/authorize?client_id=$OAUTH_ID&redirect_uri=$CALLBACK&code_challenge=tooshort&code_challenge_method=S256")" \
    '"error":"invalid_request"'
# njs returns an ARRAY for a repeated parameter, and a string method on
# that array throws inside the handler: an HTML 500 with, under the
# shipped log settings, no explanation anywhere. The first value wins.
DUPLICATED="$BASE/oauth/authorize?client_id=$OAUTH_ID&client_id=$OTHER_ID&redirect_uri=$CALLBACK&code_challenge=$CHALLENGE&code_challenge_method=plain&code_challenge_method=S256"
expect_eq "a repeated query parameter does not crash the handler" \
    "$(status "$DUPLICATED")" "400"
expect_contains "the first value of the repeat is the one that counts" \
    "$(body "$DUPLICATED")" "only the S256 code_challenge_method is supported"
expect_contains "plain PKCE is refused" \
    "$(body "$BASE/oauth/authorize?client_id=$OAUTH_ID&redirect_uri=$CALLBACK&code_challenge=abc&code_challenge_method=plain")" \
    '"error":"invalid_request"'

location_of() { headers "$1" | tr -d '\r' | sed -n 's/^[Ll]ocation: //p'; }
code_from() { echo "$1" | sed -n 's/.*[?&]code=\([^&]*\).*/\1/p'; }
# A missing code would otherwise surface three assertions later as a
# puzzling "code is required", so name it here.
mint_code() {
    _code=$(code_from "$(location_of "$BASE/oauth/authorize?client_id=$OAUTH_ID&redirect_uri=$CALLBACK$1")")
    [ -n "$_code" ] || _code=NO-CODE-WAS-MINTED
    printf '%s' "$_code"
}

# Every "invalid_grant" assertion below would also pass against a code
# that was never minted, so each code is checked for what it is first.
assert_code() {
    expect_eq "$1 is a real code" "$(printf '%s' "$2" | grep -cE '^[0-9a-f]{32}$')" "1"
}

LOC=$(location_of "$BASE/oauth/authorize?client_id=$OAUTH_ID&redirect_uri=$CALLBACK&state=xyz%20123")
expect_contains "the browser is sent to the allowlisted callback" "$LOC" "$CALLBACK?code="
expect_contains "state is handed back, re-encoded" "$LOC" "state=xyz%20123"
CODE=$(code_from "$LOC")
expect_eq "the code is 32 hex characters" \
    "$(printf '%s' "$CODE" | grep -cE '^[0-9a-f]{32}$')" "1"
CODE_B=$(mint_code "")
CODE_C=$(mint_code "")
expect_eq "two outstanding codes differ" "$([ "$CODE_B" != "$CODE_C" ] && echo differ)" "differ"
expect_eq "and neither repeats the first" \
    "$([ "$CODE_B" != "$CODE" ] && [ "$CODE_C" != "$CODE" ] && echo differ)" "differ"

echo "authorization_code grant"
expect_contains "a code exchanges for the token" \
    "$(body -X POST -d "client_id=$OAUTH_ID&client_secret=$OAUTH_SECRET&grant_type=authorization_code&code=$CODE&redirect_uri=$CALLBACK" "$BASE/oauth/token")" \
    "\"access_token\":\"$OAUTH_TOKEN\""
expect_contains "the same code cannot be used twice" \
    "$(body -X POST -d "client_id=$OAUTH_ID&client_secret=$OAUTH_SECRET&grant_type=authorization_code&code=$CODE&redirect_uri=$CALLBACK" "$BASE/oauth/token")" \
    '"error":"invalid_grant"'
expect_contains "an invented code is refused" \
    "$(body -X POST -d "client_id=$OAUTH_ID&client_secret=$OAUTH_SECRET&grant_type=authorization_code&code=deadbeef" "$BASE/oauth/token")" \
    '"error":"invalid_grant"'
expect_contains "the grant needs a code at all" \
    "$(body -X POST -d "client_id=$OAUTH_ID&client_secret=$OAUTH_SECRET&grant_type=authorization_code" "$BASE/oauth/token")" \
    '"error":"invalid_request"'

CODE2=$(mint_code "")
assert_code CODE2 "$CODE2"
expect_contains "another client cannot redeem it" \
    "$(body -X POST -d "client_id=$OTHER_ID&client_secret=$OTHER_SECRET&grant_type=authorization_code&code=$CODE2" "$BASE/oauth/token")" \
    '"error":"invalid_grant"'
# A FRESH code for each of the next two: redemption deletes the code
# before it checks ownership or callback, so reusing CODE2 would test
# nothing but the deletion.
CODE_R1=$(mint_code "")
assert_code CODE_R1 "$CODE_R1"
expect_contains "a redirect_uri that changed mid-flow is refused" \
    "$(body -X POST -d "client_id=$OAUTH_ID&client_secret=$OAUTH_SECRET&grant_type=authorization_code&code=$CODE_R1&redirect_uri=https://claude.ai/api/mcp/auth_callback" "$BASE/oauth/token")" \
    '"error":"invalid_grant"'
CODE_R2=$(mint_code "")
assert_code CODE_R2 "$CODE_R2"
expect_contains "omitting redirect_uri does not skip the binding" \
    "$(body -X POST -d "client_id=$OAUTH_ID&client_secret=$OAUTH_SECRET&grant_type=authorization_code&code=$CODE_R2" "$BASE/oauth/token")" \
    '"error":"invalid_grant"'

echo "PKCE"
CODE3=$(mint_code "&code_challenge=$CHALLENGE&code_challenge_method=S256")
assert_code CODE3 "$CODE3"
expect_contains "a bound code needs a verifier" \
    "$(body -X POST -d "client_id=$OAUTH_ID&client_secret=$OAUTH_SECRET&grant_type=authorization_code&code=$CODE3&redirect_uri=$CALLBACK" "$BASE/oauth/token")" \
    '"error":"invalid_grant"'
CODE4=$(mint_code "&code_challenge=$CHALLENGE&code_challenge_method=S256")
assert_code CODE4 "$CODE4"
expect_contains "a wrong verifier is refused" \
    "$(body -X POST -d "client_id=$OAUTH_ID&client_secret=$OAUTH_SECRET&grant_type=authorization_code&code=$CODE4&redirect_uri=$CALLBACK&code_verifier=wrong" "$BASE/oauth/token")" \
    '"error":"invalid_grant"'
CODE5=$(mint_code "&code_challenge=$CHALLENGE&code_challenge_method=S256")
assert_code CODE5 "$CODE5"
expect_contains "the right verifier is accepted" \
    "$(body -X POST -d "client_id=$OAUTH_ID&client_secret=$OAUTH_SECRET&grant_type=authorization_code&code=$CODE5&redirect_uri=$CALLBACK&code_verifier=$VERIFIER" "$BASE/oauth/token")" \
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

echo "a fault is logged even with ACCESS_LOG=0"
docker stop "$STUB" >/dev/null
i=0
FAULT_STATUS=000
FAULT_BODY=
while [ "$i" -lt 30 ]; do
    FAULT_STATUS=$($CURL -o /dev/null -w '%{http_code}' "$BASE/health" || echo 000)
    case "$FAULT_STATUS" in 5*) FAULT_BODY=$(body "$BASE/health"); break ;; esac
    i=$((i + 1))
    sleep 0.5
done
sleep 1
FAULT_LOGS=$(docker logs "$PROXY" 2>&1 | grep -v '/docker-entrypoint' || true)
docker start "$STUB" >/dev/null
i=0
while [ "$i" -lt 30 ] && ! curl -fsk -o /dev/null "$BASE/health" 2>/dev/null; do
    i=$((i + 1))
    sleep 0.5
done
expect_contains "the upstream really went down" "$FAULT_STATUS" "5"
expect_eq "an upstream fault answers JSON, not nginx's HTML page" \
    "$FAULT_BODY" '{"error":"upstream_unavailable"}'
expect_contains "the fault is logged with no access log configured" \
    "$FAULT_LOGS" "\"GET /health\" $FAULT_STATUS"
expect_missing "and still without a query string or a token" "$FAULT_LOGS" "SUPERSECRETCONFIG"

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
# Port 80 is metered for the same reason as 443: it is the port certbot
# cannot afford to lose, and its redirect would be free if it were
# answered in the rewrite phase.
flood() {
    i=0
    URLS=""
    while [ "$i" -lt 200 ]; do
        URLS="$URLS $1"
        i=$((i + 1))
    done
    # shellcheck disable=SC2086
    $CURL -o /dev/null -w '%{http_code}\n' "$2" $URLS | sort | uniq -c | tr '\n' ' '
}
expect_contains "unauthenticated floods on /mcp hit the per-IP limit" \
    "$(flood "$BASE/mcp" -XPOST)" "429"
expect_contains "the redirect on port 80 is metered as well" \
    "$(flood "http://localhost:$HTTP_PORT/mcp" -XGET)" "429"


echo
if [ "$failed" -gt 0 ]; then
    printf '%s passed, %s FAILED\n' "$passed" "$failed"
    exit 1
fi
printf 'all %s deploy end-to-end checks passed\n' "$passed"
