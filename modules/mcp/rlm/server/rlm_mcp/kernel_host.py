import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings

BOOT_TIMEOUT_S = 30
INTERRUPT_GRACE_S = 4

_PREAMBLE = """
import base64 as _base64
import json as _json
import os as _os

_RLM_IMAGE_DIR = _os.environ.get("RLM_IMAGE_DIR", "")
_RLM_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_RLM_MAX_IMAGES_PER_CELL = 5

def _detect_mime(data: bytes, path: str = "") -> str:
    if data.startswith(b"\\x89PNG\\r\\n\\x1a\\n"):
        return "image/png"
    if data.startswith(b"\\xff\\xd8"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    if path:
        lower = path.lower()
        if lower.endswith(".png"):
            return "image/png"
        if lower.endswith((".jpg", ".jpeg")):
            return "image/jpeg"
        if lower.endswith(".gif"):
            return "image/gif"
        if lower.endswith(".webp"):
            return "image/webp"
    return ""

def show_image(target):
    \"\"\"Queue an image to be sent to the model for visual inspection.
    Accepts a file path (str or Path), raw bytes, or a PIL Image.
    \"\"\"
    if not _RLM_IMAGE_DIR:
        raise RuntimeError("image staging directory is not configured")
    existing = [f for f in _os.listdir(_RLM_IMAGE_DIR) if f.endswith(".json")]
    if len(existing) >= _RLM_MAX_IMAGES_PER_CELL:
        raise ValueError(f"maximum of {_RLM_MAX_IMAGES_PER_CELL} images per cell exceeded")

    path_str = ""
    if isinstance(target, (str, _os.PathLike)):
        path_str = _os.path.expanduser(str(target))
        if not _os.path.isabs(path_str):
            path_str = _os.path.abspath(path_str)
        if not _os.path.isfile(path_str):
            raise FileNotFoundError(f"image file not found: {path_str}")
        size = _os.path.getsize(path_str)
        if size > _RLM_MAX_IMAGE_BYTES:
            raise ValueError(f"image size ({size} bytes) exceeds limit of {_RLM_MAX_IMAGE_BYTES} bytes")
        with open(path_str, "rb") as f:
            data = f.read()
    elif isinstance(target, (bytes, bytearray)):
        data = bytes(target)
        if len(data) > _RLM_MAX_IMAGE_BYTES:
            raise ValueError(f"image size ({len(data)} bytes) exceeds limit of {_RLM_MAX_IMAGE_BYTES} bytes")
    elif hasattr(target, "save"):
        import io
        buf = io.BytesIO()
        fmt = getattr(target, "format", None) or "PNG"
        target.save(buf, format=fmt)
        data = buf.getvalue()
        if len(data) > _RLM_MAX_IMAGE_BYTES:
            raise ValueError(f"image size ({len(data)} bytes) exceeds limit of {_RLM_MAX_IMAGE_BYTES} bytes")
    else:
        raise TypeError(f"expected file path, bytes, or PIL Image, got {type(target).__name__}")

    mime = _detect_mime(data, path_str)
    if not mime:
        raise ValueError("unsupported image format; expected PNG, JPEG, GIF, or WEBP")

    b64 = _base64.b64encode(data).decode("ascii")
    idx = len(existing)
    meta_path = _os.path.join(_RLM_IMAGE_DIR, f"image_{idx}.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        _json.dump({"data": b64, "mime": mime}, f)
    return f"<Image: {mime}, {len(data)} bytes>"
"""


class KernelError(RuntimeError):
    pass


@dataclass
class ExecResult:
    ok: bool
    stdout: str
    stderr: str
    result: str | None
    images: list[dict] = field(default_factory=list)


class KernelHost:
    def __init__(
        self,
        settings: Settings,
        on_rlm: Callable[[str, int], tuple[bool, str]],
        on_refine: Callable[[str | None, bool], tuple[bool, str | None]],
        snapshot_path: Path | None = None,
        preload: dict[str, str] | None = None,
        cwd: str | Path | None = None,
    ):
        self.settings = settings
        self.on_rlm = on_rlm
        self.on_refine = on_refine
        self.snapshot_path = snapshot_path
        self.preload = preload or {}
        self.cwd = str(cwd) if cwd else None
        self.proc: subprocess.Popen | None = None
        self.conn: socket.socket | None = None
        self.rfile = None
        self.ready: dict = {}
        self._next_id = 0
        self._lock = threading.RLock()
        self._busy = threading.Event()
        self._tmpdir: str | None = None
        self._image_dir: str | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _inject_preamble(self) -> None:
        self.execute(_PREAMBLE)

    def _clear_image_dir(self) -> None:
        if self._image_dir and os.path.exists(self._image_dir):
            for entry in os.scandir(self._image_dir):
                try:
                    os.unlink(entry.path)
                except OSError:
                    pass

    def _collect_images(self) -> list[dict]:
        if not self._image_dir or not os.path.exists(self._image_dir):
            return []
        items = []
        for p in sorted(Path(self._image_dir).glob("image_*.json")):
            try:
                items.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass
        return items

    def start(self) -> dict:
        if self.running:
            return self.ready
        self.close()
        self._tmpdir = tempfile.mkdtemp(prefix="rlm-kernel-")
        self._image_dir = os.path.join(self._tmpdir, "images")
        os.makedirs(self._image_dir, exist_ok=True)
        sock_path = os.path.join(self._tmpdir, "k.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(1)
        server.settimeout(BOOT_TIMEOUT_S)
        env = {
            **os.environ,
            "PI_RLM_SOCKET": sock_path,
            "PI_RLM_DEPTH": str(self.settings.depth),
            "RLM_IMAGE_DIR": self._image_dir,
        }
        env.pop("PI_RLM_SNAPSHOT", None)
        if self.snapshot_path is not None:
            self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            env["PI_RLM_SNAPSHOT"] = str(self.snapshot_path)
        self.proc = subprocess.Popen(
            [self.settings.python, self.settings.kernel],
            cwd=self.cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            self.conn, _ = server.accept()
        except socket.timeout as exc:
            self.close()
            raise KernelError("kernel did not connect within 30 s") from exc
        finally:
            server.close()
        self.conn.settimeout(None)
        self.rfile = self.conn.makefile("r", encoding="utf-8")
        self.ready = self._read()
        if self.ready.get("type") != "ready":
            raise KernelError(f"unexpected first message: {self.ready}")
        self._inject_preamble()
        for name, value in self.preload.items():
            self.set(name, value)
        return self.ready

    def _send(self, msg: dict) -> None:
        assert self.conn is not None
        self.conn.sendall((json.dumps(msg) + "\n").encode())

    def _read(self) -> dict:
        assert self.rfile is not None
        line = self.rfile.readline()
        if not line:
            raise KernelError("kernel exited")
        return json.loads(line)

    def _request(self, msg: dict, reply_type: str) -> dict:
        with self._lock:
            if not self.running:
                self.start()
            self._next_id += 1
            msg = {**msg, "id": self._next_id}
            self._send(msg)
            while True:
                reply = self._read()
                kind = reply.get("type")
                if kind == "rlm_request":
                    ok, value = self.on_rlm(reply.get("prompt", ""), int(reply.get("depth", 0)))
                    out = {"type": "rlm_response", "id": reply["id"], "ok": ok}
                    out["result" if ok else "error"] = value
                    self._send(out)
                elif kind == "refine_request":
                    scheduled, reason = self.on_refine(reply.get("instructions"), bool(reply.get("global")))
                    out = {"type": "refine_response", "id": reply["id"], "scheduled": scheduled}
                    if reason:
                        out["reason"] = reason
                    self._send(out)
                elif kind == reply_type and reply.get("id") == msg["id"]:
                    return reply

    def execute(self, code: str) -> ExecResult:
        self._busy.set()
        try:
            self._clear_image_dir()
            reply = self._request({"type": "exec", "code": code}, "result")
        except KernelError as exc:
            self.close()
            return ExecResult(False, "", f"{exc}; the next cell starts a fresh kernel\n", None)
        finally:
            self._busy.clear()
        images = self._collect_images()
        return ExecResult(
            bool(reply.get("ok")),
            reply.get("stdout", ""),
            reply.get("stderr", ""),
            reply.get("result"),
            images=images,
        )

    def set(self, name: str, value) -> None:
        self._request({"type": "set", "name": name, "value": value}, "set_done")

    def snapshot(self) -> dict:
        if not self.running:
            return {}
        return self._request({"type": "snapshot_request"}, "snapshot_done")

    def interrupt(self) -> None:
        if not self.running:
            return
        self.proc.send_signal(signal.SIGINT)

        def escalate():
            if self._busy.is_set() and self.running:
                self.proc.kill()

        threading.Timer(INTERRUPT_GRACE_S, escalate).start()

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except OSError:
                pass
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self.conn = self.rfile = self.proc = None
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = self._image_dir = None
