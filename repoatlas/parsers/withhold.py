"""What a deployment fact may never carry: credentials, and values that look like them.

Shared by the CI, values-file, ConfigMap and compose readers (DF1_PHASE_A.md §11). A
withheld value is stored as :data:`WITHHELD`, so the fact still says *that* the key is
set and where, never what to.

* a key path, ``--set`` path, input name or environment name whose last segments name a
  credential (:func:`credential_named`);
* any URL carrying user information (``scheme://user:secret@host``);
* any GitHub ``secrets.*`` expression.
"""

from __future__ import annotations

from repoatlas.models.secrets import credential_named, secret_bearing

__all__ = ["WITHHELD", "credential_named", "guarded", "secret_bearing"]

WITHHELD = "<withheld>"


def guarded(name: str, value: str) -> str:
    """``value``, or :data:`WITHHELD` when the name or the value may carry a secret."""
    return WITHHELD if credential_named(name) or secret_bearing(value) else value
