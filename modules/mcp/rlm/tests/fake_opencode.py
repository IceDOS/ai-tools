#!/usr/bin/env python3
import json
import os
import signal
import sys
import time


def arg(name, default=None):
    if name in sys.argv:
        idx = sys.argv.index(name)
        if idx + 1 < len(sys.argv):
            return sys.argv[idx + 1]
    return default


def get_prompt():
    if "--" in sys.argv:
        idx = sys.argv.index("--")
        return " ".join(sys.argv[idx + 1 :])
    # Last argument not starting with -
    for a in reversed(sys.argv[1:]):
        if not a.startswith("-"):
            return a
    return ""


def main():
    prompt = get_prompt()
    mode = os.environ.get("FAKE_OPENCODE_MODE", "echo")
    record = os.environ.get("FAKE_OPENCODE_RECORD")
    if record:
        with open(record, "a") as f:
            f.write(
                json.dumps({
                    "argv": sys.argv[1:],
                    "depth": os.environ.get("PI_RLM_DEPTH"),
                    "task_file": os.environ.get("RLM_TASK_FILE"),
                    "child_id": os.environ.get("RLM_CHILD_ID"),
                    "parent_id": os.environ.get("RLM_PARENT_ID"),
                    "log_dir": os.environ.get("RLM_CHILD_LOG_DIR"),
                    "cwd": os.getcwd(),
                })
                + "\n"
            )

    if mode == "fail":
        sys.stderr.write("fake opencode: boom\n")
        sys.exit(3)

    if mode == "sleep":
        # Handle SIGTERM by exiting cleanly as opencode does on interruption
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        time.sleep(30)
        return

    task = ""
    if os.environ.get("RLM_TASK_FILE"):
        with open(os.environ["RLM_TASK_FILE"]) as f:
            task = f.read()

    ts = int(time.time() * 1000)
    session_id = "ses_fake_123"

    # Step start
    print(
        json.dumps({
            "type": "step_start",
            "timestamp": ts,
            "sessionID": session_id,
            "part": {
                "id": "step_1",
                "sessionID": session_id,
                "messageID": "msg_1",
                "type": "step-start",
            },
        })
    )

    # Text response
    if "Respond with a single raw JSON object" in prompt:
        structured = os.environ.get(
            "FAKE_OPENCODE_STRUCTURED", '{"summary": "none", "edits": []}'
        )
        print(
            json.dumps({
                "type": "text",
                "timestamp": ts + 10,
                "sessionID": session_id,
                "part": {
                    "id": "part_1",
                    "sessionID": session_id,
                    "messageID": "msg_1",
                    "type": "text",
                    "text": structured,
                },
            })
        )
    else:
        response = f"echo[{os.environ.get('PI_RLM_DEPTH')}]: {prompt[:200]}|task={len(task)}"
        print(
            json.dumps({
                "type": "text",
                "timestamp": ts + 10,
                "sessionID": session_id,
                "part": {
                    "id": "part_1",
                    "sessionID": session_id,
                    "messageID": "msg_1",
                    "type": "text",
                    "text": response,
                },
            })
        )

    # Step finish
    print(
        json.dumps({
            "type": "step_finish",
            "timestamp": ts + 20,
            "sessionID": session_id,
            "part": {
                "id": "step_1",
                "sessionID": session_id,
                "messageID": "msg_1",
                "type": "step-finish",
                "reason": "stop",
                "cost": 0.001,
                "tokens": {"input": 10, "output": 5},
            },
        })
    )


if __name__ == "__main__":
    main()
