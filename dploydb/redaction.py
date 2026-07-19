"""In-memory secret registration and output redaction.

The registry is deliberately an output-boundary object: callers register resolved
secret values, then redact data before it is displayed or handed to persistence.
It has no serialization API and its representation never includes secret values.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import NoReturn

REDACTION_MARKER = "[REDACTED]"

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]

_SENSITIVE_KEY_SOURCE = r"""
    (?:[a-z0-9]+[-_.])*
    (?:
        password
        | passwd
        | pwd
        | secret
        | token
        | credentials?
        | authorization
        | proxy[-_]?authorization
        | api[-_]?key
        | access[-_]?key(?:[-_]?id)?
        | secret[-_]?key
        | client[-_]?secret
        | private[-_]?key
        | aws[-_]?access[-_]?key[-_]?id
        | signature
        | sig
        | cookies?
        | set[-_]?cookie
    )
"""

_QUOTED_VALUE_SOURCE = r"""
    (?:
        \\[^\r\n]
        | (?!(?P=value_quote)) [^\\\r\n]
    )*
"""

_SENSITIVE_KEY = re.compile(rf"^(?:{_SENSITIVE_KEY_SOURCE})$", re.IGNORECASE | re.VERBOSE)

_AUTHORIZATION = re.compile(
    r"""
    (?P<prefix>
        (?<![\w.-])
        (?:authorization|proxy[-_]?authorization)
        \s*[:=]\s*
    )
    (?P<value>
        (?:(?:bearer|basic)\s+)?[^\s,;&]+
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

_QUOTED_KEY_VALUE = re.compile(
    rf"""
    (?P<prefix>
        (?<![\w.-])
        (?P<key_quote>["']?)
        {_SENSITIVE_KEY_SOURCE}
        (?P=key_quote)
        \s*[:=]\s*
    )
    (?P<value_quote>["'])
    (?P<value>{_QUOTED_VALUE_SOURCE})
    (?P=value_quote)
    """,
    re.IGNORECASE | re.VERBOSE,
)

_UNQUOTED_KEY_VALUE = re.compile(
    rf"""
    (?P<prefix>
        (?<![\w.-])
        (?P<key_quote>["']?)
        {_SENSITIVE_KEY_SOURCE}
        (?P=key_quote)
        \s*[:=]\s*
    )
    (?P<value>\[REDACTED\]|[^\s,;&}}\]]+)
    """,
    re.IGNORECASE | re.VERBOSE,
)

_QUOTED_COMMAND_OPTION = re.compile(
    rf"""
    (?P<prefix>
        (?<![\w-])
        --{_SENSITIVE_KEY_SOURCE}
        (?:=|\s+)
    )
    (?P<value_quote>["'])
    (?P<value>{_QUOTED_VALUE_SOURCE})
    (?P=value_quote)
    """,
    re.IGNORECASE | re.VERBOSE,
)

_UNQUOTED_COMMAND_OPTION = re.compile(
    rf"""
    (?P<prefix>
        (?<![\w-])
        --{_SENSITIVE_KEY_SOURCE}
        (?:=|\s+)
    )
    (?P<value>[^\s,;&]+)
    """,
    re.IGNORECASE | re.VERBOSE,
)

_URL_USERINFO = re.compile(
    r"(?P<prefix>\b[a-z][a-z0-9+.-]*://)(?P<userinfo>[^/@\s]+)@",
    re.IGNORECASE,
)


def is_sensitive_key(key: str) -> bool:
    """Return whether a mapping key conventionally contains a secret value."""
    normalized = key.strip().strip("\"'")
    if normalized == "PWD":
        return False
    candidate = normalized.removeprefix("--")
    return _SENSITIVE_KEY.fullmatch(candidate) is not None


class SecretRegistry:
    """Hold resolved secrets in memory and redact output-bound values."""

    __slots__ = ("_exact_pattern", "_secrets")

    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._exact_pattern: re.Pattern[str] | None = None

    def __repr__(self) -> str:
        return f"SecretRegistry(secret_count={len(self._secrets)})"

    def __reduce__(self) -> NoReturn:
        raise TypeError("SecretRegistry cannot be serialized")

    def register(self, value: str | None) -> None:
        """Register one resolved secret without persisting or exposing it."""
        if value is None or value == "":
            return
        if not isinstance(value, str):
            raise TypeError("secret values must be strings")
        if value not in self._secrets:
            self._secrets.add(value)
            self._exact_pattern = None

    def register_many(self, values: Iterable[str | None]) -> None:
        """Register multiple resolved secrets."""
        for value in values:
            self.register(value)

    def redact_text(self, text: str) -> str:
        """Redact registered values and recognizable credential forms in text."""
        if not isinstance(text, str):
            raise TypeError("text to redact must be a string")

        redacted = self._redact_exact_values(text)
        redacted = _URL_USERINFO.sub(
            lambda match: f"{match.group('prefix')}{REDACTION_MARKER}@", redacted
        )
        redacted = _AUTHORIZATION.sub(
            lambda match: f"{match.group('prefix')}{REDACTION_MARKER}", redacted
        )
        redacted = _QUOTED_COMMAND_OPTION.sub(self._replace_quoted_value, redacted)
        redacted = _UNQUOTED_COMMAND_OPTION.sub(self._replace_unquoted_value, redacted)
        redacted = _QUOTED_KEY_VALUE.sub(self._replace_quoted_value, redacted)
        return _UNQUOTED_KEY_VALUE.sub(self._replace_unquoted_value, redacted)

    def redact(self, value: JsonValue) -> JsonValue:
        """Recursively redact a JSON-compatible value without mutating the input."""
        if isinstance(value, str):
            return self.redact_text(value)
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, dict):
            redacted: dict[str, JsonValue] = {}
            for key, item in value.items():
                redacted_key = self.redact_text(key)
                if redacted_key in redacted:
                    suffix = 2
                    while f"{redacted_key}_{suffix}" in redacted:
                        suffix += 1
                    redacted_key = f"{redacted_key}_{suffix}"
                redacted[redacted_key] = (
                    REDACTION_MARKER if is_sensitive_key(key) else self.redact(item)
                )
            return redacted
        raise TypeError(f"value is not JSON-compatible: {type(value).__name__}")

    def _redact_exact_values(self, text: str) -> str:
        pattern = self._exact_value_pattern()
        if pattern is None:
            return text
        return pattern.sub(REDACTION_MARKER, text)

    def _exact_value_pattern(self) -> re.Pattern[str] | None:
        if not self._secrets:
            return None
        if self._exact_pattern is None:
            exact_values = sorted(
                {*self._secrets, REDACTION_MARKER},
                key=lambda value: (len(value), value == REDACTION_MARKER),
                reverse=True,
            )
            self._exact_pattern = re.compile("|".join(re.escape(value) for value in exact_values))
        return self._exact_pattern

    @staticmethod
    def _replace_quoted_value(match: re.Match[str]) -> str:
        quote = match.group("value_quote")
        return f"{match.group('prefix')}{quote}{REDACTION_MARKER}{quote}"

    @staticmethod
    def _replace_unquoted_value(match: re.Match[str]) -> str:
        return f"{match.group('prefix')}{REDACTION_MARKER}"
