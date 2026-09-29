"""A requirements file read once, line by line, for both of its readers (P3d).

The dependency layer's rows of a ``requirements*.txt`` and the facts of a file a root
``requirements.txt`` reaches through ``-r`` come from the one reader
(:func:`repoatlas.parsers.requirements.requirement_lines`), so a line is a requirement, an
include or nothing alike for both. An include is followed from the including file's own
directory, and never to a URL, a variable, an absolute path or above the repository.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.dependencies.python import RequirementsParser
from repoatlas.parsers.requirements import (
    INCLUDE_OUTSIDE,
    INCLUDE_UNSUPPORTED,
    include_target,
    requirement_lines,
    requirements_facts,
)

MIXED = (
    "# pinned by hand\n"
    "-r requirements/base.txt\n"
    "\n"
    "-rother.txt\n"
    "--requirement extra.txt\n"
    "--requirement=more.txt  # the rest\n"
    "-r\ttabbed.txt\n"
    "-c constraints.txt\n"
    "-e .\n"
    "--index-url https://example.invalid/simple\n"
    "Flask>=3 # inline\n"
    "fixture-auth[extra]>=1; python_version >= '3.10'\n"
)


def test_each_line_is_a_requirement_an_include_or_nothing() -> None:
    assert list(requirement_lines(MIXED)) == [
        (2, "include", "requirements/base.txt"),
        (4, "include", "other.txt"),
        (5, "include", "extra.txt"),
        (6, "include", "more.txt"),
        (7, "include", "tabbed.txt"),
        (11, "requirement", "Flask>=3 # inline"),
        (12, "requirement", "fixture-auth[extra]>=1; python_version >= '3.10'"),
    ]


def test_lines_are_numbered_by_the_shared_source_line_model() -> None:
    content = "-r a.txt\r\nflask\rwerkzeug\n"
    assert list(requirement_lines(content)) == [
        (1, "include", "a.txt"),
        (2, "requirement", "flask"),
        (3, "requirement", "werkzeug"),
    ]


def test_the_rows_are_the_requirement_lines_and_nothing_else() -> None:
    """The dependency layer's rows are unchanged: no include, constraint or option is one."""
    report = RequirementsParser().parse(Path("requirements.txt"), MIXED)
    assert [(d.name, d.constraint, d.line) for d in report.declared] == [
        ("Flask", ">=3 # inline", 11),  # an inline comment stays in the constraint (P3 debt)
        ("fixture-auth", ">=1", 12),
    ]


@pytest.mark.parametrize(
    ("including", "written", "expected"),
    [
        ("requirements.txt", "requirements/heroku.txt", "requirements/heroku.txt"),
        ("requirements/heroku.txt", "prod.txt", "requirements/prod.txt"),  # R3
        ("requirements/prod.txt", "../common.txt", "common.txt"),  # R4
        ("requirements/prod.txt", "./sub//x.txt", "requirements/sub/x.txt"),
        ("a/b/requirements.txt", "../../requirements.txt", "requirements.txt"),
    ],
)
def test_an_include_is_followed_from_the_including_files_directory(
    including: str, written: str, expected: str
) -> None:
    assert include_target(including, written) == (expected, None)


@pytest.mark.parametrize(
    ("including", "written", "why"),
    [
        ("requirements.txt", "../outside.txt", INCLUDE_OUTSIDE),
        ("requirements/prod.txt", "../../outside.txt", INCLUDE_OUTSIDE),  # R8
        ("requirements.txt", "..", INCLUDE_OUTSIDE),
        ("requirements.txt", "https://example.invalid/requirements.txt", INCLUDE_UNSUPPORTED),
        ("requirements.txt", "HTTP://example.invalid/r.txt", INCLUDE_UNSUPPORTED),
        ("requirements.txt", "file:requirements/base.txt", INCLUDE_UNSUPPORTED),
        ("requirements.txt", "git+https://example.invalid/r.git", INCLUDE_UNSUPPORTED),
        ("requirements.txt", "${REQS}/base.txt", INCLUDE_UNSUPPORTED),  # R9
        ("requirements.txt", "$REQS/base.txt", INCLUDE_UNSUPPORTED),
        ("requirements.txt", "~/base.txt", INCLUDE_UNSUPPORTED),
        ("requirements.txt", "/etc/base.txt", INCLUDE_UNSUPPORTED),
        ("requirements.txt", "requirements\\base.txt", INCLUDE_UNSUPPORTED),
        ("requirements.txt", "C:/base.txt", INCLUDE_UNSUPPORTED),
        ("requirements.txt", "", INCLUDE_UNSUPPORTED),
    ],
)
def test_an_include_leaving_the_repositorys_own_files_is_not_followed(
    including: str, written: str, why: str
) -> None:
    assert include_target(including, written) == (written, why)


def test_the_facts_hold_each_include_and_each_requirement_in_line_order() -> None:
    content = "-r base.txt\nFlask>=3\n-r https://example.invalid/r.txt\n-r ../../x.txt\n"
    assert requirements_facts("requirements/prod.txt", content) == {
        "v": 1,
        "includes": [
            [1, "requirements/base.txt", None],
            [3, "https://example.invalid/r.txt", INCLUDE_UNSUPPORTED],
            [4, "../../x.txt", INCLUDE_OUTSIDE],
        ],
        "requires": [[2, "Flask", ">=3"]],
    }
