#!/bin/bash
# Setup nobrainr MCP client on this machine.
#
# Usage:
#   NOBRAINR_HOST=192.168.1.100 bash scripts/setup-client.sh
#   NOBRAINR_HOST=my-server.local bash scripts/setup-client.sh
#
# Expects: Claude Code installed, connectivity to nobrainr server on port 8420

set -euo pipefail

NOBRAINR_HOST="${NOBRAINR_HOST:?Set NOBRAINR_HOST to the IP or hostname of your nobrainr server}"
NOBRAINR_PORT="${NOBRAINR_PORT:-8420}"
NOBRAINR_URL="http://${NOBRAINR_HOST}:${NOBRAINR_PORT}"
CLAUDE_DIR="$HOME/.claude"
SCRIPTS_DIR="$CLAUDE_DIR/scripts"
HOOKS_DIR="$CLAUDE_DIR/hooks"
MACHINE=$(hostname)

echo "=== nobrainr client setup on $MACHINE ==="
echo "Server: $NOBRAINR_URL"

# 1. Verify connectivity
echo -n "Checking connectivity to $NOBRAINR_URL... "
if curl -sf --max-time 5 "$NOBRAINR_URL/api/stats" >/dev/null 2>&1; then
    echo "OK"
else
    echo "FAILED"
    echo "Cannot reach $NOBRAINR_URL. Check that nobrainr is running and the host is reachable."
    exit 1
fi

# 2. Create directories
mkdir -p "$SCRIPTS_DIR" "$HOOKS_DIR"

# 3. Add nobrainr to MCP config
MCP_FILE="$CLAUDE_DIR/mcp.json"
SSE_URL="http://${NOBRAINR_HOST}:${NOBRAINR_PORT}/sse"
if [[ -f "$MCP_FILE" ]]; then
    jq --arg url "$SSE_URL" '.mcpServers.nobrainr = {"type": "sse", "url": $url}' \
        "$MCP_FILE" > "${MCP_FILE}.tmp" && mv "${MCP_FILE}.tmp" "$MCP_FILE"
else
    cat > "$MCP_FILE" << MCPEOF
{
  "mcpServers": {
    "nobrainr": {
      "type": "sse",
      "url": "$SSE_URL"
    }
  }
}
MCPEOF
fi
echo "MCP config updated: $MCP_FILE"

# 4. Add nobrainr permissions to settings.json
SETTINGS_FILE="$CLAUDE_DIR/settings.json"
NOBRAINR_PERMS=(
    "mcp__nobrainr__memory_store"
    "mcp__nobrainr__memory_search"
    "mcp__nobrainr__memory_query"
    "mcp__nobrainr__memory_get"
    "mcp__nobrainr__memory_update"
    "mcp__nobrainr__memory_delete"
    "mcp__nobrainr__memory_stats"
    "mcp__nobrainr__entity_search"
    "mcp__nobrainr__entity_graph"
    "mcp__nobrainr__memory_maintenance"
    "mcp__nobrainr__memory_extract"
    "mcp__nobrainr__memory_feedback"
    "mcp__nobrainr__memory_reflect"
    "mcp__nobrainr__log_event"
    "mcp__nobrainr__memory_import_chatgpt"
    "mcp__nobrainr__memory_import_claude"
)
if [[ -f "$SETTINGS_FILE" ]]; then
    PERMS_JSON=$(printf '%s\n' "${NOBRAINR_PERMS[@]}" | jq -R . | jq -s .)
    jq --argjson perms "$PERMS_JSON" '.permissions.allow = (.permissions.allow + $perms | unique)' \
        "$SETTINGS_FILE" > "${SETTINGS_FILE}.tmp" && mv "${SETTINGS_FILE}.tmp" "$SETTINGS_FILE"
else
    PERMS_JSON=$(printf '%s\n' "${NOBRAINR_PERMS[@]}" | jq -R . | jq -s .)
    jq -n --argjson perms "$PERMS_JSON" '{"permissions": {"allow": $perms, "deny": []}}' > "$SETTINGS_FILE"
fi
echo "Permissions updated: $SETTINGS_FILE"

# 5. Deploy stop hook with auto session capture
STOP_HOOK="$HOOKS_DIR/stop-validation.sh"
if [[ -f "$STOP_HOOK" ]] && grep -q 'store-session' "$STOP_HOOK" 2>/dev/null; then
    echo "Stop hook already has auto session capture"
else
    cat > "$STOP_HOOK" << 'HOOKEOF'
#!/bin/bash
# Stop Hook: Auto-capture session activity to nobrainr

INPUT=$(cat)
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // ""' 2>/dev/null || echo "")
OUTPUT=""

# Auto-capture session to nobrainr if substantial work was done
if [[ -n "$TRANSCRIPT_PATH" ]] && [[ -f "$TRANSCRIPT_PATH" ]]; then
  EDIT_COUNT=$(tail -100 "$TRANSCRIPT_PATH" 2>/dev/null | grep -cE '"(Edit|Write|NotebookEdit)"' | tr -d ' ' || echo "0")
  if [[ "$EDIT_COUNT" -gt 2 ]]; then
    EDITED_FILES=$(tail -200 "$TRANSCRIPT_PATH" 2>/dev/null | grep -oE '"file_path"\s*:\s*"[^"]+"' | sed 's/"file_path"\s*:\s*"//;s/"$//' | sort -u | head -20 | tr '\n' ',' | sed 's/,$//' || true)
    MACHINE=$(hostname 2>/dev/null || echo "unknown")
    python3 ~/.claude/scripts/nobrainr-query.py \
      --store-session \
      --event-machine "$MACHINE" \
      --event-files "$EDITED_FILES" \
      --event-edits "$EDIT_COUNT" \
      --timeout 10 &>/dev/null &
    OUTPUT+="<nobrainr-reminder>\nSession auto-captured to nobrainr (${EDIT_COUNT} edits).\n</nobrainr-reminder>\n"
  fi
fi

if [[ -n "$OUTPUT" ]]; then
  echo -e "$OUTPUT"
fi

exit 0
HOOKEOF
    chmod +x "$STOP_HOOK"
    echo "Created stop hook: $STOP_HOOK"
fi

# 6. Verify with test query
echo -n "Verifying nobrainr connection... "
if python3 "$SCRIPTS_DIR/nobrainr-query.py" --recent 1 --timeout 8 >/dev/null 2>&1; then
    echo "OK"
else
    echo "WARNING: Test query failed (nobrainr may not be running)"
fi

echo ""
echo "=== Setup complete on $MACHINE ==="
echo "nobrainr MCP: $NOBRAINR_URL"
echo "Stop hook: $STOP_HOOK"
echo ""
echo "Note: The nobrainr-query.py script is not included in this repo."
echo "It will be available from the nobrainr server or you can write your own"
echo "MCP SSE client. See CLAUDE.md for the protocol details."
