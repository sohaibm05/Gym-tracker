#!/usr/bin/env python3
"""Fail the build when a test was skipped that should have run.

A skipped test is green, which is what makes skips dangerous in CI. If the
database step breaks, TEST_DATABASE_URL goes missing, or a dependency stops
importing (test_observability.py uses pytest.importorskip), whole groups of
tests quietly become skips and the run still passes. This reads pytest's JUnit
report and fails unless every skip has a reason the caller expected.

    # Postgres job: nothing may skip
    python .github/scripts/check_skips.py reports/junit-postgres.xml

    # unit jobs: only the tests that need a database may skip
    python .github/scripts/check_skips.py reports/junit-unit-py3.11.xml --allow TEST_DATABASE_URL
"""

from __future__ import annotations

import argparse
import os
import sys
import xml.etree.ElementTree as ET  # the report is written by our own pytest run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail on unexpected pytest skips.")
    parser.add_argument("report", help="pytest --junitxml report")
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="TEXT",
        help="a skip whose reason contains TEXT is expected (repeatable)",
    )
    args = parser.parse_args(argv)

    cases = list(ET.parse(args.report).getroot().iter("testcase"))
    expected = 0
    unexpected: list[tuple[str, str]] = []
    for case in cases:
        skipped = case.find("skipped")
        if skipped is None:
            continue
        reason = skipped.get("message") or "(no reason given)"
        if any(text in reason for text in args.allow):
            expected += 1
        else:
            # A module skipped at collection time has an empty classname.
            name = "::".join(p for p in (case.get("classname"), case.get("name")) if p)
            unexpected.append((name, reason))

    summary = (
        f"{len(cases)} tests in {os.path.basename(args.report)}: "
        f"{expected} expected skip(s), {len(unexpected)} unexpected skip(s)"
    )
    print(summary)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(f"- {summary}\n")

    if not cases:
        print(f"::error title=No tests::{args.report} contains no test cases")
        return 1
    for name, reason in unexpected:
        print(f"::error title=Unexpected skip::{name}: {reason}")
    return 1 if unexpected else 0


if __name__ == "__main__":
    sys.exit(main())
