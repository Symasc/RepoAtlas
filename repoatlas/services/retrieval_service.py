"""Deterministic retrieval, as a service (V1_SPEC §16).

Retrieval is a pure read. A question and a repository context go in, an
:class:`~repoatlas.models.evidence.EvidenceSet` comes out, and nothing on the
path consults a generative model. That was always true --- ``locate`` never
needed one --- but these functions used to live in ``query_service`` beside V1's
embedded reasoner, which gave a deterministic call the shape of an entry point
into model execution and made every deterministic caller import the module that
pulled in an HTTP client.

The embedded reasoner is gone. These two functions are what survived it, and
they are here rather than there because retrieval is the product: an agent asks
RepoAtlas *where*, and RepoAtlas answers with evidence it can point at. The
reasoning happens somewhere else, by something else.

Two modes, which is one more than the CLI exposes:

``locate``
    The configured funnel. What ``repoatlas locate`` runs, and the same bounds
    the capability layer's ``STANDARD`` breadth retrieves under.
``locate_diagnostic``
    The same funnel widened to :data:`DIAGNOSTIC_LIMITS`, with the whole question
    also searched verbatim so an unquoted error string matches the source it was
    raised from. Reached publicly through ``search`` at
    :attr:`~repoatlas.models.search.SearchMode.DIAGNOSTIC`.

The second exists because it is a retrieval mode, not because a command used to
select it. V1 chose between the two with an intent enum belonging to the
reasoner; naming them instead costs one function and removes the last reason for
this module to know that reasoning ever existed.
"""

from __future__ import annotations

from repoatlas.models.context import RepositoryContext
from repoatlas.models.evidence import EvidenceSet
from repoatlas.retrieval.retriever import DIAGNOSTIC_LIMITS
from repoatlas.services.workspace import Workspace


def locate(workspace: Workspace, question: str, context: RepositoryContext) -> EvidenceSet:
    """Retrieve file-backed evidence for a question."""
    return workspace.retriever().retrieve(question, context)


def locate_diagnostic(
    workspace: Workspace, question: str, context: RepositoryContext
) -> EvidenceSet:
    """Retrieve under the widened diagnostic bounds (V1_SPEC §16).

    Biased toward exact error strings: the whole phrase is searched literally,
    which matches log and exception text embedded in source.
    """
    return workspace.retriever(limits=DIAGNOSTIC_LIMITS).retrieve(
        question, context, literal_phrase=True
    )
