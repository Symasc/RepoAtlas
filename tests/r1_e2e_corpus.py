"""The R1 provider-evidence corpus for the graph explorer's end-to-end run (R1-UI).

Test data only. The repositories are the ones of ``tests/r1_provider_corpus.py``, imported and
never copied, so the browser sees exactly what the backend tests and the golden snapshot pin:

* ``pyprov``, ``buyer2``, ``twinprov`` and ``emptyprov`` (the ``py`` corpus): ``buyer2`` requests
  ``GET /orders/42`` and ``GET /nothing/here`` from ``https://orders.example.test``. ``pyprov``
  declares ``/orders/{oid}`` where nothing mounts it (a hint), and ``twinprov`` declares two
  unmounted routes of that shape (two hints). ``emptyprov`` recognizes no route.
* ``nestok``, ``buyer3`` and ``nestprov`` (from the ``nest`` corpus): ``buyer3`` requests
  ``/health`` and ``/shop/items/42`` from ``https://nest.example.test``. ``nestok`` serves both,
  one of them composed (no declaration line); ``nestprov`` imports ``RouterModule``, which withholds
  its routes.
* ``fhelp`` (from the ``fastify`` corpus): a file at the Fastify helper-call cap.

``python -m tests.r1_e2e_corpus <dir>`` indexes them into ``<dir>`` exactly as
``python -m tests.scoped_e2e_corpus`` does, leaving ``config.yaml`` and ``state/atlas.db`` there
for ``repoatlas serve`` (via REPOATLAS_CONFIG and REPOATLAS_DB).
"""

from __future__ import annotations

from pathlib import Path

from tests.r1_provider_corpus import CORPUS_REPOS
from tests.topology_corpus import open_corpus

#: The nest repositories the end-to-end run uses; ``nestbare`` and ``nestcomposed`` are left out.
NEST = ("nestok", "buyer3", "nestprov")


def repos() -> dict[str, dict[str, str]]:
    nest = CORPUS_REPOS["nest"]()
    return {
        **CORPUS_REPOS["py"](),
        **{name: nest[name] for name in NEST},
        "fhelp": CORPUS_REPOS["fastify"]()["fhelp"],
    }


def main(argv: list[str] | None = None) -> None:
    """Build the R1 corpus for the graph explorer's end-to-end run.

    ``<dir>`` must be absent or empty.
    """
    import sys

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise SystemExit("usage: python -m tests.r1_e2e_corpus <empty directory>")
    root = Path(args[0])
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"{root} is not empty")
    root.mkdir(parents=True, exist_ok=True)
    open_corpus(root, repos()).close()


if __name__ == "__main__":
    main()
