import shutil
import subprocess

import pytest


@pytest.fixture
def xvfb():
    """A private Xvfb display for tests that need a real X server; skipped without Xvfb."""
    if not shutil.which("Xvfb"):
        pytest.skip("Xvfb not installed")
    proc = subprocess.Popen(["Xvfb", "-displayfd", "1", "-screen", "0", "640x480x24", "-nolisten", "tcp"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        yield ":" + proc.stdout.readline().decode().strip()
    finally:
        proc.terminate()
        proc.wait()
