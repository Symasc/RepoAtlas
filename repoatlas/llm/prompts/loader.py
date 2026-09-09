"""Prompt template loading (ARCHITECTURE.md §11).

Prompts are versioned files rather than inline strings, so a prompt change is a
reviewable diff. ``string.Template`` is used rather than ``str.format`` because
templates contain JSON braces that ``format`` would try to interpret.
"""

from __future__ import annotations

from functools import cache
from importlib import resources
from string import Template


@cache
def load(name: str) -> Template:
    """Load ``<name>.md`` from this package."""
    text = (
        resources.files("repoatlas.llm.prompts").joinpath(f"{name}.md").read_text(encoding="utf-8")
    )
    return Template(text)


def render(name: str, **values: str) -> str:
    """Render a template, failing loudly on a missing placeholder."""
    return load(name).substitute(**values)
