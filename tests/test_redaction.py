"""Tests for the Milestone 1B secret-registry and redaction boundary."""

from __future__ import annotations

import json
import pickle
from pathlib import Path

import pytest

from dploydb.redaction import REDACTION_MARKER, SecretRegistry, is_sensitive_key


def test_registered_secret_values_are_redacted_exactly() -> None:
    registry = SecretRegistry()
    registry.register_many(("token-value", "user:password", "token-value"))

    output = registry.redact_text(
        "token-value remains private; token-value-suffix and user:password do too"
    )

    assert output == (
        f"{REDACTION_MARKER} remains private; {REDACTION_MARKER}-suffix "
        f"and {REDACTION_MARKER} do too"
    )


def test_overlapping_secrets_are_redacted_longest_first() -> None:
    registry = SecretRegistry()
    registry.register_many(("abc", "abc123", "123"))

    assert registry.redact_text("abc123 abc 123") == " ".join([REDACTION_MARKER] * 3)


def test_empty_secret_values_are_ignored_without_corrupting_output() -> None:
    registry = SecretRegistry()
    registry.register(None)
    registry.register("")

    assert registry.redact_text("") == ""
    assert registry.redact_text("ordinary output TOKEN=") == "ordinary output TOKEN="
    assert repr(registry) == "SecretRegistry(secret_count=0)"


@pytest.mark.parametrize(
    "key",
    (
        "password",
        "DATABASE_PASSWORD",
        "api-key",
        "S3_SECRET_ACCESS_KEY",
        "x-amz-credential",
        "X-Amz-Signature",
        "authorization",
        "--client-secret",
        "session_token",
        "set-cookie",
    ),
)
def test_sensitive_keys_are_recognized(key: str) -> None:
    assert is_sensitive_key(key)


@pytest.mark.parametrize("key", ("project", "candidate_port", "secretary", "tokenizer"))
def test_non_sensitive_keys_are_not_over_redacted(key: str) -> None:
    assert not is_sensitive_key(key)


def test_sensitive_key_value_forms_are_redacted_without_registration() -> None:
    registry = SecretRegistry()
    raw = (
        "PASSWORD=hunter2 token: abc123 "
        '"api_key": "quoted-value" Authorization: Bearer header-token '
        "--client-secret cli-value --access-token='quoted-cli'"
    )

    output = registry.redact_text(raw)

    assert "hunter2" not in output
    assert "abc123" not in output
    assert "quoted-value" not in output
    assert "header-token" not in output
    assert "cli-value" not in output
    assert "quoted-cli" not in output
    assert output.count(REDACTION_MARKER) == 6


def test_quoted_sensitive_values_with_escaped_quotes_are_fully_redacted() -> None:
    registry = SecretRegistry()
    raw = r"""{"token": "prefix\"private-suffix"} --password='prefix\'private-suffix' """

    output = registry.redact_text(raw)

    assert "private-suffix" not in output
    assert output.count(REDACTION_MARKER) == 2


def test_credentials_and_signed_url_components_are_redacted() -> None:
    registry = SecretRegistry()
    raw = (
        "download=https://alice:password@example.invalid/archive?"
        "X-Amz-Credential=AKIAEXAMPLE%2F20260718%2Fregion%2Fs3%2Faws4_request&"
        "X-Amz-Security-Token=session-value&X-Amz-Signature=deadbeef&safe=visible"
    )

    output = registry.redact_text(raw)

    for secret in ("alice", "password", "AKIAEXAMPLE", "session-value", "deadbeef"):
        assert secret not in output
    assert "safe=visible" in output
    assert output.count(REDACTION_MARKER) == 4


def test_nested_json_compatible_values_are_redacted_without_mutation() -> None:
    registry = SecretRegistry()
    registry.register("registered-value")
    source = {
        "message": "contains registered-value",
        "password": "unregistered-password",
        "nested": [
            {"authorization": "Bearer unregistered-token"},
            "registered-value",
            42,
            True,
            None,
        ],
    }

    output = registry.redact(source)

    assert output == {
        "message": f"contains {REDACTION_MARKER}",
        "password": REDACTION_MARKER,
        "nested": [
            {"authorization": REDACTION_MARKER},
            REDACTION_MARKER,
            42,
            True,
            None,
        ],
    }
    assert source["password"] == "unregistered-password"
    assert source["nested"][0] == {"authorization": "Bearer unregistered-token"}


def test_redacted_mapping_keys_do_not_drop_colliding_evidence() -> None:
    registry = SecretRegistry()
    registry.register("secret-field-name")
    source = {"secret-field-name": "first", REDACTION_MARKER: "second"}

    output = registry.redact(source)

    assert output == {REDACTION_MARKER: "first", f"{REDACTION_MARKER}_2": "second"}
    assert registry.redact(output) == output


def test_redaction_is_idempotent() -> None:
    registry = SecretRegistry()
    registry.register_many(("secret-value", "REDACTED", "[REDACTED]-wrapped-secret"))
    source = {
        "token": "secret-value",
        "message": "Authorization: Bearer secret-value",
        "url": "https://user:password@example.invalid/?signature=abcdef",
        "wrapped": "[REDACTED]-wrapped-secret",
    }

    once = registry.redact(source)
    twice = registry.redact(once)

    assert twice == once


def test_registry_representation_and_serialization_do_not_expose_secrets() -> None:
    registry = SecretRegistry()
    registry.register("do-not-persist")

    assert "do-not-persist" not in repr(registry)
    with pytest.raises(TypeError, match="cannot be serialized"):
        pickle.dumps(registry)


def test_terminal_json_and_file_bound_outputs_contain_no_registered_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secrets = (
        "terminal-secret",
        "json-secret",
        "file-secret",
        "overlap",
        "overlap-long",
    )
    registry = SecretRegistry()
    registry.register_many(secrets)
    payload = {
        "terminal": "terminal-secret and overlap-long",
        "nested": [{"value": "json-secret"}],
        "password": "file-secret",
    }

    print(registry.redact_text("terminal-secret overlap-long"))
    terminal_output = capsys.readouterr().out
    json_output = json.dumps(registry.redact(payload), sort_keys=True)
    output_path = tmp_path / "operation.json"
    output_path.write_text(json_output, encoding="utf-8")
    file_output = output_path.read_text(encoding="utf-8")

    produced_output = "\n".join((terminal_output, json_output, file_output))
    for secret in secrets:
        assert secret not in produced_output
    assert REDACTION_MARKER in terminal_output
    assert REDACTION_MARKER in json_output
    assert REDACTION_MARKER in file_output
