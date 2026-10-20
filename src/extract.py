"""Async 7-Zip execution, staged extraction, and collision-safe publication."""

import asyncio
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import tempfile

from .recognize import Archive, groups, walk_files


class ExtractionError(RuntimeError):
    pass


@contextmanager
def staging_directory(output: Path) -> Iterator[Path]:
    """Own output/temp for this run and remove it on success, failure, or cancellation."""
    temporary = output / "temp"
    try:
        temporary.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ValueError(
            f"Temporary path already exists: {temporary}. "
            "Move or remove it before retrying; its contents were left untouched."
        ) from exc
    try:
        yield temporary
    finally:
        shutil.rmtree(temporary)


async def run_7z(
    executable: str,
    args: list[str],
    progress: Callable[[int], None] | None = None,
    line_handler: Callable[[str], None] | None = None,
) -> None:
    process = await asyncio.create_subprocess_exec(
        executable,
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    tail: deque[str] = deque(maxlen=20)
    pending = b""
    try:
        while chunk := await process.stdout.read(4096):
            pending += chunk
            records = re.split(rb"[\r\n\x08]+", pending)
            pending = records.pop()
            for record in records:
                line = record.decode("utf-8", errors="replace")
                if line_handler:
                    line_handler(line)
                if line.strip():
                    tail.append(line)
                if progress and (match := re.search(r"(?<!\d)(\d{1,3})%", line)):
                    progress(min(100, int(match[1])))
        if pending:
            line = pending.decode("utf-8", errors="replace")
            if line_handler:
                line_handler(line)
            tail.append(line)
        code = await process.wait()
        if code != 0:
            raise ExtractionError(f"7-Zip exit {code}: " + "\n".join(tail))
    finally:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


def check_listing_line(line: str) -> None:
    key, separator, value = line.partition(" = ")
    if not separator:
        return
    if key == "Path":
        normalized = value.replace("\\", "/")
        if (
            PurePosixPath(normalized).is_absolute()
            or PureWindowsPath(value).drive
            or ".." in PurePosixPath(normalized).parts
            or ":" in normalized
        ):
            raise ExtractionError(f"Unsafe archive member path: {value}")
    if key in ("Symbolic Link", "Hard Link") and value:
        raise ExtractionError("Archives containing links are not supported.")
    if key in ("Attributes", "Mode") and re.search(r"(^|\s)l[rwx-]{9}", value):
        raise ExtractionError("Archives containing symbolic links are not supported.")


def check_tree(path: Path) -> None:
    for entry in path.iterdir():
        if entry.is_symlink():
            raise ExtractionError(
                f"Extracted symbolic link is not supported: {entry.name}"
            )
        if entry.is_dir():
            check_tree(entry)
        elif not entry.is_file() or entry.stat().st_nlink > 1:
            raise ExtractionError(
                f"Extracted special file/link is not supported: {entry.name}"
            )


def publish(payload: Path, output: Path, name: str) -> Path:
    """Preserve a sole root directory; otherwise wrap contents in archive name."""
    entries = list(payload.iterdir())
    root = entries[0] if len(entries) == 1 and entries[0].is_dir() else payload
    desired = root.name if root != payload else name
    if desired in ("", ".", "..") or "/" in desired or "\\" in desired:
        raise ExtractionError("Invalid output folder name.")
    output.mkdir(parents=True, exist_ok=True)
    number = 1
    while True:
        destination = output / (desired if number == 1 else f"{desired} ({number})")
        try:
            destination.mkdir()
            break
        except FileExistsError:
            number += 1
    moved = []
    try:
        for entry in root.iterdir():
            entry.rename(destination / entry.name)
            moved.append(entry.name)
    except OSError:
        for filename in reversed(moved):
            (destination / filename).rename(root / filename)
        destination.rmdir()
        raise
    return destination


def identity(path: Path) -> tuple[int, int, int, int]:
    info = path.stat(follow_symlinks=False)
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


class Extractor:
    def __init__(self, executable: str, workspace: Path):
        self.executable = executable
        self.workspace = workspace

    async def extract(
        self,
        archive: Archive,
        layers: int,
        passwords: tuple[str, ...],
        progress: Callable[[int, int], None],
        depth: int = 0,
    ) -> Path:
        if archive.error:
            raise ExtractionError(archive.error)
        work = Path(tempfile.mkdtemp(prefix="layer-", dir=self.workspace))
        inputs, payload = work / "input", work / "payload"
        inputs.mkdir()
        payload.mkdir()
        first = None
        for part in archive.parts:
            target = inputs / (archive.name + part.suffix)
            if target.exists():
                raise ExtractionError(
                    f"Conflicting staged archive names: {target.name}"
                )
            shutil.copy2(part.path, target)
            if first is None:
                first = target
        password = passwords[min(depth, len(passwords) - 1)] if passwords else ""
        # A bare -p prompts; closed stdin prevents a missing password from hanging.
        common = ["-y", "-sccUTF-8", f"-w{work}", f"-p{password}", "--", str(first)]
        await run_7z(
            self.executable,
            ["l", "-slt", "-ba", *common],
            line_handler=check_listing_line,
        )
        await run_7z(
            self.executable,
            ["x", f"-o{payload}", "-bsp1", "-bso1", "-bse1", *common],
            progress=lambda percent: progress(depth, percent),
        )
        check_tree(payload)
        progress(depth, 100)
        if depth + 1 == layers:
            return payload
        files = walk_files(payload, True)
        nested = groups(files)
        if not nested:
            raise ExtractionError(
                f"Expected another archive at layer {depth + 2}, but found none."
            )
        nested_files = {p.path for a in nested for p in a.parts}
        # A single wrapped archive can be unwrapped without accumulating containers.
        if len(nested) == 1 and set(files) == nested_files:
            return await self.extract(nested[0], layers, passwords, progress, depth + 1)
        # Multiple archives and sidecar files are retained in their original directories.
        for inner in nested:
            result = await self.extract(inner, layers, passwords, progress, depth + 1)
            publish(result, inner.primary.parent, inner.name)
            for part in inner.parts:
                part.path.unlink()
        return payload
