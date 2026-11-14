---
name: refine
description: Refine the rlm plugin's persistent harness of memories and policies. Use when the user types /refine.
---
Map the user's `/refine` arguments to one rlm plugin tool call, then summarize the result in one or two sentences:

- `list`: call `refine_list`.
- `global <instructions>`: call `refine` with `instructions` set to the text and `global_scope` set to true.
- `<instructions>` or no argument: call `refine` with `instructions` set to the text (empty when none) and `global_scope` false.
