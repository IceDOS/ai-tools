# IceDOS MCP Server

An [MCP](https://modelcontextprotocol.io) server that exposes the IceDOS ecosystem —
configuration, modules, options, and system operations — to AI agents. Built on
[FastMCP](https://github.com/jlowin/fastmcp).

## Running

Packaged as an IceDOS module (`icedos.nix`). Once the system is rebuilt it is
available as a toolset command:

```sh
icedos mcp serve      # start the server over stdio
icedos mcp serve --live   # tools without sandbox= edit the real config
```

The command sets `ICEDOS_CONFIG_DIR` to the active configuration root before
launching. Run directly for development:

```sh
ICEDOS_CONFIG_DIR=/path/to/config icedos-mcp-server
```

The server starts in sandbox mode. Every config, module, query, repo explorer, state,
`nix_eval`, `get_diff` and `rebuild` call, and every resource, runs on a temp copy of
the config (see Sandboxes below), and `rollback` and `gc` only accept `dry`. Only `--live` or `ICEDOS_MCP_LIVE=1` lets
calls reach the real config. No tool argument can switch modes.

### Config discovery

The server locates the IceDOS config root by honoring `ICEDOS_CONFIG_DIR`, then
falling back to walking up from the working directory for a `flake.nix` root
(`config.toml` is **optional** — a root may be all `configs/*.toml` + `modules/`; the
walk-up also requires one of `config.toml`/`configs/`/`modules/` to skip unrelated flakes).
Config edits use `tomlkit` for round-trip preservation, and new tables are spaced with one
blank line between blocks to match the hand-written style.

## Capabilities

### Tools (48)

**Config**. The user config is the merged set of `config.toml` and every `*.toml` under `icedos.system.extraConfigs` (default `configs/`), recursively and sorted by path, like core. Hidden `configs/.*.toml` load too but are gitignored. A file opts out with top-level `enable = false`. The server merges like core: tables recurse, arrays concatenate, and a scalar set in two files is an error. A broken TOML file is an error, not skipped. Every `extraConfigs` entry must be a relative dir inside the config root.

Paths read the merged view. A write lands in the file that declares the key, and a new key goes next to its nearest existing parent, else `config.toml`. `[N]` counts items across every file that adds to the array and edits the file holding item N. A final `[]` appends to the last file holding the array. Keys with dots go in quotes, e.g. `environment.etc."foo.conf".text`. A `file=` argument forces one file, and indices are then local to it.

Every write runs under a per-root lock and re-reads the merged config. On any failure, including a failed or crashed `validate` eval, the server restores every config file it touched and removes files it created.
- `get_config`: the merged config (whole or by `section`); `file=` reads one file
- `get_config_value`: one value by dotted path; `resolved=true` returns the effective option value and type from the index
- `set_config_value`, `delete_config_value`: `validate` (default on) evals the generated flake. Delete takes `path|value` to drop the first matching array item, searching files in load order, and removes a key from every file that declares it
- `list_repositories`: one row per repo, folded over all its entries by base url like core, with the files that list it, modules, fetch flags, patches, input-patches and any sandbox override
- `add_repository`: appends an entry (`override_url`, `patches`, `input_patches`, `fetch_deps`, `fetch_optional_deps`); refuses a repo any file already lists, refs ignored
- `edit_user`: sets keys of `[icedos.users.<name>]`, each in the file that declares it
- `list_config_files`: every config file in load order with whether core loads it
- `set_config_file_enabled`: writes or removes a file's top-level `enable = false`
- `validate_config`: evaluates every option and module without building, like `icedos configuration validate`, and refreshes the search index
- Writes to a file with `enable = false` succeed with a warning, since core ignores that file

**Modules**
- `enable_module`, `disable_module`: match repo entries by base url (`github:o/r/ref` and `github:o/r` are one repo). Enable appends to the first entry unless any entry lists the module already. Disable removes it from every entry, and refuses a module that is only enabled as a dependency. Without `repo`, the owning repo comes from the index, and a name found in several repos is an error
- `module_graph`: forward deps and reverse dependents for a module

**Query and discovery**
- `search_options`, `search_modules`: relevance-ranked, paginated (`{total,count,offset,truncated,results}` with `limit`/`offset`)
- `get_option`: full option details and a paste-ready TOML snippet
- `get_module`, `list_modules`: module details or list (markers: ● explicit, ◐ dependency, ○ available)
- `nix_eval`: evaluates a Nix expression with the `icedos repl` bindings (`config`, `options`, `declared`, `pkgs`, `lib`, `icedosLib`, `inputs`; see `core/lib/repl-context.nix`). The eval is impure and unsandboxed and reads the generated `.state` flake, so `config` in a new sandbox is the live system's until its first full `rebuild` (`--dry` and `--genflake-only` keep nothing); `declared` shows sandbox edits at once

**Sandboxes**. A sandbox is a copy of the config root under `$TMPDIR/icedos-sandboxes-<uid>/<name>` (set `ICEDOS_SANDBOX_DIR` to move it). The base dir must be a 0700 dir owned by the server's user, not a symlink. The copy leaves out `.git` and the live `.state`, except `flake.nix`, `flake.lock` (so builds use the live pins) and the search index. A pristine copy of the config taken at creation, kept in `<base>/.meta/`, is the left side of every sandbox diff, so later edits to the live config never show as reverse hunks; the diff notes which live files changed since.

The config, module, query, repo explorer, state, input, `nix_eval`, `format_nix`, `get_diff` and `rebuild` tools take `sandbox=<name>`. Without it they use the server's default sandbox, which the server creates on first use and deletes on exit (also on SIGTERM and SIGHUP) when it has no changes. If something else deletes the default sandbox, the next call fails once with that news instead of quietly starting over. Resources read the same root as a call without `sandbox=`. In live mode, a call without `sandbox=` uses the real config. The sandbox choice applies per call, so agents sharing one server can each use their own sandbox.
- `sandbox_create`: copies the live config into a new sandbox, with a random name unless you pass `name`
- `sandbox_list`: the server mode, its default sandbox and every sandbox with path, source, creation time and repo overrides
- `sandbox_diff`: unified diff of the sandbox's edits (`a/` as copied, `b/` the sandbox), ready to review or apply
- `sandbox_inputs_diff`: flake input revs that differ between the sandbox and the live config, in `flake.lock` and `.state/flake.lock`
- `sandbox_delete`: removes a sandbox and its snapshot; `gc` frees the store paths its builds made
- `sandbox_override_repo`: builds a configured repo from a local checkout inside a sandbox (`repo` url or name, `path` to the checkout, empty `path` clears). The server writes the override to `configs/.icedos-mcp-overrides.toml` in the sandbox, one entry per exact url the config uses for that repo. Core loads that file like any other config, while `sandbox_diff` and every config write skip it. An `overrideUrl` for the same url in a file that loads earlier still wins, and the result warns about it. Repos the config does not list are refused

**System**
- `rebuild`: `icedos rebuild --build`, which never switches; inside a sandbox it adds `--dir <sandbox>`, which skips hooks and generation records. Flags go through an allowlist: `--dry`/`-n`/`--dry-run`, `--genflake-only`, `--update*`, `--update-repos-select`, `--update-state-inputs`, `--update-except`, `--lock-only` (no `--build` added), `--build-vm`, `--github-token-path`. Live mode only: `--boot`, `--builder` (nh copies the result back, which needs a trusted remote user), `--target`, `--update-hooks`, `--nh-args`, `--build-args`. Refused: `--dir`, `--run-vm`, `--ask`, `--github-token`. `logs=true` returns the full log with `--show-trace`. A successful build reports its store path. The build dir under `/tmp/icedos-build-*` keeps a `result` GC root until `icedos gc`
- `get_diff`: in a sandbox, its diff like `sandbox_diff`; live, how the config differs from the one that built the running system
- `rollback`: roll back to a previous generation (`dry` for a plan-only run; live mode only otherwise)
- `gc`: free disk: Nix store, home-manager profiles, temp build dirs and `.state/.cache` snapshots (`days`/`generations` retention overrides, `dry` preview); live mode only unless `dry`
- `doctor`: `icedos status`, the dashboard and health checks
- `list_inputs`: inputs pinned in `.state/flake.lock` with rev and age
- `diff_generations`: config diff between two generations
- `config_history`: `icedos configuration history`, the generation table as JSON, or one generation's config diff against the live config

**Packages**
- `list_packages`: installed packages
- `run_package`: `icedos pkgs run`, with `args` for the binary and `insecure` (blocking; `timeout` configurable)
- `pkgs_build`: `icedos pkgs build --path <package.nix>`, returning the store path, or the output of `run=<binary>` with `args`
- `format_nix`: `icedos nixf` on a file or dir; in sandbox mode it refuses paths inside the live config

**Remote build hosts**. Hosts come from `icedos.ai-tools.mcp-server.build.hosts` (ssh destinations), with `minFreeGb` and `pressureLimit` as thresholds. The module writes them to a JSON file and passes its path in `ICEDOS_MCP_BUILD_CONFIG`. The remote users need not be trusted.
- `build_hosts`: probes this PC (`nproc`, `/proc/loadavg`, `df /nix/store`, `/proc/pressure/{cpu,io}`) and each host over ssh with the same fixed command, then picks `local` or a host. This PC is busy when its 1 minute load exceeds its cores or a `some avg10` exceeds `pressureLimit`. A host qualifies when its load is below half its cores and it has `minFreeGb` plus `need_gb` free. Ties go to the host with the fewest running builds from any of this user's servers (lease files under `<sandbox dir>/.builds/`), then the lowest load per core
- `nix_build`: `nix build --no-link --json <installable>`, locally or with `--eval-store auto --store ssh-ng://<host>`. `host` is `auto`, `local` or a configured host; other hosts are refused. Outputs of a remote build stay on the host. The server records every path the log shows arriving there (copied or substituted paths, built `.drv` files and their outputs), never paths the host already had
- `remote_cleanup`: `nix store delete --store ssh-ng://<host>` on the recorded paths, retrying without any path still alive there. The server also runs it on exit

**Repo explorer** (follows the active root's `path:` overrides, sandbox overrides included)
- `explore_repo`: inspect a module repo (local override first, else GitHub API)
- `explore_local_repo`: inspect a local repo checkout
- `list_repo_tree`: every file path in a repo, local or online
- `read_repo_file`: raw source of one repo file; `offset`/`limit` for a line slice

**System info**
- `get_system_info_tool`: hostname, kernel, uptime, generation, store space
- `get_generations_tool`: generations with dates and the config snapshot that built each
- `list_state_files`: files and dirs under `.state/` (`subdir` to narrow)
- `read_state_file`: raw contents of one `.state/` file; `offset`/`limit` for a line slice

### Resources (7)

`icedos://config`, `icedos://config/files`, `icedos://options`, `icedos://modules`,
`icedos://generations`, `icedos://system-info`, and the templated
`icedos://repo/{url}` (URL is percent-encoded, e.g. `icedos://repo/github%3Aicedos%2Fapps`).

### Prompts (4)

`configure_module`, `troubleshoot`, `add_application`, `icedos_overview`.

## Layout

```
mcp-server/
  flake.nix          # IceDOS module scan entry point
  icedos.nix         # packages the server, wires the `icedos mcp serve` command
  src/
    pyproject.toml   # hatchling build; package = icedos_mcp
    icedos_mcp/
      server.py      # tool/resource/prompt definitions (FastMCP)
      config.py      # config read/edit across config.toml + configs/*.toml (tomlkit round-trip)
      config_files.py # config file-set enumeration, strict merge, path location (mirrors core)
      config_path.py  # dotted-path tokenizer ([N], [], quoted keys)
      cli.py         # subprocess wrappers for the icedos CLI
      build_hosts.py # remote build hosts: probe, pick, build, clean up
      modules.py     # repo/module exploration
      sandbox.py     # throwaway config copies for isolated edits and builds
      system.py      # system info & generations
      prompts.py     # prompt templates
```

## Development

```sh
cd src
python -m build --wheel --no-isolation   # build a wheel (hatchling)
python -m py_compile icedos_mcp/*.py      # quick syntax check
```

Run the test suite from the repo root (requires a Python with `tomlkit`; e.g. a venv):

```sh
PYTHONPATH=src python -m unittest tests.test_cli tests.test_sandbox tests.test_config -v
```

After editing any `.nix` file, format the tree from the IceDOS root with `icedos nixf .`.
