#!/bin/sh
# Create an OAuth client for a connector that cannot send a static
# bearer token, such as the Claude custom connector.
#
#   ./deploy/new-oauth-client.sh partner-acme
#
# Creates all three credentials a client needs, because they are useless
# apart:
#
#   client_secret   checked by auth_basic on /oauth/token. This is the
#                   only real protection the OAuth facade has.
#   access_token    what /oauth/token hands back to THIS client, looked
#                   up by client id. Every client gets its own.
#   tokens.map line what /mcp checks. The access token goes in here
#                   under the client id, so an OAuth client is a normal
#                   client everywhere downstream: its own name in the
#                   log, its own rate-limit bucket, its own revocation.
#
# Takes effect on reload. Nothing here is baked into the container.

set -eu

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
. ./deploy/lib.sh

CLIENT="${1:-}"
require_client_id "$CLIENT"

ensure_secrets_dir

if client_exists "$CLIENT"; then
    printf 'Client "%s" already exists. Replace its credentials? [y/N] ' "$CLIENT"
    read -r answer
    case "$answer" in
        y|Y) ;;
        *) echo "Nothing to do."; exit 0 ;;
    esac
    remove_client "$CLIENT"
fi

CLIENT_SECRET="$(rand40)"
ACCESS_TOKEN="$(rand40)"

add_oauth_secret "$CLIENT" "$CLIENT_SECRET"
add_oauth_token  "$CLIENT" "$ACCESS_TOKEN"
add_bearer_token "$ACCESS_TOKEN" "$CLIENT"

cat <<EOF

OAuth client "$CLIENT" created.

Apply it:

  docker compose exec nginx nginx -s reload

Put these in the connector's Advanced settings:

  OAuth Client ID:     $CLIENT
  OAuth Client Secret: $CLIENT_SECRET

The access token is issued by /oauth/token and does not need to be
copied anywhere. It is recorded in $OAUTH_TOKENS_MAP
and $TOKENS_MAP.
EOF
