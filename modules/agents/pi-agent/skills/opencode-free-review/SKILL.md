---
name: opencode-free-review
description: Review your changes with the code-review skill in a review-until-clean loop, using only opencode-free models as children. Use when the user asks for a free review or asks to review until no findings remain.
---

# opencode-free review

Read the code-review skill, then review your changes with it in its review-until-clean loop, following these rules:

1. Use only the opencode-free models listed below as children, and no other provider or model.
2. Up to four children may run at once.
3. The review is complete when a round has no findings.
4. Run the code-review steps yourself, with no orchestrator child, and start every reviewer and verify child with `child()` below instead of `rlm()`. `rlm()` always runs the child on your own model, so it cannot pick a model per child.

## Models and children

List the models from the shell. Pass each one as `opencode-free/<model id>`, exactly as listed:

```python
import json, os, pathlib, subprocess

def free_models():
    out = subprocess.run(["pi", "--list-models"], capture_output=True, text=True).stdout
    rows = [line.split() for line in out.splitlines()]
    return [f"{r[0]}/{r[1]}" for r in rows if len(r) > 1 and r[0] == "opencode-free"]

agent_dir = pathlib.Path(os.environ.get("PI_CODING_AGENT_DIR", pathlib.Path.home() / ".config/pi-agent"))
wanted = ("opencode-free.ts", "rate-limit-hook.ts")
extensions = [e for e in json.loads((agent_dir / "settings.json").read_text())["extensions"] if e.endswith(wanted)]

def child(model, prompt, timeout=1800):
    """Returns True when the child exited cleanly. Check its output file as well."""
    args = ["pi", "-p", "--no-session", "--no-extensions", "--model", model]
    for e in extensions:
        args += ["-e", e]
    try:
        # Without a closed stdin, pi -p waits on it forever.
        return subprocess.run(args + [prompt], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout).returncode == 0
    except subprocess.TimeoutExpired:
        return False
```

`child()` loads only the extensions that provide these models and their retries, so a child spends no requests on session titles or prompt suggestions. When vpn-rotate is on, the rate-limit hook rotates the VPN and retries on every rate limit, so a child rarely fails on one.

## When a child fails

A child failed when `child()` returns False or its output file is missing or is not valid JSON.

- Mark that model as failing for the rest of the review and run the same task on another listed model. Do not retry the failed model, and do not end the round with a task that no child finished.
- If every model is failing, stop and report which tasks did not finish. Do not report the change as clean.
- Before you report a round as clean, confirm every reviewer child in it produced its output file. A round with a failed reviewer is not a clean round.
