"""agy hook: note the tool agy is running, for the terminal title.

agy runs hook commands synchronously, so this stays a single JSON read and one file write.
"""
import json
import sys

from pathlib import Path
# Fallback path when running from the repo tree instead of nix store.
_LIB = Path(__file__).resolve().parent.parent / "lib" / "terminal-title"
if _LIB.is_dir() and str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
import tool_state

# agy blocks a tool call unless PreToolUse answers with a decision; "ask" is its own permission
# flow (prompt the user, honouring the always-allow cache). An empty answer denies the call.
DECISION = {"decision": "ask"}


def read_payload():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def arg(args, key):
    """One call_mcp_tool argument; agy JSON-encodes each value, so a server name arrives quoted."""
    value = args.get(key) if isinstance(args, dict) else None
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return value
        return decoded if isinstance(decoded, str) else value
    return ""


def tool_name(call):
    """The tool agy runs; an MCP call is named `server/tool`, the way agy's own tool row shows it."""
    name = call.get("name") or ""
    if name == "call_mcp_tool":
        server, tool = arg(call.get("args"), "ServerName"), arg(call.get("args"), "ToolName")
        if server and tool:
            return f"{server}/{tool}"
    return name


def main(argv):
    event = (argv[0] if argv else "").strip()
    payload = read_payload()
    event = event or str(payload.get("hookEventName") or "").strip()
    conversation = payload.get("conversationId")
    call = payload.get("toolCall") or {}
    if event == "PreToolUse":
        tool_state.save(conversation, tool_name(call), call.get("id"), payload.get("stepIdx"))
        print(json.dumps(DECISION))
        return 0
    if event == "PostToolUse":
        # The only event that says the tool finished. Stop and the invocation events also fire
        # while an async tool still runs, so they must not clear the record.
        tool_state.clear(conversation, call.get("id"))
    print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
