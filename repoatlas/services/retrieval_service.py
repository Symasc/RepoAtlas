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
    The same funnel run against :data:`DIAGNOSTIC_LIMITS` --- wider per-source
    and final caps, a larger token budget --- with the whole question also
    searched verbatim, so an unquoted error string matches the source it was
    raised from.

The second exists because it is a retrieval mode, not because a command used to
select it. V1 chose between the two with an intent enum belonging to the
reasoner; naming them instead costs one function and removes the last reason for
this module to know that reasoning ever existed.

Neither function is on the capability path, and ``locate_diagnostic`` in
particular is **not** what ``search`` calls at
:attr:`~repoatlas.models.search.SearchMode.DIAGNOSTIC`. That request reaches
:meth:`~repoatlas.capabilities.service.CapabilityService.search`, which takes
:data:`DIAGNOSTIC_LIMITS` as the *bias profile* and then replaces its item and
token caps with the ones the caller's budget actually granted
(:func:`~repoatlas.capabilities.budget.retrieval_limits_for`), and which needs
``retrieve_detailed`` for the character bounds a citation is built from. Same
funnel, same ``literal_phrase=True``, different bounds and a different return
type --- so the two are deliberately not routed through each other, and this
function's limits are its own rather than a description of what an agent gets.

``locate`` is called by ``repoatlas locate`` (through the lazy re-export in
:mod:`repoatlas.services`). ``locate_diagnostic`` has no caller in the shipped
CLI at all: its callers are the evaluation runner and the adversarial suites,
for which a plain :class:`~repoatlas.models.evidence.EvidenceSet` under fixed,
budget-free bounds is exactly the right baseline --- the runner measures
retrieval quality and must not have its caps moved by a budget policy it is not
measuring.
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
