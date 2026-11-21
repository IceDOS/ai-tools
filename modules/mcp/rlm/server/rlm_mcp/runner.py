import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .config import Settings

MAX_OUTPUT_CHARS = 64_000


@dataclass
class ChildResult:
    ok: bool
    output: str
    error: str | None
    usage: dict = field(default_factory=dict)
    turns: int = 0
    status: str = ""


@runtime_checkable
class RunnerBackend(Protocol):
    name: str

    def build_child_argv(self, settings: Settings, prompt: str, model: str | None) -> list[str]:
        ...

    def parse_child_stream(self, lines: Iterable[str]) -> ChildResult:
        ...

    def build_refine_argv(self, settings: Settings, prompt: str, schema: dict, model: str | None) -> list[str]:
        ...

    def parse_refine_output(self, stdout: str, stderr: str) -> tuple[dict | None, str | None]:
        ...


class AgyRunner:
    name = "agy"

    def build_child_argv(self, settings: Settings, prompt: str, model: str | None) -> list[str]:
        argv = [
            settings.agy,
            "-p",
            prompt,
            "--agent",
            "rlm",
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
            "--print-timeout",
            settings.child_timeout,
        ]
        if model:
            argv += ["--model", model]
        return argv

    def parse_child_stream(self, lines: Iterable[str]) -> ChildResult:
        result = None
        for line in lines:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict):
                if msg.get("event") == "result" and isinstance(msg.get("result"), dict):
                    result = msg["result"]
                elif msg.get("type") == "result":
                    result = msg
        if result is None:
            return ChildResult(False, "", "child exited without a result record")
        status = str(result.get("status", ""))
        ok = status == "SUCCESS"
        output = str(result.get("response") or "")
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + f"\n... [rlm output truncated at {MAX_OUTPUT_CHARS} chars]"
        error = None if ok else str(result.get("error") or f"child status {status or 'unknown'}")
        return ChildResult(
            ok,
            output,
            error,
            dict(result.get("usage") or {}),
            int(result.get("num_turns") or 0),
            status,
        )

    def build_refine_argv(self, settings: Settings, prompt: str, schema: dict, model: str | None) -> list[str]:
        argv = [
            settings.agy,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema),
            "--print-timeout",
            "5m",
        ]
        if model:
            argv += ["--model", model]
        return argv

    def parse_refine_output(self, stdout: str, stderr: str) -> tuple[dict | None, str | None]:
        try:
            envelope = json.loads(stdout)
            proposal = envelope["structured_output"]
            if not isinstance(proposal, dict):
                return None, "structured_output is not a dict"
            return proposal, None
        except (ValueError, KeyError, TypeError, AttributeError):
            return None, f"refinement child failed: {stderr[-2000:].strip()}"


class OpencodeRunner:
    name = "opencode"

    def build_child_argv(self, settings: Settings, prompt: str, model: str | None) -> list[str]:
        argv = [
            settings.opencode,
            "run",
            "--format",
            "json",
            "--agent",
            "rlm",
            "--auto",
            "--standalone",
        ]
        if model:
            argv += ["--model", model]
        argv += ["--", prompt]
        return argv

    def parse_child_stream(self, lines: Iterable[str]) -> ChildResult:
        current_step_text: list[str] = []
        last_step_text = ""
        turns = 0
        usage: dict = {}
        error_msg = None
        seen_event = False

        for line in lines:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue

            event_type = msg.get("type")
            seen_event = True

            if event_type == "step_start":
                turns += 1
                current_step_text = []
            elif event_type == "text":
                part = msg.get("part")
                if isinstance(part, dict) and "text" in part:
                    current_step_text.append(str(part["text"]))
            elif event_type == "step_finish":
                last_step_text = "".join(current_step_text)
                part = msg.get("part")
                if isinstance(part, dict):
                    tokens = part.get("tokens")
                    if isinstance(tokens, dict):
                        for k, v in tokens.items():
                            if isinstance(v, (int, float)):
                                usage[k] = usage.get(k, 0) + v
            elif event_type == "error":
                err = msg.get("error")
                if isinstance(err, dict):
                    error_msg = err.get("message") or str(err)
                elif err:
                    error_msg = str(err)

        if not seen_event:
            return ChildResult(False, "", "child exited without any output")

        output = last_step_text if last_step_text else "".join(current_step_text)
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + f"\n... [rlm output truncated at {MAX_OUTPUT_CHARS} chars]"

        if error_msg:
            return ChildResult(False, output, error_msg, usage, turns, "ERROR")

        if not output and turns == 0:
            return ChildResult(False, "", "child exited without a result record", usage, turns, "EMPTY")

        return ChildResult(True, output, None, usage, turns, "SUCCESS")

    def build_refine_argv(self, settings: Settings, prompt: str, schema: dict, model: str | None) -> list[str]:
        instruction = (
            f"\n\nRespond with a single raw JSON object matching this schema:\n"
            f"{json.dumps(schema, indent=2)}\n"
            f"Do not include markdown code blocks, fences, or explanation. Only valid JSON."
        )
        argv = [
            settings.opencode,
            "run",
            "--format",
            "json",
            "--auto",
            "--standalone",
        ]
        if model:
            argv += ["--model", model]
        argv += ["--", prompt + instruction]
        return argv

    def parse_refine_output(self, stdout: str, stderr: str) -> tuple[dict | None, str | None]:
        res = self.parse_child_stream(stdout.splitlines())
        text = res.output.strip() if res.ok and res.output else stdout.strip()
        if not text:
            return None, f"refinement child failed: {stderr[-2000:].strip() or res.error}"

        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end >= start:
            text = text[start : end + 1]

        try:
            proposal = json.loads(text)
            if not isinstance(proposal, dict):
                return None, "refinement output is not a dict"
            return proposal, None
        except Exception as exc:
            return None, f"failed to parse JSON from refinement output: {exc}"


_RUNNERS: dict[str, type] = {
    "agy": AgyRunner,
    "opencode": OpencodeRunner,
}


def get_runner(name: str | None = None) -> RunnerBackend:
    key = (name or os.environ.get("RLM_RUNNER") or "agy").lower()
    cls = _RUNNERS.get(key)
    if cls is None:
        raise ValueError(f"unknown runner backend: {key!r}")
    return cls()
