"""Install a fake command-line binary that the tools can really execute.

The end-to-end tests do not mock ``subprocess``: they put a stand-in for
``mkvmerge`` or ``ffprobe`` on the PATH and let the tool spawn it, because a
mock cannot get the argument quoting, the exit code or the buffering wrong.

The stand-in is a Python script, and how a Python script becomes an executable
command is the one thing that is genuinely different per platform:

* POSIX honours a ``#!`` line, so the script *is* the program.
* Windows has no shebang. ``CreateProcess`` does understand ``.bat``, and
  ``shutil.which`` honours ``PATHEXT``, so the program is a one-line batch
  file next to the script.

Both return a path a caller can hand to ``subprocess`` or drop on the PATH.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

HELPERS = str(Path(__file__).resolve().parent)


def install_python_shim(directory: Path, name: str, module: str) -> Path:
    """Write an executable ``name`` in ``directory`` that runs ``module.main()``.

    ``module`` is one of this package's fakes (``fake_mkvmerge``,
    ``fake_ffprobe``, ...). The returned path is what the tool under test
    should be pointed at.
    """
    directory.mkdir(parents=True, exist_ok=True)
    body = (
        "import sys\n"
        f"sys.path.insert(0, {HELPERS!r})\n"
        f"import {module}\n"
        f"sys.exit({module}.main())\n"
    )
    if os.name == "nt":
        script = directory / f"{name}.py"
        script.write_text(body, encoding="utf-8")
        launcher = directory / f"{name}.bat"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8",
        )
        return launcher
    program = directory / name
    program.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    program.chmod(program.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return program
