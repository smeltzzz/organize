"""The documentation has to keep working, and a front page has to stay one.

Splitting a 780-line README into `docs/` buys navigability and costs link rot:
every cross-reference is now a relative path into another file, and a heading
renamed six months from now silently breaks an anchor that nothing checks. So
the links are checked here, in the offline suite, the same way every other
claim in this repo is checked.

The size budget is the other half. The README grew to 780 lines one useful
paragraph at a time - nobody added a bad section, and it still stopped being a
front page. A ceiling turns "should this go on the front page?" into a question
with an answer.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOCS = REPO / "docs"

# The front page is a front page: pitch, quickstart, a map, and pointers.
# Depth belongs in docs/. This is deliberately generous - it is a ceiling, not
# a target.
README_MAX_LINES = 450

# [text](target) - but not images, and not reference-style definitions.
LINK_RE = re.compile(r"(?<!\!)\[(?P<text>[^\]]*)\]\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)")
HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*$", re.MULTILINE)
FENCE_RE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)


def markdown_files() -> list[Path]:
    files = sorted(REPO.glob("*.md")) + sorted(DOCS.glob("*.md"))
    files.append(REPO / "benchmarks" / "README.md")
    return [path for path in files if path.is_file()]


def strip_code(text: str) -> str:
    """Fenced blocks hold shell snippets, not links or headings."""
    return FENCE_RE.sub("", text)


def anchor_for(heading: str) -> str:
    """GitHub's slug: lowercase, drop punctuation, every space becomes a hyphen.

    The spaces left behind by a removed character are *kept*, which is why an
    emoji heading like `## 🚀 Quickstart` is reached as `#-quickstart` and not
    as `#quickstart`. Getting that wrong would make this test pass on links
    that GitHub cannot follow.
    """
    slug = heading.strip().lower()
    slug = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", slug)  # links keep their text
    # `_` is a word character and survives GitHub's slug; only the emphasis
    # and code markers are dropped.
    slug = re.sub(r"[`*~]", "", slug)
    slug = re.sub(r"[^\w\s-]", "", slug, flags=re.UNICODE)
    return re.sub(r"\s", "-", slug)


def anchors_of(path: Path) -> set[str]:
    body = strip_code(path.read_text(encoding="utf-8"))
    return {anchor_for(match.group("text")) for match in HEADING_RE.finditer(body)}


class DocumentationLinkTests(unittest.TestCase):
    def test_every_relative_link_points_at_something_that_exists(self) -> None:
        missing: list[str] = []
        for path in markdown_files():
            body = strip_code(path.read_text(encoding="utf-8"))
            for match in LINK_RE.finditer(body):
                target = match.group("target")
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                file_part = target.split("#", 1)[0]
                if not file_part:
                    continue
                resolved = (path.parent / file_part).resolve()
                if not resolved.exists():
                    missing.append(f"{path.relative_to(REPO)} -> {target}")
        self.assertEqual(missing, [], "broken relative link(s)")

    def test_every_anchor_matches_a_real_heading(self) -> None:
        broken: list[str] = []
        for path in markdown_files():
            body = strip_code(path.read_text(encoding="utf-8"))
            for match in LINK_RE.finditer(body):
                target = match.group("target")
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                if "#" not in target:
                    continue
                file_part, _, anchor = target.partition("#")
                if not anchor:
                    continue
                other = (path.parent / file_part).resolve() if file_part else path
                if other.suffix != ".md" or not other.is_file():
                    continue
                if anchor not in anchors_of(other):
                    broken.append(f"{path.relative_to(REPO)} -> {target}")
        self.assertEqual(broken, [], "link(s) to a heading that does not exist")

    def test_no_document_links_to_itself_by_filename(self) -> None:
        # A leftover `docs/tools.md` link inside docs/tools.md resolves on
        # GitHub only by accident of the current folder.
        for path in DOCS.glob("*.md"):
            body = strip_code(path.read_text(encoding="utf-8"))
            for match in LINK_RE.finditer(body):
                target = match.group("target").split("#", 1)[0]
                if target.startswith(("http", "mailto:")) or not target:
                    continue
                self.assertNotEqual(
                    (path.parent / target).resolve(), path.resolve(),
                    f"{path.name} links to itself by filename; use a bare #anchor",
                )


def live_documents() -> list[Path]:
    """The documents that describe the tree as it is, not as it once was.

    `CHANGELOG.md`, `OVERHAUL.md` and `REVIEW.md` quote the flags of tools
    that no longer exist (`--ocr-args`, `--sync-everything`) on purpose: they
    are the record of past decisions. The reference docs are not allowed that
    licence — a flag in one of them is a promise.
    """
    return [
        REPO / "README.md",
        REPO / "CONTRIBUTING.md",
        REPO / "SECURITY.md",
        *sorted(DOCS.glob("*.md")),
        REPO / "benchmarks" / "README.md",
    ]


class DocumentedFlagTests(unittest.TestCase):
    """A flag named in the reference docs has to exist in a tool.

    The failure this catches is quiet: `docs/tools.md` documents
    `--download-limit`, someone renames it, and the only symptom is a reader
    whose command line exits 2 — in a repository where `--help` is the tool
    that names every flag correctly. Renaming a flag now fails a test until
    the docs move with it.
    """

    # Flags of the programs the docs *invoke*, not of this repository: git,
    # pip/pipx and tar appear in the release and development walkthroughs.
    # `--help` is argparse's own.
    EXTERNAL_FLAGS = {
        "--help", "--delete", "--no-ff", "--reverse", "--upgrade",
        "--strip-components", "--no-binary", "--no-deps", "--target", "--prefix",
        "--user", "--quiet",
    }

    #: Every file that is a tool in its own right. `__main__.py` is the
    #: zipapp's entry point; it has no flags of its own and passes everything
    #: to the front door.
    TOOLS = ("organize.py", "pipeline.py", "subtitle_extractor.py", "mkv_track_cleaner.py",
             "bitdepth.py", "library_auditor.py", "movie_standardizer.py")

    def tool_files(self) -> list[Path]:
        return [REPO / name for name in self.TOOLS]

    def tool_flags(self) -> set[str]:
        flags: set[str] = set()
        for tool in self.tool_files():
            body = tool.read_text(encoding="utf-8")
            flags |= {match.group(1) for match in
                      re.finditer(r'add_argument\(\s*"(?P<flag>--[a-z0-9-]+)"', body)}
        return flags

    def test_every_flag_the_reference_docs_name_exists(self) -> None:
        known = self.tool_flags()
        self.assertIn("--dry-run", known, "the flag scan found no argparse in the tools")
        unknown: list[str] = []
        for path in live_documents():
            if not path.is_file():
                continue
            body = path.read_text(encoding="utf-8")
            for match in re.finditer(r"(?<![\w-])(--[a-z][a-z0-9-]+)", body):
                flag = match.group(1)
                if flag in known or flag in self.EXTERNAL_FLAGS:
                    continue
                line = body[:match.start()].count("\n") + 1
                unknown.append(f"{path.relative_to(REPO)}:{line} mentions {flag}")
        self.assertEqual(unknown, [], "documented flag(s) that no tool defines")

    def test_the_shared_flag_table_names_real_tools(self) -> None:
        """The table in `docs/tools.md` is only useful if its columns are true.

        Each row claims a flag applies to a named tool. Both halves are checked
        against the tree, because a row that survives a rename is how a
        reference table turns into folklore.
        """
        body = (DOCS / "tools.md").read_text(encoding="utf-8")
        section = body.split("### Flags the tools share", 1)
        self.assertEqual(len(section), 2, "the shared-flag section disappeared")
        self.assertIn("| Flag | Tools | Meaning |", section[1], "the table lost its header")
        table = section[1].split("| Flag | Tools | Meaning |", 1)[1].split("\n\n", 1)[0]

        aliases: dict[str, list[Path]] = {
            "pipeline": [REPO / "pipeline.py"],
            "extractor": [REPO / "subtitle_extractor.py"],
            "cleaner": [REPO / "mkv_track_cleaner.py"],
            "bit-depth": [REPO / "bitdepth.py"],
            "auditor": [REPO / "library_auditor.py"],
            "standardizer": [REPO / "movie_standardizer.py"],
            "`organize status`": [REPO / "organize.py"],
            "`organize.py`": [REPO / "organize.py"],
            "every tool": self.tool_files(),
        }
        rows = [line for line in table.splitlines() if line.startswith("| `--")]
        self.assertTrue(rows, "the shared-flag table has no rows")
        for row in rows:
            cells = [cell.strip() for cell in row.strip("|").split("|")]
            flags = re.findall(r"`(--[a-z0-9-]+)[^`]*`", cells[0])
            self.assertTrue(flags, f"no flag in the row: {row}")
            self.assertGreaterEqual(len(cells), 3, f"the row is not a table row: {row}")
            for flag in flags:
                self.assertIn(flag, self.tool_flags(), f"{flag} is documented but not defined")
                for named in (part.strip() for part in cells[1].split(",") if part.strip()):
                    # The row may name a tool the docs call "`organize status`"
                    # or a bare word; anything else is a typo in the table.
                    self.assertIn(named, aliases, f"the table names {named!r}, which is not a tool")
                    for tool in aliases[named]:
                        source = tool.read_text(encoding="utf-8")
                        self.assertIn(f'"{flag}"', source,
                                      f"the table says {tool.name} accepts {flag}, and it does not")


class DocumentedTestCountTests(unittest.TestCase):
    """The suite's own size is quoted in four places, and it has to be right.

    This number is the one claim in the repo a reader can check in thirty
    seconds, and the reason to keep it honest is the opposite of vanity: it
    drifts every time a test is added, so a stale one means the person who
    added them did not read the docs they were updating. Counting the tests
    here is the same discovery the CI job runs, so the badge cannot lie
    without this going red.
    """

    PLACES = {
        "README.md": (r"badge/tests-(\d+)%20passing", r"offline unit tests \(([\d,]+)\)"),
        "docs/development.md": (r"# ([\d,]+) unit tests",),
        "docs/merge-and-release.md": (r"whole suite \(([\d,]+)",),
    }

    def real_count(self) -> int:
        sys.path.insert(0, str(REPO))
        try:
            suite = unittest.TestLoader().discover(str(REPO / "tests"), pattern="test_*.py")
        finally:
            sys.path.remove(str(REPO))
        return suite.countTestCases()

    def test_the_counts_in_the_docs_are_the_counts_the_suite_runs(self) -> None:
        actual = self.real_count()
        self.assertGreater(actual, 0, "discovery found no tests at all")
        for name, patterns in self.PLACES.items():
            body = (REPO / name).read_text(encoding="utf-8")
            for pattern in patterns:
                found = re.findall(pattern, body)
                self.assertTrue(found, f"{name} no longer states the test count ({pattern})")
                for quoted in found:
                    self.assertEqual(
                        int(quoted.replace(",", "")), actual,
                        f"{name} says {quoted} tests; the suite discovers {actual}. "
                        f"Update every place that quotes it (see DocumentedTestCountTests.PLACES).",
                    )


class FrontPageTests(unittest.TestCase):
    def test_the_readme_stays_a_front_page(self) -> None:
        lines = (REPO / "README.md").read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(
            len(lines), README_MAX_LINES,
            f"README.md is {len(lines)} lines. Depth belongs in docs/ - "
            "add a section there and link to it from the front page.",
        )

    def test_the_front_page_points_at_every_document(self) -> None:
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        for doc in sorted(DOCS.glob("*.md")):
            if doc.name == "README.md":
                continue  # the docs index is reachable through its entries
            self.assertIn(
                f"docs/{doc.name}", readme,
                f"docs/{doc.name} exists but nothing on the front page links to it",
            )

    def test_every_document_offers_a_way_back(self) -> None:
        for doc in sorted(DOCS.glob("*.md")):
            body = doc.read_text(encoding="utf-8")
            self.assertIn("README.md", body, f"{doc.name} is a dead end")

    def test_the_navigation_bar_matches_the_pages_sections(self) -> None:
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        nav = readme.split("</div>", 1)[0]
        anchors = anchors_of(REPO / "README.md")
        targets = [
            match.group("target").lstrip("#")
            for match in LINK_RE.finditer(nav)
            if match.group("target").startswith("#")
        ]
        self.assertTrue(targets, "the front page lost its navigation bar")
        for target in targets:
            self.assertIn(target, anchors, f"nav links to #{target}, which no heading provides")


@unittest.skipUnless((REPO / ".git").exists() and shutil.which("git"),
                     "needs a git checkout (an sdist ships the patches but not .git)")
class HeldBackWorkflowPatchTests(unittest.TestCase):
    """CI changes this branch cannot commit are held as patches, and patches rot.

    The bot that pushes this repository has no `workflows` permission, so
    `.github/workflows/*` changes live in `docs/*.patch` until someone applies
    them with a token that does. A patch nobody can apply any more is worse
    than no patch: it looks like the work is done.

    There are three states, and all three are fine. A patch that applies
    cleanly is waiting. A patch already present in the tree is *done* - the
    maintainer applied it, and the suite must not go red the moment they do.
    No patches at all means the whole arrangement is over, and this class
    skips itself out of existence.
    """

    def patches(self) -> list[Path]:
        found = sorted(DOCS.glob("*.patch"))
        if not found:
            self.skipTest("no patches are held any more; nothing to keep honest")
        return found

    def _git_apply(self, patch: Path, *flags: str) -> int:
        return subprocess.run(["git", "apply", "--check", *flags, str(patch)],
                              cwd=str(REPO), capture_output=True, encoding="utf-8").returncode

    def test_every_held_patch_either_applies_or_is_already_applied(self) -> None:
        for patch in self.patches():
            with self.subTest(patch=patch.name):
                if self._git_apply(patch) == 0:
                    continue  # still waiting for someone with the permission
                self.assertEqual(
                    self._git_apply(patch, "--reverse"), 0,
                    f"{patch.name} neither applies nor is already applied; it has rotted",
                )

    def test_every_held_patch_says_why_it_is_held(self) -> None:
        for patch in self.patches():
            with self.subTest(patch=patch.name):
                head = patch.read_text(encoding="utf-8").split("diff --git", 1)[0]
                self.assertIn("workflows", head)
                self.assertIn("git apply", head)

    def test_a_held_patch_names_the_distribution_this_repo_actually_builds(self) -> None:
        """A patch is not compiled, so a rename cannot break it loudly.

        The packaging job asks `importlib.metadata` for the installed
        distribution by name. When the project was renamed to `organizekit`
        the held patch kept asking for `organize`, and nothing said so until
        the job would have run - after the merge, on someone else's morning.
        """
        name = ""
        for line in (REPO / "pyproject.toml").read_text(encoding="utf-8").splitlines():
            if line.startswith("name ="):
                name = line.split("=", 1)[1].strip().strip('"')
                break
        self.assertTrue(name, "pyproject declares no distribution name")
        for patch in self.patches():
            body = patch.read_text(encoding="utf-8")
            added = [line for line in body.splitlines()
                     if line.startswith("+") and "m.version(" in line]
            for line in added:
                with self.subTest(patch=patch.name, line=line.strip()):
                    self.assertIn(f"m.version('{name}')", line)

    def test_the_docs_index_lists_them(self) -> None:
        index = (DOCS / "README.md").read_text(encoding="utf-8")
        for patch in self.patches():
            with self.subTest(patch=patch.name):
                self.assertIn(patch.name, index)


if __name__ == "__main__":
    unittest.main()
