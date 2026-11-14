#!/usr/bin/env python3
import json
import os
import sys
import time


def arg(name, default=None):
    if name in sys.argv:
        return sys.argv[sys.argv.index(name) + 1]
    return default


def main():
    prompt = arg("-p", "")
    mode = os.environ.get("FAKE_AGY_MODE", "echo")
    record = os.environ.get("FAKE_AGY_RECORD")
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
        sys.stderr.write("fake agy: boom\n")
        sys.exit(3)
    if mode == "sleep":
        time.sleep(30)
    usage = {"input_tokens": 10, "output_tokens": 5}
    if arg("--output-format") == "json":
        out = {"status": "SUCCESS", "response": "ok", "usage": usage, "num_turns": 1}
        if arg("--json-schema"):
            out["structured_output"] = json.loads(
                os.environ.get("FAKE_AGY_STRUCTURED", '{"summary": "none", "edits": []}')
            )
        print(json.dumps(out))
        return
    task = ""
    if os.environ.get("RLM_TASK_FILE"):
        with open(os.environ["RLM_TASK_FILE"]) as f:
            task = f.read()
    print(json.dumps({"type": "init", "cwd": os.getcwd(), "tools": []}))
    print(json.dumps({"type": "step_update", "step_type": "agent_response"}))
    response = f"echo[{os.environ.get('PI_RLM_DEPTH')}]: {prompt[:200]}|task={len(task)}"
    print(
        json.dumps({
            "type": "result",
            "event": "result",
            "status": "SUCCESS",
            "response": response,
            "usage": usage,
            "num_turns": 2,
            "result": {
                "status": "SUCCESS",
                "response": response,
                "usage": usage,
                "num_turns": 2,
            },
        })
    )


if __name__ == "__main__":
    main()
