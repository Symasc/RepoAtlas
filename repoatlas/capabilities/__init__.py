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

Slice A establishes vocabulary only: context resolution, the freshness view,
error mapping and budget policy. There is deliberately no service object and no
capability operation yet.
"""
