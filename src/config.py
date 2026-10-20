"""Validated YAML configuration and atomic persistence."""

from pathlib import Path
import secrets
import tempfile
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, ValidationError, field_validator
from pydantic.dataclasses import dataclass
import yaml


class ConfigError(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    """Reject duplicate mapping keys instead of silently losing entries."""


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            raise ConfigError(
                "All YAML mapping keys must be strings (quote numeric UIDs)."
            )
        if key in result:
            raise ConfigError(f"Duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


@dataclass(
    frozen=True,
    config=ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True),
)
class Pattern:
    uid: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
    patterns: Annotated[
        tuple[Annotated[str, Field(min_length=1)], ...], Field(min_length=1)
    ]
    layer: Annotated[int, Field(ge=1)]
    passwords: tuple[str, ...]
    type: Literal["7z", "rar", "zip"]

    @field_validator("patterns", "passwords", mode="before")
    @classmethod
    def normalize_lists(cls, value):
        # YAML supplies lists; preserve immutable tuples internally.
        return tuple(value) if isinstance(value, list) else value

    @field_validator("passwords")
    @classmethod
    def validate_passwords(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any("\0" in password for password in value):
            raise ValueError("Passwords cannot contain NUL characters.")
        return value

    def password(self, layer: int) -> str:
        return (
            self.passwords[min(layer, len(self.passwords) - 1)]
            if self.passwords
            else ""
        )

    def record(self) -> dict:
        return {
            "pattern": list(self.patterns),
            "layer": self.layer,
            "password": list(self.passwords),
            "type": self.type,
        }


def load_patterns(path: Path) -> list[Pattern]:
    if not path.exists():
        return []
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if data is None:
        return []
    if not isinstance(data, dict):
        raise ConfigError("Configuration must be a mapping of UID to pattern.")
    result = []
    for uid, value in data.items():
        if not isinstance(value, dict) or set(value) != {
            "pattern",
            "layer",
            "password",
            "type",
        }:
            raise ConfigError(
                f"{uid}: expected pattern, layer, password, and type fields."
            )
        try:
            result.append(
                Pattern(
                    uid=uid,
                    patterns=value["pattern"],
                    layer=value["layer"],
                    passwords=value["password"],
                    type=value["type"],
                )
            )
        except ValidationError as exc:
            raise ConfigError(f"{uid}: invalid pattern configuration:\n{exc}") from exc
    return result


def save_patterns(path: Path, patterns: list[Pattern]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".pattern-",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            yaml.safe_dump(
                {p.uid: p.record() for p in patterns},
                stream,
                sort_keys=False,
                allow_unicode=True,
            )
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def new_uid(patterns: list[Pattern]) -> str:
    existing = {p.uid for p in patterns}
    while (uid := secrets.token_hex(4)) in existing:
        pass
    return uid
