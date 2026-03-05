#!/bin/bash
# Post-deploy hook: add stable DNS aliases on the mcp network.
# Traefik (coolify-proxy) is already on the mcp network, so it can
# route to http://nobrainr:8420.
# The nobrainr app connects to Ollama via http://ollama:11434.
#
# Run after each Coolify deploy (container names change each time).

set -e

# --- nobrainr app ---
NOBRAINR=$(docker ps --format '{{.Names}}' | grep '^q800s0skokskgc0c8w8w08ck-')
if [ -z "$NOBRAINR" ]; then
    echo "ERROR: nobrainr container not found"
    exit 1
fi
docker network disconnect mcp "$NOBRAINR" 2>/dev/null || true
docker network connect --alias nobrainr mcp "$NOBRAINR"
echo "Connected $NOBRAINR to mcp with alias 'nobrainr'"

# --- ollama ---
OLLAMA=$(docker ps --format '{{.Names}}' | grep '^y4c0c4wk40csko00skkg40wg-')
if [ -n "$OLLAMA" ]; then
    docker network disconnect mcp "$OLLAMA" 2>/dev/null || true
    docker network connect --alias ollama mcp "$OLLAMA"
    echo "Connected $OLLAMA to mcp with alias 'ollama'"
fi

# --- dashboard ---
DASHBOARD=$(docker ps --format '{{.Names}}' | grep '^k8kocgowg8sggc84cosso0o0-')
if [ -n "$DASHBOARD" ]; then
    docker network disconnect mcp "$DASHBOARD" 2>/dev/null || true
    docker network connect --alias brain-dashboard mcp "$DASHBOARD"
    echo "Connected $DASHBOARD to mcp with alias 'brain-dashboard'"
fi
