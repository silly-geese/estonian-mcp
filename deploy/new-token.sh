#!/bin/sh
# Mint a static bearer token for a client that can send an
# Authorization header (Claude Code, curl, a script).
#
#   ./deploy/new-token.sh my-laptop
#
# For the Claude custom connector use ./deploy/new-oauth-client.sh
# instead: it cannot send a static header and needs OAuth credentials
# as well.
#
# The token is stored in plain text in the token map, which is what
# nginx compares against, so you can look it up again there. Hand it to
# the client now rather than mailing the map file around.

set -eu

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
. ./deploy/lib.sh

CLIENT="${1:-}"
require_client_id "$CLIENT"

ensure_secrets_dir

if client_exists "$CLIENT"; then
    echo "client '$CLIENT' already has a token in $TOKENS_MAP" >&2
    echo "Remove it first with:  ./deploy/revoke-client.sh $CLIENT" >&2
    exit 1
fi

TOKEN="$(rand40)"
add_bearer_token "$TOKEN" "$CLIENT"

cat <<EOF
Added client '$CLIENT'.

  Token: $TOKEN

Apply it:   docker compose exec nginx nginx -s reload
Client use: Authorization: Bearer $TOKEN
EOF
