import json

from .kernel_host import ExecResult

STDOUT_MAX_BYTES = 50 * 1024
STDOUT_MAX_LINES = 2_000
REPR_MAX_BYTES = 16 * 1024
REPR_MAX_LINES = 200
STDERR_MAX_BYTES = 16 * 1024


def truncate_head(text: str, max_bytes: int, max_lines: int) -> tuple[str, bool, int, int]:
    lines = text.splitlines(keepends=True)
    kept, size = [], 0
    for line in lines[:max_lines]:
        size += len(line.encode())
        if size > max_bytes:
            break
        kept.append(line)
    return "".join(kept), len(kept) < len(lines), len(kept), len(lines)


def truncate_tail(text: str, max_bytes: int) -> tuple[str, bool]:
    data = text.encode()
    if len(data) <= max_bytes:
        return text, False
    return data[-max_bytes:].decode(errors="ignore"), True


def format_result(result: ExecResult, child_count: int, usage: dict) -> str:
    parts: list[str] = []
    if result.stdout:
        content, cut, kept, total = truncate_head(result.stdout, STDOUT_MAX_BYTES, STDOUT_MAX_LINES)
        if cut:
            content += (
                f"\n[stdout truncated: {kept} of {total} lines; the data is still in the kernel"
                " — print smaller slices]"
            )
        parts.append(content.rstrip("\n"))
    if result.result is not None:
        content, cut, _, _ = truncate_head(result.result, REPR_MAX_BYTES, REPR_MAX_LINES)
        parts.append(f"Out: {content}" + ("\n[result repr truncated]" if cut else ""))
    if result.stderr:
        content, cut = truncate_tail(result.stderr, STDERR_MAX_BYTES)
        label = "stderr" if result.ok else "Error"
        parts.append(f"{label}:\n{content.rstrip()}" + ("\n[stderr truncated]" if cut else ""))
    if child_count:
        parts.append(f"[rlm children: {child_count}, usage: {json.dumps(usage)}]")
    return "\n".join(parts) if parts else "(no output)"
