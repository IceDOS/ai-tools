# AGENTS.md — IceDOS **ai-tools**

> Utilizes the **IceDOS** framework. The full bible — module structure, config flow,
> the `icedos rebuild --build` test loop, `validate.*` helpers, dep loading — lives in
> **core**: <https://github.com/IceDOS/core/blob/main/AGENTS.md> — this file only
> covers what is specific to **ai-tools**.

## Non-negotiable rules (full detail in core)
- Build/test only via the `icedos` CLI — **never `sudo nixos-rebuild`**.
- **Never** `git commit/stash/reset/pull` — the user manages git.
- Every option uses a `validate.*`/`mk*Option` helper; **no untyped options**.
- Option-path segments that hold other options are **kebab-case** (`agent-bridge`); only leaf value keys are camelCase (`remoteLookup`).
- A module's `config.toml` defaults must mirror its `icedos.nix` defaults.
- Format with `icedos nixf .` after editing any `.nix`.

## Purpose
AI agents, local model servers and their glue: `claude-code`, `antigravity`, `pi-agent`, `opencode`
(agents), `llamacpp`, `ollama` (model servers),
`mcp-server` (`icedos mcp serve`), one module per MCP server (`aws-mcp`, `basic-memory-mcp`,
`chrome-devtools-mcp`, `context7-mcp`, `ddev-mcp`, `kwin-mcp`, `nixos-mcp`, `opencart-mcp`, `pagespeed-insights-mcp`, `plesk-mcp`, `rea-mcp`,
`signal-mcp`), `mcp-gateway` (one local HTTP server in front of the shared MCP servers),
`alimit` (usage limits for every agent and provider),
`ai-accounts` (several accounts per agent), `agent-launch` (fzf agent picker) and
`code-intelligence` (per-language LSP skills).

## Namespace
Every option here lives under `icedos.ai-tools`: the shared `rules`, `skills.pool`, `mcp-servers` and
`mcp-gateway-modes`, and one attrset per module (`icedos.ai-tools.pi-agent`, ...).

## Layout
`modules/<category>/<name>/{icedos.nix,config.toml}`; `flake.nix` exposes them via
`icedosLib.scanModules { path = ./modules; filename = "icedos.nix"; }`, which recurses, and
`meta.name` (not the folder) names the module. The categories are `agents` (antigravity,
claude-code, pi-agent, opencode), `models` (llamacpp, ollama), `mcp` (one folder per MCP server: icedos-mcp, whose module name is `mcp-server`, and
aws, basic-memory, chrome-devtools, context7, ddev, kwin, nixos, opencart, pagespeed-insights, plesk, rea, signal, each named
`<folder>-mcp` except `kwin-mcp`; gateway, whose module name is `mcp-gateway`; `mcp/lib` holds their
shared Nix helpers) and `integrations` (agent-launch, ai-accounts, alimit, code-intelligence).
`modules/default` loads whenever the repo is listed and owns the shared options.

## Shared options (`icedos.ai-tools.*`)
- `icedos.ai-tools.rules` — name = markdown. pi-agent writes them all to
  `<dataDir>/APPEND_SYSTEM.md`, which pi appends to every system prompt,
  antigravity writes `~/.config/gemini/rules/<name>.md`, `claude-code` sets
  `programs.claude-code.rules` (mkDefault per entry), and opencode concatenates them into
  `programs.opencode.context`.
- `icedos.ai-tools.skills.pool` — name = full SKILL.md. Lands in the shared on-disk tree at
  `${config.xdg.dataHome}/icedos/agents` (`~/.local/share/icedos/agents`) containing `global/`,
  `per-agent/<agent>/` and `view/<agent>/skills/` (merged global + per-agent, per-agent wins,
  minus that agent's `excludeSkills`). The whole root is one store derivation (`agents-dir`)
  linked by one Home Manager `xdg.dataFile."icedos/agents"` entry, so rebuilds swap it
  atomically and removed entries vanish. Fan-out: antigravity, Claude Code and opencode link
  per entry into `view/<agent>/skills/<name>` (never a whole-dir link; user-installed skills
  live in `~/.claude/skills`), while pi-agent lists `view/pi-agent/skills` in `settings.json`.
  Claude marketplace plugins stay per user in HM scope.
- `icedos.ai-tools.plugins.pool` — name = store path. Global plugin storage, not auto-loaded;
  agents load only the global plugins listed in `per-agent.<agent>.globalPlugins`.
- `icedos.ai-tools.per-agent.<agent>` — `{ skills, plugins, globalPlugins, excludeSkills }`
  for `claude-code`, `antigravity`, `opencode`, and `pi-agent`. Agent options feed into them
  (`claude-code.{skills,excludeSkills}`, `antigravity.{skills,excludeSkills}`,
  `opencode.{skills,excludeSkills}`, `pi-agent.skills.{extra-builtin,exclude}`). Skill modules
  (`superpowers`, `ponytail`, `unslop`) set `per-agent.<agent>.{globalPlugins,excludeSkills}` under
  `lib.mkIf (has ...)` inside one `per-agent` attrset (a shallow `//` across agents drops all but the
  last). `icedos.ai-tools.agent-views.<agent>.skills` (internal read-only) exposes the computed list of
  view skill names for agent modules to link per entry.
- Non-agent modules (`llamacpp`, `ollama` and the `mcp` modules) have a `skills`
  option that adds to `icedos.ai-tools.skills.pool`, so a skill ships with the module it documents.
- `icedos.ai-tools.mcp-servers` — name = `programs.mcp.servers` entry. `default` gives every hm
  user these servers (mkDefault per entry, so a same-named `programs.mcp.servers` entry wins),
  which every agent module reads, and writes `/etc/mcp/servers.json` (`{"mcpServers": ...}`,
  remote ones with `type = "http"`) for agents outside Home Manager.

## MCP modules
- Each one packages its server as a real derivation, puts it on the system PATH, registers it
  in `mcp-servers` under the server name agents already use (`basic-memory`, `chrome-devtools`,
  `aws-mcp`, `context7`, `ddev`, `icedos`, `kwin-mcp`, `nixos`, `opencart`, `pagespeed-insights`, `rea`, `signal-mcp`) and ships
  its skills. `plesk-mcp` instead registers one `plesk-<name>` entry per `servers.<name>`, and its
  wrapper sources the credentials from `PLESK_ENV_FILE`, so they stay out of the store.
  `opencart-mcp` sources `OPENCART_ENV_FILE` (`envFile`) the same way, and
  `pagespeed-insights-mcp` reads `GOOGLE_API_KEY` from `GOOGLE_API_KEY_FILE` (`apiKeyFile`).
- Every module with a local process also declares `gatewayMode` (`global`, `per-agent` or `stdio`) in its
  own `config.toml`, with a one-line reason, and mirrors it to
  `icedos.ai-tools.mcp-gateway-modes.<server>` (as a `lib.mkDefault`, so a user value wins) where it
  registers the `mcp-servers` entry. `context7-mcp` is a remote url, so it has none; `plesk-mcp` applies
  one mode to every `plesk-<name>` it registers.
- Skills that upstream ships come from the package source: `upstreamSkills` lists the names,
  `lib/upstream-skills.nix` reads `SKILL.md` and appends `references/*.md`. They read
  `${package.src}` (`pkgs.context7-mcp.src` for context7), an IFD that only needs the fetch,
  so skills always match the packaged version. basic-memory builds from its GitHub release
  for this, since its wheel leaves out `skills/`. The module's own `skills`
  replaces an upstream skill of the same name. `opencart-mcp` has no upstream skills; its
  `shippedSkills` reads the same layout from its own `skills/` folder (`opencart-extension`).
- `aws-mcp` runs the AWS proxy from `github:aws/mcp-proxy-for-aws` as the shared `aws-mcp` server and installs
  the plugins in `plugins` from `github:aws/agent-toolkit-for-aws` for Claude Code only, since their skills ship
  scripts that the text-only shared `skills` pool would lose. `package.nix` removes each plugin's uvx `aws-mcp`
  entry and points its hooks at a store Python.
- `lib/python.nix` is Python 3 with the MCP Python SDK 2.x from pinned wheels, used by
  `kwin-mcp`, `basic-memory-mcp` and `aws-mcp`; `basic-memory-mcp` adds the FastMCP release it pins on top.
  `aws-mcp` bumps nixpkgs' FastMCP to 4.x and the deps whose floors nixpkgs trails, each only while
  nixpkgs is older, and `check-deps.py` fails its build on any unmet requirement in the closure.
  `lib/python-mcp1.nix` pins the SDK back to 1.x for `mcp-server`, `opencart-mcp`, `plesk-mcp`
  and `signal-mcp`. nixpkgs ships SDK 2.x, which marks its FastMCP 3.x broken.
  `lib/python-bin.nix` runs an entry point through `python.withPackages`, since kwin-mcp
  re-runs `sys.executable -m kwin_mcp.*` and needs its packages in the child.
- `rea-mcp`, `ddev-mcp` and `pagespeed-insights-mcp` build the npm tarball with `package-lock.json` generated from the trimmed
  `package.json` their `postPatch` writes (rea drops frida, ddev drops its build tools). Each `update.sh` regenerates the lock
  with the same jq trim, so keep the two filters identical.
- Pins not in nixpkgs live in JSON next to the package: `source.json` per server and
  `lib/python-sdk.json` for the SDK wheels. Each has an `update.sh` (`lib/update-python-sdk.sh`
  for the SDK) and an `update-<module>.yml` workflow over `module-update.yml`. kwin-mcp, opencart-mcp and
  signal-mcp track upstream `main`, which runs ahead of their tags; basic-memory follows its
  latest release and the exact FastMCP version its `pyproject.toml` pins.

- `mcp-gateway` serves the stdio servers over streamable HTTP from one per-user service
  (`systemd.user.services.icedos-mcp-gateway`, `graphical-session.target`), so agents connect instead of
  each starting a copy. It names no servers itself: it reads `icedos.ai-tools.mcp-gateway-modes`, which
  each MCP module fills in from its `gatewayMode`. `global` keeps one child for every agent, `per-agent`
  one child per MCP session, and `stdio` (the default, and the only choice for remote-url servers)
  leaves the entry alone. `default` turns each routed registry entry into `{ type = "http"; url = ...; }` from
  `icedos.ai-tools.mcp-http-endpoints` (mkDefault per entry, port `7800`, alias `icedos-mcps` added to
  `networking.hosts` for 127.0.0.1), and the daemon spawns the registry's own `command`/`args`/`env`,
  so routing never touches a module. `mcp-gateway-modes` is a normal option, and a module's value is only
  a default, so a user config routes its own server — or flips a shipped one — without a module:
  ```toml
  [icedos.ai-tools.mcp-servers.my-server]
  command = "my-server"
  args = [ "serve" ]

  [icedos.ai-tools.mcp-gateway-modes]
  my-server = "per-agent"
  rea = "per-agent" # ships as stdio
  ```
  Only what `mcp-servers` registers is routed: entries added straight to `programs.mcp.servers` (per hm
  user) stay outside the gateway.
  A mode naming a server nothing registers (a module name like `rea-mcp` instead of `rea`, say), or a
  non-`stdio` mode on a url entry, fails the build with an assertion instead of being dropped silently.

## Agent registry
`agent-launch` owns `icedos.ai-tools.agent-launch.agents.<name>` (`command`, `label`,
`resumeArgs`, `locator`) and the `agent-launch` fzf picker. Every agent module lists it in
`meta.dependencies` and registers itself there. Don't set `zed.terminalInitCommand` from an
agent module: zed's agent bridge extends agent-launch and reads this registry.
With `relayIds`, it injects a fresh `AGENT_RELAY_ID` and prints a startup banner; it skips the banner on resume flags (`-r`, `--resume`, `-c`, `--continue`, `--conversation`, `--session`).

## Cross-repo checks
`zed` stays in `apps`, so `hasModule` calls for it pass `url = "github:icedos/apps"`
instead of `repoUrl`.

## Test a change to this repo
Point this repo's `overrideUrl` at the local checkout in the config root
(`path:/home/ice/Projects/icedos/ai-tools`), then `icedos rebuild --build`.

## Notable modules / gotchas
- `pi-agent`: upstream pi (`pkgs.pi-coding-agent`) wrapped to use `$XDG_CONFIG_HOME/pi-agent`
  (`settings.dataDir`) and to refuse `pi update`. Nix owns `mcp.json` (from `programs.mcp.servers`,
  `timeout = settings.mcpCallTimeout`) and `APPEND_SYSTEM.md`, so `pi mcp add` cannot write there;
  add servers to `icedos.ai-tools.mcp-servers` instead. Activation (`lib/seed.sh`) merges the managed
  keys into pi's `settings.json` and replaces each `settings.providers` entry in `models.json`, keeping
  everything else pi wrote. `auth.json` is never written; sign in with `/login`.
  Extensions are store paths listed in the managed `settings.json` `extensions` key, in order:
  `extensions.builtin` (pi's examples, by name), the shipped cost-footer (behind `meters.cost`), context-cap
  (behind `context-window-cap.enable`), rate-limit-hook (when `hooks.rateLimit` or `vpn-rotate` is set), opencode-free (behind `extensions.opencodeFree`), view-image (behind `extensions.viewImage`), vpn-row (with the `vpn-rotate` module, which also adds a `hooks.rateLimit` entry) and terminal-title (behind `terminal-title.enable`), then `extensions.inline` by name. terminal-title owns the terminal title, so `patches/pi-terminal-title-noop.mjs` blanks pi's own `updateTerminalTitle` in the package (there is no setting for it) and the extension writes only on change.
  The shipped ones live in `per-agent/pi-agent/plugins/pi-agent-extensions` and `settings.json` lists
  those by stable path: they import each other by relative path, so a per-file store path would break them.
  `lib/extensions.nix` holds the `bundleScript`/`bundlePaths` helpers that build and address the directory; an
  entry with `load = false` is copied but left out of `settings.json`. `extensions/free-models.ts`, which
  the others import, is such an entry.
  `extensions/free-models.ts` is the single manifest for opencode Zen's free models: the provider
  table, the cost-footer's rate-limit pools, and the parser/cache the runtime resolves through, so the
  provider list and the pools cannot drift apart. Nothing is compiled in: there is no seed table, so at
  launch opencode-free registers only what the last successful fetch cached in
  `<dataDir>/opencode-free/models.json`, and registers nothing at all when that cache is missing or
  unreadable. It then resolves `https://opencode.ai/inference/openai/v1/models` (the public catalog,
  filtered by `parseServedModels`; a nix build cannot fetch it) and publishes the result, announcing a
  changed table with a warning. A failed resolve is never silent: the error goes to stderr and to a
  `notify(..., "error")` queued for the first session, the cached table stays if there was one, and if
  there was not, the picker simply has no free models until a later session retries and succeeds.
  `PI_OPENCODE_FREE_CATALOG=off` disables the catalog entirely, `PI_OPENCODE_FREE_CATALOG_TTL_MS`
  (12h) bounds when a launch resolves again, and `PI_OPENCODE_FREE_MODELS_URL` points the resolve at
  another endpoint. `stalePools()` warns when a pooled model is no longer served, so a retired model
  cannot sit in a bucket nothing burns. llamacpp
  adds `inline.llamacpp-lifecycle` and alimit adds `inline.alimit` with `mkDefault`. pi-clm's other
  `PI_CLM_*` env goes into the wrapper as `--set-default`. Check sources with `tests/typecheck.sh`.
  `packages` entries go into settings.json `packages` and pi installs them, with `npmCommand` set to
  a store npm; activation keeps `pi install` entries and drops only those listed in
  `<dataDir>/.icedos-packages.json`. pi-ghost-text-x (`extensions.ghost-text`), rpiv-ask-user-question
  (`extensions.ask-user-question`), rpiv-todo (`extensions.todo`, which drops `todo` from
  `extensions.builtin`) pi-markdown-preview
  (`extensions.markdown-preview`) and pi-web-access (`extensions.web-access`), all on by default, pi-clm (`extensions.context-language-models`) and
  pi-rlm (`extensions.recursive-language-models`) are such packages, appended after `packages` and unpinned unless their
  `version` is set. New default packages get an `enable` and `version` option and an `optionPackages` entry. pi loads package extensions before the `extensions` list. With `steering = ""`
  the wrapper points `PI_CLM_STEERING` at the brief inside pi's npm dir, and pi-rlm gets
  `PI_RLM_PYTHON`, `PI_RLM_MAX_DEPTH` and `PI_RLM_MAX_PARALLEL`. The wrapper and its npm shim patch the installed pi-rlm:
  `patches/pi-rlm-child-extensions.patch` loads `PI_RLM_EXTENSIONS` into `rlm()` children, and
  `patches/pi-rlm-mode.patch` reads `--rlm` at session start, starts in RLM mode when `PI_RLM_AUTOLOAD=1` (`recursive-language-models.autoload`), and in RLM mode swaps only pi's built-in coding tools for ipython, keeping extension and MCP tools, their guidelines, pi-clm's guidance and pi's skills list (pi drops it when read and bash are inactive).
  `patches/pi-rlm-child-log.patch` (after the child-extensions one) appends each `rlm()` child's JSONL stream, framed by
  `rlm_child_start`/`rlm_child_end` records, to `$PI_RLM_CHILD_LOG_DIR/<id>.jsonl`; nested children inherit the dir and record
  their parent through `PI_RLM_CHILD_ID`. The shipped `rlm-children.ts` (`recursive-language-models.childViewer`) sets that dir
  to `<dataDir>/pi-rlm/children/<session id>`, shows running children below the editor and opens transcripts with `/rlm-children`.
  `patches/pi-rlm-batch.patch` adds `rlm_batch(prompts)` to the kernel: one `rlm_batch_request` that `kernel.ts` fans out to
  at most `recursive-language-models.maxParallel` children at once (upstream `rlm()` stays serial), the same call agy and opencode get.
  Bump each patch's `v<N>` marker on every change; the wrapper removes an older patched copy so pi reinstalls it.
  The shipped `view-image.ts` (`extensions.viewImage`, on by default) adds a `view_image` tool that runs pi's read on image
  files only. RLM mode drops only pi's built-in tools, and `rlmCoreExtensions` passes the tool to `rlm()` children, so vision
  models can view local images in every mode; it rejects text files so RLM mode still reads them through ipython. Prefer an upstream pi package in `packages` over a new
  shipped extension; ship code here only when no package does the job.
  `hooks.rateLimit` entries run their `command` when a listed provider's error has one of their
  `errorKinds` (`usage_limit` covers pi's non-retryable account limits). At most one hook runs per
  prompt, and on exit 0 `rate-limit-hook.ts` rewrites the error so pi's own retry fires.
  With the `vpn-rotate` module enabled, a rate-limit hook runs `vpn-rotate` for the providers in
  `extensions.vpn-rotate.providers`, only opencode-free's bun child rides the tunnel (from `mode`/`proxy` in
  `$XDG_RUNTIME_DIR/vpn-rotate.json`), and pi-agent adds `PI_CODING_AGENT_DIR` to the daemon unit so its
  probe finds the cached catalog. `extensions.vpn-rotate.maxRetries` sets the retries per prompt for
  rate-limit hooks; 0, the default, means no limit. When every matching hook fails, the hook switches to the next of
  `extensions.vpn-rotate.fallbackModels` (empty: the failing provider's free models), skipping models this prompt already
  tried, and pi retries on it; `fallbackReturn` (`prompt`, `stay` or a duration) decides when it switches back, and only while
  the session is still on the fallback.
  `skills.codeReview` ships `skills/code-review` when pi-rlm is on, and `skills.opencodeFreeReview` adds
  `skills/opencode-free-review` when code-review and opencode-free are on too. `meters.speed` adds the tok/s readout to the cost
  footer. `includeInIcedosGc` prunes `sessions/*/*.jsonl` and pi-rlm snapshots older than `sessionRetentionDays`.
  With kde's `vigor` module, unset `meters.power` values come from vigor's tariff, `gpuIdleWatts` and `windows`.
  `agent-launch` registers locator `pi-agent`, which zed's bridge resolves from pi's per-cwd session dirs.
- `antigravity`: owns `icedos.ai-tools.antigravity.users.<name>`. agy hardcodes `$HOME/.gemini`, so its package's `agy` runs `agy_home.py`, which sets `HOME` to a symlink farm of `~` (`~/.local/state/icedos/antigravity/home`, built by ai-accounts' `mirror.sync` on each launch) whose `.gemini` links to `~/.local/share/gemini`; a home that already has `.gemini` (an ai-accounts account, a nested agy) is left alone. Every process agy starts inherits that `HOME`, and a new top-level entry one of them creates lands in the farm, not `~`. Code outside agy reads `~/.local/share/gemini` directly, and ai-accounts gets it as `dataDir`. The `rlm` plugin (`modules/agents/antigravity/plugins/rlm/`, powered by the shared server in `modules/mcp/rlm/`, enabled via `extensions.recursive-language-models.enable`) ports pi-rlm to agy. It packages the unmodified `rlm_kernel.py` pinned from `@florianguyonnet/pi-rlm` alongside the `rlm_mcp` server and links `~/.config/gemini/plugins/rlm`. When `extensions.recursive-language-models.autoload` is true, the `agy` wrapper appends `--agent rlm`. Child agents run headless via `agy -p --agent rlm --output-format stream-json`; the kernel preamble adds `rlm_batch(prompts)`, one `rlm_batch_request` that the host fans out over a thread pool of `maxParallel` children per call (shared with opencode, since both use `modules/mcp/rlm/`; upstream `rlm()` stays serial). Children are logged to `<dataDir>/children/<session>/<id>.jsonl`. The CLI and server exchange metadata through `sessions/<pid>.json`. Kernel data variables are pickled after each cell and restored when a conversation resumes; the postGc hook prunes old snapshots, child logs and harness notes. The `/refine` harness records persistent prompt and memory notes injected during session start. Running children show live in the terminal title (`· 3 rlm d1:2 d2:1`, per depth from each start record, counted by `tool_state.py` from the child logs via `ANTIGRAVITY_RLM_DATA_DIR`) and as a status-bar snapshot, since agy never redraws the bar during a tool call, and the timer names an MCP call `<ServerName>/<ToolName>` (`rlm_kernel/ipython`) instead of `call_mcp_tool`; a log whose start-record `pid` (the rlm server) is gone counts as ended. `/rlm-children` is answered by the plugin's PreInvocation hook with an `errorMessage` plus `terminate`, so it costs no model turn; `/rlm-children <id>` (or `#seq` or a unique prefix) posts a child transcript; plugin skills arrive namespaced (`/rlm:rlm-children`), and agy 1.5.12 still drops a whole hook result whose `systemMessage` carries any metadata, even the documented `metadata.user_markdown` (log: `panicked: type mismatch`). `/agents` switches to rlm by forking the conversation. In agy RLM mode, `run_workflow` fans out agents using tools or MCP with `type_name="worker"`; the plugin ships the `worker` subagent.
- `opencode`: built on Home Manager's `programs.opencode`. Declarative settings land in
  `~/.config/opencode/opencode.json` (`settings`) and client TUI configuration in
  `~/.config/opencode/cli.json` (`cli`). Rules from `rules` and `opencode.rules` are concatenated
  sorted by name into `programs.opencode.context` (`~/.config/opencode/AGENTS.md`). Skills from
  `skills.pool` and `opencode.skills` map to `programs.opencode.skills`, with `excludeSkills`
  filtering out skills provided by plugins. The `ponytail` skills module adds its upstream opencode
  plugin to `opencode.plugins` and lists its skills in `excludeSkills`. Server plugins pass via
  `plugins` (`settings.plugins`). TUI plugin `icedos-status` runs via `cli.plugins` to handle terminal
  title (`cli.terminal.title = false`, managed via `terminal-title.{enable,idleGlyph,workingGlyphs}`),
  status banners (`turnEndMessage`), and tool timers (`toolTimer`; names the nested tool `kernel.ipython` during `execute`). When `alimit.users.<name>.statusLine`
  is true, an OpenCode TUI plugin (`alimit/opencode/tui.tsx`) renders usage in slot `app` via
  `cli.plugins`. CLM extension `extensions.context-language-models` (`opencode/clm/module.nix`) adds
  `opencode-clm[@version]` through opencode's npm installer (pinning a version disables auto-update),
  with the non-empty fields as plugin options; `compaction = "off"` also sets `compaction.auto = false`. CLM 0.5.1 mirrors each session into `<project>/.opencode/clm/` unless `mirrorDir` is set, and its own `compaction` option has no effect on OpenCode 2, so it isn't passed.
  RLM extension `extensions.recursive-language-models` (`opencode/rlm/module.nix`, powered by `modules/mcp/rlm/`)
  registers MCP server `kernel` and the `opencode-rlm` server plugin; `autoload` sets `default_agent = "rlm"`.
  Agent `rlm` denies every tool except `execute`, `skill`, `kernel_*`, and dynamically generated allow rules for configured MCP servers (`<server>_*`), reached from `execute` as `tools.<server>["<tool>"]` (opencode permissions are last-match). Children run headless
  via `opencode run --format json --agent rlm --auto --standalone`; `icedos-status` renders an opencode RLM children footer row. Data lives in
  `${XDG_DATA_HOME:-~/.local/share}/icedos-rlm`, shared with antigravity's rlm (disjoint session ids).
  `/rlm-children` lists children and `/rlm-children <id>` (or `#seq` or a unique prefix) posts a child transcript, as plugin commands, and `/refine` harness notes are injected through the `context` hook.
  `codeReview` ships the `code-review` skill (needs RLM) and `freeReview` its loop on `opencode/*-free`
  children started with `opencode run --agent build`. npm extension `power` (`npmExtensions` in
  `icedos.nix`) takes `{enable,version,options}`; TUI-only packages go to `cli.plugins`, the rest to
  `settings.plugins` (tok/s is native: `cli.session.tps`). OpenCode 2.3 accepts an `lsp` key but has no LSP client, so the module sets none.
  With the `vpn-rotate` module enabled, the `plugins/vpn-rotate` server plugin runs
  `vpn-rotate` from the `session.retry` hook on `provider.rate-limit`/`provider.free-limit` errors of provider
  `opencode` and retries at once on exit 0. While the state file says `tunnel`, its `model.request` hook points that
  provider's base URL at a loopback `Bun.serve` forwarder that fetches through the tunnel proxy, so other providers
  stay direct. When a rotation fails (not when cancelled), the retry hook moves the request to the next model in
  `extensions.vpn-rotate.fallbackModels` (empty means every enabled zero-cost `opencode` model, big-pickle included), skipping models that
  failed during the same prompt. It calls `session.switchModel` so the input bar shows the fallback, and its
  `experimental.model.resolve` hook covers the retry that may start before the switch lands. `fallbackReturn` picks
  when the original selection (variant included) comes back: `prompt` (next prompt), `stay` (never) or a duration
  such as `15m` (`s`/`m`/`h`); it only switches back while the session still uses the fallback. `icedos-status` shows the state as a footer row (`vpnRow`), and with agent-relay enabled the session's relay ID next to it. The module also turns off Stylix's
  opencode target, which only writes the `tui.json` OpenCode 2 ignores.
  Interactive invocations prepend `launchWrapper` and append `extraArgs`; `desktopEntry` installs a
  `.desktop` file for the launcher. The package wrapper sets `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1`
  so opencode skips `~/.claude/skills`. Background web service `web.{enable,extraArgs}` runs `opencode serve`
  under a systemd user unit. `ai-accounts` isolates credentials and session history under
  `~/.local/state/icedos/ai-accounts/opencode/<account>/` (`OPENCODE_CONFIG_DIR` seeded with
  `opencode.json`, `OPENCODE_DB` pointing to `opencode.db`); provider `opencode-go`, login
  `opencode auth login`, logged-in check reads the `credential` table read-only. `includeInIcedosGc`
  prunes `~/.local/share/opencode/log/` and deletes root sessions older than `sessionRetentionDays`
  (read from `opencode.db` read-only) via `opencode --standalone session delete`, so the background
  service is never started. `agent-launch` registers locator `opencode` (`--session {id}`), which zed's
  bridge resolves from `opencode.db` (`session_v2`, read-only).
- `claude-code` — owns `icedos.ai-tools.claude-code.users.<name>` (`mkSubmoduleAttrsOption`,
  filled per normal user via `genDefaults`) and enables `programs.claude-code` for every hm user
  (`enable = lib.mkDefault true`, `enableMcpIntegration = true`). It feeds the shared
  `icedos.ai-tools.{rules,skills}` into `programs.claude-code.{rules,skills}` with mkDefault
  per entry. `settings` and per-user skills go through the raw passthrough
  (`[home-manager.users.<name>.programs.claude-code.*]`). MCP servers come from the shared
  `programs.mcp.servers` registry.
  - `marketplaces` — fetcher results are not TOML-expressible, so list them under
    `[[icedos.ai-tools.claude-code.users.<name>.marketplaces]]` with a `source` enum
    (`fetchFromGitHub`/`fetchFromGitLab`/`fetchgit`/`path`) and that source's fields.
    `path` is copied into the store at eval time, and pure eval forbids host paths there.
  - `claudeAiMcpServers` controls `ENABLE_CLAUDEAI_MCP_SERVERS` (default false); sets default settings (`remoteControlAtStartup`, `autoCompactWindow`, `promptSuggestionEnabled`, `awaySummaryEnabled`, `autoDreamEnabled`, `precomputeCompactionEnabled`, `attribution`) via mkDefault.
  - `terminal-title` (on by default) adds title hooks on SessionStart, UserPromptSubmit, Pre/PostToolUse(Failure), Stop and SessionEnd; they run `tool_timer.py`.
- `ai-accounts` — **standalone** module that owns `icedos.ai-tools.ai-accounts.users.<name>`
  (self-materialised via `genDefaults`). `ai-account exec <agent> --` picks an account (flag,
  `AI_ACCOUNT_<AGENT>`, sticky, alimit headroom, `default`) and execs the real binary with
  per-account env. Agent modules wrap their package through `lib/wrap.nix` only when
  `lib/has-accounts.nix` finds declared accounts.
  - State lives in `~/.local/state/icedos/ai-accounts/<agent>/<account>/`. Shared mode links the real dir
    except `adapters.*_PRIVATE` (never linked) and `adapters.*_SEED` (copied once, then owned by the
    account, because agents rewrite those files by renaming over the target).
  - A logged-out account fails with exit 3 when stdin is not a tty. On a tty the launch signs in
    first: ollama prompts for the key, and the other agents start and show their own sign-in.
  - alimit keys for non-default accounts are `<provider>@<tag>/<window>`. Status lines read
    `AI_ACCOUNT_TAGS`, and `alimit headroom` feeds auto-pick.
  - Per-account `settings` merge over the agent module's `icedos.managed-settings.<agent>` into the
    account's own settings file on each launch, recorded in `.ai-account-settings.json` so removing
    an override restores the value it replaced.
  - Antigravity shares one `settings.json` across accounts (the newest copy wins at launch), so a
    per-account `settings` override can reach other accounts until their next launch re-applies theirs.
  - Antigravity accounts run behind `xdg-dbus-proxy`, which hides `org.freedesktop.secrets` so agy
    stores its token in the account's `~/.gemini`. `antigravity.busTalk` lists the reachable names.
- `alimit` — **standalone** module that owns `icedos.ai-tools.alimit.users.<name>`
  (self-materialised via `genDefaults`). One SQLite DB at `~/.local/share/alimit/usage.db`
  holds usage windows keyed `<provider>/<window>` (Claude keeps bare keys). Status lines
  record samples while agents run, and a user timer polls the providers in `providers`
  (claude, opencode-go, ollama, openrouter) for idle periods. Antigravity has no quota API,
  so only its status line records it.
  - Every bar shows only its own agent's usage: Claude Code's status line shows claude,
    antigravity's shows antigravity and pi-agent's footer shows the provider of the current
    model.
  - Weekly and monthly windows (`config.period_of`) project their end from the average burn since
    the window opened (reset minus 7 days, or one calendar month), blended with the previous
    period's for its first 2/7 (`rates.paced`). A backtest on a week of samples put it closest to
    the final weekly usage; the last hour's rate and a 5h-derived rate did worse. Antigravity opens
    its week at first use, so its reset only drifts while usage is 0%. 5h and daily windows keep
    the last hour's rate.
  - Every weekly window paired with a 5h one (`config.weekly_of`; Claude, Antigravity families,
    opencode-go `rolling`) carries `exchange`, the 5h percent per 1% weekly (`rates.exchange`):
    rises both report at the same instants, polls only when any exist, idle-session redraws
    skipped, and a missing `resets_at` (an idle 5h window) counts as a reset. Agent bars print it
    as `1%=X% 5h`, the dashboard one line per pair. Ollama's `session`
    is unpaired: its length is undocumented, and current Ollama plans only have a monthly allowance.
  - `alimit headroom` ranks accounts by each window's peak (`rates.compute`), not its last row.
  - API keys come from env, then pi-agent's `auth.json`, then opencode's; alimit never
    refreshes Claude Code's OAuth token.
  - `icedos ai limits` opens the full dashboard. `widget` installs the Plasma applet
    `org.icedos.alimit`.
- `vpn-rotate` — **standalone** module that owns `icedos.ai-tools.vpn-rotate` and the
  `vpn-rotate` user unit that pi-agent and opencode share. Enabling it turns on both agents' hooks;
  each agent keeps only `extensions.vpn-rotate.maxRetries` (pi also `providers`, opencode also the `fallback*` model options).
  - The daemon (`vpn-rotate-service`) listens on a Unix socket and persists server strikes and flags
    in `$XDG_STATE_HOME/vpn-rotate/state.json`. Each pass tries one random server in each random country;
    a failure is a strike, and 3 strikes in 15 min flag the server (15 min, 5 min if unreachable). A probe 429 that carries `Retry-After` flags the server and its IP at once, until that reset (opencode free limits report about 24h); the probe logs the 429 body and `Retry-After`.
    429 strikes and flags count per server and model; unreachable ones cover every model. It never touches the host network: once the
    host IP is rate-limited it starts a private `wireproxy` tunnel (HTTP proxy on `127.0.0.1:proxyPort`),
    rotates Proton servers inside it, and goes direct again after `directRetrySeconds`. The key comes from
    the `protonvpn signin` session in the keyring (`proton-keyring-linux`).
  - `vpn-rotate` is the socket client (`status`, `cancel`, `clear`), exposed only as `pkgs.vpn-rotate`, not on PATH.
    Both agents wrap it as `/vpn-rotate [status|cancel|clear]` (pi's `vpn-row.ts`, opencode's `plugins/vpn-rotate`).
    `cancel` stops a running rotation before its next server; `clear` cancels first, then drops all flags.
    opencode's plugin cancels by itself when the rotating session's next primary request uses another model.
  - The probe imports pi-agent's `extensions/free-models.ts` (`VPN_ROTATE_OPENCODE_FREE_MODULE`) for opencode
    Zen's URL, user-agent and cached models, probes the request's free model, and skips when it has none.
  - `cooldownSeconds` and `maxPerHour` treat 0 as no limit.
- `agent-relay` — owns `icedos.ai-tools.agent-relay`. Core pins at `${XDG_STATE_HOME:-~/.local/state}/icedos/agent-relay/pins/<kind>/<session>.txt` keep an agent's relay ID across restarts, keyed by `CLAUDE_CODE_SESSION_ID` (Claude), `getSessionId()` (pi), `rootSessionID` (opencode), and `conversationId` (Antigravity, from `/proc/<pid>/fd` presence lock at MCP start with hook fallback); a pin beats launcher `AGENT_RELAY_ID`. A conversation switch inside one process (Claude or pi `/resume`, agy changing conversation) re-registers under the resumed conversation's pinned ID and group; Claude reports it through a `SessionStart` (`resume`) hook and `session_id` in `UserPromptSubmit`, and the MCP server and agy watcher follow the new ID. Groups live in the state dir (`groups/<gid>.json`), kept across restarts and reboots, and pruned after 30d; legacy runtime groups are moved on first start.
  - `opencode`: the plugin dir `$out/lib/agent-relay/opencode` (via `icedos.ai-tools.opencode.plugins`), and `agent-relay-mcp opencode` via `opencode.extra-mcp-servers`. opencode runs one shared server for all windows, so identity is per ROOT session: each root session registers lazily with scope = `rootSessionID`, and its ID is pinned in core pins across resumes. The MCP server resolves the caller from `_meta["ai.opencode/sessionID"]`; subagents map to their root via `opencode/roots/<id>.json`, and an unmapped caller gets an error. Delivery uses `ctx.session.synthetic` steer after tool calls and on `experimental.idle`. The relay ID is injected into the system prompt. `session.deleted` retires the ID. Limit: agent-launch's `AGENT_RELAY_ID` can't choose the opencode relay ID.
- `skills` category (`ponytail`, `superpowers`, `unslop`, options at `icedos.ai-tools.skills.<name>`) — each fetches a GitHub repo (`owner`/`repo`/`rev`/`hash`)
  and reads the `skills` it lists from the repo's `skills/` dir into the shared pool through
  `mcp/lib/upstream-skills.nix`. Each name in `autoload` adds a `<name>-autoload` rule telling agents
  to load that skill at session start. Both repos are also Claude Code plugins, which bring their hooks
  and slash commands. unslop sets `programs.claude-code.plugins.unslop` to the fetched repo. ponytail
  sets `CLAUDE_CODE_PLUGIN_DIRS` in Claude's settings env instead: Claude checks a plugin's manifest hook
  files and hooks modules against its real path, and a skills-dir symlink into the store fails that check.
  ponytail is a pi package too, so it adds the repo's store path to `pi-agent.packages`. Each agent that
  gets the plugin or package lists the module's skills in its exclude option (`claude-code.excludeSkills`,
  `pi-agent.skills.exclude`), so they don't also load from the pool. Claude Code also drops a shared
  skill whose name matches a managed plugin, since home-manager rejects the clash. Neither repo ships
  an Antigravity plugin with hooks, so agy gets only the pooled skills.
- `code-intelligence` — ships one `code-intelligence-<lang>` skill per folder listed in
  `icedos.ai-tools.code-intelligence.languages`, plus a `code-intelligence` rule pointing agents
  at them. Both land in the shared `skills` and `rules` with mkDefault.
