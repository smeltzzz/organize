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


if __name__ == "__main__":
    unittest.main()
