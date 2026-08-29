"""Helpers that read the rendered plain-text reports back apart.

The reports are the user-facing product of every tool in this repository, so
the tests assert on what a reader actually sees: the scorecard counts in their
right-aligned column and the contents of each banner-delimited section.
"""

from __future__ import annotations

HEAVY = "\u2550"
LIGHT = "\u2500"


def scorecard(text: str) -> dict[str, int]:
    """Parse the scorecard block into ``{label: count}``.

    The scorecard is the run of lines between the first two light rules: a
    right-aligned count, three spaces, then the label and its hint.
    """
    lines = text.splitlines()
    rules = [i for i, line in enumerate(lines) if line.strip() and set(line.strip()) == {LIGHT}]
    if len(rules) < 2:
        raise AssertionError(f"no scorecard in report:\n{text}")
    counts: dict[str, int] = {}
    for line in lines[rules[0] + 1:rules[1]]:
        number, _, rest = line.strip().partition("   ")
        counts[rest.split("   ")[0].strip()] = int(number)
    return counts


def section(text: str, title: str) -> str:
    """Everything under one banner, up to the next banner of the same level.

    A major banner (``══ TITLE ══``) owns the subsections beneath it, so
    asking for a major section returns its groups too; a minor banner
    (``── TITLE ──``) ends at the next banner of either kind.
    """
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if title in line]
    if not starts:
        raise AssertionError(f"{title!r} not in report:\n{text}")
    start = starts[0]
    major = lines[start].lstrip().startswith(HEAVY * 2)
    end = len(lines)
    for i in range(start + 1, len(lines)):
        stripped = lines[i].lstrip()
        if stripped.startswith(HEAVY * 2 + " ") or (
            not major and stripped.startswith(LIGHT * 2 + " ")
        ):
            end = i
            break
    return "\n".join(lines[start:end])
