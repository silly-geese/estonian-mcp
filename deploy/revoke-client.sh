#!/bin/sh
# Revoke a client completely.
#
#   ./deploy/revoke-client.sh partner-acme
#
# A client can hold credentials in up to three files, and leaving one
# behind leaves a working way in. This removes all of them:
#
#   tokens.map           the bearer token /mcp checks
#   oauth_tokens.map     the token /oauth/token issues
#   oauth_secrets.map    the digest of the OAuth client secret
#
# Works for plain-token clients too; the OAuth files simply have nothing
# to remove.

set -eu

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
. ./deploy/lib.sh

CLIENT="${1:-}"
require_client_id "$CLIENT"

if ! client_exists "$CLIENT"; then
    echo "No credentials found for '$CLIENT'." >&2
    exit 1
fi

remove_client "$CLIENT"

cat <<EOF
Revoked '$CLIENT'.

Apply it:  docker compose exec nginx nginx -s reload

Until you reload, the old credentials still work.
EOF
