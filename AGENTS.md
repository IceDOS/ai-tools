# AGENTS.md — IceDOS **ai-tools**

> Utilizes the **IceDOS** framework. The full bible — module structure, config flow,
> the `icedos rebuild --build` test loop, `validate.*` helpers, dep loading — lives in
> **core**: <https://github.com/IceDOS/core/blob/main/AGENTS.md> — this file only
> covers what is specific to **ai-tools**.

## Non-negotiable rules (full detail in core)
- Build/test only via the `icedos` CLI — **never `sudo nixos-rebuild`**.
- **Never** `git commit/stash/reset/pull` — the user manages git.
- Every option uses a `validate.*`/`mk*Option` helper; **no untyped options**.
- A module's `config.toml` defaults must mirror its `icedos.nix` defaults.
- Format with `icedos nixf .` after editing any `.nix`.

## Purpose
AI agents, local model servers and their glue: `claude-code`, `antigravity`, `opencode`, `prime-agent`
(agents), `llamacpp`, `ollama` (model servers), `peon-ping` (agent-event audio),
`mcp-server` (`icedos mcp serve`), `alimit` (usage limits for every agent and provider),
`ai-accounts` (several accounts per agent), `agent-launch` (fzf agent picker) and
`code-intelligence` (per-language LSP skills).

## Namespace
Every option here lives under `icedos.ai-tools`: the shared `rules` and `skills`, and one
attrset per module (`icedos.ai-tools.prime-agent`, ...).

## Layout
`modules/<category>/<name>/{icedos.nix,config.toml}`; `flake.nix` exposes them via
`icedosLib.scanModules { path = ./modules; filename = "icedos.nix"; }`, which recurses, and
`meta.name` (not the folder) names the module. The categories are `agents` (antigravity,
claude-code, opencode, prime-agent), `models` (llamacpp, ollama), `mcp` (icedos-mcp, whose
module name is `mcp-server`) and `integrations` (agent-launch, ai-accounts, alimit, code-intelligence, peon-ping).
`modules/default` loads whenever the repo is listed and owns the shared options.

## Shared options (`icedos.ai-tools.*`)
- `icedos.ai-tools.rules` — name = markdown. prime-agent turns each into a `rule-<name>`
  system-prompt extension, antigravity writes `~/.gemini/config/rules/<name>.md`, opencode
  writes `~/.config/opencode/rules/<name>.md` and lists it in `instructions`, and `claude-code`
  sets `programs.claude-code.rules` (mkDefault per entry).
- `icedos.ai-tools.skills` — name = full SKILL.md. Same fan-out: prime-agent
  `skills.extraBuiltin`, antigravity `~/.gemini/antigravity-cli/skills/`, opencode
  `programs.opencode.skills`, Claude Code `programs.claude-code.skills`.
- Per-agent options of the same name win over the shared entry
  (`antigravity.{rules,skills}`, `opencode.skills`, `prime-agent.extensions.rules`,
  `prime-agent.skills.extraBuiltin`).
- Non-agent modules (`llamacpp`, `ollama`, `peon-ping`, `mcp-server`) have a `skills`
  option that adds to `icedos.ai-tools.skills`, so a skill ships with the module it documents.

## Agent registry
`agent-launch` owns `icedos.ai-tools.agent-launch.agents.<name>` (`command`, `label`,
`resumeArgs`, `locator`) and the `agent-launch` fzf picker. Every agent module lists it in
`meta.dependencies` and registers itself there. Don't set `zed.terminalInitCommand` from an
agent module: zed's agent bridge extends agent-launch and reads this registry.

## Cross-repo checks
`zed` stays in `apps`, so `hasModule` calls for it pass `url = "github:icedos/apps"`
instead of `repoUrl`.

## Test a change to this repo
Point this repo's `overrideUrl` at the local checkout in the config root
(`path:/home/ice/Projects/icedos/ai-tools`), then `icedos rebuild --build`.

## Notable modules / gotchas
- `peon-ping` — Warcraft-peon-style agent-event audio. **Standalone** module: owns
  `icedos.ai-tools.peon-ping.users.<name>` (self-materialised via `genDefaults`).
  Claude Code integration is upstream's own `programs.peon-ping.claudeCodeIntegration`
  (per-user opt-in, `claudeCodeIntegration = true`), and `opencode` consumes the user
  settings for its own peon plugin. See core's *Per-user (`users`) options*.
- `prime-agent` — mirrors the bundled models.dev catalog into `models.json`. Only models in
  that snapshot show up today (it lags upstream). To add a newly released model without waiting
  for a prime-agent bump, define it under `icedos.ai-tools.prime-agent.settings.providers.<name>.models`:
  prime-agent's `models[]` **merges** with the catalog — an unknown id is added (inheriting the
  built-in provider's api/baseUrl), a known id replaces the bundled definition. Use the model's
  real dot id (e.g. `glm-5.3-flash`, not `glm-5-3-flash`) and the same id in `modelOverrides`.
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
  - It declares no hooks. peon-ping registers its own via
    `icedos.ai-tools.peon-ping.users.<u>.claudeCodeIntegration`.
- `ai-accounts` — **standalone** module that owns `icedos.ai-tools.ai-accounts.users.<name>`
  (self-materialised via `genDefaults`). `ai-account exec <agent> --` picks an account (flag,
  `AI_ACCOUNT_<AGENT>`, sticky, alimit headroom, `default`) and execs the real binary with
  per-account env. Agent modules wrap their package through `lib/wrap.nix` only when
  `lib/has-accounts.nix` finds declared accounts; `prime-agent` and `opencode` also count as
  declaring when `ollama` does, because they pass its key in.
  - State lives in `~/.local/state/ai-accounts/<agent>/<account>/`. Shared mode links the real dir
    except `adapters.*_PRIVATE` (never linked) and `adapters.*_SEED` (copied once, then owned by the
    account, because agents rewrite those files by renaming over the target).
  - A logged-out account fails with exit 3 when stdin is not a tty. On a tty the launch signs in
    first: ollama prompts for the key, opencode runs `opencode auth login`, the other agents start
    and show their own sign-in. A prime-agent or opencode launch also prompts for the key of the
    ollama account it tags, when that account has no key.
  - alimit keys for non-default accounts are `<provider>@<tag>/<window>`. Status lines read
    `AI_ACCOUNT_TAGS`, and `alimit headroom` feeds auto-pick.
  - Per-account `settings` merge over the agent module's `icedos.managedSettings.<agent>` into the
    account's own settings file on each launch, recorded in `.ai-account-settings.json` so removing
    an override restores the value it replaced.
  - Antigravity accounts run behind `xdg-dbus-proxy`, which hides `org.freedesktop.secrets` so agy
    stores its token in the account's `~/.gemini`. `antigravity.busTalk` lists the reachable names.
- `alimit` — **standalone** module that owns `icedos.ai-tools.alimit.users.<name>`
  (self-materialised via `genDefaults`). One SQLite DB at `~/.local/share/alimit/usage.db`
  holds usage windows keyed `<provider>/<window>` (Claude keeps bare keys). Status lines
  record samples while agents run, and a user timer polls the providers in `providers`
  (claude, opencode-go, ollama, openrouter) for idle periods. Antigravity has no quota API,
  so only its status line records it.
  - Every bar shows only its own agent's usage: Claude Code's status line shows claude,
    antigravity's shows antigravity, prime-agent's footer and opencode's sidebar show the
    provider of the current model.
  - API keys come from env, then prime-agent's `auth.json`, then opencode's; alimit never
    refreshes Claude Code's OAuth token.
  - `icedos ai limits` opens the full dashboard. `widget` installs the Plasma applet
    `org.icedos.alimit`.
- `code-intelligence` — ships one `code-intelligence-<lang>` skill per folder listed in
  `icedos.ai-tools.code-intelligence.languages`, plus a `code-intelligence` rule pointing agents
  at them. Both land in the shared `skills` and `rules` with mkDefault.
