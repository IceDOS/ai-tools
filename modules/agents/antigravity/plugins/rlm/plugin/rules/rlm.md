---
trigger: always_on
---
## ipython + rlm() (rlm plugin)

You have an `ipython` tool from the rlm plugin: a persistent Python 3 kernel. Variables and imports survive across calls, so it doubles as scratch memory. Its namespace provides:

- `rlm(prompt, context=None) -> str`: start a recursive sub-agent with a fresh context window and get its answer back as a Python string. The sub-agent sees only what you pass it. The kernel appends context to the child's prompt text (no context variable in the child; large inputs preload as task). Use it to delegate chunk-level judgment over large data (summarize, extract or classify per chunk) without loading everything into your own context.
- `refine(instructions=None, global_=False) -> dict`: schedule a refinement of the persistent harness (memories and policies injected into future sessions). It runs when the turn ends.
- `user_prompt`: the current user request as a string.
- `show_image(path)`: queue an image (file path, bytes, or PIL Image) to inspect it visually.
- Data variables are pickled after each turn and restored when the conversation resumes.

Web search is available through search_web. Read, search and edit files and run commands from ipython (open(), pathlib, subprocess with timeouts or Popen plus polling for long jobs); there is no read_file or shell tool in this mode.
Print only small, decisive values from cells and keep bulk data in variables.
