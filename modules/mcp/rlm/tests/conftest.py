import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from rlm_mcp.config import Settings  # noqa: E402

TESTS = Path(__file__).resolve().parent


@pytest.fixture
def kernel_path() -> str:
    path = os.environ.get("RLM_KERNEL")
    if not path or not Path(path).is_file():
        pytest.skip("RLM_KERNEL not set to pi-rlm's rlm_kernel.py")
    return path


@pytest.fixture
def fake_agy(tmp_path) -> Path:
    wrapper = tmp_path / "agy"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {TESTS / 'fake_agy.py'} \"$@\"\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return wrapper


@pytest.fixture
def settings(tmp_path, fake_agy, kernel_path) -> Settings:
    return Settings.from_env({
        "RLM_AGY": str(fake_agy),
        "RLM_KERNEL": kernel_path,
        "RLM_PYTHON": sys.executable,
        "PLUGIN_DATA": str(tmp_path / "data"),
        "RLM_MAX_DEPTH": "2",
        "RLM_CHILD_TIMEOUT": "1m",
        "RLM_CHILD_LOG": "1",
        "RLM_SNAPSHOTS": "1",
    })
