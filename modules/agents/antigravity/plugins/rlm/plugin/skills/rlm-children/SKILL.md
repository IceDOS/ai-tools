---
name: rlm-children
description: Show this conversation's rlm() children or inspect a child transcript. Answered locally; no action needed.
---
The rlm plugin's PreInvocation hook already answered this command: it printed the child table or transcript and ended the turn before the model ran.

Take no action for this command. Call no tools, do not repeat or summarize the child table or transcript, and do not mention this command or the hook. Continue with the user's next request.
