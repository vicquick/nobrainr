#!/bin/bash
# Post-deploy hook: connect nobrainr container to coolify network
# Run this after each Coolify deploy, or add to Coolify's post-deploy command.
#
# Coolify container name pattern: q800s0skokskgc0c8w8w08ck-*

set -e

CONTAINER=$(docker ps --format '{{.Names}}' | grep '^q800s0skokskgc0c8w8w08ck-')

if [ -z "$CONTAINER" ]; then
    echo "ERROR: nobrainr container not found"
    exit 1
fi

# Check if already connected
if docker inspect "$CONTAINER" --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | grep -q coolify; then
    echo "Already connected to coolify network"
else
    docker network connect --alias nobrainr coolify "$CONTAINER"
    echo "Connected $CONTAINER to coolify network with alias 'nobrainr'"
fi
