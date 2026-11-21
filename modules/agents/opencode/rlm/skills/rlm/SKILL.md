---
name: rlm
description: Manage the rlm plugin's Python kernel and recursive children. Use when the user types /rlm with status, restart, depth <n> or children.
---
Map the user's `/rlm` arguments to one rlm plugin tool call, then report its output verbatim:

- no argument or `status`: call `kernel_rlm_status`.
- `restart`: call `kernel_rlm_restart`.
- `depth <n>`: call `kernel_rlm_set_depth` with `depth` set to n.
- `children`: call `kernel_rlm_children`.

RLM mode cannot be toggled inside a session. To start in RLM mode, run `opencode --agent rlm`, or enable `icedos.ai-tools.opencode.extensions.recursive-language-models.autoload`.
