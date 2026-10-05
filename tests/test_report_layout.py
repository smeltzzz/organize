"""The report renderer's layout rules, at the widths that actually break them.

Every tool in this toolkit ends by writing one of these reports, and the report
is the artefact an operator reads to decide what to do next - so its layout
rules are user-facing behaviour, not decoration. The rules under test are the
ones that only bite at awkward sizes:

* a header row with no value still gets its own line, because a label with an
  empty answer ("Library", "Nothing recorded") is information;
* a right-hand column is pushed to the margin when there is room and folded
  into the left-hand text when there is not - either way it is never dropped;
* an empty scorecard renders nothing at all rather than two rules around a gap;
* a banner too long for the width is clipped without an ellipsis, so the box
  still lines up;
* a table trims its widest columns but never below their headers, and when
  even that does not fit it overflows instead of losing a column.

The narrow cases are reachable in production: the report width follows the
terminal, and ``REPORT_MIN_WIDTH`` is a floor, not a guarantee that a long
movie path fits inside it.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

from organizekit.core import Report
from organizekit.core.text import _RULE_HEAVY, _RULE_LIGHT, REPORT_MIN_WIDTH

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def lines_of(report: Report) -> list[str]:
    return report.render().splitlines()


class HeaderBoxTests(unittest.TestCase):
    def test_a_meta_row_with_no_value_still_prints_its_label(self) -> None:
        """"Nothing to report" is an answer, and it belongs on its own line.

        The alternative - skipping the row - makes an empty value
        indistinguishable from a label the tool forgot to emit, which is the
        difference between "no movies were skipped" and "the tool did not look".
        """
        rendered = lines_of(Report("Audit", "a subtitle", width=80)
                            .meta("Library", "/media/movies")
                            .meta("Skipped", "")
                            .meta("Notes", "none"))
        self.assertTrue(any("Library" in line and "/media/movies" in line for line in rendered))
        skipped_rows = [line for line in rendered if "Skipped" in line]
        self.assertEqual(len(skipped_rows), 1)
        self.assertEqual(skipped_rows[0].strip("║ ").rstrip(), "Skipped")

    def test_a_long_meta_value_wraps_at_path_separators_inside_the_box(self) -> None:
        """Every header row stays inside the box, whatever the path length."""
        long_path = "/media/movies/" + "/".join(f"a-quite-long-directory-{index}"
                                                 for index in range(6)) + "/movie.mkv"
        report = Report("Audit", width=REPORT_MIN_WIDTH).meta("Library", long_path)
        rendered = lines_of(report)
        self.assertTrue(all(line.startswith(("╔", "║", "╟", "╚")) for line in rendered), rendered)
        self.assertTrue(all(len(line) <= REPORT_MIN_WIDTH for line in rendered))
        self.assertEqual("".join(rendered).count("movie.mkv"), 1,
                         "the file name survives the wrap")

    def test_a_none_value_is_printed_as_empty_rather_than_as_the_word_none(self) -> None:
        rendered = lines_of(Report("Audit", width=80).meta("Report", None))
        rows = [line for line in rendered if "Report" in line]
        self.assertEqual(rows[0].strip("║ ").rstrip(), "Report")


class TitleLineTests(unittest.TestCase):
    def test_a_title_with_nothing_on_the_right_is_clipped_to_the_width(self) -> None:
        report = Report("T", width=REPORT_MIN_WIDTH).title_line("x" * 200)
        rendered = lines_of(report)
        self.assertEqual(len(rendered[-1]), REPORT_MIN_WIDTH)

    def test_a_right_hand_column_sits_at_the_right_margin(self) -> None:
        """The counts a reader scans for line up in one column."""
        report = Report("T", width=96).title_line("Movies cleaned", right="17 of 42")
        line = lines_of(report)[-1]
        self.assertTrue(line.rstrip().endswith("17 of 42"), line)
        self.assertIn("Movies cleaned", line)
        self.assertLessEqual(len(line), 96)

    def test_a_right_hand_column_with_no_room_folds_into_the_title(self) -> None:
        """Folded, not dropped: a tally that does not fit is still printed.

        With a narrow terminal the gap goes negative; clipping the combined
        string keeps the row inside the width while still carrying both halves
        for as long as they fit.
        """
        report = Report("T", width=REPORT_MIN_WIDTH).title_line(
            "a title that is almost as wide as the report itself", right="17 of 42")
        line = lines_of(report)[-1]
        self.assertLessEqual(len(line), REPORT_MIN_WIDTH)
        self.assertIn("a title that is almost as wide", line)

    def test_a_very_long_pair_is_still_clipped_to_the_width(self) -> None:
        report = Report("T", width=REPORT_MIN_WIDTH).title_line("y" * 120, right="z" * 120)
        self.assertEqual(len(lines_of(report)[-1]), REPORT_MIN_WIDTH)


class ScorecardTests(unittest.TestCase):
    def test_an_empty_scorecard_renders_nothing_at_all(self) -> None:
        """Two rules around a gap read as a table that lost its rows.

        A run that classified nothing should print no scorecard rather than an
        empty frame that looks like a rendering bug.
        """
        bare = Report("T", width=80)
        with_rows = Report("T", width=80).scorecard([(1, "Cleaned", "nothing left to do")])
        self.assertEqual(lines_of(bare.scorecard([])), lines_of(Report("T", width=80)))
        self.assertIn(_RULE_LIGHT, with_rows.render())
        self.assertNotIn(_RULE_LIGHT, bare.render())

    def test_counts_are_right_aligned_in_one_column(self) -> None:
        report = Report("T", width=96).scorecard([
            (7, "Cleaned", "tracks removed"),
            (1234, "Already clean", "nothing to do"),
        ])
        body = [line for line in lines_of(report) if line.strip() and _RULE_LIGHT not in line]
        rows = [line for line in body if "Cleaned" in line or "Already clean" in line]
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0].strip().startswith("7 "), rows[0])
        self.assertTrue(rows[1].strip().startswith("1234 "), rows[1])
        self.assertEqual(rows[0].index("Cleaned"), rows[1].index("Already clean"),
                         "the labels start in the same column")


class BannerTests(unittest.TestCase):
    def test_a_section_banner_carries_its_tally(self) -> None:
        report = Report("T", width=96).section("Skipped", count=3, total=17)
        banner = [line for line in lines_of(report) if "Skipped" in line][0]
        self.assertIn("3 of 17", banner)
        self.assertIn(_RULE_HEAVY, banner)

    def test_a_tally_larger_than_the_total_is_not_printed_as_a_fraction(self) -> None:
        """"5 of 3" is nonsense, so the total is only shown when it is larger."""
        banner = [line for line in lines_of(Report("T", width=96).section("X", count=5, total=3))
                  if "X" in line][0]
        self.assertNotIn("5 of 3", banner)
        self.assertIn(" 5 ", banner)

    def test_a_banner_too_long_for_the_width_is_clipped_without_an_ellipsis(self) -> None:
        """The box has to line up, so a wide banner loses characters, not columns.

        An ellipsis would make the banner three characters longer than the
        width it was clipped to, and every report line is padded to that width.
        """
        title = "a section title that is far too long to fit inside the minimum report width"
        report = Report("T", width=REPORT_MIN_WIDTH).section(title, count=99999, total=100000)
        line = [line for line in lines_of(report) if "section title" in line][0]
        self.assertEqual(len(line), REPORT_MIN_WIDTH)
        self.assertNotIn("...", line)

    def test_a_subsection_banner_is_clipped_the_same_way(self) -> None:
        title = "a subsection title that is far too long to fit inside the minimum width either"
        report = Report("T", width=REPORT_MIN_WIDTH).subsection(title, count=99999)
        line = [line for line in lines_of(report) if "subsection title" in line][0]
        self.assertEqual(len(line), REPORT_MIN_WIDTH)
        self.assertNotIn("...", line)
        self.assertIn(_RULE_LIGHT, line)

    def test_a_subsection_separates_itself_from_the_entry_above_it(self) -> None:
        report = Report("T", width=96).entry("an entry").subsection("Group")
        rendered = lines_of(report)
        index = next(i for i, line in enumerate(rendered) if "Group" in line)
        self.assertEqual(rendered[index - 1].strip(), "", "a blank line opens the group")


class EntryTests(unittest.TestCase):
    def test_a_marker_is_padded_to_the_numbering_column(self) -> None:
        """Entries are either numbered or tagged, and both align the same way."""
        numbered = lines_of(Report("T", width=96).entry("Alpha", ordinal=12))[-1]
        marked = lines_of(Report("T", width=96).entry("Alpha", marker="!!"))[-1]
        self.assertIn("  12  Alpha", numbered)
        self.assertIn("!!    Alpha", marked)
        self.assertEqual(numbered.index("Alpha"), marked.index("Alpha"))

    def test_a_long_entry_wraps_instead_of_being_ellipsised(self) -> None:
        """The tail of a long path is the part a reader came for.

        Clipping it away hides exactly the information the report exists to
        convey, so entry text wraps and keeps every character.
        """
        long_path = "/media/movies/" + "/".join(f"directory-number-{index}"
                                                 for index in range(8)) + "/movie.mkv"
        rendered = lines_of(Report("T", width=REPORT_MIN_WIDTH).entry(long_path))
        body = [line for line in rendered if "directory-number" in line or "movie.mkv" in line]
        self.assertGreater(len(body), 1, "the path wrapped onto more than one line")
        self.assertEqual("".join(line.strip() for line in body).replace(" ", "").count("/"),
                         long_path.count("/"), "no separator - and so no component - was lost")
        self.assertTrue(any(line.rstrip().endswith("movie.mkv") for line in body))

    def test_a_detail_rides_on_the_entry_line_only_when_it_fits(self) -> None:
        report = Report("T", width=96).entry("Alpha (2001)", detail="CANONICAL", detail_column=40)
        self.assertIn("CANONICAL", lines_of(report)[-1])
        narrow = Report("T", width=REPORT_MIN_WIDTH).entry(
            "a title that is much too long to leave room for the detail column at all",
            detail="CANONICAL", detail_column=40)
        rendered = lines_of(narrow)
        self.assertTrue(any(line.strip() == "CANONICAL" or line.rstrip().endswith("CANONICAL")
                            for line in rendered), rendered)

    def test_fields_line_up_under_the_entry_text(self) -> None:
        rendered = lines_of(Report("T", width=96).entry(
            "Alpha (2001)", fields=[("Reason", "no English audio track"),
                                     ("Next", "run subtitle_extractor.py")]))
        rows = [line for line in rendered if "Reason" in line or "Next" in line]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].index("no English"), rows[1].index("run subtitle"),
                         "the values start in the same column")


class TableTests(unittest.TestCase):
    def test_a_table_with_no_columns_adds_nothing(self) -> None:
        """An empty header list is a caller bug, not a reason to print a rule."""
        self.assertEqual(lines_of(Report("T", width=96).table([], [])),
                         lines_of(Report("T", width=96)))

    def test_columns_are_aligned_and_ruled_under_the_header(self) -> None:
        rendered = lines_of(Report("T", width=96).table(
            ["Movie", "Saved", "Status"],
            [["Alpha (2001)", "1.2 GiB", "cleaned"],
             ["Bravo (2002)", None, "already clean"]],
            aligns="<<>"))
        rows = [line for line in rendered if "Alpha" in line or "Bravo" in line]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].index("Alpha"), rows[1].index("Bravo"),
                         "the first column starts in the same place")
        self.assertTrue(any(line.strip() and set(line.strip()) <= {_RULE_LIGHT, " "}
                            for line in rendered),
                        "the header is ruled off from the rows")
        self.assertIn("already clean", rows[1], "a None cell renders empty, not as the word None")
        self.assertNotIn("None", rows[1])

    def test_a_table_wider_than_the_report_keeps_every_column(self) -> None:
        """Columns are trimmed to their headers and then the table overflows.

        Dropping a column to fit would silently hide data - a report that lists
        ten movies and nine of their statuses is worse than one that is too
        wide, because the reader cannot tell what was left out.
        """
        headers = [f"column-{index}" for index in range(12)]
        rows = [[f"value-{row}-{column}" for column in range(12)] for row in range(1)]
        rendered = lines_of(Report("T", width=REPORT_MIN_WIDTH).table(headers, rows))
        header_line = [line for line in rendered if "column" in line][0]
        for name in headers:
            self.assertIn(name[:6], header_line, f"{name} was dropped from the table")
        data_line = [line for line in rendered if line.strip().startswith("value")][0]
        cells = [cell for cell in re.split(r" {2,}", data_line.strip()) if cell]
        self.assertEqual(len(cells), len(headers),
                         "every column kept its cell, however wide the table got")
        self.assertGreater(len(header_line), REPORT_MIN_WIDTH,
                           "the table overflows rather than losing a column")


if __name__ == "__main__":
    unittest.main()
