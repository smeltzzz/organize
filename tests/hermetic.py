"""Pin the host toolchain out of the tests that would otherwise consult it.

The suite is hermetic by design: it needs no media files, no MKVToolNix, no
FFmpeg and no network. Hermetic means the tests do not *require* those things.
It does not, on its own, mean they ignore them - and a handful did not.

``organize.py doctor`` answers by probing the machine it runs on, and the CLI
tests run the real doctor. So does ``movie_standardizer``: ``--ffprobe`` is a
*hint*, and when the hinted path does not exist ``find_ffprobe`` falls through
to ``shutil.which("ffprobe")`` and finds whatever the machine actually has. On
a CI runner - which has none of these installed - every one of those probes
came back empty, and the tests passed. On a workstation that has the tools, the
same tests took the other branch: a real ``mkvmerge --version`` here, a real
``ffprobe -show_streams`` against a fixture that is not a video there.

That is the whole bug: the outcome depended on the machine, and only one of the
two machines was ever tested. Pinning the lookups makes the answer the same
everywhere, and it is a no-op on a runner that has nothing installed, because
"not found" is what the real lookup already returned there.

Two ways to use it::

    class SomeTests(unittest.TestCase, HermeticToolsMixin):
        ...

    def test_x(self):
        with no_media_tools():
            ...

A test that wants a tool to be *present* patches the lookup itself, inside the
test body. That patch is applied on top of this one and wins, which is what
``test_doctor_reports_ffprobe_when_the_inspector_finds_it`` relies on.
"""

from __future__ import annotations

import contextlib
import shutil
from collections.abc import Iterator
from unittest import mock

#: Every external program the toolkit looks for. A lookup for any of these
#: answers "not installed" while the pin is held, whatever the host has.
#:
#: ``ffs`` and ``subsync`` are ffsubsync's two alternative entry points; the
#: OCR backends and their runtimes are here because ``doctor`` probes them too,
#: so a machine with Tesseract installed would otherwise read differently from
#: one without.
EXTERNAL_PROGRAMS = frozenset({
    "mkvmerge", "mkvextract", "mkvpropedit", "mkvinfo",
    "ffmpeg", "ffprobe",
    "ffsubsync", "ffs", "subsync",
    "tesseract", "pgsrip", "pgstosrt", "subtitleedit", "mono", "dotnet",
})

_real_which = shutil.which


def _which_without_media_tools(name: object, *args: object, **kwargs: object) -> str | None:
    """``shutil.which`` that cannot see the media toolchain.

    Everything else keeps its real answer - ``git`` is looked up by
    ``tests/test_docs.py`` and has nothing to do with the toolchain.
    """
    stem = str(name).lower()
    if stem.endswith(".exe"):
        stem = stem[:-4]
    if stem in EXTERNAL_PROGRAMS:
        return None
    return _real_which(name, *args, **kwargs)  # type: ignore[arg-type]


def _mkvmerge_absent(custom_path: str | None = None) -> str:
    """The same answer ``resolve_mkvmerge_path`` gives on a machine without it."""
    raise FileNotFoundError(
        "'mkvmerge' was not found in PATH or standard locations. "
        "Install MKVToolNix or pass --mkvmerge."
    )


@contextlib.contextmanager
def no_media_tools() -> Iterator[None]:
    """Hold every toolchain lookup at "not installed" for the duration.

    Pinned at the resolver each tool actually calls, not only at
    ``shutil.which``: MKVToolNix's Windows installer does not put itself on
    PATH, and the tools search their own standard install locations as well, so
    a PATH-only pin would still find a real install in ``Program Files``.
    """
    import bitdepth
    import mkv_track_cleaner
    import movie_standardizer
    import subtitle_extractor
    import sync_subtitles

    with mock.patch("shutil.which", side_effect=_which_without_media_tools), \
            mock.patch.object(mkv_track_cleaner, "resolve_mkvmerge_path", _mkvmerge_absent), \
            mock.patch.object(mkv_track_cleaner, "get_mkvmerge_version",
                              lambda path: "unknown version"), \
            mock.patch.object(subtitle_extractor, "find_mkvtoolnix_binary",
                              lambda name, explicit=None: None), \
            mock.patch.object(sync_subtitles, "find_ffsubsync", lambda explicit=None: None), \
            mock.patch.object(bitdepth, "find_ffprobe", lambda explicit=None: None), \
            mock.patch.object(bitdepth, "ffprobe_works", lambda binary: False), \
            mock.patch.object(movie_standardizer, "find_ffprobe", lambda explicit="ffprobe": None):
        yield


class HermeticToolsMixin:
    """Run every test in the class as if the machine had none of the tools.

    Mixed in to the right of ``unittest.TestCase`` so the pin is held for the
    whole test, ``setUp`` included::

        class SomeTests(HermeticToolsMixin, unittest.TestCase):
            ...
    """

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        pin = no_media_tools()
        pin.__enter__()
        self.addCleanup(pin.__exit__, None, None, None)  # type: ignore[attr-defined]
