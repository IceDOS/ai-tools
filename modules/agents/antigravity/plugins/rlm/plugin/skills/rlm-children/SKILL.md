---
name: rlm-children
description: Show this conversation's rlm() children. Answered locally; no action needed.
---
The rlm plugin's PreInvocation hook already answered this command: it printed the child table and ended the turn before the model ran.

Take no action for this command. Call no tools, do not repeat or summarize the child table, and do not mention this command or the hook. Continue with the user's next request.
