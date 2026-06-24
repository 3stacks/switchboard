# Linux-only Asterisk image (SECONDARY path — primary is native-on-Mac).
# Keeps the distro's default /etc/asterisk/* and drops switchboard's configs in
# at start; the entrypoint renders the templates from env.
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends asterisk gettext-base ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /usr/local/bin/switchboard-entrypoint
RUN chmod +x /usr/local/bin/switchboard-entrypoint
ENTRYPOINT ["/usr/local/bin/switchboard-entrypoint"]
