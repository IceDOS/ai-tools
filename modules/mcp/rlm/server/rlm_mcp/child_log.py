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


def _clip_text(text: str, max_chars: int = 2000, max_lines: int = 30) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    truncated = False
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[:max_chars]
        truncated = True
    if truncated:
        result += "\n... [trimmed]"
    return result


def _format_tool_input(inp: object, max_chars: int = 1000) -> str:
    if isinstance(inp, dict):
        if not inp:
            return "{}"
        formatted = []
        for k, v in inp.items():
            val = str(v)
            if len(val) > 200:
                val = val[:200] + "…"
            formatted.append(f"{k}: {val}")
        res = "\n".join(formatted)
    else:
        res = str(inp)
    return _clip_text(res, max_chars=max_chars, max_lines=15)


def find_child(log_dir: Path | str, target: str) -> Child | list[Child] | None:
    # Match exact ID, sequence number (#1 or 1), or unique ID prefix.
    children = read(log_dir)
    target = target.strip().removesuffix(".jsonl")
    if not target:
        return None
    for c in children:
        if c.id == target:
            return c
    clean_seq = target.lstrip("#")
    if clean_seq.isdigit():
        seq_num = int(clean_seq)
        for c in children:
            if c.seq == seq_num:
                return c
    prefixes = [c for c in children if c.id.startswith(target)]
    if len(prefixes) == 1:
        return prefixes[0]
    if len(prefixes) > 1:
        return prefixes
    return None


def format_transcript(child: Child, lines: list[str], now: float | None = None) -> str:
    now = time.time() if now is None else now
    prompt = child.prompt
    items: list[tuple[str, object]] = []
    text_buffer: list[str] = []

    def flush_text() -> None:
        nonlocal text_buffer
        if text_buffer:
            combined = "".join(text_buffer).strip()
            if combined:
                items.append(("text", combined))
            text_buffer = []

    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue

        rtype = rec.get("type") or rec.get("event")

        if rtype == "rlm_child_start":
            if rec.get("prompt"):
                prompt = str(rec["prompt"])
        elif rtype == "tool_use":
            flush_text()
            part = rec.get("part") or {}
            tool_name = part.get("tool") or rec.get("tool") or "tool"
            state = part.get("state") or {}
            status = state.get("status") or ("error" if state.get("error") else "completed")
            items.append(("tool", {
                "name": tool_name,
                "status": status,
                "input": state.get("input") or {},
                "output": state.get("output"),
                "error": state.get("error"),
            }))
        elif rtype == "tool_execution_start":
            flush_text()
            items.append(("tool_start", {
                "name": rec.get("toolName") or "tool",
                "args": rec.get("args") or {},
            }))
        elif rtype == "tool_execution_end":
            flush_text()
            res = rec.get("result")
            is_err = bool(rec.get("isError"))
            if items and items[-1][0] == "tool_start":
                _, prev = items.pop()
                items.append(("tool", {
                    "name": prev.get("name", "tool"),
                    "status": "error" if is_err else "completed",
                    "input": prev.get("args") or {},
                    "output": None if is_err else res,
                    "error": res if is_err else None,
                }))
            else:
                items.append(("tool_result", {"result": res, "is_error": is_err}))
        elif rtype == "text":
            part = rec.get("part") or {}
            t = part.get("text") or rec.get("text") or ""
            text_buffer.append(str(t))
        elif rtype == "message_end":
            msg = rec.get("message") or {}
            if msg.get("role") == "assistant":
                for part in msg.get("content") or []:
                    if part.get("type") == "text":
                        text_buffer.append(str(part.get("text") or ""))
                    elif part.get("type") == "toolCall":
                        flush_text()
                        items.append(("tool", {
                            "name": part.get("name") or "tool",
                            "status": "pending",
                            "input": part.get("arguments") or {},
                            "output": None,
                            "error": None,
                        }))
                if msg.get("errorMessage"):
                    flush_text()
                    items.append(("error", str(msg["errorMessage"])))
        elif rtype == "result":
            flush_text()
            res_obj = rec.get("result") if isinstance(rec.get("result"), dict) else rec
            resp = res_obj.get("response") or rec.get("response")
            if resp:
                items.append(("answer", str(resp)))
            if res_obj.get("error"):
                items.append(("error", str(res_obj["error"])))
        elif rtype == "error":
            flush_text()
            err = rec.get("error")
            err_msg = err.get("message") if isinstance(err, dict) else str(err)
            items.append(("error", err_msg))

    flush_text()

    header_parts = [f"Child #{child.seq} [{child.id}]", child.state(), f"d{child.depth}"]
    elapsed = _duration((now if child.ended is None else child.ended) - child.started)
    header_parts.append(elapsed)
    if child.turns:
        header_parts.append(f"{child.turns}t")
    elif child.steps:
        header_parts.append(f"{child.steps} steps")
    if child.tokens:
        header_parts.append(_tokens_cell(child.tokens))

    out_lines = [
        " · ".join(header_parts),
        f"Model: {child.model or 'default'} | Parent: {child.parent or '-'}",
        "",
    ]

    if prompt:
        out_lines.append("--- Prompt ---")
        out_lines.append(_clip_text(prompt.strip(), max_chars=2000, max_lines=25))
        out_lines.append("")

    tool_idx = 0
    total_items = len(items)
    for i, (kind, data) in enumerate(items):
        is_last = (i == total_items - 1)
        if kind == "tool":
            tool_idx += 1
            name = data.get("name", "tool")
            status = data.get("status", "completed")
            status_suffix = f" ({status})" if status != "completed" else ""
            out_lines.append(f"--- Tool #{tool_idx}: {name}{status_suffix} ---")
            inp = _format_tool_input(data.get("input"))
            if inp:
                out_lines.append(f"Input:\n{inp}")
            if data.get("error"):
                out_lines.append(f"Error:\n{_clip_text(str(data['error']))}")
            elif data.get("output") is not None:
                out_lines.append(f"Output:\n{_clip_text(str(data['output']))}")
            out_lines.append("")
        elif kind == "tool_result":
            res = data.get("result")
            is_err = data.get("is_error")
            lbl = "Error" if is_err else "Output"
            out_lines.append(f"{lbl}:\n{_clip_text(str(res))}")
            out_lines.append("")
        elif kind == "answer":
            out_lines.append("--- Answer ---")
            out_lines.append(_clip_text(str(data).strip(), max_chars=4000, max_lines=40))
            out_lines.append("")
        elif kind == "text":
            label = "--- Answer ---" if is_last else "--- Agent ---"
            out_lines.append(label)
            out_lines.append(_clip_text(str(data).strip(), max_chars=4000, max_lines=40))
            out_lines.append("")
        elif kind == "error":
            out_lines.append("--- Error ---")
            out_lines.append(_clip_text(str(data).strip(), max_chars=2000, max_lines=20))
            out_lines.append("")

    if child.error and not any(k == "error" for k, _ in items):
        out_lines.append("--- Error ---")
        out_lines.append(_clip_text(child.error.strip()))
        out_lines.append("")

    out_lines.append(f"Log: {child.path}")
    return "\n".join(out_lines).strip()


def transcript(log_dir: Path | str, target: str, now: float | None = None) -> str:
    log_dir = Path(log_dir)
    found = find_child(log_dir, target)
    if isinstance(found, list):
        matches = ", ".join(f"#{c.seq} ({c.id})" for c in found)
        return f"ambiguous: matches {matches}"
    if found is None:
        if not log_dir.exists() or not list(log_dir.glob("*.jsonl")):
            return f"children in {log_dir}: none logged"
        return f"child '{target}' not found in {log_dir}"
    child = found
    try:
        lines = child.path.read_text().splitlines()
    except OSError as exc:
        return f"could not read log for child #{child.seq} ({child.id}): {exc}"
    return format_transcript(child, lines, now=now)
