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
# Keep it on a supported branch: out-of-support branches stop getting
# security fixes and the image starts accumulating CVEs.
FROM nginx:1.30-alpine

RUN rm -f /etc/nginx/conf.d/default.conf

# njs is nginx's own JavaScript engine, written in C by the nginx team
# and shipped as a first-party dynamic module. It is not Node: no
# runtime, no package manager, no separate process. The handler runs
# inside the nginx worker.
#
# It is here for one reason. Claude's connector authenticates to the
# token endpoint with client_secret_post, meaning the secret arrives in
# the request body, and nginx core cannot read a request body. auth_basic
# can only see the Authorization header, so it can never validate that
# client. njs reads the body; everything else stays plain nginx config.
#
# load_module has to be in the main context, and the official image's
# nginx.conf has no modules include, so it is prepended here.
RUN apk add --no-cache nginx-module-njs \
 && sed -i '1i load_module modules/ngx_http_js_module.so;' /etc/nginx/nginx.conf

COPY njs/ /etc/nginx/njs/

# The official entrypoint runs /docker-entrypoint.d/* in sort order
# before exec'ing nginx, and only if the file is executable - hence
# --chmod here rather than a bind mount, whose permission bits do not
# survive a Windows host reliably.
#
# 19- runs before the 20-envsubst step and exports the variables it
# renders; 40- runs after it, once the config is rendered.
COPY --chmod=755 render-vars.envsh       /docker-entrypoint.d/19-render-vars.envsh
COPY --chmod=755 reload-loop.sh          /docker-entrypoint.d/40-reload-loop.sh
