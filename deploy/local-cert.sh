#!/bin/sh
# Self-signed certificate for local testing.
#
# Let's Encrypt cannot issue a certificate when the stack is published
# on ports other than 80 and 443, because its HTTP-01 challenge always
# connects to the public port 80. This script plants a self-signed
# certificate in the same place instead, so the stack comes up on
# whatever ports you set in .env.
#
# Run from the repo root:  ./deploy/local-cert.sh
#
# Clients must skip certificate verification (curl -k). Do not use this
# for anything reachable from the internet.

set -eu

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
    echo "No .env found. Copy .env.example to .env and fill it in first." >&2
    exit 1
fi

# shellcheck disable=SC1091
. ./deploy/lib.sh

# Read, do not execute: see env_value in deploy/lib.sh.
DOMAIN="$(env_value DOMAIN)"
INTERNAL_TOKEN="$(env_value INTERNAL_TOKEN)"
HTTPS_PORT="$(env_value HTTPS_PORT)"

: "${DOMAIN:?set DOMAIN in .env}"
: "${INTERNAL_TOKEN:?set INTERNAL_TOKEN in .env}"

if [ ! -f deploy/nginx/secrets/tokens.map ]; then
    echo "deploy/nginx/secrets/tokens.map is missing - nginx will not start without it." >&2
    echo "Create it with:  ./deploy/new-token.sh <client-id>" >&2
    exit 1
fi

mkdir -p "./deploy/letsencrypt/conf/live/$DOMAIN" ./deploy/letsencrypt/www

echo "==> Making a self-signed certificate for $DOMAIN (valid 365 days)"
# Generated inside the certbot container so the result does not depend
# on an openssl being present on the host, and so the image stays
# pinned in one place. MSYS_NO_PATHCONV keeps Git Bash on Windows from
# rewriting the container paths and the -subj argument.
MSYS_NO_PATHCONV=1 docker compose run --rm --entrypoint openssl certbot \
    req -x509 -nodes -newkey rsa:2048 -days 365 \
        -keyout "/etc/letsencrypt/live/$DOMAIN/privkey.pem" \
        -out    "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" \
        -subj "/CN=$DOMAIN" \
        -addext "subjectAltName=DNS:$DOMAIN,DNS:localhost,IP:127.0.0.1"

echo "==> Starting the stack"
docker compose up -d

echo
echo "Done. The certificate is self-signed, so clients must skip verification."
echo
echo "  curl -k https://localhost:${HTTPS_PORT:-443}/health"
echo "  curl -ki https://localhost:${HTTPS_PORT:-443}/mcp                # expect 401"
echo "  curl -ki -H 'Authorization: Bearer <token>' https://localhost:${HTTPS_PORT:-443}/mcp"
