"""Management CLI — ``office-layer ...``.

Mostly a thin shell over the same engine the MCP server uses. Useful for
non-Claude-Code workflows (debug, scripting, CI smoke-test).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .engine import get_engine
from .engine.evidence import EvidencePacketBuilder
from .models import SearchQuery, WorkspacePolicy
from .safety import classify_operation

console = Console()


@click.group(help="Local Office Evidence Layer for Claude Code.")
def main() -> None:
    pass


@main.command()
def status() -> None:
    """Show adapter availability and workspace state."""
    engine = get_engine()
    r = engine.registry
    console.rule("[bold]Adapters")
    t = Table(show_header=True, header_style="bold")
    t.add_column("Kind")
    t.add_column("Selected")
    t.add_row("file_discovery", r.file_discovery.name)
    t.add_row("pdf", r.pdf.name if r.pdf else "[red]none[/red]")
    t.add_row("office", ", ".join(a.name for a in r.office) or "[red]none[/red]")
    t.add_row("text", ", ".join(a.name for a in r.text))
    t.add_row("ocr", r.ocr.name if r.ocr else "[yellow]disabled[/yellow]")
    sem_label = "[yellow]disabled[/yellow]"
    if engine.semantic.is_available():
        sem_label = f"{r.semantic_search.name} + {engine.embedder.name}"
    elif r.semantic_search and r.semantic_search.is_available():
        # Backend installed but embedder missing — surface that to the user.
        sem_label = f"{r.semantic_search.name} (no embedder: pip install fastembed)"
    t.add_row("semantic_search", sem_label)
    t.add_row("file_watcher", r.file_watcher.name if r.file_watcher else "[yellow]disabled[/yellow]")
    console.print(t)

    if r.degraded:
        console.rule("[yellow]Degraded modes")
        for d in r.degraded:
            console.print(f"- [bold]{d.adapter_kind}[/bold]: {d.reason}")
            if d.install_hint:
                console.print(f"  install: [dim]{d.install_hint}[/dim]")

    console.rule("[bold]Workspaces")
    workspaces = engine.workspaces.list()
    if not workspaces:
        console.print("(none — run `office-layer workspace add <path>` to register one)")
        return
    t = Table(show_header=True, header_style="bold")
    t.add_column("ID")
    t.add_column("Name")
    t.add_column("Path")
    t.add_column("Policy")
    t.add_column("Status")
    t.add_column("Docs", justify="right")
    for w in workspaces:
        t.add_row(
            w.id,
            w.name,
            w.root_path,
            w.policy if isinstance(w.policy, str) else w.policy.value,
            w.status if isinstance(w.status, str) else w.status.value,
            str(w.document_count),
        )
    console.print(t)


@main.group()
def workspace() -> None:
    """Manage workspaces."""


@workspace.command("add")
@click.argument("root_path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--name", default=None, help="Display name; defaults to folder name.")
@click.option(
    "--policy",
    type=click.Choice(["read-only", "draft-write", "full-write"]),
    default="read-only",
    show_default=True,
)
@click.option("--enable-ocr", is_flag=True, help="Allow OCR on scanned PDFs / images.")
@click.option(
    "--enable-vector",
    is_flag=True,
    help="Enable semantic (vector) search. Needs sqlite-vec + an embedder.",
)
@click.option("--max-size-mb", type=int, default=100, show_default=True)
def workspace_add(
    root_path: Path,
    name: str | None,
    policy: str,
    enable_ocr: bool,
    enable_vector: bool,
    max_size_mb: int,
) -> None:
    engine = get_engine()
    if enable_vector and not engine.semantic.is_available():
        console.print(
            "[yellow]warning:[/yellow] --enable-vector requested but no embedder + sqlite-vec "
            "backend is loaded. Flag will be stored, but indexing falls back to keyword-only "
            "until you `pip install 'claude-code-office-layer[vec-sqlite]' fastembed`."
        )
    ws = engine.workspaces.add(
        name=name or root_path.name,
        root_path=str(root_path),
        policy=WorkspacePolicy(policy),
        enable_ocr=enable_ocr,
        enable_vector_search=enable_vector,
        max_file_size_mb=max_size_mb,
    )
    console.print(f"[green]Added[/green] workspace [bold]{ws.id}[/bold] → {ws.root_path}")


@workspace.command("list")
def workspace_list() -> None:
    for w in get_engine().workspaces.list():
        console.print(
            f"{w.id}  [bold]{w.name}[/bold]  {w.root_path}  policy={w.policy}  docs={w.document_count}"
        )


@workspace.command("remove")
@click.argument("workspace_id")
def workspace_remove(workspace_id: str) -> None:
    ok = get_engine().workspaces.remove(workspace_id)
    if ok:
        console.print(f"[green]Removed[/green] {workspace_id}")
    else:
        console.print(f"[red]Not found[/red]: {workspace_id}")
        sys.exit(1)


@workspace.command("set-vector")
@click.argument("workspace_id")
@click.argument("state", type=click.Choice(["on", "off"]))
def workspace_set_vector(workspace_id: str, state: str) -> None:
    """Toggle semantic (vector) search on a workspace. Re-index after turning on."""
    engine = get_engine()
    ws = engine.workspaces.set_vector_search(workspace_id, state == "on")
    if ws is None:
        console.print(f"[red]Not found[/red]: {workspace_id}")
        sys.exit(1)
    console.print(
        f"[green]Updated[/green] {workspace_id}: enable_vector_search={ws.enable_vector_search}"
    )
    if state == "on" and not engine.semantic.is_available():
        console.print(
            "[yellow]note:[/yellow] no embedder + sqlite-vec available — flag is set but "
            "indexing stays keyword-only until the backend is installed."
        )
    elif state == "on":
        console.print(
            "[dim]Run `office-layer index <workspace_id> --force` to embed existing chunks.[/dim]"
        )


@main.command()
@click.argument("workspace_id")
@click.option("--force", is_flag=True, help="Re-extract all files even if mtime unchanged.")
@click.option("--max-files", type=int, default=None)
def index(workspace_id: str, force: bool, max_files: int | None) -> None:
    """Build or refresh the workspace search index."""
    engine = get_engine()
    progress = engine.indexer.index_workspace(workspace_id, force=force, max_files=max_files)
    console.print(
        f"seen={progress.total_seen}  indexed={progress.total_indexed}  "
        f"skipped={progress.total_skipped}  errors={progress.total_errors}"
    )
    if progress.errors:
        console.print("[yellow]Errors:[/yellow]")
        for path, msg in progress.errors[:25]:
            console.print(f"  {path}: {msg}")


@main.command()
@click.argument("text")
@click.option("--workspace", "workspace_id", default=None)
@click.option("--limit", type=int, default=10)
def search(text: str, workspace_id: str | None, limit: int) -> None:
    """Run a hybrid search."""
    engine = get_engine()
    q = SearchQuery(
        text=text,
        workspace_ids=[workspace_id] if workspace_id else None,
        limit=limit,
    )
    resp = engine.search.search(q)
    console.print(f"[dim]{resp.total} matches in {resp.elapsed_ms:.1f} ms[/dim]")
    for r in resp.results:
        doc = r.document
        loc = ""
        if r.chunk:
            if r.chunk.page_number:
                loc += f"p.{r.chunk.page_number} "
            if r.chunk.sheet_name:
                loc += f"sheet={r.chunk.sheet_name} "
            if r.chunk.cell_range:
                loc += f"{r.chunk.cell_range} "
        console.print(
            f"[bold]{doc.name}[/bold] {loc}[dim]({doc.path})[/dim] score={r.score:.2f}"
        )
        if r.chunk:
            preview = r.chunk.text.strip().splitlines()[:3]
            for line in preview:
                console.print(f"  {line[:120]}")
        if r.reason:
            console.print(f"  [dim]why: {r.reason}[/dim]")


@main.command()
@click.argument("intent")
@click.argument("query")
@click.option("--workspace", "workspace_id", default=None)
@click.option("--max-sources", type=int, default=6)
@click.option("--json-output", "json_output", is_flag=True, help="Emit packet JSON for piping.")
def evidence(intent: str, query: str, workspace_id: str | None, max_sources: int, json_output: bool) -> None:
    """Build an Evidence Packet for Claude / other downstream tools."""
    engine = get_engine()
    q = SearchQuery(
        text=query,
        workspace_ids=[workspace_id] if workspace_id else None,
        limit=max_sources * 2,
    )
    response = engine.search.search(q)
    packet = EvidencePacketBuilder(engine.storage).build(
        intent=intent, results=response, max_sources=max_sources
    )
    if json_output:
        click.echo(packet.model_dump_json(indent=2))
    else:
        console.print(packet.summary_markdown())


@main.group()
def watch() -> None:
    """Background incremental indexing."""


@watch.command("start")
@click.argument("workspace_id")
def watch_start(workspace_id: str) -> None:
    """Start watching a workspace for file-system changes."""
    engine = get_engine()
    try:
        handle = engine.background.start(workspace_id)
    except (RuntimeError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(1)
    console.print(
        f"[green]Watching[/green] {workspace_id} (started_at={handle.started_at.isoformat()})"
    )
    console.print(
        "[dim]The watcher runs in this process and stops when the CLI exits. "
        "For long-running watches, use the MCP server via Claude Code.[/dim]"
    )


@watch.command("stop")
@click.argument("workspace_id")
def watch_stop(workspace_id: str) -> None:
    ok = get_engine().background.stop(workspace_id)
    if ok:
        console.print(f"[green]Stopped[/green] watch for {workspace_id}")
    else:
        console.print(f"[yellow]No active watch for {workspace_id}[/yellow]")


@watch.command("list")
def watch_list() -> None:
    for w in get_engine().background.list_watches():
        console.print(
            f"{w['workspace_id']}  started={w['started_at']}  events={w['events_processed']}  alive={w['alive']}"
        )


@main.command("risk")
@click.argument("operation")
@click.option("--workspace", "workspace_id", default=None)
@click.option("--target", "targets", multiple=True)
def risk_cmd(operation: str, workspace_id: str | None, targets: tuple[str, ...]) -> None:
    """Classify operation risk."""
    engine = get_engine()
    ws = engine.workspaces.get(workspace_id) if workspace_id else None
    r = classify_operation(operation, targets=list(targets), workspace=ws)
    console.print(r.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
