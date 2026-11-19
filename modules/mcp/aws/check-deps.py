# Fails when any requirement reachable from argv[1], requested extras included, is unmet.
# Further args are "dist:requirement" pairs allowed to mismatch.
import sys
from importlib.metadata import PackageNotFoundError, distribution

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

seen, errors = set(), []
allowed = {tuple(map(canonicalize_name, a.split(":"))) for a in sys.argv[2:]}


def visit(name, extras):
    key = (canonicalize_name(name), frozenset(extras))
    if key in seen:
        return
    seen.add(key)
    dist = distribution(name)
    envs = [{"extra": e} for e in ("", *extras)]
    for raw in dist.requires or []:
        req = Requirement(raw)
        if req.marker and not any(req.marker.evaluate(env) for env in envs):
            continue
        try:
            found = distribution(req.name)
        except PackageNotFoundError:
            errors.append(f"{dist.name}: {req} (missing)")
            continue
        ok = req.specifier.contains(found.version, prereleases=True)
        if not ok and (canonicalize_name(dist.name), canonicalize_name(req.name)) not in allowed:
            errors.append(f"{dist.name}: {req} (have {found.version})")
            continue
        visit(req.name, req.extras)


visit(sys.argv[1], ())
print("\n".join(errors) or f"all requirements of {sys.argv[1]} met ({len(seen)} checked)")
sys.exit(bool(errors))
