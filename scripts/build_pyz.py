#!/usr/bin/env python3
"""Build ``dist/organize.pyz``: the whole toolkit as one file.

Why this exists: the toolkit's home is a NAS or a home server, and the honest
install story there is "copy one file next to your media and run it". A wheel
needs pip; a clone needs git; a zipapp needs neither, and because the toolkit
has *zero* runtime third-party dependencies the archive is self-contained by
construction rather than by vendoring.

What it does, and does not do:

* The module list comes from ``pyproject.toml`` - the same list the wheel
  ships - so the two builds cannot drift into shipping different code.
* Nothing is compiled or bundled: the archive holds the source, and the
  interpreter that runs it is whatever the machine already has (3.11+).
* Tests, benchmarks, docs and caches are excluded. ``--self-test`` still works
  in the archive because every tool's field smoke test is inside the tool.
* The build is reproducible: file order is sorted and every timestamp is
  pinned, so the same source always produces a byte-identical archive.

Usage:  python3 scripts/build_pyz.py [--output dist/organize.pyz]
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tomllib
import zipapp
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO / "dist" / "organize.pyz"

# A fixed timestamp for every member, so two builds of the same source are the
# same bytes. 1980-01-01 is the earliest a zip entry can express.
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

EXCLUDED_DIRS = {"__pycache__", ".git", ".mypy_cache", ".ruff_cache", ".pytest_cache"}


def shipped_modules(pyproject: Path) -> tuple[list[str], list[str]]:
    """(top-level modules, packages) exactly as declared for the wheel."""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    setuptools = data["tool"]["setuptools"]
    return list(setuptools["py-modules"]), list(setuptools["packages"])


def stage(root: Path, staging: Path) -> list[Path]:
    """Copy the shipped source into ``staging``; return what was copied."""
    modules, packages = shipped_modules(root / "pyproject.toml")
    copied: list[Path] = []

    for module in modules:
        source = root / f"{module}.py"
        if not source.is_file():
            raise SystemExit(f"pyproject declares {module}.py but it does not exist")
        shutil.copy2(source, staging / source.name)
        copied.append(source)

    for package in sorted({name.split(".")[0] for name in packages}):
        source_dir = root / package
        if not source_dir.is_dir():
            raise SystemExit(f"pyproject declares package {package} but it does not exist")
        shutil.copytree(
            source_dir, staging / package,
            ignore=shutil.ignore_patterns(*EXCLUDED_DIRS, "*.pyc"),
        )
        copied.extend(sorted(source_dir.rglob("*.py")))

    entry = root / "__main__.py"
    if not entry.is_file():
        raise SystemExit("__main__.py (the archive's entry point) is missing")
    shutil.copy2(entry, staging / "__main__.py")
    copied.append(entry)
    return copied


def normalise(archive: Path) -> None:
    """Rewrite the archive with sorted entries and pinned timestamps."""
    with zipfile.ZipFile(archive) as original:
        entries = sorted(original.infolist(), key=lambda info: info.filename)
        payload = [(info.filename, original.read(info.filename)) for info in entries]

    shebang = archive.read_bytes().split(b"\n", 1)[0] + b"\n"
    has_shebang = shebang.startswith(b"#!")

    rebuilt = archive.with_suffix(".rebuilt")
    with rebuilt.open("wb") as handle:
        if has_shebang:
            handle.write(shebang)
        with zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as target:
            for name, data in payload:
                info = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                target.writestr(info, data)
    rebuilt.replace(archive)


def build(output: Path = DEFAULT_OUTPUT, *, root: Path = REPO) -> Path:
    """Write the archive and return its path."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory() as tmp:
        staging = Path(tmp) / "app"
        staging.mkdir()
        stage(root, staging)
        zipapp.create_archive(
            staging, target=output,
            interpreter="/usr/bin/env python3",
            compressed=True,
        )
    normalise(output)
    output.chmod(0o755)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help=f"where to write the archive (default: {DEFAULT_OUTPUT})")
    args = parser.parse_args(argv)

    archive = build(args.output)
    size_kib = archive.stat().st_size / 1024
    with zipfile.ZipFile(archive) as bundle:
        members = len(bundle.namelist())
    print(f"{archive}  ({size_kib:,.0f} KiB, {members} modules)")
    print(f"  run it with:  python3 {archive.name} --help")
    return 0


if __name__ == "__main__":
    sys.exit(main())
