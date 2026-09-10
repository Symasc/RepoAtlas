"""The vendor-neutral capability layer (RepoAtlas V2).

What an external agent talks to. It sits **above** ``services/`` and may use
everything below it; nothing below it may use this package. That arrow is
enforced in ``tests/test_layering.py`` rather than left to review, because the
tempting violation is small and one-directional: a service wanting one typed
value from here would reverse the layering for a single import.

Three things are forbidden by the same test and for one reason — this layer must
stay usable with no terminal and no model attached:

``cli``
    a capability call has no argv, no exit status and no terminal.
``rendering``
    it returns models, and formatting them is the caller's business.
``llm``
    every capability is deterministic. A capability that could reach a model
    could manufacture evidence, which is the one thing RepoAtlas may never do.

The application boundary is :class:`CapabilityService`. It owns one workspace,
one investigation registry and one lock for its whole life, and every operation
goes through it. There is no module-level function that opens a database, because
a second lifecycle is how two answers to "is this index usable?" get shipped.

Capabilities appear here one slice at a time, and only when they are complete. An
operation that exists as a stub is worse than one that does not exist: a caller
cannot tell a stub from a capability that found nothing.
"""

from repoatlas.capabilities.service import CapabilityService

__all__ = ["CapabilityService"]
