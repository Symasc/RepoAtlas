"""The built-in secret-exclusion policy (V1_SPEC §17, ARCHITECTURE.md §12).

One module, one question: *does this path name obvious secret material?*

It lives at the top level, beside :mod:`repoatlas.errors`, because it is below
every layer that has to obey it. Enumeration asks it, and so does the git
working-tree comparison, which reads file contents for an entirely different
reason and must still not read these. A second copy of the rule in either place
would be a second policy, and a security control with two implementations has
whichever behaviour the caller happened to reach.

**What this guarantees.** A file whose *name* matches the built-in policy is
rejected from the path alone, before it is opened — so its contents cannot be
parsed, chunked, retrieved, serialized into a prompt, or logged.

**What it does not.** This is filename policy, not content scanning. A credential
pasted into ``app.py`` is indexed like any other line of that file. RepoAtlas
does not run entropy heuristics, provider-specific key regexes, or any other
secret detector over file contents, and V1 does not claim to (V1_SPEC §17).
"""

from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath

from repoatlas.config.settings import SECRET_PATTERNS

#: The built-in patterns, case-folded once at import. Folding the *patterns* too
#: keeps both sides symmetric, so a pattern added later in mixed case behaves.
_FOLDED_SECRET_PATTERNS: tuple[str, ...] = tuple(pattern.casefold() for pattern in SECRET_PATTERNS)


def is_secret_path(rel_path: str) -> bool:
    """Whether ``rel_path`` names obvious secret material.

    Matching is on the **basename**, in **case-folded** form, against the glob
    patterns in ``SECRET_PATTERNS`` — so ``.ENV``, ``Id_Rsa``, ``server.PEM`` and
    ``private.KEY`` are the files they obviously are, at any depth.

    Three details are deliberate:

    ``fnmatchcase`` rather than ``fnmatch``
        ``fnmatch`` runs the name through ``os.path.normcase``, which lowercases
        on Windows and macOS and does nothing on Linux. The *security behaviour
        of identical code therefore differed by platform*, which is how case
        variants got through: on Linux, ``fnmatch("ID_RSA", "id_rsa")`` is
        ``False``. Folding explicitly and matching case-sensitively afterwards
        gives one answer everywhere.

    Glob semantics, not substring search
        ``*.key`` means the basename *ends* with ``.key``. ``monkey.py`` does not
        match it, ``server.pem.txt`` does not match ``*.pem``, and
        ``id_rsa_notes.txt`` is not the private key ``id_rsa``. Over-exclusion
        silently loses real evidence, which is its own kind of wrong answer.

    ``casefold`` rather than ``lower``
        The patterns are ASCII, so for every realistic filename the two agree.
        Where they differ, ``casefold`` folds harder — ``ß`` to ``ss``, the
        Kelvin sign to ``k`` — and for a security comparison the aggressive
        direction is the safe one: it can only exclude a strange filename that
        was not going to be excluded, never admit one.

    The stored path is never rewritten. Folding happens for the comparison only,
    so a file keeps its real spelling wherever it is displayed: identity and
    security matching are different concerns.
    """
    name = PurePosixPath(rel_path).name.casefold()
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in _FOLDED_SECRET_PATTERNS)
