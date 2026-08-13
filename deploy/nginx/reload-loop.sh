#!/bin/sh
# Reload nginx every 6 hours so renewed certificates take effect.
#
# certbot rewrites /etc/letsencrypt/live/<domain>/*.pem in its own
# container; nginx holds the old certificate in memory until it is told
# to re-read the files. Renewal happens at 30 days remaining, so a
# 6-hourly reload has ~120 reload opportunities before the old
# certificate would expire.
#
# This script is sourced by the official entrypoint BEFORE nginx starts,
# so it must background itself and return immediately. The subshell is
# reparented to the nginx master (PID 1) and dies with the container.

set -eu

(
    while :; do
        sleep 21600
        nginx -s reload 2>/dev/null || true
    done
) &

exit 0
