"""Command-line workflow and Rich terminal presentation."""

import argparse
import asyncio
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
from time import perf_counter

from rich.console import Console
from rich.filesize import decimal
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
    TimeElapsedColumn,
)
from rich.prompt import Prompt, IntPrompt, Confirm
from rich.table import Table
from rich.text import Text

from .config import ConfigError, Pattern, load_patterns, save_patterns, new_uid
from .extract import (
    Extractor,
    ExtractionError,
    check_tree,
    identity,
    publish,
    staging_directory,
    SourceNames,
    reserve_directory,
)
from .matching import parse_rule
from .recognize import Archive, recognize, walk_files

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "pattern.yaml"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Extract archives sequentially using YAML regex rules."
    )
    result.add_argument(
        "-p",
        "--path",
        type=Path,
        help="Directory to search (default: current directory).",
    )
    result.add_argument(
        "-o", "--output", type=Path, help="Output directory (default: PATH/extracted)."
    )
    result.add_argument(
        "-s",
        "--save",
        action="store_true",
        help="Keep original archives and all their volumes.",
    )
    result.add_argument(
        "-c",
        "--child",
        action="store_true",
        help="Search child directories, excluding output and symlinks.",
    )
    management = result.add_mutually_exclusive_group()
    management.add_argument(
        "-l", "--list", action="store_true", help="List all configured patterns."
    )
    management.add_argument(
        "-d", "--delete", metavar="UID", help="Delete a pattern by UID."
    )
    management.add_argument(
        "-a", "--add", action="store_true", help="Interactively add a pattern."
    )
    result.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG, help="YAML configuration path."
    )
    result.add_argument(
        "--seven-zip",
        metavar="EXECUTABLE",
        help="7z executable path or command (auto: 7z, then 7zz).",
    )
    return result


def add_pattern(patterns: list[Pattern], console: Console) -> Pattern:
    console.print(
        "Step 1/4: Enter one filename regex, or s/regex/replacement/."
    )
    while True:
        value = Prompt.ask("Pattern", console=console, default="")
        if not value:
            console.print("Enter a nonempty pattern.")
            continue
        try:
            parse_rule(value)
        except (ValueError, re.error) as exc:
            console.print(Text(f"Invalid pattern: {exc}", style="red"))
            continue
        break
    console.print(
        "Step 2/4: Optionally choose a type. Its matching suffix is removed before regex matching; auto matches the full filename."
    )
    kind = Prompt.ask(
        "Type", choices=["auto", "7z", "rar", "zip"], default="auto", console=console
    )
    console.print("Step 3/4: Choose how many nested archive layers to extract.")
    while (layers := IntPrompt.ask("Layers", default=1, console=console)) < 1:
        console.print("Layers must be at least 1.")
    console.print("Step 4/4: Enter passwords in layer order. Empty means no password.")
    passwords = []
    for index in range(layers):
        if index and Confirm.ask(
            "Reuse the last password for all remaining layers?",
            default=True,
            console=console,
        ):
            break
        passwords.append(
            Prompt.ask(
                f"Password for layer {index + 1}",
                default="",
                password=True,
                console=console,
            )
        )
    return Pattern(new_uid(patterns), value, layers, tuple(passwords),
                   None if kind == "auto" else kind)


def password_text(pattern: Pattern) -> str:
    return " → ".join(pattern.password(i) or "(none)" for i in range(pattern.layer))


def list_patterns(patterns: list[Pattern], console: Console) -> None:
    table = Table("UID", "Pattern", "Layers", "Passwords by layer", "Type")
    for pattern in patterns:
        table.add_row(
            Text(pattern.uid),
            Text(pattern.pattern),
            str(pattern.layer),
            Text(password_text(pattern)),
            pattern.type or "auto",
        )
    console.print(table)
    if not patterns:
        console.print("No patterns yet. Run --add to create one.")


@dataclass
class Result:
    archive: Archive
    seconds: float
    payload: Path | None = None
    snapshots: dict | None = None
    destination: Path | None = None
    error: str = ""
    disposition: str = "retained"
    sources: Archive | None = None
    names: SourceNames | None = None


def cleanup_unpublished(result: Result) -> None:
    if result.destination is None and result.payload is not None and result.payload.exists():
        shutil.rmtree(result.payload)


def fail_result(result: Result, error: Exception) -> None:
    result.error = str(error)
    try:
        if result.names is not None:
            result.names.restore()
    except (OSError, ExtractionError) as exc:
        result.error += f"\nFilename restoration failed: {exc}"
    cleanup_unpublished(result)


async def extract_batch(args, patterns: list[Pattern], console: Console) -> int:
    started = perf_counter()
    source = (args.path or Path.cwd()).expanduser().resolve()
    output = (args.output or source / "extracted").expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"Search directory does not exist: {source}")
    if output != source and output in source.parents:
        raise ValueError("Output cannot be an ancestor of the search directory.")
    console.rule("1. recognizing")
    console.print(
        Text(
            f"Searching {source}" + (" (including child folders)" if args.child else "")
        )
    )
    excluded = (args.config.expanduser().resolve(), output / "temp")
    if output != source:
        excluded += (output,)
    files = walk_files(source, args.child, excluded)
    archives, errors = recognize(files, patterns)
    table = Table(
        "Filename / volumes", "Size", "Passwords by layer", "UID / renamed base"
    )
    for archive in archives:
        names = "\n".join(str(part.path.relative_to(source)) for part in archive.parts)
        table.add_row(
            Text(names),
            decimal(archive.size),
            Text(password_text(archive.pattern)),
            Text(f"{archive.pattern.uid} / {archive.name}"),
        )
    console.print(table)
    for error in errors:
        console.print(Text(error, style="red"))
    console.print(f"{len(archives)} archive group(s) recognized.")
    console.rule("2. extracting")
    if not archives:
        console.print(
            "Nothing to extract. Add or adjust patterns with --add or --config."
        )
        console.rule("3. ending")
        console.print(f"No files changed. Total time: {perf_counter() - started:.2f}s.")
        return 1 if errors else 0
    executable = args.seven_zip or shutil.which("7z") or shutil.which("7zz")
    if not executable or not shutil.which(str(executable)):
        raise ValueError(
            "7-Zip not found. Install 7z/7zz or pass --seven-zip /path/to/7z."
        )
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with ExitStack() as stack:
        temporary = (
            stack.enter_context(staging_directory(output))
            if any(archive.pattern.layer > 1 for archive in archives)
            else None
        )
        extractor = Extractor(str(executable), temporary)
        console.print(
            "Extracting directly from source files; failed renames are restored."
        )
        with Progress(
            SpinnerColumn(),
            TextColumn("{task.description}", markup=False),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            for archive in archives:
                tick = perf_counter()
                result = Result(archive, 0)
                task = progress.add_task(archive.primary.name, total=100)

                def update(depth, percent):
                    progress.update(
                        task,
                        completed=percent,
                        description=f"{archive.primary.name} · layer {depth + 1}/{archive.pattern.layer}",
                    )

                try:
                    result.names = stack.enter_context(SourceNames())
                    result.sources = result.names.prepare(archive)
                    result.snapshots = result.names.snapshots
                    result.payload = reserve_directory(output, archive.name)
                    stack.callback(cleanup_unpublished, result)
                    await extractor.extract(
                        result.sources,
                        archive.pattern.layer,
                        archive.pattern.passwords,
                        update,
                        destination=result.payload,
                    )
                except (OSError, ExtractionError, ValueError) as exc:
                    fail_result(result, exc)
                    progress.update(
                        task, description=f"{archive.primary.name} · FAILED"
                    )
                result.seconds = perf_counter() - tick
                results.append(result)
        console.rule("3. ending")
        console.print(
            "Checking output folders, placing successful results, and handling originals."
        )
        for result in results:
            if result.payload is None or result.error:
                continue
            tick = perf_counter()
            try:
                check_tree(result.payload)
                result.destination = publish(
                    result.payload, output, result.archive.name
                )
                if not args.save:
                    # Refuse to delete a source that changed while extraction was running.
                    if any(
                        identity(path) != snapshot
                        for path, snapshot in result.snapshots.items()
                    ):
                        raise ExtractionError(
                            "Source changed during extraction; originals retained."
                        )
                    removed = 0
                    try:
                        for part in result.sources.parts:
                            part.path.unlink()
                            removed += 1
                    finally:
                        result.disposition = (
                            f"deleted {removed}/{len(result.archive.parts)} volume(s)"
                        )
                else:
                    result.disposition = "saved (-s): " + ", ".join(
                        part.path.name for part in result.sources.parts
                    )
                result.names.committed = True
            except (OSError, ExtractionError) as exc:
                fail_result(result, exc)
            result.seconds += perf_counter() - tick
        report = Table("Archive", "Result / originals", "Where", "Time")
        for result in results:
            status = (
                ("ERROR: " + result.error if result.error else "OK")
                + "\n"
                + result.disposition
            )
            report.add_row(
                Text(result.archive.primary.name),
                Text(status),
                Text(str(result.destination) if result.destination else "—"),
                f"{result.seconds:.2f}s",
            )
        console.print(report)
    failures = len(errors) + sum(bool(result.error) for result in results)
    console.print(
        f"{sum(not r.error for r in results)} succeeded; {failures} error(s). "
        f"Total time: {perf_counter() - started:.2f}s."
    )
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    if (args.list or args.delete or args.add) and (
        args.path or args.output or args.save or args.child or args.seven_zip
    ):
        argument_parser.error(
            "Pattern management flags cannot be combined with extraction flags."
        )
    console = Console()
    args.config = args.config.expanduser().resolve()
    try:
        patterns = load_patterns(args.config)
        for pattern in patterns:
            try:
                parse_rule(pattern.pattern)
            except (ValueError, re.error) as exc:
                raise ConfigError(
                    f"{pattern.uid}: invalid pattern {pattern.pattern!r}: {exc}"
                ) from exc
        if args.list:
            list_patterns(patterns, console)
        elif args.delete:
            retained = [pattern for pattern in patterns if pattern.uid != args.delete]
            if len(retained) == len(patterns):
                raise ConfigError(f"Unknown pattern UID: {args.delete}")
            save_patterns(args.config, retained)
            console.print(Text(f"Deleted pattern {args.delete}."))
        elif args.add:
            pattern = add_pattern(patterns, console)
            save_patterns(args.config, [*patterns, pattern])
            console.print(Text(f"Added pattern {pattern.uid} to {args.config}"))
        else:
            return asyncio.run(extract_batch(args, patterns, console))
        return 0
    except (ConfigError, ExtractionError, OSError, ValueError, re.error) as exc:
        console.print(Text(f"Error: {exc}", style="red"))
        return 1
    except (KeyboardInterrupt, EOFError):
        console.print("Cancelled. Unfinished archives are retained.")
        return 130
