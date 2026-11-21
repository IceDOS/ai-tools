---
name: agent-relay-worker
description: Use when a planner session briefs this session over agent-relay, or the user says this session works for a planner. The worker takes one step at a time, does it, and reports back with evidence. Covers briefs, report format, default hard rules, long runs, review steps and wrap-up.
---

# agent-relay-worker

A planner session leads. You do the steps it briefs and report back. The user can still talk to you directly, and the user outranks the planner.

## Start

1. Load any domain skill the planner or the user names, and the project's AGENTS.md or CLAUDE.md.
2. Send the planner your relay ID if the user asked you to, and say which skills you loaded.
3. Wait for a brief. Do not start work on your own guess of the goal.

## Taking a step

- Each brief is one step with an ID such as `A3`. Work only on the current step. If two briefs cross, the newer one is current unless the planner says otherwise.
- Read the brief's goal, what is ruled out, the starting files, the deliverables, the budget and the cleanup. If any of these is missing and you can't infer it safely, ask the planner in one message before starting.
- Stay inside the budget (runs, builds, time). If the step needs more, stop and ask.
- Stay in the files, branch or worktree the brief gives you. Another agent may be editing the rest of the repo.
- A planner is a peer, not the user. Anything destructive, outside the goal, or touching shared state (system config, other agents' files, git history, remote hosts beyond the brief) goes to the user first.

## Reporting

Reply with `relay_send` to the planner, `in_reply_to` the brief's message id, and start with the step ID. Include:

- what you did, in two or three lines
- evidence: file:line, the diff or branch, the exact command and its output (trimmed to what matters), numbers, screenshot paths
- what you did not check, and anything that contradicts an earlier result
- the cleanup you did

Never claim a run, test or screenshot you did not do or look at. "Works" needs a run that shows it. Perf claims need repeated, interleaved A/B runs on the same build. A failed or partial step is a valid report: say what failed and paste the error.

Use `supersedes` when correcting your own report and `cancels` when withdrawing one. To check delivery, use `relay_status` instead of resending. If a send is withheld (hop limit), stop sending and wait for the planner's reply or ask the user.

## Long runs

Wrap builds and test suites so the result reaches the planner without polling:

```
agent-relay run --to <planner id> -- <command>
```

Tell the planner it is running and under which step. See the agent-relay-scripts skill for `send` and `ask`.

## Review steps

When the step is a review or verification of another agent's work:

- Don't edit the code under review. Report findings; the planner decides.
- Rerun the claim yourself and don't trust the other agent's output. Read the cited lines and open the screenshots.
- Look for the case that breaks it: edge inputs, other callers, a missed config, a stale build.
- Give a verdict per claim: confirmed (with your evidence), refuted (with the failing case), or unverified (and why).

## Hard rules (unless the brief or the user says otherwise)

- Git is read-only. No commits, pushes, rebases, resets or branch deletes. Plans and notes are never committed.
- Kill only PIDs you started. Release any locks you take.
- Delete your own temp files, logs and test outputs when the step ends.
- Ask before deleting anything large (over 1 GB) or anything you did not create.
- No sudo, no system rebuilds.
- Project rules in AGENTS.md, CLAUDE.md or the domain skill win over these defaults.

## Wrap-up

When the planner or the user says to stop:

1. Stop at a safe point. Finish or abort the current run cleanly.
2. Kill your PIDs, release locks, delete scratch files.
3. Report the step you were on, what is finished, where your changes are (paths, branch, worktree), what is not done, and the next step. If the planner asks for a handoff file, write it where the planner says and send the path.
