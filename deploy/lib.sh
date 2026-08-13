# Shared helpers for the deploy scripts. Sourced, not executed.
#
# Every script that mints or removes a credential goes through here, so
# the token format and the map file syntax are defined in one place.

SECRETS_DIR=deploy/nginx/secrets
TOKENS_MAP="$SECRETS_DIR/tokens.map"
OAUTH_TOKENS_MAP="$SECRETS_DIR/oauth_tokens.map"
OAUTH_SECRETS_MAP="$SECRETS_DIR/oauth_secrets.map"

# 40 alphanumeric characters, comfortably over 200 bits.
#
# The base64 padding and the "+" and "/" characters are stripped rather
# than kept. That matters for the OAuth client secret specifically:
# RFC 6749 percent-encodes the secret before base64 in
# client_secret_basic, so a secret containing "+" or "/" would be
# re-encoded by the client and would no longer match the stored digest.
# Alphanumeric survives that step unchanged.
rand40() {
    openssl rand -base64 48 | tr -d '=+/\n' | cut -c1-40
}

require_client_id() {
    case "${1:-}" in
        "")
            echo "usage: $0 <client-id>" >&2
            return 1
            ;;
        *[!a-zA-Z0-9_-]*)
            echo "client id must be alphanumeric, dash or underscore only" >&2
            return 1
            ;;
    esac
}

ensure_secrets_dir() {
    # Must exist before compose starts. Docker creates a missing bind
    # mount source itself, and then owns it as root.
    mkdir -p "$SECRETS_DIR"
}

# True if the client has a credential in ANY of the three files. It has
# to check all of them: a client whose entry survives in only one file
# still has a working way in, and a caller that checked only tokens.map
# could append a second secret line for the same id. An nginx map takes
# the first match, so a stale duplicate would silently win over the new
# secret.
client_exists() {
    { [ -f "$TOKENS_MAP" ]        && grep -qE "[[:space:]]$1;[[:space:]]*\$" "$TOKENS_MAP"; } ||
    { [ -f "$OAUTH_TOKENS_MAP" ]  && grep -qE "^\"$1\"[[:space:]]"           "$OAUTH_TOKENS_MAP"; } ||
    { [ -f "$OAUTH_SECRETS_MAP" ] && grep -qE "^\"$1\"[[:space:]]"           "$OAUTH_SECRETS_MAP"; }
}

# Remove every trace of a client from all three files. Safe to call for
# a client that only ever had a plain token and no OAuth credentials.
remove_client() {
    _c="$1"
    _strip "$TOKENS_MAP"        "[[:space:]]$_c;[[:space:]]*\$"
    _strip "$OAUTH_TOKENS_MAP"  "^\"$_c\"[[:space:]]"
    _strip "$OAUTH_SECRETS_MAP" "^\"$_c\"[[:space:]]"
}

_strip() {
    [ -f "$1" ] || return 0
    # grep -v exits 1 when everything matched, which set -e would treat
    # as fatal, hence the guard.
    grep -vE "$2" "$1" > "$1.tmp" || true
    mv "$1.tmp" "$1"
}

# Append "<token>" <client>; to tokens.map, which is what /mcp checks.
add_bearer_token() {
    if [ ! -f "$TOKENS_MAP" ]; then
        echo "# Bearer credential -> client id. See tokens.map.example." > "$TOKENS_MAP"
    fi
    printf '"%s"   %s;\n' "$1" "$2" >> "$TOKENS_MAP"
}

# Append <client> -> token to the OAuth lookup, which is what the token
# endpoint returns for that client.
add_oauth_token() {
    if [ ! -f "$OAUTH_TOKENS_MAP" ]; then
        echo "# OAuth client id -> the access token issued to it." > "$OAUTH_TOKENS_MAP"
    fi
    printf '"%s"   "%s";\n' "$1" "$2" >> "$OAUTH_TOKENS_MAP"
}

# Store the SHA-256 of the client secret, never the secret. njs hashes
# whatever the client presents and compares digests.
#
# A plain digest rather than a slow KDF is deliberate: the secret is 40
# random alphanumeric characters, not a human-chosen password, so there
# is no dictionary to defend against and nothing for bcrypt to buy.
add_oauth_secret() {
    _digest="$(printf '%s' "$2" | openssl dgst -sha256 | awk '{print $NF}')"
    if [ ! -f "$OAUTH_SECRETS_MAP" ]; then
        echo "# OAuth client id -> SHA-256 of its client secret." > "$OAUTH_SECRETS_MAP"
    fi
    printf '"%s"   "%s";\n' "$1" "$_digest" >> "$OAUTH_SECRETS_MAP"
}
