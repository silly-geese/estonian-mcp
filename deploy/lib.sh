# shellcheck shell=sh
# Shared helpers for the deploy scripts. Sourced, not executed.
#
# Every script that mints or removes a credential goes through here, so
# the token format, the map file syntax and the file permissions are
# defined in one place.

# Credentials are written by these functions. A default umask of 022
# would leave every client's token world-readable, which on a shared
# host hands them to every local account. 077 makes new files 0600 and
# new directories 0700. nginx parses its config as root, so 0600 loads
# and reloads normally.
umask 077

SECRETS_DIR=deploy/nginx/secrets
TOKENS_MAP="$SECRETS_DIR/tokens.map"
OAUTH_TOKENS_MAP="$SECRETS_DIR/oauth_tokens.map"
OAUTH_SECRETS_MAP="$SECRETS_DIR/oauth_secrets.map"

# 40 alphanumeric characters, comfortably over 200 bits even after nginx
# folds the case away in its map lookup.
#
# The base64 padding and the "+" and "/" characters are stripped rather
# than kept. That matters for the OAuth client secret specifically:
# RFC 6749 percent-encodes the secret before base64 in
# client_secret_basic, so a secret containing "+" or "/" would be
# re-encoded by the client and would no longer match the stored digest.
# Alphanumeric survives that step unchanged.
rand40() {
    _value="$(openssl rand -base64 48 | tr -d '=+/\n' | cut -c1-40)"
    # A POSIX pipeline reports the status of its LAST command, so a
    # missing or failing openssl leaves `cut` returning 0 and this
    # function returning an empty string. An empty token is not a weak
    # credential, it is an open door: the nginx map would gain a `""`
    # key, and $bearer_token is also empty for a request that carries no
    # Authorization header at all.
    case "$_value" in
        [A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9]*)
            ;;
        *)
            echo "openssl produced no usable random value; refusing to write a credential" >&2
            return 1
            ;;
    esac
    if [ "${#_value}" -ne 40 ]; then
        echo "expected a 40-character credential, got ${#_value}; refusing to write it" >&2
        return 1
    fi
    printf '%s\n' "$_value"
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
    # The OAuth maps use the client id as the map KEY, and these four
    # words are map parameters rather than keys. A client called
    # `default` would silently become the map's default value, and one
    # called `include` would make nginx try to open a file.
    case "$(_lower "$1")" in
        default|include|hostnames|volatile)
            echo "client id '$1' is an nginx map keyword; pick another" >&2
            return 1
            ;;
    esac
}

_lower() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

# Every file nginx actually reads, not just the canonical three. The
# template includes tokens*.map, oauth_secrets*.map and
# oauth_tokens*.map, and the README tells operators they may split
# clients across several files - so a revocation that edited only
# tokens.map would leave a working credential behind.
_maps() {
    # shellcheck disable=SC2231  # $1 is a fixed prefix, never a path
    for _f in "$SECRETS_DIR"/$1*.map; do
        [ -f "$_f" ] && printf '%s\n' "$_f"
    done
}

ensure_secrets_dir() {
    # Must exist before compose starts. Docker creates a missing bind
    # mount source itself, and then owns it as root.
    mkdir -p "$SECRETS_DIR"
    # Explicit rather than relying on the umask: the directory may
    # predate this version of the script, or have been created by Docker.
    chmod 700 "$SECRETS_DIR"
}

# Reads one key out of .env without executing it.
#
# `. ./.env` used to do this, which runs the file as shell. Compose's
# parser and POSIX sh disagree about quoting, backticks and $(...), so
# the two would read different values out of the same line - and the
# shell would additionally EXECUTE whatever a substitution contained.
# This reads the three shapes that matter here: a double-quoted value, a
# single-quoted value, or a bare value up to an inline comment. It is
# deliberately NOT a Compose parser: Compose also interpolates ${VAR},
# which this returns literally. The scripts below refuse a value that
# still contains ${, rather than requesting a certificate for the
# literal string.
env_value() {
    [ -f .env ] || return 0
    awk -v key="$1" '
        { sub(/\r$/, "") }                      # a .env written on Windows
        $0 ~ "^[ \t]*(export[ \t]+)?" key "[ \t]*=" {
            sub(/^[ \t]*(export[ \t]+)?[^=]*=[ \t]*/, "")
            c = substr($0, 1, 1)
            if (c == "\"") { sub(/^"/, ""); sub(/".*$/, "") }
            else if (c == "\047") { sub(/^\047/, ""); sub(/\047.*$/, "") }
            else { sub(/[ \t]+#.*$/, ""); sub(/[ \t]+$/, "") }
            value = $0
            found = 1
        }
        END { if (found) print value }
    ' .env
}

# Compose interpolates ${VAR} and $(...) in .env; env_value deliberately
# does not, because expanding it would mean reimplementing Compose. Say
# so rather than requesting a certificate for the literal string.
require_literal() {
    # shellcheck disable=SC2016  # matching the literal characters is the point
    case "$2" in
        *'${'*|*'$('*|*'`'*)
            echo "$1 in .env uses a substitution these scripts do not expand:" >&2
            echo "  $1=$2" >&2
            echo "Write the literal value, or export $1 before running this script." >&2
            return 1
            ;;
    esac
}

# True if the client has a credential in ANY of the three files. It has
# to check all of them: a client whose entry survives in only one file
# still has a working way in, and a caller that checked only tokens.map
# could append a second secret line for the same id. An nginx map takes
# the first match, so a stale duplicate would silently win over the new
# secret.
# The matching is case-INSENSITIVE, because nginx lowercases map keys:
# `Foo` and `foo` are one client to nginx, and writing both would give
# the OAuth maps a duplicate key that makes the next reload fail.
client_exists() {
    _c="$1"
    for _f in $(_maps tokens); do
        grep -qiE "[[:space:]]$_c;[[:space:]]*\$" "$_f" && return 0
    done
    for _f in $(_maps oauth_tokens) $(_maps oauth_secrets); do
        grep -qiE "^\"$_c\"[[:space:]]" "$_f" && return 0
    done
    return 1
}

# Remove every trace of a client from all three files. Safe to call for
# a client that only ever had a plain token and no OAuth credentials.
remove_client() {
    _c="$1"
    for _f in $(_maps tokens); do
        _strip "$_f" "[[:space:]]$_c;[[:space:]]*\$"
    done
    for _f in $(_maps oauth_tokens) $(_maps oauth_secrets); do
        _strip "$_f" "^\"$_c\"[[:space:]]"
    done
}

_strip() {
    [ -f "$1" ] || return 0
    # $$ rather than a fixed name: two operators revoking at once would
    # otherwise write the same temp file and one would move the other's
    # half-written copy over a live credential map.
    _tmp="$1.$$.tmp"
    # grep -v exits 1 when everything matched, which set -e would treat
    # as fatal, hence the guard. -i because nginx matches that way.
    grep -viE "$2" "$1" > "$_tmp" || true
    mv "$_tmp" "$1"
    chmod 600 "$1"
}

# Append "<token>" <client>; to tokens.map, which is what /mcp checks.
add_bearer_token() {
    if [ ! -f "$TOKENS_MAP" ]; then
        echo "# Bearer credential -> client id. See tokens.map.example." > "$TOKENS_MAP"
    fi
    printf '"%s"   %s;\n' "$1" "$2" >> "$TOKENS_MAP"
    chmod 600 "$TOKENS_MAP"
}

# Append <client> -> token to the OAuth lookup, which is what the token
# endpoint returns for that client.
add_oauth_token() {
    if [ ! -f "$OAUTH_TOKENS_MAP" ]; then
        echo "# OAuth client id -> the access token issued to it." > "$OAUTH_TOKENS_MAP"
    fi
    printf '"%s"   "%s";\n' "$1" "$2" >> "$OAUTH_TOKENS_MAP"
    chmod 600 "$OAUTH_TOKENS_MAP"
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
    chmod 600 "$OAUTH_SECRETS_MAP"
}
