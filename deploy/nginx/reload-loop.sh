#!/bin/sh
# Reload nginx every 6 hours so renewed certificates take effect.
#
# certbot rewrites /etc/letsencrypt/live/<domain>/*.pem in its own
# container; nginx holds the old certificate in memory until it is told
# to re-read the files. Renewal happens at 30 days remaining, so a
# 6-hourly reload has ~120 reload opportunities before the old
# certificate would expire.
#
# The official entrypoint runs this BEFORE nginx starts, so it must
# background itself and hand control straight back. The subshell outlives
# it, gets reparented to PID 1, and dies with the container.
#
# The entrypoint dispatches by extension: it EXECUTES *.sh as a
# subprocess and SOURCES *.envsh into its own shell. This file is a .sh,
# so it is executed and ending it with `exit` is correct. Renaming it to
# .envsh would make that `exit` terminate the entrypoint itself and nginx
# would never start, so the ending below works either way.

set -eu

(
    while :; do
        sleep 21600
        nginx -s reload 2>/dev/null || true
    done
) &

# `return` succeeds only when sourced. When executed it fails harmlessly
# and the exit runs instead.
# shellcheck disable=SC2317  # the exit is reached when this file is executed
return 0 2>/dev/null || exit 0
