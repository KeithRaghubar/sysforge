# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
junit_annotate.py — turn a pytest JUnit report into GitHub Actions annotations.

Usage: python3 tools/junit_annotate.py <junit.xml>

tools/ci.sh runs it inside Actions after the `test` gate (3.5.0-DEV6), so each
failing test shows on the run page with its file and line instead of only in
the raw log. The report must be written with ``-o junit_family=xunit1``: the
default xunit2 family drops the ``file`` and ``line`` attributes.

Standard library only: it runs in the CI container as root, outside the
project's uv environment. Always exits 0; the gate's own exit status is what
fails the run.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET


def _escape(text: str) -> str:
    """Workflow-command escaping for a message (data) value."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_prop(text: str) -> str:
    """Workflow-command escaping for a property value (file=, title=)."""
    return _escape(text).replace(":", "%3A").replace(",", "%2C")


def error_lines(report: str) -> list[str]:
    """One ``::error`` line per failed or errored test case in *report*."""
    lines: list[str] = []
    # The report is the one this CI job's own pytest just wrote.
    for case in ET.parse(report).getroot().iter("testcase"):  # noqa: S314
        for kind in ("failure", "error"):
            node = case.find(kind)
            if node is None:
                continue
            props = []
            if case.get("file"):
                props.append(f"file={_escape_prop(case.get('file', ''))}")
                # pytest records the 0-based line of the test's def.
                if (case.get("line") or "").isdigit():
                    props.append(f"line={int(case.get('line', '0')) + 1}")
            title = f"{case.get('classname', '')}::{case.get('name', '')}"
            props.append(f"title={_escape_prop(title)}")
            body = node.get("message") or (node.text or "").strip() or kind
            lines.append(f"::error {','.join(props)}::{_escape(body)}")
    return lines


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: junit_annotate.py <junit.xml>", file=sys.stderr)
        return 2
    try:
        for line in error_lines(argv[1]):
            print(line)
    except (OSError, ET.ParseError) as exc:
        print(f"junit_annotate: cannot read {argv[1]}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
