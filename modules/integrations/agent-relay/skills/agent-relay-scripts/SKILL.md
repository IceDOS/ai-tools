---
name: agent-relay-scripts
description: Push notifications, command results, or blocking questions into an agent's relay inbox from scripts, timers, cron jobs, and background tasks.
---

# agent-relay-scripts

The `agent-relay` CLI pushes messages into an agent's relay inbox without manual linking.

## Security model

The relay directory (`$XDG_RUNTIME_DIR/agent-relay`) uses permissions `0700`. Only processes running as the same Unix user can access or write to it. Scripts skip group linking because file system permissions isolate user sessions.

## Target resolution

Commands resolve their target agent using this priority order:
1. `--to <id|prefix|name>`: matches live agent sessions.
2. `AGENT_RELAY_TARGET` environment variable.
3. Process ancestor walk: finds the enclosing agent session that launched the script.

## Patterns

### Notify on long builds

Wrap long builds or tasks with `run`. The CLI runs the command, prints output in real time, and sends a summary with the exit status and output tail to the agent:

```bash
agent-relay run -- nix build .#package
```

Specify a custom line count for the output tail:

```bash
agent-relay run --tail 50 -- cargo test
```

### File watcher loop

Notify the agent on file modifications:

```bash
inotifywait -m -e close_write src/ | while read -r dir event file; do
  agent-relay send "file modified: $dir$file"
done
```

### Detached background job

Capture the agent ID before detaching, then pass it to the detached process:

```bash
ID=$(agent-relay id)
setsid nohup ./long-job.sh "$ID" &
```

Inside `long-job.sh`:

```bash
TARGET="$1"
agent-relay send --to "$TARGET" "background job finished"
```

### Timers and cron jobs

Target a specific agent by ID from cron or systemd user timers:

```bash
agent-relay send --to 3f2a9 "timer: backup completed"
```

### Blocking question (ask)

Pause a script until the agent replies via `relay_send`:

```bash
REPLY=$(agent-relay ask --timeout 60 "Run migrations now? (yes/no)")
if [ "$REPLY" = "yes" ]; then
  ./migrate.sh
fi
```
