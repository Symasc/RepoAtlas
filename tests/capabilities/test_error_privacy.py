"""What an unrecognized failure is allowed to say (RC audit, RCA-03).

Everything RepoAtlas models has a code, a category and wording it wrote itself.
Whatever reaches the end of the dispatch has been positively identified as
unidentifiable, and its text was written by whoever raised it --- the standard
library, SQLite, a third-party package. That text used to be the envelope's
``message``.

It is not a contract and it is not safe. ``OSError`` interpolates the absolute
path it failed on. A decoding failure quotes the bytes, which are repository
content. A driver error can quote SQL. The audit's reproduction was an ordinary
missing file: the agent on the other end of the transport received
``/tmp/.../repos/app/NO_SUCH_FILE_XYZ``, which is host layout it had no other way
to learn and no reason to be told.

Two properties are asserted here, and they pull in opposite directions on
purpose:

* **an unrecognized failure discloses nothing** --- not the path, not the class
  name, not one substring of the exception, in any field of the envelope;
* **a recognized failure is untouched** --- RepoAtlas wrote those words for a
  caller to act on, and sanitizing them would trade a privacy defect for a
  diagnostic one.

The sentinels below are distinct per category so a failure names what leaked
rather than only that something did.
"""

from __future__ import annotations

import json

import pytest
from repoatlas.capabilities.errors import (
    INTERNAL_ERROR_DETAIL,
    INTERNAL_ERROR_MESSAGE,
    FileNotIndexedError,
    InvalidCursorError,
    StaleCursorError,
    SyncFailedError,
    capability_error,
)
from repoatlas.capabilities.ledger import LedgerCapacityError
from repoatlas.capabilities.session import UnknownInvestigationError
from repoatlas.errors import (
    ContextFailure,
    ContextResolutionError,
    RepoAtlasError,
    SyncInProgressError,
)
from repoatlas.models.capability import ErrorCategory

# --- sentinels, one per thing that must never cross ------------------------
HOST_PATH = "/tmp/PRIVATE_HOST_ROOT/repos/app/NO_SUCH_FILE_XYZ"  # noqa: S108 - a sentinel that is never opened
SECRET = "RCA03_SECRET_SENTINEL_918273"  # noqa: S105 - a marker, not a secret
SOURCE = "RCA03_SOURCE_SENTINEL_DO_NOT_LEAK"
WINDOWS_PATH = r"C:\Users\PrivateUser\repo\secret.py"
CONFIG_PATH = "/home/private-user/.config/repoatlas/config.yaml"
DATABASE_PATH = "/home/private-user/.local/share/repoatlas/atlas.db"
UNC_PATH = r"\\fileserver\share\private\repo.py"
CONNECTION_STRING = "postgresql://admin:hunter2@10.0.0.4:5432/prod"


def _walk(value: object) -> list[str]:
    """Every string anywhere in the envelope, unescaped."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [part for item in value.items() for part in _walk(item)]
    if isinstance(value, (list, tuple)):
        return [part for item in value for part in _walk(item)]
    return [repr(value)]


def _serialized(exc: Exception) -> str:
    """The whole envelope as one string --- every field, nested ones included.

    Assembled from the values rather than from ``json.dumps``, because JSON
    escapes a backslash and a Windows path would then be searched for in a form
    it cannot appear in. A sentinel that can never match is a test that can never
    fail, and this suite exists to catch leaks of exactly that shape --- the JSON
    text is appended too, so an escaped occurrence is caught as well.
    """
    envelope = capability_error(exc).model_dump(mode="json")
    return "\u0000".join(_walk(envelope)) + json.dumps(envelope, sort_keys=True)


# ---------------------------------------------------------------------------
# The audit reproduction
# ---------------------------------------------------------------------------


def test_the_audit_reproduction_no_longer_discloses_the_path() -> None:
    """CASE 1. The exact failure the RC audit reported."""
    exc = FileNotFoundError(2, "No such file or directory", HOST_PATH)
    error = capability_error(exc)

    assert error.code == "INTERNAL_ERROR"
    assert error.category is ErrorCategory.INTERNAL
    assert error.retryable is False
    assert error.message == INTERNAL_ERROR_MESSAGE
    assert error.detail == INTERNAL_ERROR_DETAIL
    assert error.context == {}

    blob = _serialized(exc)
    assert HOST_PATH not in blob
    assert "NO_SUCH_FILE_XYZ" not in blob
    assert "Errno" not in blob


@pytest.mark.parametrize(
    ("label", "leak"),
    [
        ("absolute posix path", HOST_PATH),
        ("secret", SECRET),
        ("repository source", SOURCE),
        ("windows path", WINDOWS_PATH),
        ("unc path", UNC_PATH),
        ("config path", CONFIG_PATH),
        ("database path", DATABASE_PATH),
        ("connection string", CONNECTION_STRING),
    ],
)
def test_no_category_of_exception_text_crosses(label: str, leak: str) -> None:
    """CASES 2-5. One parametrization because one rule covers all of them.

    The fallback does not inspect the text, so there is no category of content it
    could treat differently --- which is the property being asserted, rather than
    a hopeful list of the shapes someone remembered to scrub.
    """
    assert leak not in _serialized(RuntimeError(leak)), label


def test_one_exception_carrying_every_sentinel_at_once() -> None:
    """The realistic shape: a message with several private things in it."""
    exc = RuntimeError(
        f"failed reading {HOST_PATH}: token={SECRET} while parsing {SOURCE!r} "
        f"(config {CONFIG_PATH}, db {DATABASE_PATH}, also {WINDOWS_PATH})"
    )
    blob = _serialized(exc)
    for leak in (HOST_PATH, SECRET, SOURCE, CONFIG_PATH, DATABASE_PATH, WINDOWS_PATH):
        assert leak not in blob


def test_the_exception_class_name_is_not_a_consolation_prize() -> None:
    """Named because it is the tempting half-measure.

    "Just the type" reads as harmless and is the same mistake in a smaller size:
    a third-party exception class names the package that failed, and it is still
    text the caller cannot act on.
    """
    for exc in (RuntimeError(), FileNotFoundError(2, "gone", HOST_PATH), ValueError("x")):
        blob = _serialized(exc)
        assert type(exc).__name__ not in blob


def test_exception_chaining_does_not_reach_the_wire() -> None:
    """Chaining is for a developer reading a traceback, not for the envelope."""
    try:
        try:
            raise FileNotFoundError(2, "No such file or directory", HOST_PATH)
        except FileNotFoundError as inner:
            raise RuntimeError(f"while handling: {SECRET}") from inner
    except RuntimeError as outer:
        blob = _serialized(outer)
        assert HOST_PATH not in blob
        assert SECRET not in blob
        assert "Traceback" not in blob
        assert "__cause__" not in blob


def test_an_unmapped_repoatlas_error_is_sanitized_too() -> None:
    """It reaches the same code, so it gets the same envelope.

    RepoAtlas wrote the words, but it wrote them for a condition the mapping
    table does not recognize --- so nothing downstream knows what they mean, and
    ``INTERNAL_ERROR`` has already said the only actionable thing.
    """
    exc = RepoAtlasError(f"cannot open {DATABASE_PATH}", detail=f"tried {CONFIG_PATH}")
    error = capability_error(exc)
    assert error.code == "INTERNAL_ERROR"
    assert error.message == INTERNAL_ERROR_MESSAGE
    blob = _serialized(exc)
    assert DATABASE_PATH not in blob
    assert CONFIG_PATH not in blob


# ---------------------------------------------------------------------------
# Recognized failures keep every word
# ---------------------------------------------------------------------------


def test_a_mapped_repository_error_is_unchanged() -> None:
    """CASE 7."""
    exc = ContextResolutionError(
        "Repository name is ambiguous: common",
        reason=ContextFailure.AMBIGUOUS,
        candidates=("teamA/common", "teamB/common"),
    )
    error = capability_error(exc)
    assert error.code == "REPOSITORY_AMBIGUOUS"
    assert error.message == "Repository name is ambiguous: common"
    assert error.context["candidates"] == ["teamA/common", "teamB/common"]
    assert error.context["reason"] == "ambiguous"


def test_a_repository_relative_path_is_not_censored() -> None:
    """The distinction the correction rests on.

    ``src/config.py`` is a natural identifier the caller asked about. A rule that
    removed it along with the host paths would have made the envelope useless to
    protect it.
    """
    exc = FileNotIndexedError("app/src/config.py is not indexed", detail="Run sync for app first.")
    error = capability_error(exc)
    assert error.code == "FILE_NOT_INDEXED"
    assert "app/src/config.py" in error.message
    assert error.detail == "Run sync for app first."


def test_sync_failed_keeps_everything_a_caller_acts_on() -> None:
    """CASE 8. The index moved, and that fact must survive intact."""
    exc = SyncFailedError(cause="INDEX_UNAVAILABLE", revalidation={"checked": 3, "stale": 1})
    error = capability_error(exc)
    assert error.code == "SYNC_FAILED"
    assert error.context["index_may_have_changed"] is True
    assert error.context["cause"] == "INDEX_UNAVAILABLE"
    assert error.context["revalidation"] == {"checked": 3, "stale": 1}
    assert error.detail
    assert error.retryable is False


def test_sync_in_progress_stays_retryable() -> None:
    """CASE 9. The only condition where waiting is the remedy."""
    error = capability_error(SyncInProgressError("another sync holds the lock"))
    assert error.code == "SYNC_IN_PROGRESS"
    assert error.category is ErrorCategory.CONTENTION
    assert error.retryable is True
    assert error.message == "another sync holds the lock"


def test_unknown_investigation_keeps_its_deliberate_single_answer() -> None:
    error = capability_error(UnknownInvestigationError("investigation-nobody-issued"))
    assert error.code == "UNKNOWN_INVESTIGATION"
    assert error.category is ErrorCategory.SESSION
    assert error.detail == "Investigations do not outlive the process. Start a new one."


def test_cursor_errors_keep_their_remedies() -> None:
    invalid = capability_error(InvalidCursorError())
    stale = capability_error(StaleCursorError())
    assert invalid.code == "INVALID_CURSOR"
    assert stale.code == "STALE_CURSOR"
    assert invalid.code != stale.code
    for error in (invalid, stale):
        assert error.detail
        assert error.retryable is False


def test_ledger_full_keeps_its_figures() -> None:
    error = capability_error(LedgerCapacityError(required=3, available=2, capacity=500))
    assert error.code == "LEDGER_FULL"
    assert error.context == {"required": 3, "available": 2, "capacity": 500}


def test_distinct_conditions_keep_distinct_codes() -> None:
    """The mutation this guards is flattening everything into INTERNAL_ERROR.

    Sanitizing one code is a short step from sanitizing the envelope, and a
    mapper that lost the table would pass every individual privacy test here.
    """
    codes = {
        capability_error(exc).code
        for exc in (
            ContextResolutionError("x", reason=ContextFailure.AMBIGUOUS),
            ContextResolutionError("x", reason=ContextFailure.UNKNOWN_REPOSITORY),
            FileNotIndexedError("x"),
            InvalidCursorError(),
            StaleCursorError(),
            SyncFailedError(),
            SyncInProgressError("x"),
            UnknownInvestigationError("x"),
            LedgerCapacityError(required=1, available=0, capacity=1),
            RuntimeError("x"),
        )
    }
    assert len(codes) == 10
