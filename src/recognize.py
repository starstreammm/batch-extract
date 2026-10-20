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
    target_name: str | None = None


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


def describe_name(path: Path, hint: str | None = None) -> Part | None:
    """Parse archive/volume naming independently of regex matching."""
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
    return None


def describe(path: Path, hint: str | None = None) -> Part | None:
    if part := describe_name(path, hint):
        return part
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
    return Part(path, path.name, kind, "plain", 0, f".{kind}") if kind else None


def groups(
    files: list[Path], hint: str | None = None, *, include_unknown: bool = False
) -> list[Archive]:
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
        elif include_unknown:
            # Matching is allowed without a type or recognizable signature.
            # 7-Zip will determine whether the selected file is an archive.
            parts.append(Part(path, path.name, "", "plain", 0, ""))
    return collect_parts(parts)


def collect_parts(parts: list[Part]) -> list[Archive]:
    """Group source paths using their planned archive names, without renaming files."""
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


def match_name(filename: str, kind: str) -> tuple[str, str]:
    """Strip a complete archive/volume suffix only when it fits the required type."""
    styles = {
        "7z": r"(.+?)(\.7z(?:\.\d{3,})?)",
        "zip": r"(.+?)(\.zip(?:\.\d{3,})?|\.z\d{2,})",
        "rar": r"(.+?)(\.part\d+\.rar|\.rar(?:\.\d{3,})?|\.r\d{2,})",
    }
    match = re.fullmatch(styles[kind], filename, re.IGNORECASE)
    if match:
        return match[1], match[2]
    return filename, ""


def clean_base(name: str) -> str:
    """Remove a known archive suffix left ahead of a loose volume marker."""
    return re.sub(r"\.(?:7z|rar|zip)$", "", name, flags=re.I) or "archive"


def volume_suffix(kind: str, index: int) -> tuple[str, str]:
    if kind == "rar":
        return "part", f".part{index:02d}.rar"
    return "numeric", f".{kind}.{index:03d}"


def intermediate_groups(files: list[Path], kind: str) -> list[Archive]:
    """Plan canonical names for inner archives, retaining unrelated sidecar files."""
    candidates = []
    for path in files:
        canonical = describe_name(path)
        if (
            canonical is not None and canonical.kind == kind
            and canonical.scheme != "plain" and match_name(path.name, kind)[1]
        ):
            candidates.append(canonical)
            continue
        # Loose markers may have arbitrary trailing extensions, or none.
        part_marker = re.fullmatch(
            r"(?:(.+?)[._ -])?part[._ -]?(\d+)(?:[._ -].*)?", path.name, re.I
        )
        numeric_marker = re.fullmatch(r"(.+?)\.(\d{3,})(?:\..*)?", path.name)
        marker = part_marker or numeric_marker
        if marker:
            base = clean_base(marker[1] or "archive")
            index = int(marker[2])
            scheme, suffix = volume_suffix(kind, index)
            candidates.append(Part(path, base, kind, scheme, index, suffix))
        elif (part := describe(path)) is not None:
            candidates.append(part)
    # A lone opaque file is the only possible next layer (e.g. an SFX archive).
    if not candidates and len(files) == 1:
        candidates.append(Part(files[0], files[0].name, kind, "plain", 0, f".{kind}"))

    planned = []
    for archive in collect_parts(candidates):
        parts = []
        for part in archive.parts:
            described = describe_name(part.path)
            if (
                described is not None and described.kind == kind
                and described.scheme == part.scheme and match_name(part.path.name, kind)[1]
            ):
                parts.append(part)
                continue
            if part.scheme == "plain":
                if match_name(part.path.name, kind)[1]:
                    base = part.base
                else:
                    base = clean_base(
                        Path(part.base).stem if part.base == part.path.name else part.base
                    )
                suffix = f".{kind}"
                scheme, index = "plain", 0
            elif part.scheme == "legacy" and part.kind == kind:
                # Keep native .rar/.r00 and .zip/.z01 conventions.
                base, suffix, scheme, index = part.base, part.suffix, part.scheme, part.index
            else:
                base = clean_base(part.base)
                index = part.index + 1 if part.scheme == "legacy" else part.index
                scheme, suffix = volume_suffix(kind, index)
            target = base + suffix
            parts.append(replace(
                part, base=base, kind=kind, scheme=scheme, index=index, suffix=suffix,
                target_name=target if target != part.path.name else None,
            ))
        planned.extend(parts)
    return collect_parts(planned)


def recognize(
    files: list[Path], patterns: list[Pattern]
) -> tuple[list[Archive], list[str]]:
    selected: dict[tuple[Path, ...], Archive] = {}
    errors = []
    conflicted: set[Path] = set()
    for pattern in patterns:
        rule = parse_rule(pattern.pattern)
        planned_parts = []
        matched: set[Path] = set()
        inconsistent: set[Path] = set()
        for archive in groups(files, pattern.type, include_unknown=True):
            renamed_bases = set()
            replacements = {}
            for part in archive.parts:
                subject, suffix = match_name(part.path.name, pattern.type)
                renamed = rule.apply(subject)
                if renamed is None:
                    continue
                matched.add(part.path)
                full_name = renamed + suffix
                renamed_part = describe_name(
                    part.path.with_name(full_name), archive.kind or None
                )
                renamed_bases.add(renamed_part.base if renamed_part else full_name)
                if rule.replacement is not None:
                    replacements[part.path] = (full_name, renamed_part)
            if not renamed_bases:
                planned_parts.extend(archive.parts)
                continue
            if len(renamed_bases) > 1:
                inconsistent.update(p.path for p in archive.parts)
            renamed = sorted(renamed_bases)[0]
            for part in archive.parts:
                if len(archive.parts) == 1 and part.scheme == "plain" and replacements:
                    full_name, described = replacements[part.path]
                    planned_parts.append(replace(
                        described or part, path=part.path, base=renamed,
                        target_name=full_name if full_name != part.path.name else None,
                    ))
                elif replacements:
                    # A match on one volume renames the entire group's base.
                    original_suffix = part.path.name[len(part.base):]
                    target = renamed + original_suffix
                    planned_parts.append(replace(
                        part, base=renamed,
                        target_name=target if target != part.path.name else None,
                    ))
                else:
                    planned_parts.append(part)
        # A renamed first volume (e.g. .7z.exe -> .7z.001) now joins its siblings.
        for archive in collect_parts(planned_parts):
            key = tuple(p.path for p in archive.parts)
            if not matched.intersection(key):
                continue
            if inconsistent.intersection(key):
                archive = replace(
                    archive, error="Replacement produces inconsistent basenames across volumes."
                )
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
                selected[key] = replace(archive, pattern=pattern)
    return [
        a for key, a in selected.items() if not conflicted.intersection(key)
    ], errors
