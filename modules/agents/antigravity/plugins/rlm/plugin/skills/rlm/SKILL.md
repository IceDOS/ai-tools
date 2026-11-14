---
name: rlm
description: Manage the rlm plugin's Python kernel and recursive children. Use when the user types /rlm with status, restart, depth <n> or children.
---
Map the user's `/rlm` arguments to one rlm plugin tool call, then report its output verbatim:

- no argument or `status`: call `rlm_status`.
- `restart`: call `rlm_restart`.
- `depth <n>`: call `rlm_set_depth` with `depth` set to n.
- `children`: call `rlm_children`.

RLM mode cannot be toggled inside a session. To start in RLM mode, run `agy --agent rlm`, or enable `icedos.ai-tools.antigravity.extensions.recursive-language-models.autoload`.
