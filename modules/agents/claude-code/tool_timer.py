import argparse
import json
import os
import sys
from pathlib import Path

# Fallback path when running from the repo tree instead of nix store.
_LIB = Path(__file__).resolve().parent.parent / "lib" / "terminal-title"
if _LIB.is_dir() and str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import terminal_title
import tool_state


def read_payload():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def mcp_tool_name(call_args):
    if not isinstance(call_args, dict):
        return ""
    server = call_args.get("ServerName") or ""
    tool = call_args.get("ToolName") or ""
    if isinstance(server, str) and server.startswith('"') and server.endswith('"'):
        server = server[1:-1]
    if isinstance(tool, str) and tool.startswith('"') and tool.endswith('"'):
        tool = tool[1:-1]
    return f"{server}/{tool}" if server and tool else ""


def parse_args(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("event", nargs="?", default="")
    parser.add_argument("--idle-glyph", default="✳")
    parser.add_argument("--working-glyphs", nargs="*", default=["✶", "✷", "✸", "✹"])
    parser.add_argument("--fallback", default="Claude Code")
    parser.add_argument("--comm-names", nargs="*", default=["claude", ".claude-wrapped"])
    args = parser.parse_args(argv)

    working_glyphs = []
    for item in args.working_glyphs:
        working_glyphs.extend(item.split())
    if not working_glyphs:
        working_glyphs = ["✶", "✷", "✸", "✹"]

    comm_names = []
    for item in args.comm_names:
        comm_names.extend(item.split())
    if not comm_names:
        comm_names = ["claude", ".claude-wrapped"]

    return args.event, args.idle_glyph, working_glyphs, args.fallback, comm_names


def main(argv):
    event, idle_glyph, working_glyphs, fallback, comm_names = parse_args(argv)
    payload = read_payload()
    event = event.strip() or str(payload.get("hook_event_name") or "").strip()
    session_id = payload.get("session_id", "")
    transcript_path = payload.get("transcript_path", "")
    session_title = payload.get("session_title", "")

    agent_pid = terminal_title.find_agent(comm_names=comm_names)
    tty = terminal_title.tty_path(agent_pid) if agent_pid else None

    if tty:
        cur = terminal_title.load(tty)
        name = session_title or cur.get("name")
        t_path = transcript_path or cur.get("transcript_path")
        meta = {
            "idle_glyph": idle_glyph,
            "working_frames": working_glyphs,
            "fallback": fallback,
            "comm_names": comm_names,
        }

        if event == "SessionStart":
            terminal_title.save(tty, {
                "agent": agent_pid,
                "agy": agent_pid,
                "conversation": session_id,
                "name": name,
                "transcript_path": t_path,
                "working": False,
                **meta,
            })
            terminal_title.start_ticker(tty)

        elif event == "UserPromptSubmit":
            terminal_title.save(tty, {
                "agent": agent_pid,
                "agy": agent_pid,
                "conversation": session_id,
                "name": name,
                "transcript_path": t_path,
                "working": True,
                **meta,
            })
            terminal_title.start_ticker(tty)

        elif event == "PreToolUse":
            raw_tool = payload.get("tool_name", "")
            tool = raw_tool
            if raw_tool == "call_mcp_tool":
                mcp_name = mcp_tool_name(payload.get("tool_input"))
                if mcp_name:
                    tool = mcp_name
            call_id = payload.get("tool_use_id")
            tool_state.save(session_id, tool, call_id=call_id)
            save_payload = {"working": True}
            if not cur.get("comm_names"):
                save_payload.update(meta)
            terminal_title.save(tty, save_payload)

        elif event in ("PostToolUse", "PostToolUseFailure"):
            call_id = payload.get("tool_use_id")
            tool_state.clear(session_id, call_id=call_id)

        elif event == "Stop":
            terminal_title.save(tty, {"working": False})

        elif event == "SessionEnd":
            terminal_title.emit_to_tty(tty, "")
            terminal_title.save(tty, {"working": False})

    print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
