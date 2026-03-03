import json
import os
import sys
import subprocess
from typing import Any, Literal


try:
    this_file = __file__
except NameError:
    this_file = sys._getframe().f_code.co_filename
this_dir = os.path.dirname(this_file)


if "vim" not in globals():

    class vim:
        @staticmethod
        def eval(cmd: str) -> Any: ...

        @staticmethod
        def command(cmd: str) -> None: ...

        class current:
            buffer = [""]


def htime_move(up_or_down: Literal["down", "up"]) -> None:
    lineno = vim.eval('line(".")')
    cmdline = [
        "python3",
        os.path.join(this_dir, "htime.py"),
        "move",
        "--up-or-down",
        up_or_down,
        "--lineno",
        str(lineno),
    ]
    proc = subprocess.run(
        cmdline,
        check=False,
        input="".join(f"{line}\n" for line in vim.current.buffer),
        text=True,
        capture_output=True,
    )
    if proc.returncode:
        print(proc.stderr)
        return
    lastline = proc.stdout.splitlines()[-1]
    if lastline.startswith("{"):
        cmd = json.loads(lastline)
        if "message" in cmd:
            vim.command(f"echom {json.dumps(cmd['message'])}")
        if "movelines" in cmd:
            mv = cmd["movelines"]
            if up_or_down == "down":
                vim.command(f"norm dd{mv}jP")
            else:
                vim.command(f"norm dd{mv}kP")
        if "command" in cmd:
            vim.command(cmd["command"])


def htime_cleanup() -> None:
    cmdline = [
        "time",
        "python3",
        os.path.join(this_dir, "htime.py"),
        "cleanup",
    ]
    proc = subprocess.run(
            cmdline,
            check=False,
            input="".join(f"{line}\n" for line in vim.current.buffer),
            text=True,
            capture_output=True,
            )
    if proc.returncode:
        print(proc.stderr)
        return
    vim.current.buffer[:] = (proc.stderr + proc.stdout).splitlines()
    print("DONE")
