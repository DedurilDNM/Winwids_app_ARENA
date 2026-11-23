"""Xvfb start after a service restart (real Xvfb): leftovers of a killed Xvfb
(/tmp/.XN-lock, socket, our display lock) must not block a new display, and an
Xvfb that exits at once makes the next display number be tried."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not shutil.which("Xvfb"), reason="no Xvfb")


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _manager(monkeypatch, base: int):
    from app.services import vnc_manager as vm

    monkeypatch.setattr(vm.get_settings(), "XVFB_BASE_DISPLAY", base)
    return vm.VncManager()


def test_stale_lock_of_a_killed_xvfb_does_not_block_the_display(monkeypatch):
    m = _manager(monkeypatch, 160)
    display = 160
    Path(f"/tmp/.X{display}-lock").write_text(f"{_dead_pid():>10}\n")
    Path("/tmp/.X11-unix").mkdir(exist_ok=True)

    async def go():
        d, proc = await m._spawn_xvfb()
        alive = m.x_display_alive(d)
        m._terminate(proc)
        return d, alive

    d, alive = asyncio.run(go())
    assert d == display and alive  # the stale lock was removed and :160 reused


def test_lock_of_a_live_process_is_respected(monkeypatch):
    m = _manager(monkeypatch, 170)
    Path(f"/tmp/.X170-lock").write_text(f"{os.getpid():>10}\n")  # a live pid (this test)
    try:
        assert m._free_display() == 171
    finally:
        Path("/tmp/.X170-lock").unlink(missing_ok=True)


def test_an_xvfb_that_exits_at_once_makes_the_next_display_be_tried(monkeypatch):
    m = _manager(monkeypatch, 180)
    # a real X server already runs on :180, but its files are hidden from the check
    # below -> our Xvfb on :180 exits at once («already active»)
    other = subprocess.Popen(["Xvfb", ":180", "-nolisten", "tcp"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            if m.x_display_alive(180):
                break
            time.sleep(0.1)
        order = iter([180, 181, 182])
        monkeypatch.setattr(m, "_free_display", lambda: next(order))

        async def go():
            d, proc = await m._spawn_xvfb()
            m._terminate(proc)
            return d

        assert asyncio.run(go()) == 181
        assert 180 in m._bad_displays
    finally:
        other.terminate()
        other.wait()
