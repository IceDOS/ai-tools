"""ai-account: run coding agents under several accounts."""
import argparse
import getpass
import json
import os
import subprocess
import sys

from . import adapters, config, dbusproxy, mirror, resolve, state
from .config import Account

EXIT_UNKNOWN, EXIT_LOGIN, EXIT_MIRROR, EXIT_PROXY = 2, 3, 4, 5
# alimit providers each agent's account covers, for AI_ACCOUNT_TAGS.
PROVIDERS = {
    "claude": ("claude",),
    "antigravity": ("antigravity",),
    "ollama": ("ollama",),
    "opencode": ("opencode-go", "openrouter"),
    "prime-agent": ("opencode-go", "openrouter"),
}


def tag(agent: str, name: str) -> str:
    if name == "default":
        return "default"
    return f"{agent}.{name}" if agent in ("opencode", "prime-agent") else name


def _headroom(cfg, agent, eligible):
    if not cfg.alimit:
        return None
    try:
        done = subprocess.run(
            [cfg.alimit, "headroom", "--agent", agent, "--threshold", str(cfg.threshold),
             "--eligible", ",".join(eligible)],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode:
        return None
    try:
        return json.loads(done.stdout)
    except ValueError:
        return None


def auto_pick(cfg, agent, accounts):
    eligible = [n for n, a in accounts.items() if adapters.logged_in(a)]
    data = _headroom(cfg, agent, eligible) if eligible else None
    return (data or {}).get("pick")


def choose(cfg, agent, flag=None, environ=None) -> Account:
    accounts = cfg.accounts.get(agent, {})
    name = resolve.resolve(
        agent, accounts, flag=flag, environ=environ, sticky=state.sticky().get(agent),
        select=cfg.select, auto=lambda: auto_pick(cfg, agent, accounts),
    )
    return accounts.get(name) or Account(agent, "default")


def build_env(cfg, agent, acct, launch, environ) -> dict:
    env = dict(environ)
    env.setdefault("ALIMIT_DB", str(config.alimit_db()))
    env.update(launch.env)
    tags = {p: tag(agent, acct.name) for p in PROVIDERS[agent]}
    if agent == "opencode" and cfg.accounts.get("ollama"):
        ollama = choose(cfg, "ollama", environ=environ)
        if not adapters.logged_in(ollama) and sys.stdin.isatty():
            _prompt_key(ollama, cfg)  # prepare raises NotLoggedIn without a key
        env.update(adapters.prepare(ollama, cfg).env)
        tags["ollama"] = tag("ollama", ollama.name)
    env["AI_ACCOUNT"], env["AI_ACCOUNT_AGENT"] = acct.name, agent
    env["AI_ACCOUNT_TAGS"] = ",".join(f"{p}={t}" for p, t in sorted(tags.items()))
    return env


def _record_status(cfg, agent):
    state.write_status(agent, {n: adapters.logged_in(a) for n, a in cfg.accounts.get(agent, {}).items()})


def _fail(code: int, msg: str) -> int:
    print(f"ai-account: {msg}", file=sys.stderr)
    return code


def cmd_exec(args) -> int:
    cfg = config.load()
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not argv:
        return _fail(EXIT_UNKNOWN, "missing command after --")
    has_own = bool(cfg.accounts.get(args.agent))
    has_ollama = args.agent == "opencode" and bool(cfg.accounts.get("ollama"))
    if not (has_own or has_ollama):
        os.execvp(argv[0], argv)
        return 0
    try:
        acct = choose(cfg, args.agent, flag=args.account)
        if not adapters.logged_in(acct):
            if not sys.stdin.isatty():
                return _fail(EXIT_LOGIN, f"{args.agent}/{acct.name} is not logged in; run: ai-account login {args.agent} {acct.name}")
            code = _login_before_launch(args.agent, acct, cfg)
            if code:
                return code
        launch = adapters.prepare(acct, cfg)
        env = build_env(cfg, args.agent, acct, launch, os.environ)
    except resolve.UnknownAccount as e:
        return _fail(EXIT_UNKNOWN, str(e))
    except adapters.NotLoggedIn as e:
        return _fail(EXIT_LOGIN, str(e))
    except (mirror.MirrorConflict, adapters.AdapterError) as e:
        return _fail(EXIT_MIRROR, str(e))
    except dbusproxy.ProxyError as e:
        return _fail(EXIT_PROXY, str(e))
    _record_status(cfg, args.agent)
    if args.agent == "opencode" and acct.mode == "shared" and adapters.opencode_oauth(acct):
        print("ai-account: opencode OAuth tokens refresh into the shared auth.json; use mode = \"isolated\"", file=sys.stderr)
    os.execvpe(argv[0], [argv[0], *launch.args, *argv[1:]], env)


def _prompt_key(acct, cfg) -> int:
    """ollama's login: ask for the API key and store it owner-only."""
    hint = adapters.login_plan(acct, cfg)[2]
    key = getpass.getpass(f"{hint}: ").strip()
    acct.dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(acct.dir / "key", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key + "\n")
    _record_status(cfg, acct.agent)
    return 0


def _login_before_launch(agent, acct, cfg) -> int:
    """Sign in the picked account on the way to the launch. Returns 0 to keep going."""
    argv, extra, hint = adapters.login_plan(acct, cfg)
    if argv is None:
        return _prompt_key(acct, cfg)
    if agent == "opencode":
        print(f"ai-account: {agent}/{acct.name} is not logged in; {hint}", file=sys.stderr)
        code = subprocess.run(argv, env={**os.environ, **extra}).returncode
        if code or not adapters.logged_in(acct):
            return _fail(EXIT_LOGIN, f"opencode login did not finish (exit {code}); run: ai-account login {agent} {acct.name}")
        return 0
    # At launch the agent signs in itself and the account keeps it, so no "then quit".
    who = "agy" if agent == "antigravity" else "the agent"
    print(f"ai-account: {agent}/{acct.name} is not logged in; sign in when {who} asks", file=sys.stderr)
    return 0


def cmd_login(args) -> int:
    cfg = config.load()
    acct = cfg.accounts.get(args.agent, {}).get(args.account)
    if acct is None or acct.is_default:
        return _fail(EXIT_UNKNOWN, f"{args.agent} has no declared account {args.account!r}")
    argv, extra, hint = adapters.login_plan(acct, cfg)
    if argv is None:
        return _prompt_key(acct, cfg)
    try:
        launch = adapters.prepare(acct, cfg) if args.agent != "opencode" else adapters.Launch({}, [])
    except dbusproxy.ProxyError as e:
        return _fail(EXIT_PROXY, str(e))
    except (mirror.MirrorConflict, adapters.AdapterError) as e:
        return _fail(EXIT_MIRROR, str(e))
    print(f"ai-account: {args.agent}/{acct.name}: {hint}", file=sys.stderr)
    env = {**os.environ, **launch.env, **extra}
    code = subprocess.run(argv, env=env, pass_fds=launch.keep_fds).returncode
    _record_status(cfg, args.agent)
    return code


def cmd_use(args) -> int:
    cfg = config.load()
    accounts = cfg.accounts.get(args.agent, {})
    if args.account != "auto" and args.account not in accounts:
        return _fail(EXIT_UNKNOWN, f"{args.agent}: unknown account {args.account!r}; valid: auto, {', '.join(sorted(accounts))}")
    state.set_sticky(args.agent, args.account)
    return 0


def list_rows(cfg) -> dict:
    out = {}
    sticky = state.sticky()
    for agent, accounts in cfg.accounts.items():
        if not accounts:
            continue
        logged = {n: adapters.logged_in(a) for n, a in accounts.items()}
        data = _headroom(cfg, agent, [n for n in accounts if logged[n]]) or {}
        usage = {r["account"]: r for r in data.get("accounts", [])}
        chosen = sticky.get(agent)
        nxt = data.get("pick") if (cfg.select == "auto" or chosen == "auto") and chosen in (None, "auto") else None
        out[agent] = [
            {
                "account": n,
                "label": a.label or n,
                "loggedIn": logged[n],
                "maxUtil": (usage.get(n) or {}).get("max_util"),
                "windows": (usage.get(n) or {}).get("windows") or {},
                "resetTs": (usage.get(n) or {}).get("reset_ts"),
                "sticky": chosen == n,
                "next": nxt == n,
            }
            for n, a in accounts.items()
        ]
    return out


def cmd_list(args) -> int:
    rows = list_rows(config.load())
    if args.agent:
        rows = {k: v for k, v in rows.items() if k == args.agent}
    if args.json:
        print(json.dumps(rows))
        return 0
    for agent, accts in rows.items():
        print(agent)
        for r in accts:
            mark = ("★" if r["sticky"] else "") + ("→" if r["next"] else "")
            util = "—" if r["maxUtil"] is None else f"{r['maxUtil']:.0f}%"
            login = "logged in" if r["loggedIn"] else "needs login"
            print(f"  {r['label']:<12} {mark:<2} {util:>5}  {login}")
    return 0


def cmd_doctor(args) -> int:
    cfg = config.load()
    problems = []
    for agent, accounts in cfg.accounts.items():
        for a in accounts.values():
            if a.is_default:
                continue
            if not adapters.logged_in(a):
                problems.append(f"{agent}/{a.name}: not logged in")
            if a.mode != "shared" or agent in ("opencode", "ollama"):
                continue
            real = cfg.real_dirs.get(agent, "")
            if agent == "claude":
                problems += mirror.check(config.Path(real), a.dir, adapters.CLAUDE_PRIVATE,
                                             seed=adapters.CLAUDE_SEED)
            elif agent == "prime-agent":
                problems += mirror.check(config.Path(real), a.dir, adapters.PRIME_PRIVATE,
                                             seed=adapters.PRIME_SEED)
            elif agent == "antigravity":
                home = a.dir / "home"
                problems += mirror.check(config.Path(real), home, skip={".gemini"})
                problems += mirror.check(config.Path(real) / ".gemini", home / ".gemini", adapters.AGY_PRIVATE,
                                         seed=adapters.AGY_SEED)
        for a in accounts.values():
            if agent == "opencode" and a.mode == "shared" and adapters.opencode_oauth(a):
                problems.append(f"opencode/{a.name}: OAuth entries {adapters.opencode_oauth(a)} in shared mode; use isolated")
    for p in problems:
        print(p)
    return 1 if problems else 0


def build_parser():
    p = argparse.ArgumentParser(prog="ai-account", description="Run coding agents under several accounts.")
    sub = p.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("exec", help="resolve an account and exec the agent")
    ex.add_argument("agent", choices=config.AGENTS)
    ex.add_argument("--account")
    ex.add_argument("argv", nargs="*")
    lg = sub.add_parser("login", help="run the agent's login inside an account")
    lg.add_argument("agent", choices=config.AGENTS)
    lg.add_argument("account")
    us = sub.add_parser("use", help="set the sticky account (or auto)")
    us.add_argument("agent", choices=config.AGENTS)
    us.add_argument("account")
    ls = sub.add_parser("list", help="accounts, login state and usage")
    ls.add_argument("--agent", choices=config.AGENTS)
    ls.add_argument("--json", action="store_true")
    sub.add_parser("doctor", help="report broken mirrors and logged-out accounts")
    return p


def _split_exec(argv):
    """argparse's REMAINDER eats `--account` once the agent positional is seen, so cut the
    command tail at `--` before parsing."""
    if argv[:1] != ["exec"]:
        return argv, None
    i = argv.index("--") if "--" in argv else len(argv)
    if i == len(argv):
        return argv, None
    return argv[:i], argv[i:]


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    head, tail = _split_exec(argv)
    args = build_parser().parse_args(head)
    if tail is not None:
        args.argv = tail
    return {"exec": cmd_exec, "login": cmd_login, "use": cmd_use, "list": cmd_list, "doctor": cmd_doctor}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
