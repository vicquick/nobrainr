# Claude Code Setup Prompt for nobrainr

Copy everything below the line and paste it as a prompt to Claude Code on any new machine you want to connect to your nobrainr server. The agent will set up hooks, scripts, and MCP config automatically.

---

Set up this machine for full integration with nobrainr, a shared memory service for AI agents.

**Before doing anything, ask me two things:**
1. **Machine name** — a short identifier for this machine (e.g. "workpc", "laptop", "desktop-home"). Used in all `source_machine` fields so memories are tagged by origin.
2. **Nobrainr server URL** — the base URL where nobrainr is running (e.g. `http://my-server:8420`). Must be reachable from this machine.

Do NOT proceed until I've confirmed both. Use `MACHINE_NAME` and `NOBRAINR_URL` as placeholders below — replace them everywhere with my answers.

## 1. Prerequisites

Check connectivity before doing anything:

```bash
curl -sf --max-time 3 NOBRAINR_URL/api/stats | jq .total_memories
```

If it fails, stop and tell me the nobrainr server is not reachable.

Also ensure these are available: `curl`, `python3`, `jq`, `npm`/`npx`. Install any that are missing.

## 2. MCP Servers

Create/update `~/.claude/mcp.json` — add the nobrainr server:

```json
{
  "mcpServers": {
    "nobrainr": {
      "type": "streamable-http",
      "url": "NOBRAINR_URL/mcp"
    }
  }
}
```

If the file already exists, **merge** this entry — don't overwrite existing servers.

## 3. Nobrainr Integration Scripts

Create directories first: `mkdir -p ~/.claude/scripts ~/.claude/hooks`

### `~/.claude/scripts/nobrainr-query.py`

Core nobrainr MCP client script used by hooks. Replace `NOBRAINR_URL` and `MACHINE_NAME` with the values I gave you.

```python
#!/usr/bin/env python3
"""Query nobrainr MCP server for memories. Used by hooks and /recall skill."""

import argparse
import json
import queue
import sys
import threading
import requests

MCP_URL = "NOBRAINR_URL"
MACHINE_NAME = "MACHINE_NAME"


class MCPClient:
    def __init__(self, base_url, timeout=10):
        self.base_url = base_url
        self.messages_url = None
        self.session = requests.Session()
        self.responses = queue.Queue()
        self._stop = threading.Event()
        self._connect(timeout)
        self._initialize(timeout)

    def _connect(self, timeout):
        self._sse_resp = self.session.get(f"{self.base_url}/sse", stream=True, timeout=timeout)
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
        self._call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "nobrainr-query", "version": "1.0.0"},
        }, msg_id="init-1", timeout=timeout)
        self.session.post(self.messages_url, json={
            "jsonrpc": "2.0", "method": "notifications/initialized"
        })

    def _call(self, method, params, msg_id=None, timeout=10):
        import uuid
        msg_id = msg_id or str(uuid.uuid4())
        self.session.post(self.messages_url, json={
            "jsonrpc": "2.0", "id": msg_id, "method": method, "params": params
        })
        try:
            while True:
                resp = self.responses.get(timeout=timeout)
                if resp.get("id") == msg_id:
                    return resp
        except queue.Empty:
            return None

    def call_tool(self, name, arguments, timeout=30):
        resp = self._call("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
        if resp and "result" in resp:
            content = resp["result"].get("content", [])
            for c in content:
                if c.get("type") == "text":
                    return c["text"]
        return None

    def close(self):
        self._stop.set()
        try:
            self._sse_resp.close()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--search", help="Semantic search query")
    parser.add_argument("--recent", type=int, help="Get N recent memories")
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--store-session", action="store_true")
    parser.add_argument("--event-machine", default=MACHINE_NAME)
    parser.add_argument("--event-files", default="")
    parser.add_argument("--event-edits", default="0")
    args = parser.parse_args()

    try:
        client = MCPClient(MCP_URL, timeout=args.timeout)

        if args.search:
            result = client.call_tool("memory_search", {"query": args.search, "limit": 5})
            if result:
                data = json.loads(result)
                for m in data.get("result", []):
                    cat = m.get("category", "general")
                    summary = m.get("summary") or m.get("content", "")[:80]
                    date = (m.get("created_at") or "")[:10]
                    mid = m.get("id", "")
                    print(f"- [{cat}] {summary} [{date}] {{id:{mid}}}")

        elif args.recent:
            result = client.call_tool("memory_search", {"query": "recent", "limit": args.recent})
            if result:
                data = json.loads(result)
                for m in data.get("result", []):
                    print(m.get("summary") or m.get("content", "")[:80])

        elif args.store_session:
            edits = int(args.event_edits) if args.event_edits else 0
            if edits > 0:
                machine = args.event_machine or MACHINE_NAME
                client.call_tool("log_event", {
                    "event_type": "session_end",
                    "category": "session-log",
                    "description": f"Session: {edits} edits on {machine}",
                    "metadata": {"files": args.event_files, "edits": edits, "machine": machine}
                })
                files_short = ", ".join(f.split("/")[-1] for f in args.event_files.split(",")[:5]) if args.event_files else "various"
                client.call_tool("memory_store", {
                    "content": f"Session on {machine}: {edits} edits on {files_short}",
                    "summary": f"Session: {edits} edits on {machine}",
                    "tags": ["auto-captured", "session"],
                    "category": "session-log",
                    "source_type": "session",
                    "source_machine": machine
                })

        client.close()
    except Exception as e:
        print(f"nobrainr error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
```

Make it executable: `chmod +x ~/.claude/scripts/nobrainr-query.py`

Requires: `pip install requests` (install if missing)

### `~/.claude/scripts/nobrainr-recall.sh`

Fast recall via REST API (used by prompt enhancement hook). Replace `NOBRAINR_URL`:

```bash
#!/bin/bash
QUERY="${1:-}"
LIMIT="${2:-3}"
[[ -z "$QUERY" ]] && exit 0
NOBRAINR_URL="NOBRAINR_URL"
ENCODED=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote_plus(sys.argv[1]))" "$QUERY" 2>/dev/null || echo "$QUERY")
RESPONSE=$(curl -sf --max-time 2 "${NOBRAINR_URL}/api/recall?q=${ENCODED}&limit=${LIMIT}" 2>/dev/null) || exit 0
echo "$RESPONSE" | python3 -c "
import json, sys
try:
    memories = json.load(sys.stdin)
    for m in memories:
        cat = m.get('category', 'general')
        summary = m.get('summary') or (m.get('content', '')[:80] + '...' if m.get('content') else 'no content')
        date = (m.get('created_at') or '')[:10]
        mid = m.get('id', '')
        print(f'- [{cat}] {summary} [{date}] {{id:{mid}}}')
except Exception:
    pass
" 2>/dev/null || exit 0
```

Make it executable: `chmod +x ~/.claude/scripts/nobrainr-recall.sh`

## 4. Claude Code Hooks

### `~/.claude/hooks/session-start.sh`

Loads nobrainr memories at session start. Replace `NOBRAINR_URL`:

```bash
#!/bin/bash
NOBRAINR_API="NOBRAINR_URL"
INPUT=$(cat 2>/dev/null || true)
CWD=$(echo "$INPUT" | jq -r '.cwd // ""' 2>/dev/null || echo "")

# Auto-detect project from working directory.
# Add your own project patterns here.
PROJECT_TAG=""
PROJECT_NAME=""
DIRNAME=$(basename "$CWD" 2>/dev/null || echo "")
if [[ -n "$DIRNAME" ]]; then
  PROJECT_TAG="$DIRNAME"
  PROJECT_NAME="$DIRNAME"
fi

NOBRAINR_CONTEXT=""
if [[ -n "$PROJECT_TAG" ]]; then
  PROJECT_MEMORIES=$(curl -sf --max-time 3 "${NOBRAINR_API}/api/recall?q=${PROJECT_TAG}&limit=7" 2>/dev/null || true)
  if [[ -n "$PROJECT_MEMORIES" ]] && [[ "$PROJECT_MEMORIES" != "[]" ]]; then
    NOBRAINR_CONTEXT=$(echo "$PROJECT_MEMORIES" | python3 -c "
import json, sys
try:
    for m in json.load(sys.stdin):
        cat = m.get('category', 'general')
        summary = m.get('summary') or (m.get('content', '')[:80])
        date = (m.get('created_at') or '')[:10]
        print(f'- [{cat}] {summary} [{date}]')
except: pass
" 2>/dev/null || true)
  fi
fi

RECENT_MEMORIES=$(curl -sf --max-time 3 "${NOBRAINR_API}/api/memories?limit=5" 2>/dev/null || true)
RECENT_CONTEXT=""
if [[ -n "$RECENT_MEMORIES" ]] && [[ "$RECENT_MEMORIES" != "[]" ]]; then
  RECENT_CONTEXT=$(echo "$RECENT_MEMORIES" | python3 -c "
import json, sys
try:
    for m in json.load(sys.stdin):
        cat = m.get('category', 'general')
        summary = m.get('summary') or (m.get('content', '')[:80])
        date = (m.get('created_at') or '')[:10]
        print(f'- [{cat}] {summary} [{date}]')
except: pass
" 2>/dev/null || true)
fi

CONTEXT=""
[[ -n "$NOBRAINR_CONTEXT" ]] && CONTEXT="Nobrainr memories for ${PROJECT_NAME}:
${NOBRAINR_CONTEXT}"
[[ -n "$RECENT_CONTEXT" ]] && CONTEXT="${CONTEXT}

Recent nobrainr memories:
${RECENT_CONTEXT}"

CONTEXT_JSON=$(python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" <<< "$CONTEXT" 2>/dev/null || echo '""')

cat << HOOK
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": ${CONTEXT_JSON}
  }
}
HOOK
exit 0
```

### `~/.claude/hooks/enhance-prompt.sh`

Auto-recalls relevant nobrainr memories for every prompt:

```bash
#!/bin/bash
INPUT=$(cat)
PROMPT=$(echo "$INPUT" | jq -r '.prompt // ""' 2>/dev/null || echo "")

[[ "$PROMPT" == /* ]] && exit 0
[[ "$PROMPT" == \** ]] && exit 0
[[ ${#PROMPT} -gt 500 ]] && exit 0
[[ -z "$PROMPT" ]] && exit 0

PROMPT_LOWER=$(echo "$PROMPT" | tr '[:upper:]' '[:lower:]')

is_acknowledgement() {
  echo "$1" | grep -qiE '^(ok|yes|no|sure|thanks|cool|alright|done|lgtm|yep|great|nice|good|fine|correct|right|perfect|got it|sounds good|go ahead|proceed|looks good|ship it)[.!?,]*$'
}

CONTEXT=""

# Git context
CWD=$(echo "$INPUT" | jq -r '.cwd // ""' 2>/dev/null || echo "")
if [[ ${#PROMPT} -lt 100 ]] && [[ -n "$CWD" ]] && git -C "$CWD" rev-parse --git-dir &>/dev/null; then
  BRANCH=$(git -C "$CWD" branch --show-current 2>/dev/null || echo "unknown")
  DIRTY=$(git -C "$CWD" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  [[ "$DIRTY" -gt 0 ]] && CONTEXT+="Git: $BRANCH ($DIRTY uncommitted changes)\n"
fi

# Nobrainr auto-recall
if [[ ${#PROMPT} -gt 4 ]] && ! is_acknowledgement "$PROMPT_LOWER"; then
  KEYWORDS=$(echo "$PROMPT" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9 ]/ /g' | sed 's/\b\(the\|a\|an\|is\|are\|was\|to\|for\|with\|on\|at\|from\|by\|and\|or\|not\|i\|me\|my\|we\|you\|it\|this\|that\)\b/ /g' | tr -s ' ' | xargs -n1 | head -4 | tr '\n' ' ' | sed 's/ $//')
  if [[ -n "$KEYWORDS" ]]; then
    RECALL=$(bash ~/.claude/scripts/nobrainr-recall.sh "$KEYWORDS" 3 2>/dev/null || true)
    [[ -n "$RECALL" ]] && CONTEXT+="Relevant memories:\n$RECALL\n"
  fi
fi

if [[ -n "$CONTEXT" ]]; then
  echo "<prompt-context>"
  echo -e "$CONTEXT" | head -15
  echo "</prompt-context>"
fi
exit 0
```

### `~/.claude/hooks/stop-validation.sh`

Auto-logs session activity to nobrainr on stop. Replace `MACHINE_NAME`:

```bash
#!/bin/bash
INPUT=$(cat)
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // ""' 2>/dev/null || echo "")
MACHINE_NAME="MACHINE_NAME"
OUTPUT=""

if [[ -n "$TRANSCRIPT_PATH" ]] && [[ -f "$TRANSCRIPT_PATH" ]]; then
  EDIT_COUNT=$(tail -100 "$TRANSCRIPT_PATH" 2>/dev/null | grep -cE '"(Edit|Write|NotebookEdit)"' | tr -d ' ' || echo "0")
  if [[ "$EDIT_COUNT" -gt 2 ]]; then
    EDITED_FILES=$(tail -200 "$TRANSCRIPT_PATH" 2>/dev/null | grep -oE '"file_path"\s*:\s*"[^"]+"' | sed 's/"file_path"\s*:\s*"//;s/"$//' | sort -u | head -20 | tr '\n' ',' | sed 's/,$//' || true)
    python3 ~/.claude/scripts/nobrainr-query.py --store-session --event-machine "$MACHINE_NAME" --event-files "$EDITED_FILES" --event-edits "$EDIT_COUNT" &>/dev/null &
  fi
fi

echo -e "$OUTPUT"
exit 0
```

Make all hooks executable: `chmod +x ~/.claude/hooks/*.sh`

## 5. Settings

Update `~/.claude/settings.json` — **merge** these into existing settings (don't replace the file):

```json
{
  "enableAllProjectMcpServers": true,
  "enabledMcpjsonServers": ["nobrainr"],
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": "~/.claude/hooks/session-start.sh"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "~/.claude/hooks/enhance-prompt.sh", "timeout": 5}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "~/.claude/hooks/stop-validation.sh", "timeout": 10}]}]
  }
}
```

## 6. Verify

After setup, verify everything works:

```bash
# nobrainr API
curl -sf NOBRAINR_URL/api/stats | jq '{memories: .total_memories, entities: .total_entities}'

# Recall script
~/.claude/scripts/nobrainr-recall.sh "test" 3
```

Tell me the results. If all checks pass, log the setup to nobrainr using the MCP `memory_store` tool:
- content: "New machine MACHINE_NAME set up with nobrainr integration: MCP, hooks, scripts"
- summary: "Machine MACHINE_NAME joined the nobrainr network"
- tags: ["setup", "infrastructure"]
- category: "infrastructure"
- source_machine: "MACHINE_NAME"

## What you get

- **Shared memory** — `memory_store` saves learnings, `memory_search` recalls them. Always set `source_machine` to your machine name.
- **Auto-recall** — every prompt automatically searches nobrainr for relevant context
- **Session logging** — sessions with >2 edits auto-log to nobrainr on stop
- **Knowledge graph** — entities and relationships are auto-extracted and queryable
