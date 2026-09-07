"""Entry point for the single-file build (``python organize.pyz ...``).

The toolkit ships two ways: as a normal package (``pip install organize``, or
just run the scripts out of a clone) and as one file you can copy to a NAS that
has nothing on it but Python. Both run the *same* modules; the only thing that
differs is how a tool gets started as a child process.

That difference lives here and in ``organizekit.core.toolchain.tool_command``.
Out of a checkout there is a ``bitdepth.py`` to point an interpreter at. Out of
the archive there is no such file, so the archive re-enters itself:

    python organize.pyz run-tool bitdepth.py --self-test

Steps stay separate processes either way, which is not an accident of history:
each tool takes its own locks, writes its own log and report, and returns its
own exit code, and a crash in one must not be able to take the run down with
it. Making the zipapp run them in-process would have quietly changed that.

Running the repository directory (``python /path/to/organize``) uses this file
too, so the dispatch is exercised by the normal test suite as well as by the
built archive.
"""

from __future__ import annotations

import importlib
import sys

from organizekit.core.toolchain import RUN_TOOL_VERB, TOOL_SCRIPTS, tool_module_name

# The tools this entry point will start. Everything the toolkit runs as a step
# plus the two orchestrators and the CLI itself; nothing else, because the
# argument naming the module arrives from a command line.
RUNNABLE = (*TOOL_SCRIPTS, "organize.py", "pipeline.py", "jellyfin_one_shot.py",
            "movie_standardizer.py")


def run_tool(argv: list[str]) -> int:
    """``run-tool <script.py> [args...]`` - start one tool inside this archive."""
    if not argv:
        print(f"usage: {RUN_TOOL_VERB} <tool.py> [args...]", file=sys.stderr)
        print(f"tools: {', '.join(RUNNABLE)}", file=sys.stderr)
        return 2
    script, rest = argv[0], argv[1:]
    if script not in RUNNABLE:
        print(f"unknown tool: {script}", file=sys.stderr)
        print(f"tools: {', '.join(RUNNABLE)}", file=sys.stderr)
        return 2
    module = importlib.import_module(tool_module_name(script))
    # Every tool's main() defaults to sys.argv[1:] and its parser prints the
    # program name, so the child sees exactly the argv it would have seen as a
    # script on disk.
    sys.argv = [script, *rest]
    return int(module.main() or 0)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == RUN_TOOL_VERB:
        return run_tool(args[1:])
    import organize
    sys.argv = ["organize", *args]
    return int(organize.main(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
