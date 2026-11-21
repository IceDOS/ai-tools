"""Read the child logs children.run_child writes, for the rlm_children table and any live viewer.

agy reports usage only on each child's final result record, so tokens and turns appear when a
child ends; a running child shows its last stream event and its stream step count instead.
"""

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

PREVIEW_CHARS = 60
IDLE_SECONDS = 60.0


@dataclass
class Child:
    path: Path
    id: str
    seq: int = 0
    parent: str | None = None
    depth: int = 1
    prompt: str = ""
    model: str | None = None
    pid: int = 0
    started: float = 0.0
    ended: float | None = None
    ok: bool | None = None
    error: str | None = None
    lost: bool = False
    turns: int = 0
    steps: int = 0
    tokens: int = 0
    usage: dict = field(default_factory=dict)
    activity: str = ""
    mtime: float = 0.0

    def state(self) -> str:
        if self.ended is None:
            return "running"
        if self.lost:
            return "lost"
        return "ok" if self.ok else "failed"


def _alive(pid: int) -> bool:
    """A process that is gone or a zombie writes no more log lines."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, TypeError, ValueError):
        return True
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def _tokens(usage: object) -> int:
    """Token count from a result usage block, which agy sends only at the end."""
    if not isinstance(usage, dict):
        return 0
    total = next(
        (usage[k] for k in ("total_tokens", "totalTokens") if isinstance(usage.get(k), (int, float))),
        None,
    )
    if total is not None and not isinstance(total, bool):
        return int(total)
    return int(
        sum(
            v
            for k, v in usage.items()
            if "token" in k and "cache" not in k and isinstance(v, (int, float))
        )
    )


def _apply(child: Child, record: dict) -> None:
    kind = record.get("type")
    if kind == "rlm_child_start":
        child.parent = record.get("parent") or None
        child.depth = int(record.get("depth") or 1)
        child.prompt = str(record.get("prompt") or "")
        child.model = record.get("model") or None
        child.pid = int(record.get("pid") or 0)
        child.started = float(record.get("ts") or child.started)
    elif kind == "rlm_child_end":
        child.ended = float(record.get("ts") or 0) or None
        child.ok = bool(record.get("ok"))
        child.error = record.get("error") or None
        child.turns = int(record.get("turns") or 0)
        child.usage = dict(record.get("usage") or {})
        child.tokens = _tokens(child.usage)
    else:
        child.activity = str(kind or "?")
        if kind == "step_update":
            child.steps += 1


def read(log_dir: Path | str) -> list[Child]:
    """The children logged in one conversation's dir, oldest first."""
    log_dir = Path(log_dir)
    try:
        paths = list(log_dir.glob("*.jsonl"))
    except OSError:
        return []
    out = []
    for path in paths:
        child = Child(path=path, id=path.stem)
        try:
            child.mtime = path.stat().st_mtime
            lines = path.read_text().splitlines()
        except OSError:
            continue
        child.started = child.mtime
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                _apply(child, record)
        # A child whose spawning server died never writes its end record.
        if child.ended is None and child.pid and not _alive(child.pid):
            child.lost, child.ended = True, child.mtime
        out.append(child)
    out.sort(key=lambda c: (c.started, c.mtime, c.id))
    for seq, child in enumerate(out, 1):
        child.seq = seq
    return out


def _duration(seconds: float) -> str:
    total = max(0, int(seconds))
    if total >= 3600:
        return f"{total // 3600}h{total % 3600 // 60:02d}m"
    return f"{total // 60}:{total % 60:02d}"


def _one_line(text: str, limit: int = PREVIEW_CHARS) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _turns(child: Child, running: bool) -> str:
    """Turns are final-only; a running child shows the stream steps it has taken instead."""
    if running:
        return f"{child.steps} steps" if child.steps else "-"
    return f"{child.turns}t" if child.turns else "-"


def _tokens_cell(n: int) -> str:
    if not n:
        return "-"
    return f"{n / 1000:.1f}k tok" if n >= 1000 else f"{n} tok"


def _row(child: Child, seqs: dict[str, int], now: float) -> list[str]:
    running = child.ended is None
    elapsed = _duration((now if running else child.ended) - child.started)
    activity = ""
    if running:
        idle = now - child.mtime
        activity = "▸ " + (child.activity or "starting")
        if idle > IDLE_SECONDS:
            activity += f" (idle {_duration(idle)})"
    return [
        f"#{child.seq}",
        child.state(),
        f"d{child.depth}",
        elapsed,
        _turns(child, running),
        _tokens_cell(child.tokens),
        child.model or "default",
        f"#{seqs[child.parent]}" if child.parent in seqs else "-",
        activity,
        f'"{_one_line(child.prompt)}"' if child.prompt else "",
        child.path.name,
    ]


def report(log_dir: Path | str, now: float | None = None) -> str:
    """A table a model can print verbatim: one line per child, then the error of each failure."""
    log_dir = Path(log_dir)
    now = time.time() if now is None else now
    rows = read(log_dir)
    if not rows:
        return f"children in {log_dir}: none logged"
    running = sum(1 for child in rows if child.ended is None)
    head = f"children in {log_dir}: {len(rows)} total, {running} running"
    seqs = {c.id: c.seq for c in rows}
    table = [_row(child, seqs, now) for child in rows]
    widths = [max(len(row[i]) for row in table) for i in range(len(table[0]) - 1)]
    lines = []
    for child, row in zip(rows, table):
        cells = [cell.ljust(width) for cell, width in zip(row[:-1], widths)]
        lines.append("  ".join(cells + row[-1:]).rstrip())
        if child.error:
            lines.append("    error: " + _one_line(child.error, 120))
    return "\n".join([head, *lines])

