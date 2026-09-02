"""The Typer application (V1_SPEC §4).

The CLI resolves context, calls one service and renders. It contains no indexing
logic (ARCHITECTURE.md §1 rule 3).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from repoatlas import services
from repoatlas.errors import ExitCode, ReasoningModelUnavailableError, RepoAtlasError
from repoatlas.rendering import render_evidence, render_repos, render_status, render_sync
from repoatlas.services.workspace import Workspace, open_workspace

app = typer.Typer(
    name="repoatlas",
    help="Local multi-repository engineering intelligence.",
    add_completion=False,
    no_args_is_help=True,
)

_JsonOption = Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")]
_VerboseOption = Annotated[
    bool, typer.Option("--verbose", "-v", help="Print diagnostics to stderr.")
]
_RepoOption = Annotated[
    str | None, typer.Option("--repo", help="Explicit repository; overrides the current directory.")
]


def _fail(error: RepoAtlasError) -> None:
    typer.echo(error.render(), err=True)
    raise typer.Exit(error.exit_code)


def _emit(payload: object, text: str, as_json: bool) -> None:
    if as_json:
        typer.echo(_to_json(payload))
    else:
        typer.echo(text)


def _to_json(payload: object) -> str:
    import json

    from pydantic import BaseModel

    if isinstance(payload, BaseModel):
        return payload.model_dump_json(indent=2)
    if isinstance(payload, list) and payload and isinstance(payload[0], BaseModel):
        return json.dumps([item.model_dump(mode="json") for item in payload], indent=2)
    return json.dumps(payload, indent=2, default=str)


def _resolve(
    workspace: Workspace,
    repository: str | None,
    command: str,
    verbose: bool,
):
    context = workspace.resolver().resolve(Path.cwd(), repository, command=command)
    if verbose:
        typer.echo(f"context: {context.reason}", err=True)
    return context


@app.command()
def init(
    directory: Annotated[Path, typer.Argument(help="Directory containing Git repositories.")],
    json_output: _JsonOption = False,
) -> None:
    """Discover and index every Git repository beneath DIRECTORY."""
    try:
        workspace = open_workspace(require_root=False)
    except RepoAtlasError as exc:
        _fail(exc)
        return
    try:
        report, count = services.initialize(workspace, directory)
    except RepoAtlasError as exc:
        _fail(exc)
        return
    finally:
        pass

    if json_output:
        typer.echo(_to_json(report))
    else:
        typer.echo(f"Root: {directory.expanduser().resolve()}")
        typer.echo(f"Repositories discovered: {count}")
        typer.echo("")
        typer.echo(render_sync(report))
    workspace.close()


@app.command()
def sync(
    repository: Annotated[str | None, typer.Argument(help="Repository to sync.")] = None,
    repo: _RepoOption = None,
    full: Annotated[bool, typer.Option("--full", help="Rehash every file.")] = False,
    json_output: _JsonOption = False,
    verbose: _VerboseOption = False,
) -> None:
    """Incrementally synchronize the index with the working tree."""
    workspace = _open()
    try:
        context = _resolve(workspace, repo or repository, "sync", verbose)
        report = services.sync(workspace, context, full=full)
    except RepoAtlasError as exc:
        _fail(exc)
        return
    _emit(report, render_sync(report), json_output)
    workspace.close()


@app.command()
def status(
    repository: Annotated[str | None, typer.Argument(help="Repository to report on.")] = None,
    repo: _RepoOption = None,
    json_output: _JsonOption = False,
    verbose: _VerboseOption = False,
) -> None:
    """Show index status for a repository or the whole workspace."""
    workspace = _open()
    try:
        context = _resolve(workspace, repo or repository, "status", verbose)
        report = services.status(workspace, context)
    except RepoAtlasError as exc:
        _fail(exc)
        return
    _emit(report, render_status(report), json_output)
    workspace.close()


@app.command()
def repos(json_output: _JsonOption = False) -> None:
    """List managed repositories."""
    workspace = _open()
    try:
        found = services.list_repositories(workspace)
    except RepoAtlasError as exc:
        _fail(exc)
        return
    _emit(found, render_repos(found), json_output)
    workspace.close()


@app.command()
def locate(
    question: Annotated[str, typer.Argument(help="What to locate.")],
    repo: _RepoOption = None,
    json_output: _JsonOption = False,
    verbose: _VerboseOption = False,
) -> None:
    """Find where something is implemented or configured. Requires no LLM."""
    workspace = _open()
    try:
        context = _resolve(workspace, repo, "locate", verbose)
        evidence = services.locate(workspace, question, context)
    except RepoAtlasError as exc:
        _fail(exc)
        return
    _emit(evidence, render_evidence(evidence), json_output)
    workspace.close()


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="Engineering question.")],
    repo: _RepoOption = None,
    json_output: _JsonOption = False,
    verbose: _VerboseOption = False,
) -> None:
    """Answer an engineering question with grounded evidence."""
    _reasoning_command(question, repo, json_output, verbose)


@app.command()
def debug(
    question: Annotated[str, typer.Argument(help="Symptom or error to investigate.")],
    repo: _RepoOption = None,
    json_output: _JsonOption = False,
    verbose: _VerboseOption = False,
) -> None:
    """Investigate a failure, separating facts from hypotheses."""
    _reasoning_command(question, repo, json_output, verbose)


def _reasoning_command(question: str, repo: str | None, json_output: bool, verbose: bool) -> None:
    """``ask`` and ``debug`` need a reasoning model, which Slice B provides.

    Failing with exit code 4 and a clear message is the specified behavior when no
    model is available (V1_SPEC §4); returning evidence-only output here would
    silently answer a different question than the one asked.
    """
    del question, repo, json_output, verbose
    _fail(
        ReasoningModelUnavailableError(
            "No reasoning model is configured.",
            detail=(
                "`ask` and `debug` require a local reasoning model, which arrives in\n"
                "Slice B of docs/V1_SPEC.md.\n\n"
                "Available now, with no model required:\n\n"
                '  repoatlas locate "<question>"'
            ),
        )
    )


def _open() -> Workspace:
    try:
        return open_workspace()
    except RepoAtlasError as exc:
        typer.echo(exc.render(), err=True)
        raise typer.Exit(exc.exit_code) from exc


def run() -> None:
    """Console-script entry point."""
    try:
        app()
    except RepoAtlasError as exc:  # pragma: no cover - defensive
        print(exc.render(), file=sys.stderr)
        raise SystemExit(ExitCode.RUNTIME) from exc


if __name__ == "__main__":  # pragma: no cover
    run()
