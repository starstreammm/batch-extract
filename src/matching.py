"""Regex matching and optional sed-style substitutions on archive basenames."""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Rule:
    regex: re.Pattern
    replacement: str | None = None

    def apply(self, name: str) -> str | None:
        if not self.regex.search(name):
            return None
        renamed = (
            self.regex.sub(self.replacement, name)
            if self.replacement is not None
            else name
        )
        if (
            not renamed
            or renamed in (".", "..")
            or any(c in renamed for c in "/\\\0\r\n")
        ):
            raise ValueError(
                "Replacement must produce a nonempty filename, without path separators."
            )
        return renamed


def parse_rule(value: str) -> Rule:
    # s/regex/replacement/ or s|regex|replacement|; escaped delimiters are literal.
    if len(value) >= 2 and value[0] == "s" and value[1] in "/|#~":
        delimiter = value[1]
        parts, current = [], []
        i = 2
        while i < len(value):
            ch = value[i]
            if ch == "\\" and i + 1 < len(value):
                following = value[i + 1]
                current.append(following if following == delimiter else ch + following)
                i += 2
                continue
            if ch == delimiter:
                parts.append("".join(current))
                current = []
                if len(parts) == 2:
                    flags = value[i + 1 :]
                    if flags not in ("", "i"):
                        raise ValueError(
                            "Substitution accepts only an optional i flag."
                        )
                    regex = re.compile(parts[0], re.IGNORECASE if flags == "i" else 0)
                    # Compile replacement references even when there is no match.
                    regex.sub(parts[1], "")
                    return Rule(regex, parts[1])
            else:
                current.append(ch)
            i += 1
        raise ValueError("Expected s/regex/replacement/ (optional trailing i).")
    return Rule(re.compile(value))
