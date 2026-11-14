"""Run agy with HOME at a symlink farm of the real home whose .gemini is the XDG data dir.

agy hardcodes $HOME/.gemini, so this keeps it out of the real home.
"""
import os
import sys
from pathlib import Path

from ai_accounts import mirror


def _xdg(name: str, home: Path, default: str) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else home / default


def paths(home: Path) -> tuple[Path, Path]:
    data = _xdg("XDG_DATA_HOME", home, ".local/share") / "gemini"
    farm = _xdg("XDG_STATE_HOME", home, ".local/state") / "icedos" / "antigravity" / "home"
    return data, farm


def needs_farm(home: Path) -> bool:
    data, farm = paths(home)
    if home.resolve() == farm.resolve():  # a nested agy already runs in the farm
        return False
    gem = home / ".gemini"
    # The old layout's ~/.gemini -> data link does not count; an ai-accounts home has a real .gemini.
    return not os.path.lexists(gem) or (gem.is_symlink() and os.readlink(gem) == str(data))


def prepare(home: Path) -> Path:
    data, farm = paths(home)
    data.mkdir(parents=True, exist_ok=True)
    with mirror.locked("antigravity-home"):
        mirror.sync(home, farm, skip={".gemini"})
        link = farm / ".gemini"
        if not (link.is_symlink() and os.readlink(link) == str(data)):
            if link.is_symlink():
                link.unlink()
            link.symlink_to(data)
    return farm


def main() -> None:
    agy, args = sys.argv[1], sys.argv[2:]
    home = Path.home()
    if needs_farm(home):
        os.environ["HOME"] = str(prepare(home))
    os.execv(agy, [agy, *args])


if __name__ == "__main__":
    main()
