# syntax=docker/dockerfile:1.7
#
# The stock nginx image plus two things it cannot do on its own:
#   1. A periodic reload, so renewed Let's Encrypt certificates are
#      picked up without restarting the container.
#   2. Removal of the bundled default.conf, which would otherwise claim
#      port 80 as the default server and shadow ours.
#
# Everything else (the config template, the token map, the certificates)
# is bind-mounted at runtime so it can change without a rebuild.
# Stable branch, minor pinned so Dependabot can offer the patch bumps.
# 1.27 is out of support and carries known CVEs.
FROM nginx:1.30-alpine

RUN rm -f /etc/nginx/conf.d/default.conf

# The official entrypoint runs /docker-entrypoint.d/* in sort order
# before exec'ing nginx, and only if the file is executable - hence
# --chmod here rather than a bind mount, whose permission bits do not
# survive a Windows host reliably.
#
# 19- runs before the 20-envsubst step and exports a variable it needs;
# 40- runs after it, once the config is rendered.
COPY --chmod=755 https-port-suffix.envsh /docker-entrypoint.d/19-https-port-suffix.envsh
COPY --chmod=755 reload-loop.sh          /docker-entrypoint.d/40-reload-loop.sh
