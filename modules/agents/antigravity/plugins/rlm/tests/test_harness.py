from rlm_mcp import harness


def test_create_update_delete(tmp_path):
    g, l = harness.global_path(tmp_path), harness.local_path(tmp_path, "c1")
    applied, errors = harness.apply_edits(
        [
            {
                "action": "create",
                "kind": "prompt",
                "title": "Terse",
                "content": "Be terse.",
            },
            {
                "action": "create",
                "scope": "global",
                "title": "Tests",
                "content": "Run pytest -q.",
            },
        ],
        "local",
        g,
        l,
    )
    assert errors == [] and len(applied) == 2
    local = harness.load(l)
    assert local[0]["kind"] == "prompt" and local[0]["scope"] == "local"
    gid = harness.load(g)[0]["id"]
    applied, errors = harness.apply_edits(
        [
            {
                "action": "update",
                "scope": "global",
                "id": gid,
                "content": "Run pytest -x.",
            },
            {"action": "delete", "id": local[0]["id"]},
            {"action": "update", "id": "nope"},
        ],
        "local",
        g,
        l,
    )
    assert len(errors) == 1 and "nope" in errors[0]
    assert harness.load(g)[0]["content"] == "Run pytest -x." and harness.load(g)[0]["version"] == 2
    assert harness.load(l) == []


def test_caps(tmp_path):
    harness.apply_edits(
        [{"action": "create", "title": "T" * 500, "content": "C" * 5000}],
        "global",
        harness.global_path(tmp_path),
        harness.local_path(tmp_path, "c"),
    )
    e = harness.load(harness.global_path(tmp_path))[0]
    assert len(e["title"]) == 120 and len(e["content"]) == 2000


def test_section():
    assert harness.build_section([], []) == ""
    entries = [
        {
            "id": "a1",
            "kind": "memory",
            "title": "M",
            "content": "x" * 600,
            "created_at": "1",
        },
        {"id": "b2", "kind": "prompt", "title": "P", "content": "y", "created_at": "2"},
    ]
    s = harness.build_section(entries, [])
    assert s.startswith("## Continual harness (rlm plugin)")
    assert "### Memories\n- [a1] M — " + "x" * 500 + "…" in s
    assert "### Policies\n- [b2] P — y" in s
    assert harness.section_hash(s) == harness.section_hash(s) and len(harness.section_hash(s)) == 64


def test_prompt_and_schema():
    p = harness.build_refine_prompt("focus on tests", "local", [], "user: hi")
    assert (
        "Focus instructions: focus on tests" in p
        and 'Default scope: "local"' in p
        and "user: hi" in p
    )
    assert harness.PROPOSAL_SCHEMA["required"] == ["summary", "edits"]
