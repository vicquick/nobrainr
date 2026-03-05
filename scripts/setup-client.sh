#!/bin/bash
# Setup nobrainr MCP client on a remote machine.
# Run via: ssh <machine> 'bash -s' < scripts/setup-client.sh
#
# Expects: machine has Claude Code installed, connectivity to 10.10.10.12:8420

set -euo pipefail

NOBRAINR_URL="http://10.10.10.12:8420"
CLAUDE_DIR="$HOME/.claude"
SCRIPTS_DIR="$CLAUDE_DIR/scripts"
HOOKS_DIR="$CLAUDE_DIR/hooks"
MACHINE=$(hostname)

echo "=== nobrainr client setup on $MACHINE ==="

# 1. Verify connectivity
echo -n "Checking connectivity to $NOBRAINR_URL... "
if curl -sf --max-time 5 "$NOBRAINR_URL/api/stats" >/dev/null 2>&1; then
    echo "OK"
else
    echo "FAILED"
    echo "Cannot reach $NOBRAINR_URL. Ensure VPN is connected."
    exit 1
fi

# 2. Create directories
mkdir -p "$SCRIPTS_DIR" "$HOOKS_DIR"

# 3. Add nobrainr to MCP config
MCP_FILE="$CLAUDE_DIR/mcp.json"
if [[ -f "$MCP_FILE" ]]; then
    # Merge nobrainr into existing config
    if jq -e '.mcpServers.nobrainr' "$MCP_FILE" >/dev/null 2>&1; then
        echo "nobrainr already in mcp.json, updating..."
    fi
    jq '.mcpServers.nobrainr = {"type": "sse", "url": "http://10.10.10.12:8420/sse"}' \
        "$MCP_FILE" > "${MCP_FILE}.tmp" && mv "${MCP_FILE}.tmp" "$MCP_FILE"
else
    cat > "$MCP_FILE" << 'MCPEOF'
{
  "mcpServers": {
    "nobrainr": {
      "type": "sse",
      "url": "http://10.10.10.12:8420/sse"
    }
  }
}
MCPEOF
fi
echo "MCP config updated: $MCP_FILE"

# 4. Add nobrainr permissions to settings.json
SETTINGS_FILE="$CLAUDE_DIR/settings.json"
if [[ -f "$SETTINGS_FILE" ]]; then
    # Add mcp__nobrainr permissions if not present
    if ! jq -e '.permissions.allow | map(select(startswith("mcp__nobrainr"))) | length > 0' "$SETTINGS_FILE" >/dev/null 2>&1; then
        jq '.permissions.allow += ["mcp__nobrainr__memory_store", "mcp__nobrainr__memory_search", "mcp__nobrainr__memory_query", "mcp__nobrainr__memory_get", "mcp__nobrainr__memory_update", "mcp__nobrainr__memory_delete", "mcp__nobrainr__memory_stats", "mcp__nobrainr__entity_search", "mcp__nobrainr__entity_graph", "mcp__nobrainr__memory_maintenance", "mcp__nobrainr__memory_extract", "mcp__nobrainr__log_event", "mcp__nobrainr__memory_import_chatgpt", "mcp__nobrainr__memory_import_claude"]' \
            "$SETTINGS_FILE" > "${SETTINGS_FILE}.tmp" && mv "${SETTINGS_FILE}.tmp" "$SETTINGS_FILE"
        echo "Permissions added to settings.json"
    else
        echo "nobrainr permissions already present"
    fi
else
    cat > "$SETTINGS_FILE" << 'SETTEOF'
{
  "permissions": {
    "allow": [
      "mcp__nobrainr__memory_store",
      "mcp__nobrainr__memory_search",
      "mcp__nobrainr__memory_query",
      "mcp__nobrainr__memory_get",
      "mcp__nobrainr__memory_update",
      "mcp__nobrainr__memory_delete",
      "mcp__nobrainr__memory_stats",
      "mcp__nobrainr__entity_search",
      "mcp__nobrainr__entity_graph",
      "mcp__nobrainr__memory_maintenance",
      "mcp__nobrainr__memory_extract",
      "mcp__nobrainr__log_event",
      "mcp__nobrainr__memory_import_chatgpt",
      "mcp__nobrainr__memory_import_claude"
    ],
    "deny": []
  }
}
SETTEOF
    echo "Settings created: $SETTINGS_FILE"
fi

# 5. Deploy nobrainr-query.py
cat > "$SCRIPTS_DIR/nobrainr-query.py" << 'PYEOF'
#!/usr/bin/env python3
"""Query nobrainr MCP server for memories. Used by hooks and /recall skill."""

import argparse
import json
import queue
import sys
import threading
import time
import uuid

import requests

MCP_URL = "http://10.10.10.12:8420"


class MCPClient:
    """MCP SSE client with initialization handshake."""

    def __init__(self, base_url, timeout=10):
        self.base_url = base_url
        self.messages_url = None
        self.session = requests.Session()
        self.responses = queue.Queue()
        self._stop = threading.Event()
        self._connect(timeout)
        self._initialize(timeout)

    def _connect(self, timeout):
        self._sse_resp = self.session.get(
            f"{self.base_url}/sse", stream=True, timeout=timeout
        )
        self._sse_resp.raise_for_status()
        self._sse_thread = threading.Thread(target=self._read_sse, daemon=True)
        self._sse_thread.start()

        try:
            msg = self.responses.get(timeout=timeout)
            if msg.get("_type") == "endpoint":
                self.messages_url = f"{self.base_url}{msg['endpoint']}"
            else:
                raise RuntimeError(f"Expected endpoint, got: {msg}")
        except queue.Empty:
            raise RuntimeError("Timeout waiting for SSE endpoint")

    def _read_sse(self):
        event_type = None
        data_lines = []
        try:
            for line in self._sse_resp.iter_lines(decode_unicode=True):
                if self._stop.is_set():
                    break
                if line is None or line == "":
                    if data_lines:
                        data = "\n".join(data_lines)
                        if event_type == "endpoint":
                            self.responses.put({"_type": "endpoint", "endpoint": data})
                        elif event_type == "message":
                            try:
                                self.responses.put(json.loads(data))
                            except json.JSONDecodeError:
                                pass
                    event_type = None
                    data_lines = []
                elif line.startswith("event: "):
                    event_type = line[7:].strip()
                elif line.startswith("data: "):
                    data_lines.append(line[6:])
        except Exception:
            pass

    def _initialize(self, timeout):
        init_payload = {
            "jsonrpc": "2.0",
            "id": "init-1",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "nobrainr-query", "version": "1.0.0"},
            },
        }
        self.session.post(
            self.messages_url,
            json=init_payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        try:
            self.responses.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError("Timeout waiting for initialize response")

        self.session.post(
            self.messages_url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        time.sleep(0.3)

    def call_tool(self, tool_name, arguments, timeout=10):
        request_id = str(uuid.uuid4())[:8]
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        resp = self.session.post(
            self.messages_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        if resp.status_code not in (200, 202):
            return {"error": f"HTTP {resp.status_code}"}
        try:
            return self.responses.get(timeout=timeout)
        except queue.Empty:
            return {"error": "Timeout"}

    def close(self):
        self._stop.set()
        try:
            self._sse_resp.close()
        except Exception:
            pass


def format_memories(result):
    """Extract and format memories from MCP tool result."""
    lines = []
    try:
        content = result.get("result", {}).get("content", [])
        if not content:
            return ""

        memories = []
        for item in content:
            text = item.get("text", "")
            if not text:
                continue
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and "summary" in parsed:
                    memories.append(parsed)
                elif isinstance(parsed, list):
                    memories.extend(parsed)
                elif isinstance(parsed, dict) and "result" in parsed:
                    memories.extend(parsed["result"] if isinstance(parsed["result"], list) else [])
            except json.JSONDecodeError:
                lines.append(text[:200])

        for mem in memories:
            summary = mem.get("summary", "")
            category = mem.get("category", "")
            created = mem.get("created_at", "")[:10] if mem.get("created_at") else ""

            line = f"- [{category}]" if category else "-"
            line += f" {summary}" if summary else f" {mem.get('content', '')[:100]}"
            if created:
                line += f" [{created}]"
            lines.append(line)
    except Exception:
        try:
            content = result.get("result", {}).get("content", [])
            if content:
                return content[0].get("text", "")[:200]
        except Exception:
            pass
    return "\n".join(lines)


def log_event(machine, files_edited, edit_count, timeout=8):
    """Log a session_end event to nobrainr via MCP."""
    try:
        client = MCPClient(MCP_URL, timeout=timeout)
        result = client.call_tool("log_event", {
            "event_type": "session_end",
            "description": f"Session ended on {machine} with {edit_count} edits",
            "metadata": {
                "machine": machine,
                "files_edited": files_edited,
                "edit_count": edit_count,
            },
        }, timeout=timeout)
        client.close()
        return result
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description="Query nobrainr memories")
    parser.add_argument("--recent", type=int, help="Fetch N most recent memories")
    parser.add_argument("--search", type=str, help="Search query")
    parser.add_argument("--machine", type=str, help="Filter by source machine")
    parser.add_argument("--tags", type=str, help="Comma-separated tags to filter by")
    parser.add_argument("--timeout", type=int, default=10, help="Timeout in seconds")
    parser.add_argument("--raw", action="store_true", help="Output raw JSON")
    parser.add_argument("--log-event", action="store_true", help="Log a session_end event")
    parser.add_argument("--event-machine", type=str, help="Machine name for log-event")
    parser.add_argument("--event-files", type=str, help="Comma-separated edited file paths")
    parser.add_argument("--event-edits", type=int, default=0, help="Number of edits")
    args = parser.parse_args()

    if args.log_event:
        machine = args.event_machine or "unknown"
        files = args.event_files.split(",") if args.event_files else []
        log_event(machine, files, args.event_edits, timeout=args.timeout)
        sys.exit(0)

    if not args.recent and not args.search:
        args.recent = 10

    try:
        client = MCPClient(MCP_URL, timeout=args.timeout)

        if args.search:
            tool_args = {"query": args.search, "limit": args.recent or 10}
            if args.tags:
                tool_args["tags"] = args.tags.split(",")
            result = client.call_tool("memory_search", tool_args, timeout=args.timeout)
        else:
            tool_args = {"limit": args.recent or 10}
            if args.machine:
                tool_args["source_machine"] = args.machine
            if args.tags:
                tool_args["tags"] = args.tags.split(",")
            result = client.call_tool("memory_query", tool_args, timeout=args.timeout)

        client.close()

        if args.raw:
            print(json.dumps(result, indent=2))
        else:
            output = format_memories(result)
            if output:
                print(output)
    except Exception:
        # Fail silently — startup hooks must not block
        sys.exit(0)


if __name__ == "__main__":
    main()
PYEOF
chmod +x "$SCRIPTS_DIR/nobrainr-query.py"
echo "Deployed: $SCRIPTS_DIR/nobrainr-query.py"

# 6. Deploy stop hook with auto log_event
STOP_HOOK="$HOOKS_DIR/stop-validation.sh"
if [[ -f "$STOP_HOOK" ]]; then
    # Check if auto log_event is already present
    if grep -q 'log-event' "$STOP_HOOK" 2>/dev/null; then
        echo "Stop hook already has auto log_event"
    else
        # Append auto log_event block before the final output section
        # Find the line before "# Output if we have reminders" and insert
        HOOK_ADDITION='
# Auto-log session event to nobrainr
if [[ -n "$TRANSCRIPT_PATH" ]] && [[ -f "$TRANSCRIPT_PATH" ]]; then
  EDIT_COUNT=$(tail -100 "$TRANSCRIPT_PATH" 2>/dev/null | grep -cE '"'"'"(Edit|Write|NotebookEdit)"'"'"' | tr -d '"'"' '"'"' || echo "0")
  if [[ "$EDIT_COUNT" -gt 2 ]]; then
    EDITED_FILES=$(tail -200 "$TRANSCRIPT_PATH" 2>/dev/null | grep -oE '"'"'"file_path"\s*:\s*"[^"]+"'"'"' | sed '"'"'s/"file_path"\s*:\s*"//;s/"$//'"'"' | sort -u | head -20 | tr '"'"'\n'"'"' '"'"','"'"' | sed '"'"'s/,$//'"'"' || true)
    MACHINE=$(hostname 2>/dev/null || echo "unknown")
    python3 ~/.claude/scripts/nobrainr-query.py --log-event --event-machine "$MACHINE" --event-files "$EDITED_FILES" --event-edits "$EDIT_COUNT" --timeout 8 &>/dev/null &
  fi
fi
'
        # Append before the output section
        echo "$HOOK_ADDITION" >> "$STOP_HOOK"
        echo "Updated stop hook with auto log_event"
    fi
else
    # Create minimal stop hook
    cat > "$STOP_HOOK" << 'HOOKEOF'
#!/bin/bash
# Stop Hook: Auto-log session activity to nobrainr

INPUT=$(cat)
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // ""' 2>/dev/null || echo "")
OUTPUT=""

# Auto-log session event to nobrainr if substantial work was done
if [[ -n "$TRANSCRIPT_PATH" ]] && [[ -f "$TRANSCRIPT_PATH" ]]; then
  EDIT_COUNT=$(tail -100 "$TRANSCRIPT_PATH" 2>/dev/null | grep -cE '"(Edit|Write|NotebookEdit)"' | tr -d ' ' || echo "0")
  if [[ "$EDIT_COUNT" -gt 2 ]]; then
    EDITED_FILES=$(tail -200 "$TRANSCRIPT_PATH" 2>/dev/null | grep -oE '"file_path"\s*:\s*"[^"]+"' | sed 's/"file_path"\s*:\s*"//;s/"$//' | sort -u | head -20 | tr '\n' ',' | sed 's/,$//' || true)
    MACHINE=$(hostname 2>/dev/null || echo "unknown")
    python3 ~/.claude/scripts/nobrainr-query.py --log-event --event-machine "$MACHINE" --event-files "$EDITED_FILES" --event-edits "$EDIT_COUNT" --timeout 8 &>/dev/null &
    OUTPUT+="<nobrainr-reminder>\nSession activity logged to nobrainr (${EDIT_COUNT} edits).\n</nobrainr-reminder>\n"
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

# 7. Verify with test query
echo -n "Verifying nobrainr connection... "
if python3 "$SCRIPTS_DIR/nobrainr-query.py" --recent 1 --timeout 8 >/dev/null 2>&1; then
    echo "OK"
else
    echo "WARNING: Test query failed (nobrainr may not be running)"
fi

echo ""
echo "=== Setup complete on $MACHINE ==="
echo "nobrainr MCP: $NOBRAINR_URL"
echo "Client script: $SCRIPTS_DIR/nobrainr-query.py"
echo "Stop hook: $STOP_HOOK"
