"""VM build and lifecycle helpers for running test VMs from sandbox configs."""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from . import config_files, leftovers, storage

logger = logging.getLogger(__name__)

# Beyond -pidfile; GL flags append to this when gl=True.
DEFAULT_QEMU_OPTS = ""
VIRGL_QEMU_OPTS = "-display gtk,gl=on -device virtio-vga-gl"
DEFAULT_DISK_SIZE_MB = 20480

_active_vms: dict[str, dict[str, Any]] = {}


def find_free_port() -> int:
    """Probe an available ephemeral TCP port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pick_vm_user(merged: dict[str, Any], explicit_user: str = "") -> str:
    """Find the target login user: explicit user or first normal user in icedos.users."""
    users = merged.get("icedos", {}).get("users", {})
    if explicit_user:
        if explicit_user not in users:
            raise ValueError(f"user '{explicit_user}' is not defined in icedos.users")
        return explicit_user
    for name, u in users.items():
        if isinstance(u, dict) and u.get("isNormalUser", True) and not u.get("isSystemUser", False):
            return name
    raise ValueError("no normal user found in icedos.users (set one or pass user=)")


def check_disk_size(merged: dict[str, Any]) -> tuple[float, bool]:
    """Return (need_gb, needs_injection) based on build-vm.diskSize."""
    val = merged.get("icedos", {}).get("system", {}).get("build-vm", {}).get("diskSize")
    if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
        gb = round(val / 1024, 1) if val >= 512 else float(val)
        return gb, False
    return 20.0, True


def generate_ssh_keypair(key_path: Path, comment: str) -> str:
    """Generate an ed25519 keypair with mode 0600 and return the public key text."""
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if key_path.exists():
        key_path.unlink()
    pub_path = key_path.with_suffix(".pub")
    if pub_path.exists():
        pub_path.unlink()
    proc = subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(key_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"failed to generate ssh key: {proc.stderr.strip()}")
    key_path.chmod(0o600)
    return pub_path.read_text().strip()


def vm_override_path(root: Path) -> Path:
    """Path to the server-owned VM config in the sandbox."""
    dirs = config_files.extra_config_dirs(root)
    if not dirs:
        raise ValueError("icedos.system.extraConfigs is empty, so core loads no extra config dir")
    return root / dirs[0] / config_files.VM_FILE


def generate_vm_toml(
    user: str,
    password: str,
    ssh_pubkey: str = "",
    ssh_port: int | None = None,
    needs_disk_size: bool = True,
    disk_size_mb: int = DEFAULT_DISK_SIZE_MB,
    ssh: bool = True,
) -> str:
    """Generate TOML injecting VM-only credentials and port forwards via vmVariant."""
    escaped_user = json.dumps(user)
    escaped_pw = json.dumps(password)
    lines = [
        "# Managed by the icedos MCP (vm_build). Left out of sandbox diffs.\n\n",
        "[virtualisation.vmVariant.services.displayManager.autoLogin]\n",
        "enable = true\n",
        f"user = {escaped_user}\n\n",
        "[virtualisation.vmVariant.services.getty]\n",
        f"autologinUser = {escaped_user}\n\n",
        f"[virtualisation.vmVariant.users.users.{escaped_user}]\n",
        f"password = {escaped_pw}\n",
    ]
    if ssh and ssh_pubkey and ssh_port:
        escaped_key = json.dumps(ssh_pubkey)
        lines.append(f"openssh.authorizedKeys.keys = [{escaped_key}]\n\n")
        lines.extend([
            "[virtualisation.vmVariant.services.openssh]\n",
            "enable = true\n",
            "settings.PasswordAuthentication = false\n",
            "settings.KbdInteractiveAuthentication = false\n",
            'settings.PermitRootLogin = "no"\n\n',
            "[[virtualisation.vmVariant.virtualisation.forwardPorts]]\n",
            'from = "host"\n',
            'host.address = "127.0.0.1"\n',
            f"host.port = {ssh_port}\n",
            "guest.port = 22\n",
        ])
    else:
        lines.append("\n")
    if needs_disk_size:
        lines.append(f"[icedos.system.build-vm]\ndiskSize = {disk_size_mb}\n")
    return "".join(lines)


def write_vm_override(root: Path, toml_text: str) -> Path:
    """Write the injected VM config file inside the sandbox."""
    path = vm_override_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(toml_text)
    return path


def remove_vm_override(root: Path) -> bool:
    """Remove the injected VM config file if present."""
    try:
        path = vm_override_path(root)
        if path.is_file():
            path.unlink()
            return True
    except (ValueError, OSError):
        pass
    return False


def find_vm_script(build_dir: Path | None, built_path: Path | None) -> Path:
    """Locate the run-*-vm executable script from build artifacts, returning the store path."""
    target_base: Path | None = None
    if build_dir and (build_dir / "result").exists():
        res_link = build_dir / "result"
        if res_link.is_symlink():
            link_target = Path(os.readlink(res_link))
            target_base = link_target if link_target.is_absolute() else (build_dir / link_target).resolve()
        else:
            target_base = res_link
    elif built_path:
        target_base = built_path

    if target_base and (target_base / "bin").is_dir():
        candidates = sorted((target_base / "bin").glob("run-*-vm"))
        if candidates:
            return candidates[0]

    if built_path and (built_path / "bin").is_dir():
        candidates = sorted((built_path / "bin").glob("run-*-vm"))
        if candidates:
            return candidates[0]

    if build_dir and (build_dir / "result" / "bin").is_dir():
        candidates = sorted((build_dir / "result" / "bin").glob("run-*-vm"))
        if candidates:
            return candidates[0].resolve()

    raise FileNotFoundError("no VM runner script (run-*-vm) found in build output")


def format_hint(
    desktop_tools: list[str],
    script: str,
    env: dict[str, str],
    ssh_port: int | None,
    key: str | None,
    user: str,
) -> str:
    """Return tool-agnostic or desktop-control next-step instructions."""
    ssh_part = f"; ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p {ssh_port} -i {key} {user}@127.0.0.1 for a shell" if (ssh_port and key) else ""
    if "kwin-mcp" in desktop_tools:
        return (
            f"kwin-mcp is configured: session_start, then app_launch command={script} "
            f"env={json.dumps(env)}; screen_capture to see it{ssh_part}"
        )
    if ssh_port and key:
        return f"No desktop-control MCP detected. Launch the VM using launch.argv/env/cwd{ssh_part}."
    return f"No desktop-control MCP detected. Launch the VM using launch.argv/env/cwd; console login user={user}."


def _is_qemu_for_disk(pid: int, disk_dir_str: str) -> bool:
    """Verify /proc/<pid>/cmdline matches a qemu process holding this disk directory."""
    try:
        cmdline_raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        parts = [p.decode(errors="replace") for p in cmdline_raw.split(b"\0") if p]
        return any("qemu" in p for p in parts) and any(disk_dir_str in p for p in parts)
    except (OSError, ValueError):
        return False


def kill_vm_pid(pidfile: Path, disk_dir_str: str) -> int | None:
    """Terminate the QEMU process safely if verified; never kill a foreign PID."""
    if not pidfile.is_file():
        return None
    try:
        pid = int(pidfile.read_text().strip())
    except (ValueError, OSError):
        pidfile.unlink(missing_ok=True)
        return None

    if not _is_qemu_for_disk(pid, disk_dir_str):
        logger.warning("skipping pid %s: cmdline does not match qemu for %s", pid, disk_dir_str)
        pidfile.unlink(missing_ok=True)
        return None

    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except OSError:
                break
        else:
            os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    pidfile.unlink(missing_ok=True)
    return pid


def persist_vm_record(
    disk_dir: Path,
    sandbox_name: str,
    root: Path,
    build_dir: Path | None = None,
    ssh_port: int | None = None,
    user: str = "",
) -> None:
    """Persist VM record to disk_dir/vm.json so vm_cleanup works across restarts."""
    rec = {
        "sandbox": sandbox_name,
        "root": str(root),
        "build_dir": str(build_dir) if build_dir else None,
        "ssh_port": ssh_port,
        "user": user,
    }
    try:
        (disk_dir / "vm.json").write_text(json.dumps(rec, indent=2))
    except Exception as e:
        logger.warning("could not persist %s/vm.json: %s", disk_dir, e)


def record_vm(
    sandbox_name: str,
    disk_dir: Path,
    root: Path,
    build_dir: Path | None = None,
    ssh_port: int | None = None,
    user: str = "",
) -> None:
    """Track an active VM in memory and on disk for server exit / vm_cleanup."""
    rec = _active_vms.get(sandbox_name, {})
    rec["disk_dir"] = disk_dir
    rec["root"] = root
    if build_dir is not None:
        rec["build_dir"] = build_dir
    if ssh_port is not None:
        rec["ssh_port"] = ssh_port
    if user:
        rec["user"] = user
    _active_vms[sandbox_name] = rec
    persist_vm_record(
        disk_dir,
        sandbox_name,
        root,
        build_dir=rec.get("build_dir"),
        ssh_port=rec.get("ssh_port"),
        user=rec.get("user", ""),
    )


def cleanup_vm(sandbox_name: str, root: Path | None = None) -> dict[str, Any]:
    """Clean up a VM instance, its disk directory, and injected sandbox config."""
    record = _active_vms.pop(sandbox_name, {})
    disk_dir: Path | None = record.get("disk_dir")
    cleaned_root = root or record.get("root")
    build_dir: Path | None = record.get("build_dir")

    if disk_dir is None or not disk_dir.is_dir():
        for d in storage.drives():
            candidate = Path(d["mount"]) / storage.AI_DUMP / f"vm-{sandbox_name}"
            if candidate.is_dir():
                disk_dir = candidate
                break

    if disk_dir and (disk_dir / "vm.json").is_file():
        try:
            disk_rec = json.loads((disk_dir / "vm.json").read_text())
            if not build_dir and disk_rec.get("build_dir"):
                build_dir = Path(disk_rec["build_dir"])
            if not cleaned_root and disk_rec.get("root"):
                cleaned_root = Path(disk_rec["root"])
        except Exception as e:
            logger.warning("could not read %s/vm.json: %s", disk_dir, e)

    killed_pid: int | None = None
    deleted_disk_dir: str | None = None
    if disk_dir and disk_dir.is_dir():
        killed_pid = kill_vm_pid(disk_dir / "qemu.pid", str(disk_dir))
        shutil.rmtree(disk_dir, ignore_errors=True)
        deleted_disk_dir = str(disk_dir)

    removed_root: str | None = None
    if build_dir:
        result_link = build_dir / "result"
        if result_link.is_symlink() or result_link.exists():
            try:
                leftovers.remove([str(result_link)], delete=True)
            except Exception as e:
                logger.warning("leftovers.remove failed for %s: %s", result_link, e)
            result_link.unlink(missing_ok=True)
            removed_root = str(result_link)
        if build_dir.is_dir():
            shutil.rmtree(build_dir, ignore_errors=True)
    else:
        removed_root = "no build record (skipped root cleanup)"

    cleaned_overrides = False
    if cleaned_root and cleaned_root.is_dir():
        cleaned_overrides = remove_vm_override(cleaned_root)

    return {
        "sandbox": sandbox_name,
        "killed_pid": killed_pid,
        "deleted_disk_dir": deleted_disk_dir,
        "removed_root": removed_root,
        "cleaned_overrides": cleaned_overrides,
    }


def cleanup_all() -> dict[str, str]:
    """Clean all tracked VMs on server exit or broad build_cleanup."""
    report: dict[str, str] = {}
    for name in list(_active_vms):
        res = cleanup_vm(name)
        report[f"vm {name}"] = (
            f"killed pid {res['killed_pid']}" if res["killed_pid"] else "cleaned"
        )
    return report
