#!/bin/sh
# The app has no login of its own, so nginx protects everything with HTTP basic auth.
set -eu
: "${BASIC_AUTH_USER:?BASIC_AUTH_USER is required}"
: "${BASIC_AUTH_PASSWORD:?BASIC_AUTH_PASSWORD is required}"
hash=$(printf '%s' "$BASIC_AUTH_PASSWORD" | openssl passwd -apr1 -stdin)
printf '%s:%s\n' "$BASIC_AUTH_USER" "$hash" > /etc/nginx/.htpasswd
chown root:nginx /etc/nginx/.htpasswd
chmod 640 /etc/nginx/.htpasswd
