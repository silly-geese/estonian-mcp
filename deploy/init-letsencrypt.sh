#!/bin/sh
# One-time bootstrap for the Let's Encrypt certificate.
#
# There is a chicken-and-egg problem: nginx refuses to start when the
# ssl_certificate file is missing, and certbot's HTTP-01 challenge needs
# nginx running to serve /.well-known/acme-challenge/. So this script
# plants a throwaway self-signed certificate, starts nginx with it, gets
# the real one, and reloads.
#
# Run once from the repo root:  ./deploy/init-letsencrypt.sh
# Renewal after that is automatic (the certbot service in compose).
#
# FORCE=1 skips the "already exists" prompt and asks certbot for a fresh
# certificate even when the current one is nowhere near expiry. Let's
# Encrypt allows 5 duplicate certificates per week, so leave it unset
# unless you mean it.

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
LETSENCRYPT_EMAIL="$(env_value LETSENCRYPT_EMAIL)"
INTERNAL_TOKEN="$(env_value INTERNAL_TOKEN)"
STAGING="$(env_value STAGING)"

: "${DOMAIN:?set DOMAIN in .env}"
: "${LETSENCRYPT_EMAIL:?set LETSENCRYPT_EMAIL in .env}"
: "${INTERNAL_TOKEN:?set INTERNAL_TOKEN in .env}"

if [ ! -f deploy/nginx/secrets/tokens.map ]; then
    echo "deploy/nginx/secrets/tokens.map is missing - nginx will not start without it." >&2
    echo "Create it with:  ./deploy/new-token.sh <client-id>" >&2
    exit 1
fi

CONF_DIR="./deploy/letsencrypt/conf"
WWW_DIR="./deploy/letsencrypt/www"
LIVE_DIR="$CONF_DIR/live/$DOMAIN"

if [ -d "$LIVE_DIR" ] && [ "${FORCE:-0}" != "1" ]; then
    printf 'A certificate for %s already exists. Re-issue? [y/N] ' "$DOMAIN"
    read -r answer
    case "$answer" in
        y|Y) ;;
        *) echo "Nothing to do."; exit 0 ;;
    esac
fi

STAGING_ARG=""
if [ "${STAGING:-1}" = "1" ]; then
    STAGING_ARG="--staging"
    echo "==> STAGING mode: the resulting certificate will NOT be trusted by browsers."
    echo "    Set STAGING=0 in .env and re-run once DNS and port 80 are confirmed working."
fi

# --force-renewal counts against the duplicate-certificate limit (5 per
# week per exact hostname set), so an unconditional one would burn the
# week's budget on a script anyone is likely to run more than once. The
# lineage is deleted below anyway, which is what actually makes certbot
# issue rather than renew.
FORCE_ARG=""
if [ "${FORCE:-0}" = "1" ]; then
    FORCE_ARG="--force-renewal"
fi

mkdir -p "$CONF_DIR" "$WWW_DIR" "$LIVE_DIR"

echo "==> Planting a throwaway self-signed certificate so nginx can start"
# Through compose rather than a bare `docker run`, so the certbot image
# is pinned in exactly one place (docker-compose.yaml) and Dependabot
# can see it. MSYS_NO_PATHCONV keeps Git Bash on Windows from rewriting
# the container paths and the -subj argument.
MSYS_NO_PATHCONV=1 docker compose run --rm --entrypoint openssl certbot \
    req -x509 -nodes -newkey rsa:2048 -days 1 \
        -keyout "/etc/letsencrypt/live/$DOMAIN/privkey.pem" \
        -out    "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" \
        -subj "/CN=localhost"

echo "==> Starting nginx so it can serve the ACME challenge"
docker compose up -d nginx

echo "==> Removing the throwaway certificate"
# certbot would otherwise see a lineage it did not create and refuse to
# take it over. nginx has already loaded it into memory and keeps
# serving until the reload below, so deleting the files is safe.
MSYS_NO_PATHCONV=1 docker compose run --rm --entrypoint sh certbot -c \
    "rm -rf /etc/letsencrypt/live/$DOMAIN \
            /etc/letsencrypt/archive/$DOMAIN \
            /etc/letsencrypt/renewal/$DOMAIN.conf"

echo "==> Requesting the real certificate"
# shellcheck disable=SC2086
MSYS_NO_PATHCONV=1 docker compose run --rm --entrypoint certbot certbot \
    certonly --webroot -w /var/www/certbot \
        -d "$DOMAIN" \
        --email "$LETSENCRYPT_EMAIL" \
        --agree-tos --no-eff-email \
        --non-interactive \
        $FORCE_ARG \
        $STAGING_ARG

echo "==> Reloading nginx onto the real certificate"
docker compose exec nginx nginx -s reload

echo "==> Bringing the whole stack up"
docker compose up -d

echo
echo "Done. Check it with:"
echo "  curl https://$DOMAIN/health"
echo "  curl -i https://$DOMAIN/mcp                              # expect 401"
echo "  curl -i -H 'Authorization: Bearer <token>' https://$DOMAIN/mcp"
