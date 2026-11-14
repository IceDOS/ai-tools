import json
import os
import socket
import threading
from pathlib import Path

from . import children, refine_run, session
from .config import Settings
from .harness import global_path, load, local_path
from .kernel_host import KernelHost
from .output import format_result


class CellResult(str):
    images: list[dict]

    def __new__(cls, text: str, images: list[dict] | None = None):
        obj = super().__new__(cls, text)
        obj.images = list(images) if images else []
        return obj


class RlmService:
    def __init__(self, settings: Settings, agy_pid: int, cwd: str | Path | None = None):
        self.settings = settings
        self.agy_pid = agy_pid
        self.cwd = str(cwd) if cwd is not None else session.resolve_workspace(agy_pid)
        self.depth_limit = settings.max_depth
        self.host: KernelHost | None = None
        self._cell_children = 0
        self._cell_usage: dict = {}
        self._control: socket.socket | None = None

    def session(self) -> dict:
        return session.read_session(self.settings.data_dir, self.agy_pid, cwd=self.cwd) or {}

    def _conversation(self) -> str | None:
        return self.session().get("conversationId")

    def _snapshot_path(self) -> Path | None:
        conv = self._conversation()
        if not self.settings.snapshots or not conv:
            return None
        return self.settings.data_dir / "snapshots" / f"{conv}.pickle"

    def _host(self) -> KernelHost:
        if self.host is None:
            preload = {}
            task_file = os.environ.get("RLM_TASK_FILE")
            if task_file and Path(task_file).is_file():
                preload["task"] = Path(task_file).read_text()
            self.host = KernelHost(
                self.settings,
                self.on_rlm,
                self.on_refine,
                snapshot_path=self._snapshot_path(),
                preload=preload,
                cwd=self.cwd,
            )
        return self.host

    def _log_dir(self) -> Path:
        inherited = os.environ.get("RLM_CHILD_LOG_DIR")
        if inherited:
            return Path(inherited)
        return self.settings.data_dir / "children" / (self._conversation() or "unknown")

    def on_rlm(self, prompt: str, depth: int) -> tuple[bool, str]:
        if depth > self.depth_limit:
            return False, (
                f"maximum recursion depth {self.depth_limit} reached (requested depth {depth}); "
                "solve this subtask yourself"
            )
        result = children.run_child(
            self.settings,
            prompt,
            depth,
            self.session().get("modelName"),
            self._log_dir(),
            os.environ.get("RLM_CHILD_ID"),
            cwd=self.cwd,
        )
        self._cell_children += 1
        children.add_usage(self._cell_usage, result.usage)
        return (True, result.output) if result.ok else (False, result.error or "child failed")

    def on_refine(self, instructions: str | None, global_: bool) -> tuple[bool, str | None]:
        return refine_run.queue_refine(
            self.settings.data_dir, self._conversation(), instructions, global_
        )

    def ipython(self, code: str) -> CellResult:
        host = self._host()
        self._cell_children, self._cell_usage = 0, {}
        host.start()
        host.set("user_prompt", self.session().get("lastUserInput", ""))
        exec_res = host.execute(code)
        text = format_result(exec_res, self._cell_children, self._cell_usage)
        return CellResult(text, exec_res.images)

    def status(self) -> str:
        sess = self.session()
        running = self.host is not None and self.host.running
        return "\n".join([
            f"conversation: {sess.get('conversationId') or 'unknown (SessionStart hook has not run)'}",
            f"model for children: {sess.get('modelName') or 'agy default'}",
            f"depth: {self.settings.depth} of {self.depth_limit}",
            f"kernel: {'running' if running else 'not started'}",
            f"snapshots: {self._snapshot_path() or 'off'}",
            f"child logs: {self._log_dir() if self.settings.child_log else 'off'}",
        ])

    def restart(self) -> str:
        if self.host is not None:
            self.host.close()
            self.host = None
        return "kernel restarted; the namespace is empty"

    def set_depth(self, n: int) -> str:
        self.depth_limit = max(0, int(n))
        return f"rlm() depth limit for this session: {self.depth_limit}"

    def refine_now(self, instructions: str | None, global_: bool) -> str:
        conv = self._conversation()
        if not conv:
            return "no conversation id yet; try again after the first turn"
        out = refine_run.run_refinement(
            self.settings,
            conv,
            self.session().get("transcriptPath"),
            {"instructions": instructions, "global": global_},
            self.session().get("modelName"),
        )
        return json.dumps(out, indent=2)

    def refine_list(self) -> str:
        conv = self._conversation() or ""
        entries = load(global_path(self.settings.data_dir)) + (
            load(local_path(self.settings.data_dir, conv)) if conv else []
        )
        if not entries:
            return "no harness entries"
        return "\n".join(
            f"[{e['id']}] {e['scope']}/{e['kind']} v{e['version']}: {e['title']} — {e['content']}"
            for e in entries
        )

    def children(self) -> str:
        log_dir = self._log_dir()
        rows = []
        for f in sorted(log_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime):
            lines = f.read_text().splitlines()
            start = json.loads(lines[0]) if lines else {}
            end = json.loads(lines[-1]) if len(lines) > 1 else {}
            state = (
                "running"
                if end.get("type") != "rlm_child_end"
                else ("ok" if end.get("ok") else "failed")
            )
            rows.append(
                f"{start.get('id')} parent={start.get('parent')} depth={start.get('depth')} {state} {f}"
            )
        return f"{log_dir}\n" + ("\n".join(rows) if rows else "no children logged")

    def snapshot(self) -> None:
        if self.host is not None and self.host.running and self.host.snapshot_path is not None:
            self.host.snapshot()

    def serve_control(self) -> None:
        path = session.control_socket_path(self.settings.data_dir, self.agy_pid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        self._control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._control.bind(str(path))
        self._control.listen(4)

        def loop():
            while self._control is not None:
                try:
                    conn, _ = self._control.accept()
                except OSError:
                    return
                with conn:
                    try:
                        msg = json.loads(conn.makefile("r").readline() or "{}")
                        if msg.get("type") == "snapshot":
                            self.snapshot()
                        conn.sendall(b'{"ok": true}\n')
                    except Exception as exc:  # report to the hook instead of killing thread
                        conn.sendall((json.dumps({"ok": False, "error": str(exc)}) + "\n").encode())

        threading.Thread(target=loop, daemon=True).start()

    def shutdown(self) -> None:
        try:
            self.snapshot()
        finally:
            if self.host is not None:
                self.host.close()
                self.host = None
            if self._control is not None:
                self._control.close()
                self._control = None
                session.control_socket_path(self.settings.data_dir, self.agy_pid).unlink(
                    missing_ok=True
                )
