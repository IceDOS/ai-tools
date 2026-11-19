"""agy hook: note the tool agy is running, for the terminal title.

agy runs hook commands synchronously, so this stays a single JSON read and one file write.
"""
import json
import sys

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


def main(argv):
    event = (argv[0] if argv else "").strip()
    payload = read_payload()
    event = event or str(payload.get("hookEventName") or "").strip()
    conversation = payload.get("conversationId")
    call = payload.get("toolCall") or {}
    if event == "PreToolUse":
        tool_state.save(conversation, call.get("name") or "", call.get("id"), payload.get("stepIdx"))
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
