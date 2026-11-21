---
name: free-review
description: Review your changes with the code-review skill in a review-until-clean loop, using only opencode's free models as children. Use when the user asks for a free review or asks to review until no findings remain.
---

# Free review

Read the code-review skill, then review your changes with it in its review-until-clean loop, following these rules:

1. Use only the free `opencode/*-free` models listed below as children, and no other provider or model.
2. Up to four children may run at once.
3. The review is complete when a round has no findings.
4. Run the code-review steps yourself, with no orchestrator child, and start every reviewer and verify child with `child()` below instead of `rlm()`. `rlm()` always runs the child on your own model, so it cannot pick a model per child.

## Models and children

List the models from the shell and pass each one exactly as listed:

```python
import subprocess

def free_models():
    out = subprocess.run(["opencode", "models", "opencode"], stdin=subprocess.DEVNULL,
                         capture_output=True, text=True).stdout
    return [m for m in out.split() if m.startswith("opencode/") and m.endswith("-free")]

def child(model, prompt, timeout=1800):
    """Returns True when the child exited cleanly. Check its output file as well."""
    args = ["opencode", "run", "--agent", "build", "--auto", "--standalone", "--model", model, "--", prompt]
    try:
        # Without a closed stdin, opencode run can wait on it.
        return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout).returncode == 0
    except subprocess.TimeoutExpired:
        return False
```

`--agent build` keeps children out of RLM mode, so each one reads files and runs commands with the normal tools. Run up to four `child()` calls at once from threads; they are separate processes, so the kernel does not serialize them.

## When a child fails

A child failed when `child()` returns False or its output file is missing or is not valid JSON.

- Mark that model as failing for the rest of the review and run the same task on another listed model. Do not retry the failed model, and do not end the round with a task that no child finished.
- If every model is failing, stop and report which tasks did not finish. Do not report the change as clean.
- Before you report a round as clean, confirm every reviewer child in it produced its output file. A round with a failed reviewer is not a clean round.
