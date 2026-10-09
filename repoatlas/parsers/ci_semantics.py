"""The actions RepoAtlas gives semantics to, by exact revision (DF1_PHASE_A.md §3, D1-5).

An action reference is ``owner/name[/path]@ref``. It is recognised only as
``owner/name@vN`` or ``owner/name@vN.M[.P]`` with ``N`` listed below. A commit SHA, a
branch (``@main``, ``@release/v1``), an unlisted major, a ``docker://`` image or a
``./local`` action is **unsupported**: it infers no build, release, deployment or image,
and it is recorded and decided as such (``ci_step unsupported-action``). A SHA is not
mapped to the version it may correspond to: no exact, independently supported mapping
exists, so none is guessed.

* **Builtin** (basis ``builtin``): ``docker/build-push-action`` v5 and v6. Its ``push``
  input decides publication: a literal ``true`` configures publishing, ``false`` or no
  ``push`` does not, and an expression leaves it unresolved. Configured publishing is
  never a claim that an image was pushed or exists in a registry.
* **Inert**: actions that build, publish and deploy nothing -- checkout, the official
  ``setup-*``, cache, artifact upload and download, registry login, buildx and QEMU
  setup. They are recognised so as to produce no decision; their inputs are never read.
"""

from __future__ import annotations

import re

INERT: dict[str, frozenset[int]] = {
    "actions/checkout": frozenset({2, 3, 4}),
    "actions/setup-python": frozenset({2, 3, 4, 5}),
    "actions/setup-node": frozenset({2, 3, 4}),
    "actions/setup-go": frozenset({3, 4, 5}),
    "actions/setup-java": frozenset({3, 4}),
    "actions/cache": frozenset({2, 3, 4}),
    "actions/upload-artifact": frozenset({2, 3, 4}),
    "actions/download-artifact": frozenset({2, 3, 4}),
    "docker/login-action": frozenset({2, 3}),
    "docker/setup-buildx-action": frozenset({2, 3}),
    "docker/setup-qemu-action": frozenset({2, 3}),
}
BUILD_PUSH_ACTION = "docker/build-push-action"
BUILTIN_BUILDS: dict[str, frozenset[int]] = {BUILD_PUSH_ACTION: frozenset({5, 6})}

INERT_ACTION = "inert"
BUILTIN_BUILD = "builtin-build"
UNSUPPORTED = "unsupported"

_REVISION = re.compile(r"v(\d+)(?:\.\d+){0,2}")


def classify_action(reference: str) -> str:
    """:data:`INERT_ACTION`, :data:`BUILTIN_BUILD` or :data:`UNSUPPORTED`."""
    if reference.startswith(("./", "docker://")) or "@" not in reference:
        return UNSUPPORTED
    name, _, revision = reference.partition("@")
    matched = _REVISION.fullmatch(revision)
    if matched is None:
        return UNSUPPORTED
    major = int(matched.group(1))
    if major in INERT.get(name, frozenset()):
        return INERT_ACTION
    if major in BUILTIN_BUILDS.get(name, frozenset()):
        return BUILTIN_BUILD
    return UNSUPPORTED


def action_kind(reference: str) -> str:
    """How an unsupported reference is recorded: ``docker-action``, ``local-action`` or
    ``action``."""
    if reference.startswith("docker://"):
        return "docker-action"
    if reference.startswith("./"):
        return "local-action"
    return "action"
