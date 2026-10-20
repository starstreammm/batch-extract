"""Recognize complete volume groups without traversing symlink directories."""

from dataclasses import dataclass, replace
from pathlib import Path
import re

from .config import Pattern
from .matching import parse_rule


@dataclass(frozen=True)
class Part:
    path: Path
    base: str
    kind: str
    scheme: str
    index: int
    suffix: str


@dataclass(frozen=True)
class Archive:
    parts: tuple[Part, ...]
    name: str
    kind: str
    pattern: Pattern | None = None
    error: str = ""

    @property
    def primary(self) -> Path:
        return self.parts[0].path

    @property
    def size(self) -> int:
        return sum(p.path.stat().st_size for p in self.parts)


def walk_files(
    root: Path, recursive: bool, excluded: tuple[Path, ...] = ()
) -> list[Path]:
    result = []
    for path in sorted(root.iterdir()):
        if path.is_symlink() or any(path == e or e in path.parents for e in excluded):
            continue
        if path.is_file():
            result.append(path)
        elif recursive and path.is_dir():
            result.extend(walk_files(path, True, excluded))
    return result


def describe(path: Path, hint: str | None = None) -> Part | None:
    name = path.name
    if match := re.fullmatch(r"(.+?)\.part(\d+)(?:\.rar)?", name, re.I):
        return Part(
            path, match[1], "rar", "part", int(match[2]), f".part{match[2]}.rar"
        )
    if match := re.fullmatch(r"(.+?)(?:\.(7z|zip|rar))?\.(\d{3,})", name, re.I):
        kind = (match[2] or hint or "").lower()
        if kind:
            return Part(
                path, match[1], kind, "numeric", int(match[3]), f".{kind}.{match[3]}"
            )
    if match := re.fullmatch(r"(.+)\.([rz])(\d{2,})", name, re.I):
        kind = "rar" if match[2].lower() == "r" else "zip"
        index = int(match[3]) + (1 if kind == "rar" else 0)
        return Part(
            path, match[1], kind, "legacy", index, f".{match[2].lower()}{match[3]}"
        )
    if match := re.fullmatch(r"(.+)\.(7z|rar|zip)", name, re.I):
        kind = match[2].lower()
        return Part(path, match[1], kind, "plain", 0, f".{kind}")
    if hint:
        return Part(path, name, hint, "plain", 0, f".{hint}")
    # Signature detection also permits extensionless archives between layers.
    with path.open("rb") as stream:
        header = stream.read(8)
    kind = next(
        (
            kind
            for magic, kind in (
                (b"7z\xbc\xaf\x27\x1c", "7z"),
                (b"Rar!\x1a\x07", "rar"),
                (b"PK\x03\x04", "zip"),
                (b"PK\x05\x06", "zip"),
            )
            if header.startswith(magic)
        ),
        None,
    )
    return Part(path, name, kind, "plain", 0, f".{kind}") if kind else None


def groups(files: list[Path], hint: str | None = None) -> list[Archive]:
    inferred: dict[tuple[Path, str], str] = {}
    if hint is None:
        # Bare numeric volumes inside another archive have no configured type.
        # Infer it from the first volume, then use it for every sibling volume.
        for path in files:
            match = re.fullmatch(r"(.+)\.(0*1)", path.name)
            if match and (first := describe(path)) is not None:
                inferred[path.parent, match[1]] = first.kind
    parts = []
    for path in files:
        numeric = re.fullmatch(r"(.+)\.\d{3,}", path.name)
        local_hint = hint or (
            inferred.get((path.parent, numeric[1])) if numeric else None
        )
        if (part := describe(path, local_hint)) is not None:
            parts.append(part)
    legacy = {(p.path.parent, p.base, p.kind) for p in parts if p.scheme == "legacy"}
    collected: dict[tuple, list[Part]] = {}
    for part in parts:
        if (
            part.scheme == "plain"
            and (part.path.parent, part.base, part.kind) in legacy
        ):
            part = replace(part, scheme="legacy")
        key = (part.path.parent, part.base, part.kind, part.scheme)
        collected.setdefault(key, []).append(part)
    result = []
    for (_, base, kind, scheme), members in collected.items():
        members.sort(key=lambda p: (p.index, p.path.name))
        indices = [p.index for p in members]
        start = 1 if scheme in ("numeric", "part") else 0
        error = ""
        if indices != list(range(start, start + len(indices))):
            error = "Missing first/intermediate volume or duplicate volume number."
        result.append(Archive(tuple(members), base, kind, error=error))
    return sorted(result, key=lambda a: str(a.primary))


def recognize(
    files: list[Path], patterns: list[Pattern]
) -> tuple[list[Archive], list[str]]:
    selected: dict[tuple[Path, ...], Archive] = {}
    errors = []
    conflicted: set[Path] = set()
    for pattern in patterns:
        rules = [parse_rule(value) for value in pattern.patterns]
        for archive in groups(files, pattern.type):
            if archive.kind != pattern.type:
                continue
            renamed = None
            for rule in rules:
                renamed = rule.apply(archive.name)
                if renamed is not None:
                    break
            if renamed is None:
                continue
            key = tuple(p.path for p in archive.parts)
            overlap = next(
                (other for old, other in selected.items() if set(old) & set(key)), None
            )
            if overlap:
                errors.append(
                    f"{archive.primary.name}: matches multiple patterns ({overlap.pattern.uid}, {pattern.uid})."
                )
                conflicted.update(key)
                conflicted.update(p.path for p in overlap.parts)
            else:
                selected[key] = replace(archive, name=renamed, pattern=pattern)
    return [
        a for key, a in selected.items() if not conflicted.intersection(key)
    ], errors
