---
name: code-review
description: Review the current diff, or a pull request number, branch, commit range, or path, for correctness bugs and for reuse, simplification, and efficiency cleanups at a given effort level, verify every finding adversarially, and report only what survives. Reads a pull request but never posts to one. Use when the user asks for a code review, a diff review, or a pull request review.
---

# Code review

Two passes. Reviewer children read the change along separate dimensions. A verify pass then attacks each finding and assigns a verdict, and only findings that survive reach the user.

Run the whole flow from the `ipython` tool. `rlm(prompt)` blocks until the child finishes and returns its final answer as a string. The kernel serializes `rlm()` calls, so children run one after another, even from threads. Collect results through files: give each child an output path and read the file after the call returns.

Keep the review out of the conversation. Spawn one orchestrator child that owns steps 1 through 6, writes `report.md` into the work directory, and returns that path. The parent then prints the file. Only the report reaches the main context, not the diff, the per-dimension findings, or the verify transcripts. Skip the orchestrator and run the steps directly only when the user asks to watch it happen, or when a `--fix` run needs to edit the tree the user is working in right now.

The orchestrator and its reviewers need an `rlm()` depth of 2, the default. If `rlm()` raises a depth error, tell the user to run `/rlm depth 2` and stop.

## 1. Resolve the target

The default target is the branch's commits ahead of its upstream plus any uncommitted changes, not the working tree alone. Write the diff to disk once and pass that path to every child.

```python
import json, os, pathlib, subprocess, time

def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout

work = pathlib.Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / f"code-review-{int(time.time())}"
work.mkdir(parents=True, exist_ok=True)
patch = work / "diff.patch"

upstream = sh("git rev-parse --abbrev-ref --symbolic-full-name @{upstream}").strip()
base = f"{upstream}..." if upstream else "HEAD"   # no upstream: fall back to the working tree
patch.write_text(sh(f"git diff {base}"))
```

Other targets:

- a branch or ref range such as `main...my-feature`: `git diff <range>`
- a single commit: `git show <sha>`
- a path: `git diff {base} -- <path>`, or review the file as it stands when the path is unchanged
- a number, such as `1234`, or a pull request URL: read the diff and the description

`gh` is not on the PATH, so reach it through nix-shell. Reading a pull request is the only thing `gh` is for here:

```python
GH = 'nix-shell -p gh --run {!r}'
patch.write_text(sh(GH.format("gh pr diff 1234")))
body = sh(GH.format("gh pr view 1234"))
```

The first call downloads `gh` and takes a few seconds. It reads the credentials already in `~/.config/gh`. When a call reports that authentication is missing, say so and stop rather than guessing at the target.

If the patch is empty, say there is nothing to review and stop.

Collect the instruction files that apply and pass their paths, not their contents, to the children: the root `AGENTS.md` or `CLAUDE.md`, plus any in the directories the diff touches.

## 2. Resolve the effort level

Levels are `low`, `medium`, `high`, `xhigh`, and `max`. When the invocation names none, reuse the level the user typed last, even from an earlier session, and say so in one line before starting, such as `Reusing high effort, the level you typed last time`. With no remembered level, use `high`.

The level outlives the session, so keep it in a file. Only a level the user typed updates it:

```python
agent_dir = pathlib.Path(os.environ.get("PI_CODING_AGENT_DIR", pathlib.Path.home() / ".config/pi-agent"))
memory = agent_dir / "code-review-level"
level = requested_level or (memory.read_text().strip() if memory.exists() else "high")
if requested_level:
    memory.write_text(requested_level)
```

What each level changes:

| Level | Dimensions | Verdicts reported |
| --- | --- | --- |
| low | correctness only | CONFIRMED |
| medium | correctness, reuse | CONFIRMED |
| high | all six | CONFIRMED, PLAUSIBLE |
| xhigh | all six, plus a second reviewer per dimension that is given the first one's findings and looks for what it missed | CONFIRMED, PLAUSIBLE |
| max | all six, one child per changed file per dimension | CONFIRMED, PLAUSIBLE |

Low and medium aim for few findings that are certainly real. High and above trade that for coverage and may surface findings the verify pass could not fully confirm.

Children use the model pi-rlm picks for them, shown by `/rlm status`. `rlm()` cannot choose a model per call. Children load no extensions, so they fail on a provider an extension registers, such as `opencode-free`. On such a session model, switch models first.

## 3. Review pass

Dimensions, each mapping to a finding category:

1. Correctness in the diff. Read the changed lines and their immediate context. Large bugs only.
2. Reuse. A helper in the repository already does this, or the change duplicates logic that exists elsewhere.
3. Simplification. The code collapses into something shorter with identical behavior. Dead parameters, redundant branches, needless indirection.
4. Efficiency. Repeated work in a loop, an avoidable allocation or query per item, a linear scan where the data is already indexed.
5. History. `git log` and `git blame` on the modified lines, looking for bugs that only show up against that history, including a fix being undone.
6. Instructions and comments. The change against the `AGENTS.md` and `CLAUDE.md` files from step 1, and against the comments in the modified files. Those instruction files guide writing code, so not every line applies to a review.

Child prompt template:

```python
TEMPLATE = """[review dimension: {name}]
{instructions}

The diff under review is at {patch}.
Repository instruction files: {agents}.
Repository root: {root}.
Already judged not actionable, do not report again: {declined}.

Write a JSON array to {out}. One object per finding:
{{"file": "<repo-relative path>", "line": <int>, "category": "{category}",
  "short_summary": "<the claim alone, at most 60 chars, no rationale>",
  "summary": "<one sentence stating the defect>",
  "failure_scenario": "<concrete inputs or state, then the wrong output or crash>",
  "evidence": "<what you read that proves it>"}}

A correctness finding without a concrete failure scenario is not a finding. Drop it.
Write [] when you find nothing. Do not report outside your dimension.
Answer with the single word done.
"""

for dim in DIMENSIONS:
    out = work / f"{dim['name']}.json"
    rlm(TEMPLATE.format(out=out, patch=patch, root=root, agents=agents,
                        declined=declined or "nothing yet", **dim))
```

## 4. Verify pass

Load every findings file and drop duplicates that name the same file, line, and claim. Then spawn one verify child per finding whose job is to disprove it, not to agree with it.

Give each verify child the finding, the patch path, and the instruction files, have it write its verdict to a file, and require one of three verdicts:

- `CONFIRMED`: the child traced the code and the failure scenario holds. For a cleanup, the simpler form is behavior-identical and the child checked the callers.
- `PLAUSIBLE`: the child could not disprove it and could not fully confirm it. The reasoning is sound but some path was unreadable or depends on runtime data.
- `REJECTED`: the child disproved it, or it is one of the false positives below, or it is a pre-existing issue the change does not introduce.

A finding flagged from an instruction file needs that file to name the issue specifically. Otherwise it is `REJECTED`.

Drop every `REJECTED`. At `low` and `medium`, drop `PLAUSIBLE` too.

## 5. Report

Rank most severe first: correctness before cleanups, and within correctness by how much the failure scenario costs. Print one block per finding:

```
path/to/file.ts:42  [correctness, CONFIRMED]
  <short_summary>
  <summary>
  Fails when: <failure_scenario>
```

Keep it short, cite real paths and line numbers, and use no emoji. When nothing survives, say the change looks clean at this level and name the level, so the user knows what was and was not covered.

## 6. Flags

`--fix` applies the surviving findings to the working tree after reporting, then re-reports each one with an outcome of `fixed`, `skipped`, or `no_change_needed`. Fix in severity order, keep each fix to the narrowest edit that addresses the finding, and mark a finding `skipped` with a reason rather than forcing an edit you do not believe in.

`--until-clean` runs the loop below.

## Git and gh are read-only

Read with `git diff`, `git show`, `git log`, `git blame`, `git rev-parse`, and with `gh pr diff` and `gh pr view`. Nothing else.

Never change repository state and never write anything to a remote or to a pull request. No `commit`, `add`, `push`, `pull`, `fetch`, `checkout`, `switch`, `branch`, `merge`, `rebase`, `reset`, `stash`, `tag`, or `restore`. No `gh pr comment`, `gh pr review`, `gh pr create`, `gh pr edit`, `gh pr close`, `gh pr merge`, `gh issue` of any kind, and no `gh api` with a method other than GET. No `glab`. Reviewing a pull request means reading it and reporting back here.

`--fix` edits files in the working tree and stops there. Leave the changes unstaged and uncommitted, and report what was edited so the user can stage what they want. A `--fix` run on a pull request target edits nothing, because the branch is not checked out; report the findings instead.

When a finding would need a commit, a branch, or a comment on a pull request to resolve, report it and say what the user needs to do.

## False positives

Do not report these:

- Pre-existing issues the change does not introduce
- Something that looks like a bug but is not
- Nitpicks a senior engineer would not raise
- Anything a linter, type checker, compiler, or test run would catch, such as missing imports, type errors, or formatting. Assume those run in CI.
- Thin test coverage or weak documentation, unless an instruction file requires them
- An issue an instruction file names but the code explicitly silences, for example with a lint ignore comment

## Review until clean

Turn this on with `--until-clean`, or when the request asks for the change to end up clean, for example "review until nothing is left" or "fix what you find and review again". A plain review request stays a single pass.

Run the rounds in this turn, with the round state on disk:

```python
loop = {"target": target, "level": level, "work": str(work), "round": 0,
        "fixed": [], "declined": [], "last_fingerprints": []}
(work / "loop.json").write_text(json.dumps(loop, indent=2))
```

A fingerprint is the file, line, and short summary of one finding. Each round:

1. Re-derive the diff. The working tree moved since the last round, so never reuse an earlier patch file.
2. Run the review pass and the verify pass at the recorded level. Pass the `declined` fingerprints to every reviewer child so they stop resurfacing and the loop can converge.
3. If nothing survives, report how many rounds it took and what changed, and stop.
4. Otherwise fix each surviving finding. Move a finding you judge wrong on a second look to `declined` with the reason instead of editing around it. Append the rest to `fixed` as you fix them.
5. Record this round's fingerprints in `last_fingerprints`, increment `round`, and write `loop.json`.

Never edit code only to clear a finding. A wrong finding belongs in `declined`.

Stop when a round has no surviving finding. Also stop when the surviving fingerprints match `last_fingerprints` two rounds running, because another round would repeat the same work on the same code. In that case, report what is still open and what was fixed, and stop editing.

## Cleanup

Delete the work directory once the report is printed, unless the user asks to keep it.
