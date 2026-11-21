import json
import os
import subprocess
from pathlib import Path

from . import harness
from .config import Settings

TRANSCRIPT_CAP = 20_000


def queue_path(data_dir: Path, conversation_id: str) -> Path:
    return Path(data_dir) / "refine-queue" / f"{conversation_id}.json"


def queue_refine(
    data_dir: Path,
    conversation_id: str | None,
    instructions: str | None,
    global_: bool,
) -> tuple[bool, str | None]:
    if not conversation_id:
        return False, "no conversation id yet; try again after the first turn"
    path = queue_path(data_dir, conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps({"instructions": instructions, "global": bool(global_)}) + "\n")
    return True, None


def pop_queue(data_dir: Path, conversation_id: str) -> list[dict]:
    path = queue_path(data_dir, conversation_id)
    try:
        claimed = path.with_suffix(f".{os.getpid()}.claim")
        os.replace(path, claimed)
    except OSError:
        return []
    requests = [json.loads(line) for line in claimed.read_text().splitlines() if line.strip()]
    claimed.unlink()
    return requests


def transcript_tail(path: str | None, cap: int = TRANSCRIPT_CAP) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(errors="replace")[-cap:]
    except OSError:
        return ""


def run_refinement(
    settings: Settings,
    conversation_id: str,
    transcript_path: str | None,
    request: dict,
    model: str | None,
) -> dict:
    scope = "global" if request.get("global") else "local"
    g = harness.global_path(settings.data_dir)
    l = harness.local_path(settings.data_dir, conversation_id)
    prompt = harness.build_refine_prompt(
        request.get("instructions"),
        scope,
        harness.load(g) + harness.load(l),
        transcript_tail(transcript_path),
    )
    argv = [
        settings.agy,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(harness.PROPOSAL_SCHEMA),
        "--print-timeout",
        "5m",
    ]
    if model:
        argv += ["--model", model]
    proc = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    try:
        envelope = json.loads(proc.stdout)
        proposal = envelope["structured_output"]
        edits = list(proposal.get("edits") or [])
    except (ValueError, KeyError, TypeError, AttributeError):
        return {
            "summary": "",
            "applied": [],
            "errors": [f"refinement child failed: {proc.stderr[-2000:].strip()}"],
        }
    applied, errors = harness.apply_edits(edits, scope, g, l)
    return {"summary": str(proposal.get("summary", "")), "applied": applied, "errors": errors}


def spawn_detached(settings: Settings, conversation_id: str) -> None:
    subprocess.Popen(
        settings.self_cmd + ["refine-run", "--conversation", conversation_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
