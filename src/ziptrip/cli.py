"""Typer commands for building and inspecting cross-platform zipapps."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from ziptrip import __version__
from ziptrip.builder import BuildError, BuildOptions, build_archive, inspect_archive
from ziptrip.targets import TARGETS, get_target

app = typer.Typer(
    help="Build Python zipapps for other platforms using Rust-style targets.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
console = Console()
errors = Console(stderr=True)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"ziptrip {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Show the version.")
    ] = False,
) -> None:
    pass


@app.command()
def targets(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List supported Rust target triples and platform baselines."""
    values = sorted(TARGETS.values(), key=lambda target: target.triple)
    if json_output:
        typer.echo(json.dumps([target.as_dict() for target in values], indent=2))
        return
    table = Table("Target", "Platform baseline", title="Ziptrip targets")
    for target in values:
        table.add_row(target.triple, target.baseline)
    console.print(table)
    console.print("Each build also needs --python-version. Compatible wheels must be available.")


@app.command()
def build(
    target: Annotated[str, typer.Option("--target", "-t", help="Rust target triple.")],
    python_version: Annotated[
        str, typer.Option("--python-version", "-p", help="Target CPython version, e.g. 3.12.")
    ],
    output: Annotated[Path, typer.Option("--output", "-o", help="Output .pyz path.")],
    packages: Annotated[
        list[str] | None,
        typer.Argument(help="Package requirements or local project directories (e.g. .)."),
    ] = None,
    source: Annotated[
        Path | None, typer.Option("--source", "-s", help="Application source directory to bundle.")
    ] = None,
    entry_point: Annotated[
        str | None,
        typer.Option("--entry-point", "-e", help="Function to call, e.g. myapp.cli:main."),
    ] = None,
    module: Annotated[
        str | None, typer.Option("--module", "-m", help="Module to run as python -m MODULE.")
    ] = None,
    console_script: Annotated[
        str | None, typer.Option("--console-script", help="Console script from a bundled wheel.")
    ] = None,
    requirements: Annotated[
        list[Path] | None,
        typer.Option(
            "--requirements", "-r", help="Requirements file or pyproject.toml; repeatable."
        ),
    ] = None,
    constraints: Annotated[
        list[Path] | None, typer.Option("--constraint", "-c", help="Constraints file; repeatable.")
    ] = None,
    find_links: Annotated[
        list[str] | None,
        typer.Option("--find-links", "-f", help="Wheel directory or URL; repeatable."),
    ] = None,
    index_url: Annotated[
        str | None, typer.Option("--index-url", help="Primary package index URL.")
    ] = None,
    no_index: Annotated[bool, typer.Option(help="Disable package indexes.")] = False,
    offline: Annotated[bool, typer.Option(help="Disable network access.")] = False,
    require_hashes: Annotated[
        bool, typer.Option(help="Require hashes for all dependencies in requirements files.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Replace an existing output file.")
    ] = False,
) -> None:
    """Resolve target wheels and package an executable, self-extracting zipapp."""
    try:
        selected = get_target(target)
        options = BuildOptions(
            target=selected,
            python_version=python_version,
            output=output,
            packages=packages or [],
            requirements=requirements or [],
            constraints=constraints or [],
            source=source,
            entry_point=entry_point,
            module=module,
            console_script=console_script,
            find_links=find_links or [],
            index_url=index_url,
            no_index=no_index,
            offline=offline,
            require_hashes=require_hashes,
            force=force,
        )
        with errors.status(f"Building for {target} / CPython {python_version}…"):
            manifest = build_archive(options)
    except (BuildError, OSError, ValueError) as error:
        errors.print(Text(f"Error: {error}", style="red"))
        raise typer.Exit(1) from None
    console.print(Text(f"Built {output} ({output.stat().st_size:,} bytes)", style="green"))
    console.print(
        f"{len(manifest['distributions'])} distributions, "
        f"{len(manifest['native_files'])} native files. "
        f"Requires CPython {python_version} on {target} ({selected.baseline})."
    )


@app.command("inspect")
def inspect_command(
    archive: Annotated[
        Path, typer.Argument(help="Ziptrip archive to inspect without executing it.")
    ],
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the embedded manifest as JSON.")
    ] = False,
) -> None:
    """Show the target, entry point, bundled distributions, and native files."""
    try:
        manifest = inspect_archive(archive)
    except BuildError as error:
        errors.print(Text(f"Error: {error}", style="red"))
        raise typer.Exit(1) from None
    if json_output:
        typer.echo(json.dumps(manifest, indent=2, sort_keys=True))
        return
    console.print(Text(str(archive), style="bold"))
    console.print(f"Target: {manifest['target']['triple']} ({manifest['target']['baseline']})")
    console.print(f"Python: CPython {manifest['python_version']}")
    entry = manifest["entry_point"]
    console.print(Text(f"Entry point: {entry['kind']} {entry['value']}"))
    table = Table("Distribution", "Version", "Wheel tags")
    for distribution in manifest["distributions"]:
        table.add_row(
            distribution["name"], distribution["version"], "\n".join(distribution["wheel_tags"])
        )
    console.print(table)
    console.print(f"Native files: {len(manifest['native_files'])}")
    for name in manifest["native_files"]:
        console.print(Text(f"  {name}"))
