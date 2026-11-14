import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

ENTRY_CONTENT_PROMPT_CAP = 500
SECTION_ENTRY_LIMIT = 20
TITLE_CAP = 120
CONTENT_CAP = 2_000

PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "update", "delete"]},
                    "scope": {"type": "string", "enum": ["local", "global"]},
                    "kind": {"type": "string", "enum": ["memory", "prompt"]},
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["action"],
            },
        },
    },
    "required": ["summary", "edits"],
}


def global_path(data_dir: Path) -> Path:
    return Path(data_dir) / "harness.jsonl"


def local_path(data_dir: Path, conversation_id: str) -> Path:
    return Path(data_dir) / "harness" / f"{conversation_id}.jsonl"


def load(path: Path) -> list[dict]:
    by_id: dict[str, dict] = {}
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            by_id[entry["id"]] = entry
    return sorted(
        (e for e in by_id.values() if not e.get("deleted")),
        key=lambda e: e.get("created_at", ""),
    )


def _append(path: Path, records: list[dict]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def apply_edits(
    edits: list[dict], default_scope: str, global_file: Path, local_file: Path
) -> tuple[list[dict], list[str]]:
    now = datetime.now(timezone.utc).isoformat()
    entries = {"global": load(global_file), "local": load(local_file)}
    records: dict[str, list[dict]] = {"global": [], "local": []}
    applied, errors = [], []
    for edit in edits:
        scope = edit.get("scope") if edit.get("scope") in ("global", "local") else default_scope
        kind = "prompt" if edit.get("kind") == "prompt" else "memory"
        action = edit.get("action")
        existing = next((e for e in entries[scope] if e["id"] == edit.get("id")), None)
        try:
            if action == "create":
                if not edit.get("title") or not edit.get("content"):
                    raise ValueError("create needs title and content")
                entry = {
                    "id": uuid.uuid4().hex[:8],
                    "kind": kind,
                    "scope": scope,
                    "title": edit["title"][:TITLE_CAP],
                    "content": edit["content"][:CONTENT_CAP],
                    "version": 1,
                    "created_at": now,
                    "updated_at": now,
                }
            elif action == "update":
                if existing is None:
                    raise ValueError(f'update: no entry with id "{edit.get("id", "?")}" ({scope})')
                entry = {
                    **existing,
                    "title": (edit.get("title") or existing["title"])[:TITLE_CAP],
                    "content": (edit.get("content") or existing["content"])[:CONTENT_CAP],
                    "version": existing["version"] + 1,
                    "updated_at": now,
                }
            elif action == "delete":
                if existing is None:
                    raise ValueError(f'delete: no entry with id "{edit.get("id", "?")}" ({scope})')
                entry = {
                    **existing,
                    "deleted": True,
                    "version": existing["version"] + 1,
                    "updated_at": now,
                }
            else:
                raise ValueError(f'unknown action "{action}"')
        except ValueError as exc:
            errors.append(str(exc))
            continue
        records[scope].append(entry)
        applied.append(entry)
    _append(Path(global_file), records["global"])
    _append(Path(local_file), records["local"])
    return applied, errors


def _render(entries: list[dict]) -> str:
    out = []
    for e in entries[-SECTION_ENTRY_LIMIT:]:
        content = e["content"]
        if len(content) > ENTRY_CONTENT_PROMPT_CAP:
            content = content[:ENTRY_CONTENT_PROMPT_CAP] + "…"
        out.append(f"- [{e['id']}] {e['title']} — {content}")
    return "\n".join(out)


def build_section(global_entries: list[dict], local_entries: list[dict]) -> str:
    all_entries = [*global_entries, *local_entries]
    if not all_entries:
        return ""
    parts = [
        "## Continual harness (rlm plugin)",
        "Persistent notes from past refinements. Follow them; they survive this conversation.",
    ]
    memories = [e for e in all_entries if e.get("kind") == "memory"]
    prompts = [e for e in all_entries if e.get("kind") == "prompt"]
    if memories:
        parts += ["### Memories", _render(memories)]
    if prompts:
        parts += ["### Policies", _render(prompts)]
    parts.append('Manage via /refine, or refine("instructions") inside the ipython kernel.')
    return "\n".join(parts)


def section_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def build_refine_prompt(
    instructions: str | None, scope: str, entries: list[dict], conversation: str
) -> str:
    entries_json = json.dumps(
        [
            {k: e.get(k) for k in ("id", "kind", "scope", "title", "content", "version")}
            for e in entries
        ],
        indent=2,
    )
    focus = f"Focus instructions: {instructions}\n" if instructions else ""
    return f"""You are the refinement harness of a self-improving coding agent. Analyze the conversation trajectory and propose small, evidence-backed updates to the agent's persistent harness.

The harness is a set of short notes injected into the agent's system prompt on every future run:
- kind "memory": a reusable fact, tactic, or failure pattern (e.g. "this repo's tests need --runInBand")
- kind "prompt": a behavior policy (e.g. "prefer small diffs over rewrites")
- scope "local": relevant only to the current project/session; scope "global": useful across projects

Current harness entries (may be empty):
{entries_json}

Conversation trajectory (recent, possibly truncated):
{conversation or "(empty)"}

{focus}
Rules:
- Only propose an edit with concrete evidence in the trajectory: repeated failure, reusable tactic, repeated delegation pattern, or a behavior policy worth persisting.
- Prefer updating an existing entry over creating near-duplicates.
- Each entry: imperative, generalizable, under 300 characters. No session trivia.
- Default scope: "{scope}". Use the other scope only when clearly warranted.
- If nothing is worth persisting, return an empty edits array.

Return an object with "summary" (one sentence) and "edits" (a list of create, update or delete edits)."""
