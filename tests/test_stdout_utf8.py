"""0.36.3: the CLI never writes invalid UTF-8 (lone surrogates become U+FFFD)."""
import subprocess
import sys


def test_lone_surrogate_on_stdout_becomes_replacement_char():
    code = ("import json; from voicehook_agent import cli; cli._safe_stdio(); "
            "print(json.loads('\"a\\\\udcffb\"'), flush=True)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, env={"PYTHONIOENCODING": "utf-8:surrogateescape", "PATH": ""}, check=True).stdout
    out.decode("utf-8")                       # strict reader: must not raise
    assert out.strip() in (b"a?b", "a\ufffdb".encode())


def test_without_safe_stdio_the_byte_leaks():
    code = "import json; print(json.loads('\"a\\\\udcffb\"'), flush=True)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, env={"PYTHONIOENCODING": "utf-8:surrogateescape", "PATH": ""}, check=True).stdout
    assert b"\\xff" in out or b"\xff" in out   # the counter-check: this is what 0.36.3 prevents
