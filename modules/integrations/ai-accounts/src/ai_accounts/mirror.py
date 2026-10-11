"""Shared mode: an account dir of symlinks into the agent's real dir, minus private entries."""
import filecmp
import fcntl
import os
import shutil
from contextlib import contextmanager
from pathlib import Path

from . import config


class MirrorConflict(Exception):
    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"{path} is a real file where a symlink belongs; move it away and retry, "
                         "or add it to the adapter's seed set if the agent rewrites it")


def _split(private):
    here, nested = set(), {}
    for p in private:
        head, sep, rest = p.partition("/")
        if sep:
            nested.setdefault(head, set()).add(rest)
        else:
            here.add(head)
    return here, nested


def _seed(src: Path, dst: Path, private) -> None:
    """Copy a seed entry once; the account owns it afterwards."""
    if dst.is_symlink():
        dst.unlink()
    elif dst.is_dir() and src.is_dir():
        # Accounts made before seeds existed still hold links into the real dir; turn them into copies.
        for link in [p for p in dst.rglob("*") if p.is_symlink()]:
            target = Path(os.readlink(link))
            if target.is_relative_to(src) and target.is_file():
                link.unlink()
                shutil.copy2(target, link)
        return
    elif os.path.lexists(dst):
        return
    if src.is_dir():
        skip = {src / p for p in private}
        shutil.copytree(src, dst, symlinks=True, ignore=lambda d, names: [n for n in names if Path(d) / n in skip])
    else:
        shutil.copy2(src, dst)


def _walk(real: Path, acct: Path, private, skip, fix: bool, seed=frozenset()) -> list:
    problems = []
    here, nested = _split(private)
    seed_here, seed_nested = _split(seed)
    names = {e.name for e in real.iterdir()} if real.is_dir() else set()
    if fix:
        acct.mkdir(parents=True, exist_ok=True)
    for name in sorted(names - here - set(skip)):
        src, dst = real / name, acct / name
        if name in seed_here:
            if fix:
                _seed(src, dst, nested.get(name, set()))
            elif dst.is_symlink():
                problems.append(f"{dst}: symlink, should be the account's own copy")
            continue
        if (name in nested or name in seed_nested) and src.is_dir():
            if dst.is_symlink():
                if not fix:
                    problems.append(f"{dst}: symlink, needs a real dir for its private or seed entries")
                    continue
                dst.unlink()
            problems += _walk(src, dst, nested.get(name, set()), (), fix, seed_nested.get(name, set()))
            continue
        if dst.is_symlink():
            if os.readlink(dst) == str(src):
                continue
            if fix:
                dst.unlink()
            else:
                problems.append(f"{dst}: points to {os.readlink(dst)}, not {src}")
                continue
        elif dst.exists():
            # An agent's rename-over-save leaves an identical copy; relink it without asking.
            same = dst.is_file() and src.is_file() and filecmp.cmp(dst, src, shallow=False)
            if not same:
                if fix:
                    raise MirrorConflict(dst)
                problems.append(f"{dst}: real file where a symlink belongs")
                continue
            if fix:
                dst.unlink()
        if fix:
            dst.symlink_to(src)
        # check reports only what sync cannot fix itself; a missing link is normal for a new account.
    if acct.is_dir():
        for dst in acct.iterdir():
            if dst.name not in here | seed_here and dst.is_symlink() and not dst.exists():
                if fix:
                    dst.unlink()
                else:
                    problems.append(f"{dst}: dangling link")
    return problems


def sync(real: Path, acct: Path, private=frozenset(), skip=frozenset(), seed=frozenset()) -> None:
    _walk(real, acct, private, skip, fix=True, seed=seed)


def check(real: Path, acct: Path, private=frozenset(), skip=frozenset(), seed=frozenset()) -> list:
    return _walk(real, acct, private, skip, fix=False, seed=seed)


@contextmanager
def locked(key: str):
    path = config.runtime_dir() / f"{key}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
