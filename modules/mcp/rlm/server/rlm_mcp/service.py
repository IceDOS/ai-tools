import json
import os
import socket
import threading
from pathlib import Path

from . import child_log, children, refine_run, session
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
    def __init__(
        self,
        settings: Settings,
        agy_pid: int | None = None,
        cwd: str | Path | None = None,
        session_source: session.SessionSource | None = None,
    ):
        self.settings = settings
        self.default_session_source = session_source or session.get_session_source(
            agy_pid, runner=settings.runner
        )
        self.session_source = self.default_session_source
        self.agy_pid = getattr(self.default_session_source, "pid", agy_pid or 0)
        self.cwd = str(cwd) if cwd is not None else self.default_session_source.resolve_workspace()
        self.depth_limit = settings.max_depth
        self._hosts: dict[str, KernelHost] = {}
        self._sources: dict[str, session.SessionSource] = {}
        self._cell_children: dict[str, int] = {}
        self._count_lock = threading.Lock()
        self._cell_usage: dict[str, dict] = {}
        self._control: socket.socket | None = None

    def get_source(self, session_id: str | None = None) -> session.SessionSource:
        if not session_id or session_id == self.default_session_source.session_key:
            return self.default_session_source
        if session_id not in self._sources:
            if str(session_id).isdigit():
                self._sources[session_id] = session.get_session_source(
                    pid=int(session_id), runner=self.settings.runner
                )
            else:
                self._sources[session_id] = session.get_session_source(
                    key=session_id, runner=self.settings.runner
                )
        return self._sources[session_id]

    def get_host(self, session_id: str | None = None) -> KernelHost:
        source = self.get_source(session_id)
        key = source.session_key
        if key not in self._hosts:
            preload = {}
            task_file = os.environ.get("RLM_TASK_FILE")
            if task_file and Path(task_file).is_file():
                preload["task"] = Path(task_file).read_text()

            def on_rlm(prompt: str, depth: int) -> tuple[bool, str]:
                return self.on_rlm(prompt, depth, session_id=session_id)

            def on_refine(instructions: str | None, global_: bool) -> tuple[bool, str | None]:
                return self.on_refine(instructions, global_, session_id=session_id)

            conv = source.conversation_id(self.settings.data_dir, cwd=self.cwd)
            snapshot_path = None
            if self.settings.snapshots and conv:
                snapshot_path = self.settings.data_dir / "snapshots" / f"{conv}.pickle"

            self._hosts[key] = KernelHost(
                self.settings,
                on_rlm,
                on_refine,
                snapshot_path=snapshot_path,
                preload=preload,
                cwd=self.cwd,
            )
        return self._hosts[key]

    def _host(self, session_id: str | None = None) -> KernelHost:
        return self.get_host(session_id)

    @property
    def host(self) -> KernelHost | None:
        key = self.default_session_source.session_key
        return self._hosts.get(key)

    @host.setter
    def host(self, val: KernelHost | None) -> None:
        key = self.default_session_source.session_key
        if val is None:
            self._hosts.pop(key, None)
        else:
            self._hosts[key] = val

    def session(self, session_id: str | None = None) -> dict:
        return self.get_source(session_id).read_session(self.settings.data_dir, cwd=self.cwd)

    def _conversation(self, session_id: str | None = None) -> str | None:
        return self.get_source(session_id).conversation_id(self.settings.data_dir, cwd=self.cwd)

    def _snapshot_path(self, session_id: str | None = None) -> Path | None:
        conv = self._conversation(session_id)
        if not self.settings.snapshots or not conv:
            return None
        return self.settings.data_dir / "snapshots" / f"{conv}.pickle"

    def _log_dir(self, session_id: str | None = None) -> Path:
        inherited = os.environ.get("RLM_CHILD_LOG_DIR")
        if inherited:
            return Path(inherited)
        return self.settings.data_dir / "children" / (self._conversation(session_id) or "unknown")

    def on_rlm(self, prompt: str, depth: int, session_id: str | None = None) -> tuple[bool, str]:
        if depth > self.depth_limit:
            return False, (
                f"maximum recursion depth {self.depth_limit} reached (requested depth {depth}); "
                "solve this subtask yourself"
            )
        source = self.get_source(session_id)
        key = source.session_key
        result = children.run_child(
            self.settings,
            prompt,
            depth,
            self.session(session_id).get("modelName"),
            self._log_dir(session_id),
            os.environ.get("RLM_CHILD_ID"),
            cwd=self.cwd,
        )
        with self._count_lock:
            self._cell_children[key] = self._cell_children.get(key, 0) + 1
            children.add_usage(self._cell_usage.setdefault(key, {}), result.usage)
        return (True, result.output) if result.ok else (False, result.error or "child failed")

    def on_refine(
        self, instructions: str | None, global_: bool, session_id: str | None = None
    ) -> tuple[bool, str | None]:
        return refine_run.queue_refine(
            self.settings.data_dir, self._conversation(session_id), instructions, global_
        )

    def ipython(self, code: str, session_id: str | None = None) -> CellResult:
        host = self.get_host(session_id)
        key = self.get_source(session_id).session_key
        self._cell_children[key] = 0
        self._cell_usage[key] = {}
        host.start()
        host.set("user_prompt", self.session(session_id).get("lastUserInput", ""))
        exec_res = host.execute(code)
        text = format_result(
            exec_res,
            self._cell_children.get(key, 0),
            self._cell_usage.get(key, {}),
        )
        return CellResult(text, exec_res.images)

    def status(self, session_id: str | None = None) -> str:
        sess = self.session(session_id)
        key = self.get_source(session_id).session_key
        running = key in self._hosts and self._hosts[key].running
        return "\n".join([
            f"conversation: {sess.get('conversationId') or 'unknown (SessionStart hook has not run)'}",
            f"model for children: {sess.get('modelName') or 'default'}",
            f"depth: {self.settings.depth} of {self.depth_limit}",
            f"kernel: {'running' if running else 'not started'}",
            f"snapshots: {self._snapshot_path(session_id) or 'off'}",
            f"child logs: {self._log_dir(session_id) if self.settings.child_log else 'off'}",
        ])

    def restart(self, session_id: str | None = None) -> str:
        key = self.get_source(session_id).session_key
        h = self._hosts.pop(key, None)
        if h is not None:
            h.close()
        return "kernel restarted; the namespace is empty"

    def set_depth(self, n: int) -> str:
        self.depth_limit = max(0, int(n))
        return f"rlm() depth limit for this session: {self.depth_limit}"

    def refine_now(self, instructions: str | None, global_: bool, session_id: str | None = None) -> str:
        conv = self._conversation(session_id)
        if not conv:
            return "no conversation id yet; try again after the first turn"
        out = refine_run.run_refinement(
            self.settings,
            conv,
            self.session(session_id).get("transcriptPath"),
            {"instructions": instructions, "global": global_},
            self.session(session_id).get("modelName"),
        )
        return json.dumps(out, indent=2)

    def refine_list(self, session_id: str | None = None) -> str:
        conv = self._conversation(session_id) or ""
        entries = load(global_path(self.settings.data_dir)) + (
            load(local_path(self.settings.data_dir, conv)) if conv else []
        )
        if not entries:
            return "no harness entries"
        return "\n".join(
            f"[{e['id']}] {e['scope']}/{e['kind']} v{e['version']}: {e['title']} — {e['content']}"
            for e in entries
        )

    def children(self, session_id: str | None = None) -> str:
        return child_log.report(self._log_dir(session_id))

    def snapshot(self, session_id: str | None = None) -> None:
        if session_id:
            h = self._hosts.get(session_id)
            if h is not None and h.running and h.snapshot_path is not None:
                h.snapshot()
        else:
            for h in self._hosts.values():
                if h.running and h.snapshot_path is not None:
                    h.snapshot()

    def serve_control(self) -> None:
        path = self.default_session_source.control_socket_path(self.settings.data_dir)
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
                            self.snapshot(msg.get("session_id"))
                        conn.sendall(b'{"ok": true}\n')
                    except Exception as exc:
                        conn.sendall((json.dumps({"ok": False, "error": str(exc)}) + "\n").encode())

        threading.Thread(target=loop, daemon=True).start()

    def shutdown(self) -> None:
        try:
            self.snapshot()
        finally:
            for h in list(self._hosts.values()):
                h.close()
            self._hosts.clear()
            if self._control is not None:
                self._control.close()
                self._control = None
                self.default_session_source.control_socket_path(self.settings.data_dir).unlink(
                    missing_ok=True
                )
