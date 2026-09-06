"""Unit tests for the doctor's check table in ``organize.py``.

``run_doctor`` used to be one 350-line function, so the only way to test a
check was to run all twelve and grep the printed page - which is why, before
this file existed, six tests covered the entire diagnostic. Each check is now
a function from a :class:`organize.DoctorContext` to verdicts, so each one can
be asked the question it actually answers: what do you say when the program is
missing, and what do you say when it is there?

The fakes here replace the *sibling module* a check imports, never the check's
own logic, so a test fails if the check stops calling what it claims to call.
"""

from __future__ import annotations

import collections
import io
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import organize


def context(library: Path | str = "/nonexistent-library", source: Path | str = "/nonexistent-source") -> organize.DoctorContext:
    return organize.DoctorContext(library=Path(library), source=Path(source))


# Mirrors sys.version_info: compares like a tuple, reads like an object.
VersionInfo = collections.namedtuple("VersionInfo", "major minor micro releaselevel serial")


class FakePath:
    """A stand-in root, so a device-ID test never has to patch ``Path.stat``.

    ``Path.exists()`` is itself a ``stat`` call, so patching ``stat`` to raise
    makes the *existence* test explode before the check under test is reached.
    Handing the check an object that answers both questions keeps the two apart.
    """

    def __init__(self, present: bool = True, device: int = 2049, error: OSError | None = None) -> None:
        self.present = present
        self.device = device
        self.error = error

    def exists(self) -> bool:
        return self.present

    def is_dir(self) -> bool:
        return self.present

    def stat(self) -> object:
        if self.error is not None:
            raise self.error
        return types.SimpleNamespace(st_dev=self.device)


def fake_module(name: str, **attributes: object) -> types.ModuleType:
    """A stand-in sibling module, installed into ``sys.modules`` by the caller."""
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def with_modules(**modules: types.ModuleType | None):
    """Patch ``sys.modules`` entries; ``None`` makes an import raise."""
    replacements = {}
    for name, module in modules.items():
        replacements[name] = module
    return patch.dict(sys.modules, replacements)


class ProbeHelperTests(unittest.TestCase):
    """The single place in the doctor that is allowed to catch everything."""

    def test_a_working_probe_returns_its_answer_and_no_error(self) -> None:
        self.assertEqual(organize.probe_outcome(lambda: "ffmpeg"), ("ffmpeg", None))

    def test_a_raising_probe_returns_the_exception_instead_of_propagating(self) -> None:
        def probe() -> str:
            raise RuntimeError("registry unreadable")

        value, exc = organize.probe_outcome(probe)
        self.assertIsNone(value)
        self.assertIsInstance(exc, RuntimeError)
        self.assertEqual(str(exc), "registry unreadable")

    def test_probe_quietly_drops_the_exception(self) -> None:
        self.assertIsNone(organize.probe_quietly(lambda: (_ for _ in ()).throw(ImportError("no module"))))

    def test_a_probe_answering_falsey_is_not_confused_with_a_failure(self) -> None:
        """``None`` from the probe and ``None`` from a crash must be tellable apart."""
        value, exc = organize.probe_outcome(lambda: None)
        self.assertIsNone(value)
        self.assertIsNone(exc)


class PythonRuntimeCheckTests(unittest.TestCase):
    def test_current_interpreter_passes(self) -> None:
        check = organize.check_python_runtime(context())
        self.assertEqual(check.status, "ok")
        self.assertIn(f"{sys.version_info.major}.{sys.version_info.minor}", check.message)
        self.assertEqual(check.detail, sys.executable)

    def test_python_310_fails_and_says_where_to_upgrade(self) -> None:
        with patch.object(organize.sys, "version_info", VersionInfo(3, 10, 12, "final", 0)):
            check = organize.check_python_runtime(context())
        self.assertEqual(check.status, "fail")
        self.assertIn("3.10.12", check.message)
        self.assertIn("below required Python 3.11+", check.message)
        self.assertIn("python.org/downloads", check.remedy)

    def test_python_311_is_the_boundary_and_passes(self) -> None:
        with patch.object(organize.sys, "version_info", VersionInfo(3, 11, 0, "final", 0)):
            self.assertEqual(organize.check_python_runtime(context()).status, "ok")


class OperatingSystemCheckTests(unittest.TestCase):
    def test_reports_system_release_and_machine(self) -> None:
        check = organize.check_operating_system(context())
        self.assertEqual(check.status, "ok")
        for part in (organize.platform.system(), organize.platform.release(), organize.platform.machine()):
            self.assertIn(part, check.message)


class BinaryCheckTests(unittest.TestCase):
    """The five external-program checks: found, missing, and unimportable."""

    def test_mkvmerge_found_reports_version_and_path(self) -> None:
        cleaner = fake_module(
            "mkv_track_cleaner",
            resolve_mkvmerge_path=lambda: "/usr/bin/mkvmerge",
            get_mkvmerge_version=lambda path: "mkvmerge v80.0",
        )
        with with_modules(mkv_track_cleaner=cleaner):
            check = organize.check_mkvtoolnix(context())
        self.assertEqual(check.status, "ok")
        self.assertEqual(check.message, "Found: mkvmerge v80.0")
        self.assertEqual(check.detail, "/usr/bin/mkvmerge")
        self.assertEqual(check.remedy, "")

    def test_mkvmerge_missing_warns_with_install_lines_for_three_platforms(self) -> None:
        def explode() -> str:
            raise FileNotFoundError("mkvmerge")

        cleaner = fake_module("mkv_track_cleaner", resolve_mkvmerge_path=explode)
        with with_modules(mkv_track_cleaner=cleaner):
            check = organize.check_mkvtoolnix(context())
        self.assertEqual(check.status, "warn")
        self.assertIn("winget", check.remedy)
        self.assertIn("apt install", check.remedy)
        self.assertIn("brew install", check.remedy)

    def test_ffprobe_present_but_not_working_is_reported_as_missing(self) -> None:
        """A binary that is on disk but cannot answer is no use to bitdepth.py."""
        probe_mod = fake_module(
            "bitdepth",
            find_ffprobe=lambda: "/usr/bin/ffprobe",
            ffprobe_works=lambda path: False,
        )
        with with_modules(bitdepth=probe_mod):
            check = organize.check_ffprobe(context())
        self.assertEqual(check.status, "warn")
        self.assertIn("10-bit bit depth scanning", check.detail)

    def test_ffprobe_working_passes(self) -> None:
        probe_mod = fake_module(
            "bitdepth",
            find_ffprobe=lambda: "/usr/bin/ffprobe",
            ffprobe_works=lambda path: True,
        )
        with with_modules(bitdepth=probe_mod), patch.object(organize, "get_binary_version", return_value="ffprobe 6.1"):
            check = organize.check_ffprobe(context())
        self.assertEqual(check.status, "ok")
        self.assertEqual(check.message, "Found: ffprobe 6.1")
        self.assertEqual(check.detail, "/usr/bin/ffprobe")

    def test_ffprobe_falls_back_to_the_binary_name_when_version_is_unreadable(self) -> None:
        probe_mod = fake_module("bitdepth", find_ffprobe=lambda: "/x/ffprobe", ffprobe_works=lambda path: True)
        with with_modules(bitdepth=probe_mod), patch.object(organize, "get_binary_version", return_value=""):
            self.assertEqual(organize.check_ffprobe(context()).message, "Found: ffprobe")

    def test_ffmpeg_found_on_path(self) -> None:
        with patch.object(organize.shutil, "which", return_value="/usr/bin/ffmpeg"), \
                patch.object(organize, "get_binary_version", return_value="ffmpeg version 6.1"):
            check = organize.check_ffmpeg(context())
        self.assertEqual(check.status, "ok")
        self.assertEqual(check.detail, "/usr/bin/ffmpeg")

    def test_ffmpeg_missing_explains_that_sync_needs_it(self) -> None:
        with patch.object(organize.shutil, "which", return_value=None):
            check = organize.check_ffmpeg(context())
        self.assertEqual(check.status, "warn")
        self.assertIn("ffsubsync needs ffmpeg", check.detail)

    def test_ffsubsync_found(self) -> None:
        sync = fake_module("sync_subtitles", find_ffsubsync=lambda: "/usr/local/bin/ffsubsync")
        with with_modules(sync_subtitles=sync), patch.object(organize, "get_binary_version", return_value="0.4.25"):
            check = organize.check_ffsubsync(context())
        self.assertEqual(check.status, "ok")
        self.assertEqual(check.message, "Found: 0.4.25")

    def test_ffsubsync_missing_warns_rather_than_fails(self) -> None:
        """Sync is an optional step, so its absence must not stop a scheduler."""
        sync = fake_module("sync_subtitles", find_ffsubsync=lambda: None)
        with with_modules(sync_subtitles=sync):
            check = organize.check_ffsubsync(context())
        self.assertEqual(check.status, "warn")
        self.assertIn("pip install ffsubsync", check.remedy)

    def test_mkvextract_needs_both_programs_not_either(self) -> None:
        def only_mkvmerge(name: str) -> str | None:
            return "/usr/bin/mkvmerge" if name == "mkvmerge" else None

        fetcher = fake_module("subtitle_fetcher", find_mkvtoolnix_binary=only_mkvmerge)
        with with_modules(subtitle_fetcher=fetcher):
            check = organize.check_mkvextract(context())
        self.assertEqual(check.status, "warn")
        self.assertIn("embedded subtitle tracks", check.detail)

    def test_mkvextract_found_names_both_paths(self) -> None:
        fetcher = fake_module(
            "subtitle_fetcher",
            find_mkvtoolnix_binary=lambda name: f"/usr/bin/{name}",
        )
        with with_modules(subtitle_fetcher=fetcher), patch.object(organize, "get_binary_version", return_value="v80.0"):
            check = organize.check_mkvextract(context())
        self.assertEqual(check.status, "ok")
        self.assertIn("/usr/bin/mkvextract", check.detail)
        self.assertIn("/usr/bin/mkvmerge", check.detail)

    def test_an_unimportable_sibling_is_a_warning_not_a_crash(self) -> None:
        """The doctor runs on machines where a sibling tool is broken."""
        for check_fn in (organize.check_mkvtoolnix, organize.check_ffprobe,
                         organize.check_ffsubsync, organize.check_mkvextract):
            with self.subTest(check=check_fn.__name__), \
                    patch.dict(sys.modules, {"mkv_track_cleaner": None, "bitdepth": None,
                                             "sync_subtitles": None, "subtitle_fetcher": None}):
                self.assertEqual(check_fn(context()).status, "warn")


class OcrCheckTests(unittest.TestCase):
    def test_backend_found_reports_its_label(self) -> None:
        backend = types.SimpleNamespace(label="pgsrip")
        fetcher = fake_module(
            "subtitle_fetcher",
            OCR_BACKEND_AUTO="auto",
            detect_ocr_backend=lambda mode: (backend, ""),
        )
        with with_modules(subtitle_fetcher=fetcher):
            check = organize.check_ocr_backend(context())
        self.assertEqual(check.status, "ok")
        self.assertEqual(check.message, "Found: pgsrip")

    def test_no_backend_keeps_the_reason_from_the_detector(self) -> None:
        fetcher = fake_module(
            "subtitle_fetcher",
            OCR_BACKEND_AUTO="auto",
            detect_ocr_backend=lambda mode: (None, "tesseract not installed"),
        )
        with with_modules(subtitle_fetcher=fetcher):
            check = organize.check_ocr_backend(context())
        self.assertEqual(check.status, "warn")
        self.assertTrue(check.detail.startswith("tesseract not installed. "))
        self.assertIn("Text tracks (SRT/SSA/ASS) are still extracted", check.detail)

    def test_a_broken_detector_names_the_exception_in_the_detail(self) -> None:
        """The one check that reports *why* the probe failed, so keep it doing so."""
        def explode(mode: str) -> tuple[object, str]:
            raise ValueError("bad OCR config")

        fetcher = fake_module("subtitle_fetcher", OCR_BACKEND_AUTO="auto", detect_ocr_backend=explode)
        with with_modules(subtitle_fetcher=fetcher):
            check = organize.check_ocr_backend(context())
        self.assertEqual(check.status, "warn")
        self.assertIn("subtitle_fetcher is unavailable (bad OCR config)", check.detail)


class ProviderKeyCheckTests(unittest.TestCase):
    """Zero, one or both keys - and never a key printed in full."""

    OPENSUBTITLES = "os-secret-key-1234567890"
    SUBDL = "subdl-secret-key-0987654321"

    def keys(self, opensubtitles: str, subdl: str) -> list[organize.DiagnosticCheck]:
        fetcher = fake_module("subtitle_fetcher", OPENSUBTITLES_API_KEY="", SUBDL_API_KEY="")
        env = {"OPENSUBTITLES_API_KEY": opensubtitles, "SUBDL_API_KEY": subdl}
        with with_modules(subtitle_fetcher=fetcher), patch.dict(os.environ, env, clear=False):
            return organize.check_provider_keys(context())

    def test_no_keys_produces_one_warning(self) -> None:
        checks = self.keys("", "")
        self.assertEqual([c.name for c in checks], ["Subtitle Provider API Key"])
        self.assertEqual(checks[0].status, "warn")
        self.assertIn("opensubtitles.com", checks[0].remedy)
        self.assertIn("subdl.com", checks[0].remedy)

    def test_one_key_is_enough_to_silence_the_warning(self) -> None:
        checks = self.keys(self.OPENSUBTITLES, "")
        self.assertEqual([c.name for c in checks], ["OpenSubtitles API Key"])
        self.assertEqual(checks[0].status, "ok")

    def test_subdl_alone_is_a_supported_configuration(self) -> None:
        checks = self.keys("", self.SUBDL)
        self.assertEqual([c.name for c in checks], ["SubDL API Key"])
        self.assertIn("sole provider", checks[0].detail)

    def test_both_keys_produce_two_rows_and_no_warning(self) -> None:
        checks = self.keys(self.OPENSUBTITLES, self.SUBDL)
        self.assertEqual([c.name for c in checks], ["OpenSubtitles API Key", "SubDL API Key"])
        self.assertTrue(all(c.status == "ok" for c in checks))

    def test_a_key_is_never_printed_in_full(self) -> None:
        """A doctor report gets pasted into issues; a key must not ride along."""
        for check in self.keys(self.OPENSUBTITLES, self.SUBDL):
            rendered = f"{check.message} {check.detail} {check.remedy}"
            self.assertNotIn(self.OPENSUBTITLES, rendered)
            self.assertNotIn(self.SUBDL, rendered)
            self.assertNotIn(self.OPENSUBTITLES[4:-4], rendered)

    def test_whitespace_only_key_counts_as_unset(self) -> None:
        self.assertEqual([c.name for c in self.keys("   ", "\t")], ["Subtitle Provider API Key"])

    def test_mask_shows_the_ends_of_a_long_key(self) -> None:
        self.assertEqual(organize.mask_key("abcdefghijklmnop"), "abcd...mnop")

    def test_mask_hides_a_short_key_entirely(self) -> None:
        """Eight characters or fewer: the ends would be most of the key."""
        self.assertEqual(organize.mask_key("12345678"), "***")
        self.assertEqual(organize.mask_key(""), "***")


class DirectoryCheckTests(unittest.TestCase):
    def test_existing_library_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            check = organize.check_library_directory(context(library=td))
        self.assertEqual(check.status, "ok")
        self.assertIn("Accessible", check.message)

    def test_missing_library_warns_and_names_the_env_var_and_flag(self) -> None:
        check = organize.check_library_directory(context(library="/no/such/library"))
        self.assertEqual(check.status, "warn")
        self.assertIn("/no/such/library", check.message)
        self.assertIn("ORGANIZE_LIBRARY", check.remedy)
        self.assertIn("--target", check.remedy)

    def test_a_file_where_the_library_should_be_is_not_accessible(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "library-is-a-file"
            path.write_text("not a directory")
            self.assertEqual(organize.check_library_directory(context(library=path)).status, "warn")

    def test_existing_source_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(organize.check_source_directory(context(source=td)).status, "ok")

    def test_missing_source_names_movie_std_source_and_source_flag(self) -> None:
        check = organize.check_source_directory(context(source="/no/such/source"))
        self.assertEqual(check.status, "warn")
        self.assertIn("MOVIE_STD_SOURCE", check.remedy)
        self.assertIn("--source", check.remedy)


class HardlinkCheckTests(unittest.TestCase):
    """The invariant the whole hardlink-only ingest rests on."""

    def test_same_filesystem_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "lib").mkdir()
            (root / "src").mkdir()
            checks = organize.check_hardlink_compatibility(context(root / "lib", root / "src"))
        self.assertEqual([c.status for c in checks], ["ok"])
        self.assertIn("os.link", checks[0].detail)

    def test_different_devices_is_a_failure_not_a_warning(self) -> None:
        """Cross-device means ingest cannot work at all, so doctor must exit 1."""
        checks = organize.check_hardlink_compatibility(organize.DoctorContext(
            library=FakePath(device=2049), source=FakePath(device=2050),
        ))
        self.assertEqual([c.status for c in checks], ["fail"])
        self.assertIn("DIFFERENT filesystems", checks[0].message)
        self.assertIn("Source dev=2050", checks[0].detail)
        self.assertIn("Target dev=2049", checks[0].detail)
        self.assertEqual(organize.diagnostics_exit_code(checks), 1)

    def test_stays_silent_when_a_root_is_missing(self) -> None:
        """The missing folder is already reported; saying it twice is noise."""
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(organize.check_hardlink_compatibility(context(td, "/no/such/source")), [])
            self.assertEqual(organize.check_hardlink_compatibility(context("/no/such/library", td)), [])

    def test_an_unreadable_device_id_warns_with_the_os_error(self) -> None:
        checks = organize.check_hardlink_compatibility(organize.DoctorContext(
            library=FakePath(error=PermissionError("nope")), source=FakePath(),
        ))
        self.assertEqual([c.status for c in checks], ["warn"])
        self.assertIn("nope", checks[0].message)


class CheckTableTests(unittest.TestCase):
    def test_every_check_is_registered_once_under_a_unique_key(self) -> None:
        keys = [key for key, _ in organize.DOCTOR_CHECKS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_collect_runs_every_registered_check(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            checks = organize.collect_diagnostics(context(td, td))
        names = [c.name for c in checks]
        self.assertIn("Python Runtime", names)
        self.assertIn("Operating System", names)
        self.assertIn("Hardlink Compatibility", names)
        self.assertEqual(len(names), len(set(names)), "two checks printed the same row name")

    def test_a_check_that_raises_becomes_a_failed_row_not_a_traceback(self) -> None:
        """Doctor is what you run when the machine is broken; it must survive it."""
        def broken(ctx: organize.DoctorContext) -> organize.DiagnosticCheck:
            raise RuntimeError("probe exploded")

        table = ((organize.DOCTOR_CHECKS[0]), ("broken", broken))
        with patch.object(organize, "DOCTOR_CHECKS", table):
            checks = organize.collect_diagnostics(context())
        self.assertEqual(len(checks), 2)
        failed = checks[1]
        self.assertEqual(failed.name, "Check: broken")
        self.assertEqual(failed.status, "fail")
        self.assertIn("RuntimeError: probe exploded", failed.message)
        self.assertIn("github.com/smeltzzz/organize/issues", failed.remedy)

    def test_a_check_returning_a_list_is_flattened_into_the_rows(self) -> None:
        def two(ctx: organize.DoctorContext) -> list[organize.DiagnosticCheck]:
            return [organize.DiagnosticCheck(name="A", status="ok", message=""),
                    organize.DiagnosticCheck(name="B", status="ok", message="")]

        with patch.object(organize, "DOCTOR_CHECKS", (("two", two),)):
            self.assertEqual([c.name for c in organize.collect_diagnostics(context())], ["A", "B"])

    def test_a_check_returning_nothing_adds_no_rows(self) -> None:
        with patch.object(organize, "DOCTOR_CHECKS", (("none", lambda ctx: []),)):
            self.assertEqual(organize.collect_diagnostics(context()), [])


class ExitCodeTests(unittest.TestCase):
    def check(self, status: str) -> organize.DiagnosticCheck:
        return organize.DiagnosticCheck(name=status, status=status, message="")

    def test_all_ok_exits_zero(self) -> None:
        self.assertEqual(organize.diagnostics_exit_code([self.check("ok"), self.check("ok")]), 0)

    def test_warnings_alone_still_exit_zero(self) -> None:
        """A warning is a step that will skip, not a reason to refuse to start."""
        self.assertEqual(organize.diagnostics_exit_code([self.check("ok"), self.check("warn")]), 0)

    def test_any_failure_exits_one(self) -> None:
        self.assertEqual(organize.diagnostics_exit_code([self.check("ok"), self.check("fail")]), 1)

    def test_no_checks_at_all_exits_zero(self) -> None:
        self.assertEqual(organize.diagnostics_exit_code([]), 0)


class RenderTests(unittest.TestCase):
    def render(self, *checks: organize.DiagnosticCheck) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            organize.render_diagnostics(checks)
        return buf.getvalue()

    def test_a_bare_check_prints_one_line(self) -> None:
        output = self.render(organize.DiagnosticCheck(name="Thing", status="ok", message="all good"))
        self.assertEqual(len(output.strip().splitlines()), 1)
        self.assertIn("Thing", output)
        self.assertIn("all good", output)

    def test_detail_and_every_remedy_line_are_printed(self) -> None:
        output = self.render(organize.DiagnosticCheck(
            name="Thing", status="warn", message="missing",
            detail="why it matters", remedy="line one\nline two\nline three",
        ))
        self.assertIn("why it matters", output)
        self.assertEqual(output.count("Fix:"), 3)
        for line in ("line one", "line two", "line three"):
            self.assertIn(line, output)

    def test_an_unknown_status_renders_as_a_failure_rather_than_blank(self) -> None:
        output = self.render(organize.DiagnosticCheck(name="Thing", status="bogus", message="?"))
        self.assertIn(organize.SYM_FAIL, output)

    def test_scorecard_counts_each_status(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            organize.render_scorecard([
                organize.DiagnosticCheck(name="a", status="ok", message=""),
                organize.DiagnosticCheck(name="b", status="warn", message=""),
                organize.DiagnosticCheck(name="c", status="warn", message=""),
                organize.DiagnosticCheck(name="d", status="fail", message=""),
            ])
        output = buf.getvalue()
        self.assertIn("1 passed", output)
        self.assertIn("2 warnings", output)
        self.assertIn("1 failed", output)
        self.assertIn("Action required", output)

    def test_a_clean_machine_says_all_systems_operational(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            organize.render_scorecard([organize.DiagnosticCheck(name="a", status="ok", message="")])
        self.assertIn("All systems operational", buf.getvalue())
        self.assertNotIn("warnings", buf.getvalue())


class RunDoctorTests(unittest.TestCase):
    """The whole command, still behaving as it did before the table existed."""

    def test_prints_the_banner_every_check_and_the_scorecard(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = organize.run_doctor(library_path=Path(td), source_path=Path(td))
        output = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("SYSTEM & PREREQUISITE DIAGNOSTICS", output)
        self.assertIn("Python Runtime", output)
        self.assertIn("Hardlink Compatibility", output)
        self.assertIn("Scorecard:", output)

    def test_exit_code_follows_the_checks_not_the_printing(self) -> None:
        def failing(ctx: organize.DoctorContext) -> organize.DiagnosticCheck:
            return organize.DiagnosticCheck(name="Doomed", status="fail", message="broken")

        with patch.object(organize, "DOCTOR_CHECKS", (("doomed", failing),)):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = organize.run_doctor(library_path=Path("/tmp"), source_path=Path("/tmp"))
        self.assertEqual(code, 1)
        self.assertIn("Doomed", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
