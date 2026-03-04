#!/bin/bash
# Post-deploy hook: add stable 'nobrainr' DNS alias on the mcp network.
# Traefik (coolify-proxy) is already on the mcp network, so it can
# route to http://nobrainr:8420 without needing the coolify network.
#
# Run after each Coolify deploy (container name changes each time).

set -e

CONTAINER=$(docker ps --format '{{.Names}}' | grep '^q800s0skokskgc0c8w8w08ck-')

if [ -z "$CONTAINER" ]; then
    echo "ERROR: nobrainr container not found"
    exit 1
fi

# Reconnect to mcp with stable alias (disconnect+connect to reset aliases)
docker network disconnect mcp "$CONTAINER" 2>/dev/null || true
docker network connect --alias nobrainr mcp "$CONTAINER"
echo "Connected $CONTAINER to mcp network with alias 'nobrainr'"
