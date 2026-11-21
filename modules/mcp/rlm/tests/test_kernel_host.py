import threading
import time

import pytest

from rlm_mcp.kernel_host import KernelHost


def make_host(settings, calls, tmp_path=None, snapshot=None, preload=None):
    def on_rlm(prompt, depth):
        calls.append((prompt, depth))
        if "fail" in prompt:
            return False, "child failed"
        return True, f"answer:{prompt}"

    def on_refine(instructions, global_):
        calls.append(("refine", instructions, global_))
        return True, None

    return KernelHost(settings, on_rlm, on_refine, snapshot_path=snapshot, preload=preload)


def test_state_persists_and_repr(settings):
    host = make_host(settings, [])
    try:
        host.start()
        assert host.execute("x = 41").ok
        r = host.execute("print('hi'); x + 1")
        assert r.ok and r.stdout == "hi\n" and r.result == "42"
    finally:
        host.close()


def test_error_is_reported(settings):
    host = make_host(settings, [])
    try:
        r = host.execute("1/0")
        assert not r.ok and "ZeroDivisionError" in r.stderr
    finally:
        host.close()


def test_rlm_round_trip_and_depth(settings):
    calls = []
    host = make_host(settings, calls)
    try:
        r = host.execute("rlm('sum', context='abc')")
        assert r.result == repr("answer:sum\n\nabc")
        assert calls == [("sum\n\nabc", 1)]
        r = host.execute("rlm('fail')")
        assert not r.ok and "child failed" in r.stderr
    finally:
        host.close()


def test_refine_round_trip(settings):
    calls = []
    host = make_host(settings, calls)
    try:
        r = host.execute("refine('be terse', global_=True)")
        assert r.result == "{'scheduled': True}"
        assert calls == [("refine", "be terse", True)]
    finally:
        host.close()


def test_set_and_preload(settings):
    host = make_host(settings, [], preload={"task": "T" * 10})
    try:
        host.start()
        host.set("user_prompt", "do it")
        assert host.execute("len(task), user_prompt").result == "(10, 'do it')"
    finally:
        host.close()


def test_snapshot_round_trip(settings, tmp_path):
    snap = tmp_path / "s.pickle"
    host = make_host(settings, [], snapshot=snap)
    host.execute("data = [1, 2, 3]\ndef f(): pass")
    host.snapshot()
    host.close()
    again = make_host(settings, [], snapshot=snap)
    try:
        ready = again.start()
        assert "data" in ready.get("restored", [])
        assert again.execute("data").result == "[1, 2, 3]"
        assert not again.execute("f").ok
    finally:
        again.close()


def test_interrupt_and_restart(settings):
    host = make_host(settings, [])
    try:
        host.execute("y = 1")
        t = threading.Timer(0.5, host.interrupt)
        t.start()
        r = host.execute("import time\nwhile True: time.sleep(0.1)")
        assert not r.ok and "KeyboardInterrupt" in r.stderr
        assert host.execute("y").result == "1"
    finally:
        host.close()


def test_kernel_host_cwd(settings, tmp_path):
    host = KernelHost(settings, lambda p, d: (True, ""), lambda i, g: (True, None), cwd=str(tmp_path))
    try:
        host.start()
        r = host.execute("import os; os.getcwd()")
        assert r.ok and r.result == repr(str(tmp_path))
    finally:
        host.close()


def test_show_image_path(settings, tmp_path):
    host = make_host(settings, [])
    img_file = tmp_path / "test.png"
    img_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
    try:
        r = host.execute(f"show_image({str(img_file)!r})")
        assert r.ok
        assert len(r.images) == 1
        assert r.images[0]["mime"] == "image/png"
        assert "<Image: image/png, 28 bytes>" in r.result
    finally:
        host.close()


def test_show_image_bytes(settings):
    host = make_host(settings, [])
    try:
        r = host.execute("show_image(b'\\x89PNG\\r\\n\\x1a\\n' + b'\\x00' * 20)")
        assert r.ok
        assert len(r.images) == 1
        assert r.images[0]["mime"] == "image/png"
    finally:
        host.close()


def test_show_image_pil_like(settings):
    host = make_host(settings, [])
    code = """
class FakeImage:
    def save(self, buf, format="PNG"):
        buf.write(b"\\x89PNG\\r\\n\\x1a\\n" + b"\\x00" * 20)
show_image(FakeImage())
"""
    try:
        r = host.execute(code)
        assert r.ok
        assert len(r.images) == 1
        assert r.images[0]["mime"] == "image/png"
    finally:
        host.close()


def test_show_image_file_not_found(settings):
    host = make_host(settings, [])
    try:
        r = host.execute("show_image('/nonexistent/image.png')")
        assert not r.ok and "FileNotFoundError" in r.stderr
        assert len(r.images) == 0
    finally:
        host.close()


def test_show_image_invalid_type(settings):
    host = make_host(settings, [])
    try:
        r = host.execute("show_image(12345)")
        assert not r.ok and "TypeError" in r.stderr
        assert len(r.images) == 0
    finally:
        host.close()


def test_show_image_unsupported_format(settings):
    host = make_host(settings, [])
    try:
        r = host.execute("show_image(b'not a valid image format')")
        assert not r.ok and "unsupported image format" in r.stderr
        assert len(r.images) == 0
    finally:
        host.close()


def test_show_image_size_limit(settings):
    host = make_host(settings, [])
    try:
        r = host.execute("show_image(b'\\x89PNG\\r\\n\\x1a\\n' + b'\\x00' * (11 * 1024 * 1024))")
        assert not r.ok and "exceeds limit" in r.stderr
        assert len(r.images) == 0
    finally:
        host.close()


def test_show_image_count_limit(settings):
    host = make_host(settings, [])
    code = """
for _ in range(6):
    show_image(b'\\x89PNG\\r\\n\\x1a\\n' + b'\\x00' * 10)
"""
    try:
        r = host.execute(code)
        assert not r.ok and "maximum of 5 images per cell exceeded" in r.stderr
    finally:
        host.close()


def test_show_image_cleared_between_cells(settings):
    host = make_host(settings, [])
    try:
        r1 = host.execute("show_image(b'\\x89PNG\\r\\n\\x1a\\n' + b'\\x00' * 10)")
        assert r1.ok and len(r1.images) == 1
        r2 = host.execute("x = 42")
        assert r2.ok and len(r2.images) == 0
    finally:
        host.close()

