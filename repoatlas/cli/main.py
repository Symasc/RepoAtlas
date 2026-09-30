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
from repoatlas.errors import ExitCode, RepoAtlasError
from repoatlas.models.evidence import EvidenceSet
from repoatlas.models.reports import SyncReport
from repoatlas.rendering import (
    render_evidence,
    render_repos,
    render_status,
    render_sync,
)
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
    if report.unavailable:
        # Same rule as `sync`: a run that could not establish a repository's file
        # set indexed nothing from it and did not succeed.
        raise typer.Exit(ExitCode.RUNTIME)


@app.command()
def sync(
    repository: Annotated[str | None, typer.Argument(help="Repository to sync.")] = None,
    repo: _RepoOption = None,
    full: Annotated[bool, typer.Option("--full", help="Rehash every file.")] = False,
    embed: Annotated[
        bool,
        typer.Option(
            "--embed",
            help="Download the embedding model if needed, then embed pending chunks.",
        ),
    ] = False,
    json_output: _JsonOption = False,
    verbose: _VerboseOption = False,
) -> None:
    """Incrementally synchronize the index with the working tree."""
    workspace = _open()
    if embed:
        # Fetching the model is an explicit act, never a silent stall inside an
        # ordinary sync (ARCHITECTURE.md §10).
        workspace.settings = workspace.settings.model_copy(
            update={
                "embeddings": workspace.settings.embeddings.model_copy(
                    update={"allow_download": True}
                )
            }
        )
    try:
        context = _resolve(workspace, repo or repository, "sync", verbose)
        report = services.sync(workspace, context, full=full)
    except RepoAtlasError as exc:
        _fail(exc)
        return
    _emit(report, render_sync(report), json_output)
    if verbose:
        _report_sync_telemetry(report)
    workspace.close()
    if report.unavailable:
        # Healthy repositories in the same run kept their facts and are current;
        # this run still did not do what it was asked, so it must not report
        # success. Exit 1 rather than a new code: the taxonomy already means
        # "the command failed", and a wrapper that retries contention (5) or
        # reconfigures a missing model (4) has nothing different to do here
        # (V1_SPEC §4).
        raise typer.Exit(ExitCode.RUNTIME)


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
    """Find where something is implemented or configured."""
    workspace = _open()
    try:
        context = _resolve(workspace, repo, "locate", verbose)
        evidence = services.locate(workspace, question, context)
    except RepoAtlasError as exc:
        _fail(exc)
        return
    _emit(evidence, render_evidence(evidence), json_output)
    if verbose:
        _report_retrieval(evidence)
    workspace.close()


@app.command()
def mcp() -> None:
    """Serve the RepoAtlas capabilities to an agent over MCP on stdio.

    Runs until the transport ends; there is no daemon, no socket and no pidfile.

    Nothing is inferred from where this command was started. It opens the
    configured index and serves it, and every tool call states its own repository
    scope --- which is the point, since a tool launcher starts this process in
    whatever directory it happens to be in, and that directory is evidence about
    nothing.

    Deliberately silent. On this transport stdout is the protocol, so a startup
    banner here would be a framing error rather than a courtesy, and there is no
    ``--json`` for the same reason: the output is already JSON-RPC.

    The import is local because it is the only path that pulls in the MCP SDK.
    Keeping it out of module scope is what lets ``locate``, ``sync`` and the rest
    go on starting without loading a protocol library they never use.
    """
    from repoatlas.mcp.server import serve

    try:
        serve()
    except RepoAtlasError as exc:
        _fail(exc)


@app.command()
def serve(
    host: Annotated[
        str, typer.Option("--host", help="Loopback address: 127.0.0.1, ::1 or localhost.")
    ] = "127.0.0.1",
    port: Annotated[
        int, typer.Option("--port", min=0, max=65535, help="Port; 0 picks a free one.")
    ] = 7317,
) -> None:
    """Serve the RepoAtlas capabilities to a local app over HTTP on loopback.

    Runs until interrupted or terminated. Every request needs the bearer token,
    which is ``REPOATLAS_API_TOKEN`` when set and a fresh random one otherwise; it
    is written, with the URL, to the connection file
    (``REPOATLAS_API_CONNECTION_FILE``, or ``api.json`` beside the index), readable
    by its owner only, and is never printed. See docs/local-api.md.

    The import is local for the same reason as ``mcp``'s.
    """
    from repoatlas.api import server as api

    def announce(serving: api.Serving) -> None:
        typer.echo(f"RepoAtlas local API at {serving.url}", err=True)
        typer.echo(f"connection file: {serving.connection_file}", err=True)

    try:
        api.serve(host=host, port=port, ready=announce)
    except RepoAtlasError as exc:
        _fail(exc)


def _report_sync_telemetry(report: SyncReport) -> None:
    """Stage costs of the global link and embedding passes (V1_SPEC §20)."""
    if report.link_trace is not None:
        trace = report.link_trace
        typer.echo(
            f"link: {trace.duration_ms:.1f}ms over {trace.claims_considered} claim(s), "
            f"{trace.files_indexed} file(s), {trace.symbols_indexed} symbol(s)",
            err=True,
        )
        for stage in trace.stages:
            typer.echo(
                f"  {stage.name:<13} {stage.duration_ms:>8.2f}ms  "
                f"{stage.candidates_in:>6} -> {stage.candidates_out:<6} {stage.detail}",
                err=True,
            )
        if trace.uses is not None:
            work = trace.uses
            typer.echo(
                f"  uses: {work.mode}, {work.files_invalidated} file(s), "
                f"{work.claims_examined} claim(s) examined, {work.edges_deleted} edge(s) "
                f"replaced by {work.edges_inserted}",
                err=True,
            )
    if report.embedding_detail:
        typer.echo(
            f"embeddings: {report.embedded_chunks} embedded, "
            f"{report.pending_embeddings} pending ({report.embedding_detail})",
            err=True,
        )


def _report_retrieval(evidence: EvidenceSet) -> None:
    """Per-stage retrieval instrumentation (ARCHITECTURE.md §10, V1_SPEC §20)."""
    trace = evidence.trace
    if trace is None:
        return
    typer.echo(
        f"retrieval: {trace.total_ms:.1f}ms, {trace.deterministic_candidates} deterministic + "
        f"{trace.associative_candidates} associative, ~{trace.token_estimate} tokens",
        err=True,
    )
    if trace.named_paths:
        # Which files the question identified by their whole path, and therefore
        # which ones took precedence over every less specific match. Printed
        # because "why is this first?" is otherwise unanswerable from the outside
        # (ARCHITECTURE.md §10).
        typer.echo(f"  named path(s): {', '.join(trace.named_paths)}", err=True)
    if trace.suffix_paths:
        # And which ones it identified by a trailing component path. Reported
        # separately: these also outrank every less specific match, but the query
        # may have named several of them and RepoAtlas has not chosen between them.
        typer.echo(f"  suffix path(s): {', '.join(trace.suffix_paths)}", err=True)
    for stage in trace.stages:
        typer.echo(
            f"  {stage.name:<8} {stage.duration_ms:>8.2f}ms  "
            f"{stage.candidates_in:>5} -> {stage.candidates_out:<5} {stage.detail}",
            err=True,
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
