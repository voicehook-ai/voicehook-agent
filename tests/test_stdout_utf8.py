"""0.36.3: the CLI never writes invalid UTF-8 (lone surrogates become U+FFFD)."""
import os
import subprocess
import sys
from pathlib import Path

# inherit the environment (PATH, PYTHONPATH, venv) and make src importable without an installed package
_SRC = str(Path(__file__).resolve().parents[1] / "src")
_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8:surrogateescape",
        "PYTHONPATH": os.pathsep.join(x for x in (_SRC, os.environ.get("PYTHONPATH", "")) if x)}


def test_lone_surrogate_on_stdout_becomes_replacement_char():
    code = ("import json; from voicehook_agent import cli; cli._safe_stdio(); "
            "print(json.loads('\"a\\\\udcffb\"'), flush=True)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, env=_ENV, check=True).stdout
    out.decode("utf-8")                       # strict reader: must not raise
    assert out.strip() in (b"a?b", "a\ufffdb".encode())


def test_without_safe_stdio_the_byte_leaks():
    code = "import json; print(json.loads('\"a\\\\udcffb\"'), flush=True)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, env=_ENV, check=True).stdout
    assert b"\\xff" in out or b"\xff" in out   # the counter-check: this is what 0.36.3 prevents
