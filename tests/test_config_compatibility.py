"""The configuration compatibility policy, stated rather than inherited (F-02).

An old RepoAtlas configuration file loads into this build. The keys it carries
that this build no longer understands --- a V1 ``llm:`` section, above all ---
are ignored rather than refused, because refusing to start over a setting that
now controls nothing would cost a user their workspace to protect them from
nothing.

That behaviour was already true and already tested (``test_no_embedded_reasoner``
CASES 5-6). What it was *not* was chosen: it fell out of ``extra="ignore"``,
which is what Pydantic happens to default to. A policy that exists only as a
framework default is one nobody decided, nothing records, and a dependency
upgrade can revoke without a line of RepoAtlas changing.

So :class:`~repoatlas.config.settings.Settings` now declares it, and these tests
hold the declaration in place. The distinction they turn on is exact: Pydantic
puts ``extra`` into ``model_config`` only when a model states it, so
``"extra" in Settings.model_config`` is the difference between a decision and an
accident, and the first test fails the moment the keyword is deleted --- even
though deleting it changes no behaviour at all today.

What this is not is validation. Tolerance at the top level means a misspelled
section is ignored exactly as silently as a retired one; that is the cost of the
policy and is pinned here rather than papered over.
"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml
from repoatlas.config.settings import Settings, load_settings, save_settings

#: A complete V1 reasoner section, as a 0.1.x config file really carried it.
LEGACY_LLM_BLOCK = {
    "provider": "ollama",
    "model": "gpt-oss:20b",
    "base_url": "http://localhost:11434",
    "timeout_seconds": 120.0,
    "max_tokens": 4096,
}

PRODUCTION = Path(__file__).resolve().parent.parent / "repoatlas"


def _write(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The policy itself
# ---------------------------------------------------------------------------


def test_the_tolerance_policy_is_declared_and_not_inherited() -> None:
    """The whole of F-02.

    ``model_config`` carries ``extra`` only when the model states it: a class
    declaring just ``ConfigDict(frozen=True)`` has no ``extra`` key at all. So
    this asserts authorship, not behaviour --- remove the keyword and every other
    test in this file still passes, while this one fails.
    """
    assert "extra" in Settings.model_config, (
        "Settings must state its compatibility policy rather than inherit it "
        "from Pydantic's default (F-02)"
    )
    assert Settings.model_config["extra"] == "ignore"


def test_the_declaration_is_in_the_source_and_not_assembled_at_runtime() -> None:
    """Stated on the class, where a reader of the model finds it.

    A policy computed in a factory or patched on after import is one that does
    not appear where somebody editing the settings model would look.
    """
    tree = ast.parse((PRODUCTION / "config" / "settings.py").read_text(encoding="utf-8"))
    settings_class = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "Settings"
    )
    config_calls = [
        node
        for node in ast.walk(settings_class)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ConfigDict"
    ]
    assert len(config_calls) == 1, "Settings declares exactly one ConfigDict"
    keywords = {kw.arg: kw.value for kw in config_calls[0].keywords}
    assert "extra" in keywords, "extra= is written on the class, not inherited"
    extra = keywords["extra"]
    assert isinstance(extra, ast.Constant) and extra.value == "ignore"
    # The other half of the existing contract is not disturbed by saying so.
    assert "frozen" in keywords


def test_the_reason_for_the_policy_is_written_down() -> None:
    """A bare ``extra="ignore"`` is as unexplained as the default it replaces."""
    doc = Settings.__doc__ or ""
    assert "compatibility" in doc.lower()
    assert "llm" in doc.lower(), "the retired section the policy exists for is named"
    # And the limit of the policy is stated, so nobody reads it as validation.
    assert "not" in doc.lower() and "validation" in doc.lower()


# ---------------------------------------------------------------------------
# CASE A / CASE B. An old file loads, and the settings that survived are intact
# ---------------------------------------------------------------------------


def test_case_a_a_v1_config_with_a_full_retired_llm_block_loads(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "config.yaml",
        {
            "repos": {"root": str(tmp_path / "repos")},
            "llm": LEGACY_LLM_BLOCK,
            "embeddings": {"provider": "hashing", "enabled": True},
            "output": {"verbosity": "concise"},
        },
    )

    settings = load_settings(config)

    assert settings.repos.root == tmp_path / "repos"
    assert not hasattr(settings, "llm")


def test_case_b_the_deterministic_settings_keep_their_values(tmp_path: Path) -> None:
    """Tolerance must not be quietly reached by discarding the whole file."""
    config = _write(
        tmp_path / "config.yaml",
        {
            "repos": {"root": str(tmp_path / "repos")},
            "llm": LEGACY_LLM_BLOCK,
            "embeddings": {"provider": "hashing", "dim": 384, "enabled": False},
            "retrieval": {"final": 7, "token_budget": 4321},
            "storage": {"lock_timeout_seconds": 12.5},
            "output": {"verbosity": "concise", "max_evidence": 5},
        },
    )

    settings = load_settings(config)

    assert settings.embeddings.provider == "hashing"
    assert settings.embeddings.enabled is False
    assert settings.retrieval.final == 7
    assert settings.retrieval.token_budget == 4321
    assert settings.storage.lock_timeout_seconds == 12.5
    assert settings.output.max_evidence == 5


# ---------------------------------------------------------------------------
# CASE C. Tolerance is one-way
# ---------------------------------------------------------------------------


def test_case_c_re_saving_drops_the_retired_block(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "config.yaml",
        {"repos": {"root": str(tmp_path)}, "llm": LEGACY_LLM_BLOCK, "ollama": {"port": 11434}},
    )

    save_settings(load_settings(config), config)

    rewritten = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert "llm" not in rewritten
    assert "ollama" not in rewritten
    assert rewritten["repos"]["root"] == str(tmp_path)


# ---------------------------------------------------------------------------
# CASE D. The policy is about unknown keys, not about one known-retired name
# ---------------------------------------------------------------------------


def test_case_d_an_arbitrary_unknown_top_level_key_is_ignored(tmp_path: Path) -> None:
    """``llm`` is not special-cased anywhere, and this is how that is visible.

    A future build's key reaching an older one is the same situation as a retired
    key reaching a newer one, and the same tolerance covers both.
    """
    config = _write(
        tmp_path / "config.yaml",
        {
            "repos": {"root": str(tmp_path)},
            "reranker": {"provider": "cross-encoder"},
            "telemetry": {"enabled": True},
            "a_key_from_2030": [1, 2, 3],
        },
    )

    settings = load_settings(config)

    assert settings.repos.root == tmp_path
    for absent in ("reranker", "telemetry", "a_key_from_2030"):
        assert not hasattr(settings, absent)


def test_case_d_nothing_in_production_special_cases_the_retired_section() -> None:
    """No executable branch names ``llm``. The tolerance is structural."""
    offenders: list[str] = []
    for path in sorted((PRODUCTION / "config").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(ast.get_docstring(node, clean=False))
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node.value) in docstrings:
                continue
            if "llm" in node.value.lower():
                offenders.append(f"{path.name}:{node.lineno}: {node.value!r}")
    assert not offenders, offenders


# ---------------------------------------------------------------------------
# CASE E. Nested models are untouched by this milestone
# ---------------------------------------------------------------------------


def test_case_e_nested_models_do_not_declare_a_policy_of_their_own() -> None:
    """Deliberately: tightening them would be a behaviour change, not a statement.

    A nested ``extra="forbid"`` would turn today's silently-ignored typo into a
    startup failure. That is an improvement somebody has to choose on purpose,
    and choosing it is not what F-02 was.
    """
    for name in ("repos", "discovery", "indexing", "embeddings", "retrieval", "storage"):
        nested = Settings.model_fields[name].annotation
        assert nested is not None
        assert "extra" not in nested.model_config, f"{name} gained an explicit policy"


def test_case_e_an_unknown_nested_key_is_still_ignored(tmp_path: Path) -> None:
    """Pins the current behaviour rather than asserting it is the right one."""
    config = _write(
        tmp_path / "config.yaml",
        {"embeddings": {"provider": "hashing", "privder": "typo", "unknown_knob": 3}},
    )

    settings = load_settings(config)

    assert settings.embeddings.provider == "hashing"
    assert not hasattr(settings.embeddings, "privder")


# ---------------------------------------------------------------------------
# CASE F / CASE G. What a fresh install writes, and what it never reads
# ---------------------------------------------------------------------------


def test_case_f_generated_configuration_names_no_retired_subsystem(tmp_path: Path) -> None:
    config = save_settings(Settings(), tmp_path / "config.yaml")
    written = config.read_text(encoding="utf-8").lower()

    for marker in ("llm", "ollama", "reasoner", "gpt-oss", "11434", "base_url"):
        assert marker not in written, marker
    # The deterministic sections are present, which is what makes the absence
    # above evidence of removal rather than of an empty file.
    parsed = yaml.safe_load(written)
    for section in ("embeddings", "retrieval", "storage", "indexing"):
        assert section in parsed


def test_case_f_the_schema_publishes_no_retired_section() -> None:
    """Read from the generated schema, so a field defaulting to ``None`` cannot
    pass by being absent from a dump."""
    schema = Settings.model_json_schema()

    assert "llm" not in schema["properties"]
    # Every nested settings model, by name and by definition --- a retired
    # section could survive as a `$defs` entry nothing references yet.
    assert not [name for name in schema.get("$defs", {}) if "llm" in name.lower()]
    for name, definition in schema.get("$defs", {}).items():
        assert "llm" not in definition.get("properties", {}), name

    # Tolerance is not published as a promise about arbitrary keys either: an
    # `extra="ignore"` model says nothing about additionalProperties, and a
    # consumer of the schema must not read the policy as a guarantee.
    assert "additionalProperties" not in schema


def test_case_g_no_executable_production_reference_to_the_retired_variable() -> None:
    """``REPOATLAS_LLM_BASE_URL`` stays unread across the whole package.

    ``test_no_embedded_reasoner`` asserts this too, from the removal's side. It is
    repeated here from the configuration policy's side, because the tolerant load
    path is exactly where a well-meaning "read the old variable for compatibility"
    would be added.
    """
    offenders: list[str] = []
    for path in sorted(PRODUCTION.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(ast.get_docstring(node, clean=False))
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node.value) in docstrings:
                continue
            if "REPOATLAS_LLM" in node.value:
                offenders.append(f"{path}:{node.lineno}")
    assert not offenders, offenders
