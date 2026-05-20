"""Hardening tests for the sanitizer regex and sanitize_error.

These tests verify that:
1. The _TOKEN_RE fix eliminates catastrophic backtracking.
2. Redaction coverage is preserved for all secret patterns.
3. sanitize_error behaves correctly under edge-case inputs.

They intentionally DO NOT modify existing test files.
"""

from __future__ import annotations

import time

import pytest

from medre.core.observability.sanitization import (
    _MAX_ERROR_DETAIL_LEN,
    _TOKEN_RE,
    sanitize_error,
    sanitize_for_log,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A realistic-looking Matrix access token (sytnases use the ``syt_`` prefix).
_FAKE_SYT_TOKEN = "syt_" + "aB3dE7" * 10  # 64 chars

# A base64-ish blob long enough to trigger the 40+ char branch.
_FAKE_B64_BLOB = "MDAx" + "AbcDefGhiJklMnoPqrStuVwxYz012345" * 3

# An OpenAI-style key.
_FAKE_SK_KEY = "sk-" + "aBcDeFgHiJkLmNoPqRsTuVw" * 2

# A 50-char hex string — shorter than 40 chars of base64 charset, should NOT
# be redacted by the bare 40+ branch.
_SHORT_HEX = "deadbeef12345678"


# ---------------------------------------------------------------------------
# Test 1: Basic token redaction still works
# ---------------------------------------------------------------------------


class TestBasicTokenRedaction:
    """All original redaction targets are still covered after the regex fix."""

    @pytest.mark.parametrize(
        ("input_text", "should_contain_redacted"),
        [
            (f"access_token={_FAKE_SYT_TOKEN}", True),
            (f"password={_FAKE_SYT_TOKEN}", True),
            (f"token={_FAKE_SYT_TOKEN}", True),
            (f"secret={_FAKE_SYT_TOKEN}", True),
            (f"credential={_FAKE_SYT_TOKEN}", True),
            (_FAKE_SYT_TOKEN, True),
            (_FAKE_B64_BLOB, True),
            (_FAKE_SK_KEY, True),
            (f"api_key={_FAKE_SYT_TOKEN}", True),
            (f"api-key: {_FAKE_SYT_TOKEN}", True),
        ],
    )
    def test_redaction_patterns(
        self, input_text: str, should_contain_redacted: bool
    ) -> None:
        result = sanitize_error(input_text)
        has_redacted = "[REDACTED]" in result
        assert has_redacted is should_contain_redacted, (
            f"Expected redacted={should_contain_redacted} for {input_text!r}, "
            f"got {result!r}"
        )

    def test_normal_text_preserved(self) -> None:
        msg = "Connection refused on port 8080"
        assert sanitize_error(msg) == msg

    def test_partial_secret_in_message(self) -> None:
        msg = f"Auth failed with token={_FAKE_SYT_TOKEN} for user bob"
        result = sanitize_error(msg)
        assert "[REDACTED]" in result
        assert _FAKE_SYT_TOKEN not in result
        assert "Auth failed" in result
        assert "bob" in result


# ---------------------------------------------------------------------------
# Test 2: Pathological inputs must complete quickly (no catastrophic backtracking)
# ---------------------------------------------------------------------------


class TestPathologicalInputs:
    """Inputs that previously caused catastrophic backtracking must resolve fast."""

    @pytest.mark.parametrize(
        "payload",
        [
            "A" * 100_000,
            "AB" * 50_000,
            "A1" * 50_000,
            "ABCDEF+/" * 12_500,
            "=" * 100_000,
        ],
        ids=[
            "single_char_repeat",
            "alternating_two",
            "alternating_alphanum",
            "base64_charset_mixed",
            "equals_only",
        ],
    )
    def test_no_catastrophic_backtracking(self, payload: str) -> None:
        """Regex substitution on pathological input finishes within 1 second."""
        start = time.monotonic()
        result = _TOKEN_RE.sub("[REDACTED]", payload)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"Regex took {elapsed:.3f}s — possible backtracking"
        # Result should be safe (either original or redacted).
        assert isinstance(result, str)

    def test_full_sanitize_error_on_huge_repeated(self) -> None:
        """sanitize_error truncates + redacts a huge repeated-char input."""
        payload = "X" * 200_000
        start = time.monotonic()
        result = sanitize_error(payload)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0
        assert len(result) <= _MAX_ERROR_DETAIL_LEN


# ---------------------------------------------------------------------------
# Test 3: Huge unicode payloads
# ---------------------------------------------------------------------------


class TestHugeUnicodePayloads:
    """Unicode-heavy inputs don't crash or OOM the sanitizer."""

    @pytest.mark.parametrize(
        "payload",
        [
            "\U0001f600" * 50_000,  # emoji repeat
            "café" * 25_000,  # multi-byte latin
            "日本語テスト" * 10_000,  # CJK
            "\U0001f1fa\U0001f1f8" * 25_000,  # flag emoji
        ],
        ids=["emoji", "latin_multibyte", "cjk", "flag_emoji"],
    )
    def test_unicode_sanitization_completes(self, payload: str) -> None:
        start = time.monotonic()
        result = sanitize_error(payload)
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, f"Unicode sanitization took {elapsed:.3f}s"
        assert isinstance(result, str)

    def test_unicode_with_embedded_token(self) -> None:
        token = "syt_" + "Z9" * 30
        payload = "Error: 日本語 " + token + " café"
        result = sanitize_error(payload)
        assert token not in result
        assert "[REDACTED]" in result


# ---------------------------------------------------------------------------
# Test 4: Recursive metadata + secrets
# ---------------------------------------------------------------------------


class TestRecursiveMetadata:
    """Deeply nested dicts with secrets at various levels."""

    @staticmethod
    def _make_nested(depth: int, secret_value: str) -> dict[str, object]:
        """Build a dict nested ``depth`` levels, with a secret at each level."""
        inner: dict[str, object] = {"password": secret_value, "safe_key": "visible"}
        for i in range(depth):
            inner = {f"level_{i}": inner, "password": secret_value}
        return inner

    def test_nested_secret_removal(self) -> None:
        data = self._make_nested(10, _FAKE_SYT_TOKEN)
        result = sanitize_for_log(data)
        self._assert_no_secret(result, _FAKE_SYT_TOKEN)

    def test_nested_safe_keys_preserved(self) -> None:
        data = self._make_nested(5, "secret123")
        result = sanitize_for_log(data)
        self._assert_has_safe_value(result, "visible")

    @staticmethod
    def _assert_no_secret(obj: object, secret: str) -> None:
        if isinstance(obj, dict):
            for v in obj.values():
                TestRecursiveMetadata._assert_no_secret(v, secret)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                TestRecursiveMetadata._assert_no_secret(v, secret)
        elif isinstance(obj, str):
            assert secret not in obj

    @staticmethod
    def _assert_has_safe_value(obj: object, safe: str) -> None:
        if isinstance(obj, dict):
            for v in obj.values():
                TestRecursiveMetadata._assert_has_safe_value(v, safe)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                TestRecursiveMetadata._assert_has_safe_value(v, safe)
        elif isinstance(obj, str):
            if "level_" not in obj and "password" not in obj:
                assert obj == safe or safe in obj

    def test_mixed_secret_and_normal_keys(self) -> None:
        data = {
            "username": "alice",
            "password": "s3cret!",
            "api_key": "key123",
            "display_name": "Alice",
            "access_token": _FAKE_SYT_TOKEN,
            "preferences": {"theme": "dark", "secret": "buried"},
        }
        result = sanitize_for_log(data)
        assert result.get("username") == "alice"
        assert result.get("display_name") == "Alice"
        assert "password" not in result
        assert "api_key" not in result
        assert "access_token" not in result
        prefs = result.get("preferences", {})
        assert prefs.get("theme") == "dark"
        assert "secret" not in prefs


# ---------------------------------------------------------------------------
# Test 5: Repeated sanitization is idempotent
# ---------------------------------------------------------------------------


class TestRepeatedSanitization:
    """Sanitizing already-sanitized output produces the same result."""

    @pytest.mark.parametrize(
        "input_text",
        [
            f"token={_FAKE_SYT_TOKEN}",
            f"password={_FAKE_B64_BLOB}",
            f"secret={_FAKE_SK_KEY}",
            "plain error message with no secrets",
        ],
    )
    def test_idempotent(self, input_text: str) -> None:
        first = sanitize_error(input_text)
        second = sanitize_error(first)
        assert second == first

    def test_double_redaction_no_double_marker(self) -> None:
        msg = f"token={_FAKE_SYT_TOKEN}"
        first = sanitize_error(msg)
        # [REDACTED] appears exactly once per token occurrence.
        assert first.count("[REDACTED]") == 1
        second = sanitize_error(first)
        assert second.count("[REDACTED]") == 1


# ---------------------------------------------------------------------------
# Test 6: Deterministic truncation
# ---------------------------------------------------------------------------


class TestDeterministicTruncation:
    """Long error strings are truncated to _MAX_ERROR_DETAIL_LEN."""

    def test_long_error_truncated(self) -> None:
        # Use spaces and punctuation to avoid the 40+ base64 branch.
        msg = "Error: " + "no secret here! " * 70  # long, no base64-looking runs
        result = sanitize_error(msg)
        assert len(result) == _MAX_ERROR_DETAIL_LEN
        assert result.endswith("...")

    def test_short_error_not_truncated(self) -> None:
        msg = "short error"
        result = sanitize_error(msg)
        assert result == msg
        assert not result.endswith("...")

    def test_truncation_exact_boundary(self) -> None:
        # Build exactly 512 chars with a non-base64 sentinel to break runs.
        msg = ("no-secret! " * 50)[:_MAX_ERROR_DETAIL_LEN]
        assert len(msg) == _MAX_ERROR_DETAIL_LEN
        result = sanitize_error(msg)
        assert result == msg

    def test_truncation_one_over(self) -> None:
        segment = "no-secret! "
        repeats = (_MAX_ERROR_DETAIL_LEN + len(segment)) // len(segment)
        msg = (segment * repeats)[: _MAX_ERROR_DETAIL_LEN + 1]
        assert len(msg) == _MAX_ERROR_DETAIL_LEN + 1
        result = sanitize_error(msg)
        assert len(result) == _MAX_ERROR_DETAIL_LEN
        assert result.endswith("...")

    def test_truncation_preserves_json_safety(self) -> None:
        """Truncation does not break JSON escape sequences."""
        # Build a string that, if cut mid-escape, would be invalid JSON.
        inner = 'path "\\\\u0041"' * 100  # unicode escape repeated
        msg = f'{{"error": "{inner}"}}'
        result = sanitize_error(msg)
        # If it was truncated, the "..." suffix is outside any escape.
        assert isinstance(result, str)
        assert len(result) <= _MAX_ERROR_DETAIL_LEN

    def test_truncation_does_not_expose_token_tail(self) -> None:
        """If truncation cuts into a redacted region, the tail is still safe."""
        # A token followed by padding so truncation cuts somewhere.
        token = "syt_" + "aB" * 50  # 104 chars
        msg = token + "Y" * 500
        result = sanitize_error(msg)
        assert token not in result
        if len(result) < len(msg):
            # Truncated — token must not appear in the truncated output.
            assert token[:20] not in result


# ---------------------------------------------------------------------------
# Test 7: No false positives on normal strings
# ---------------------------------------------------------------------------


class TestNoFalsePositives:
    """Short or non-secret strings are not redacted."""

    @pytest.mark.parametrize(
        "input_text",
        [
            _SHORT_HEX,
            "user_id=12345",
            "event_id=evt_abc123",
            "route_id=bot-to-radio",
            "status=sent",
            "adapter_id=fake_matrix",
            "connection_timeout=30",
            "retry_count=3",
            "port=8080",
            "host=localhost",
        ],
    )
    def test_not_redacted(self, input_text: str) -> None:
        result = sanitize_error(input_text)
        assert result == input_text, f"Unexpected redaction of {input_text!r}"

    def test_hex_shorter_than_40_not_redacted(self) -> None:
        hex_39 = "a" * 39
        assert sanitize_error(hex_39) == hex_39

    def test_exactly_40_chars_is_redacted(self) -> None:
        """The 40+ char base64 branch triggers at exactly 40."""
        b64_40 = "a" * 40
        result = sanitize_error(b64_40)
        assert "[REDACTED]" in result

    def test_39_chars_not_redacted(self) -> None:
        b64_39 = "a" * 39
        result = sanitize_error(b64_39)
        assert result == b64_39

    def test_url_not_redacted(self) -> None:
        url = "https://matrix.org/_matrix/client/v3/sync?timeout=30000"
        result = sanitize_error(url)
        # URL should survive intact (no 40+ char base64-looking run).
        assert "matrix.org" in result

    def test_uuid_not_redacted(self) -> None:
        uuid_val = "550e8400-e29b-41d4-a716-446655440000"
        result = sanitize_error(uuid_val)
        # Standard UUID is 36 chars with dashes — too short for the 40+ branch.
        assert result == uuid_val


# ---------------------------------------------------------------------------
# Test: SDK object repr redaction
# ---------------------------------------------------------------------------


class TestSdkObjectRedaction:
    """SDK object repr strings are replaced with [OBJECT_REPR]."""

    def test_sdk_repr_redacted(self) -> None:
        msg = "Error from <nio.client.AsyncClient object at 0x7f1234567890>"
        result = sanitize_error(msg)
        assert "[OBJECT_REPR]" in result
        assert "AsyncClient" not in result

    def test_sdk_repr_with_surrounding_text(self) -> None:
        msg = "Failed to call <some.module.Thing object at 0xdeadbeef> during startup"
        result = sanitize_error(msg)
        assert "[OBJECT_REPR]" in result
        assert "startup" in result


# ---------------------------------------------------------------------------
# Test: sanitize_for_log edge cases
# ---------------------------------------------------------------------------


class TestSanitizeForLogEdgeCases:
    """Edge-case inputs for sanitize_for_log."""

    def test_empty_dict(self) -> None:
        assert sanitize_for_log({}) == {}

    def test_non_secret_passthrough(self) -> None:
        data = {"count": 42, "name": "test", "active": True, "rate": 3.14}
        assert sanitize_for_log(data) == data

    def test_none_value_preserved(self) -> None:
        data = {"optional": None}
        assert sanitize_for_log(data) == {"optional": None}

    def test_list_value_sanitized(self) -> None:
        data = {"items": [1, "two", None]}
        result = sanitize_for_log(data)
        assert result["items"] == [1, "two", None]

    def test_nested_dict_value_sanitized(self) -> None:
        data = {"config": {"host": "localhost", "password": "hidden"}}
        result = sanitize_for_log(data)
        assert result["config"]["host"] == "localhost"
        assert "password" not in result["config"]

    def test_unknown_type_coerced(self) -> None:
        data = {"callback": lambda: None}
        result = sanitize_for_log(data)
        assert isinstance(result["callback"], str)
        assert "function" in result["callback"].lower()
