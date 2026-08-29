#!/usr/bin/env bash
# Run every tool's built-in self-test and the repository unit-test suite.
#
#   bash run_tests.sh
#
# All checks are offline and need no media, no mkvmerge, no ffprobe and no
# OpenSubtitles credentials.
set -u

echo "================================================================================"
echo "BUILT-IN SELF-TESTS"
echo "================================================================================"
status=0
for script in organize.py 10bit.py library_auditor.py movie_standardizer.py subtitle_fetcher.py mkv_track_cleaner.py pipeline.py; do
    echo
    echo "--- $script --self-test ---"
    if python3 "$script" --self-test; then
        :
    else
        status=1
    fi
done

echo
echo "================================================================================"
echo "UNIT TESTS (python3 -m unittest discover -s tests)"
echo "================================================================================"
if python3 -m unittest discover -s tests -p 'test_*.py'; then
    :
else
    status=1
fi

echo
if [ "$status" -eq 0 ]; then
    echo "ALL TESTS PASSED"
else
    echo "SOME TESTS FAILED"
fi
exit "$status"
