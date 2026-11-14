from rlm_mcp.kernel_host import ExecResult
from rlm_mcp.output import format_result, truncate_head, truncate_tail


def test_plain_output():
    assert format_result(ExecResult(True, "hi\n", "", "42"), 0, {}) == "hi\nOut: 42"


def test_empty():
    assert format_result(ExecResult(True, "", "", None), 0, {}) == "(no output)"


def test_error_label():
    out = format_result(ExecResult(False, "", "Traceback\nZeroDivisionError\n", None), 0, {})
    assert out.startswith("Error:\n") and "ZeroDivisionError" in out


def test_stdout_truncation_notice():
    out = format_result(ExecResult(True, "x\n" * 5000, "", None), 0, {})
    assert "[stdout truncated: 2000 of 5000 lines; the data is still in the kernel" in out


def test_children_line():
    out = format_result(ExecResult(True, "", "", "1"), 2, {"input_tokens": 30})
    assert out.endswith('[rlm children: 2, usage: {"input_tokens": 30}]')


def test_truncate_helpers():
    content, truncated, kept, total = truncate_head("a\nb\nc\n", 100, 2)
    assert content == "a\nb\n" and truncated and kept == 2 and total == 3
    tail, cut = truncate_tail("0123456789", 4)
    assert tail == "6789" and cut
