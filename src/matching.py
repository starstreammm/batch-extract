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


def translate_basic_regex(pattern: str) -> str:
    """Translate sed-style grouping/quantifiers, leaving character classes intact."""
    result = []
    in_class = False
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\" and index + 1 < len(pattern):
            following = pattern[index + 1]
            if not in_class and following in "()+?{}|":
                result.append(following)
            else:
                result.append(char + following)
            index += 2
            continue
        if char == "[" and not in_class:
            in_class = True
        elif char == "]" and in_class:
            in_class = False
        if not in_class and char in "()+?{}|":
            result.append("\\" + char)
        else:
            result.append(char)
        index += 1
    return "".join(result)


def compile_substitution(pattern: str, replacement: str, flags: int) -> re.Pattern:
    try:
        regex = re.compile(pattern, flags)
        regex.sub(replacement, "")
        return regex
    except re.error as original:
        # Python regexes remain the default. Retry basic regex syntax only when
        # an escaped capture group cannot satisfy a replacement backreference.
        if "invalid group reference" not in str(original) or r"\(" not in pattern:
            raise
        try:
            regex = re.compile(translate_basic_regex(pattern), flags)
            regex.sub(replacement, "")
            return regex
        except re.error:
            raise original


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
                    regex = compile_substitution(
                        parts[0], parts[1], re.IGNORECASE if flags == "i" else 0
                    )
                    return Rule(regex, parts[1])
            else:
                current.append(ch)
            i += 1
        raise ValueError("Expected s/regex/replacement/ (optional trailing i).")
    return Rule(re.compile(value))
