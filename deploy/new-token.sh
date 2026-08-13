#!/bin/sh
# Mint a client token and append it to the nginx token map.
#
#   ./deploy/new-token.sh acme-corp
#
# Prints the token once. It is stored in cleartext in
# deploy/nginx/secrets/tokens.map (that file is gitignored and is what nginx
# compares against), so you can always look it up again there - but hand
# it to the client now rather than mailing the map file around.

set -eu

cd "$(dirname "$0")/.."

CLIENT="${1:-}"
if [ -z "$CLIENT" ]; then
    echo "usage: $0 <client-id>" >&2
    exit 1
fi

case "$CLIENT" in
    *[!a-zA-Z0-9_-]*)
        echo "client id must be alphanumeric, dash or underscore only" >&2
        exit 1
        ;;
esac

MAP=deploy/nginx/secrets/tokens.map

if [ -f "$MAP" ] && grep -qE "[[:space:]]$CLIENT;[[:space:]]*\$" "$MAP"; then
    echo "client '$CLIENT' already has a token in $MAP" >&2
    exit 1
fi

# Strip the base64 characters that are awkward inside an nginx map key
# or in a shell one-liner, then take a fixed width. ~40 chars of
# alphanumeric is comfortably over 200 bits.
TOKEN="$(openssl rand -base64 48 | tr -d '=+/\n' | cut -c1-40)"

# The directory must exist before compose starts, otherwise Docker
# creates it itself and the bind mount can end up owned by root.
mkdir -p "$(dirname "$MAP")"

if [ ! -f "$MAP" ]; then
    echo "# Bearer credential -> client id. See tokens.map.example." > "$MAP"
fi

printf '"%s"   %s;\n' "$TOKEN" "$CLIENT" >> "$MAP"

echo "Added client '$CLIENT'."
echo
echo "  Token: $TOKEN"
echo
echo "Apply it:   docker compose exec nginx nginx -s reload"
echo "Client use: Authorization: Bearer $TOKEN"
