"""Stubbed Matrix media-transfer tests for ``MatrixSession``.

The real transfer paths run against Docker Synapse in the integration tier,
which never uploads coverage, so these tests pin the same behavior with a
stubbed aiohttp boundary and a stubbed ``nio`` module: bounded download
semantics, encrypted-file structure validation, upload interception, and
the attachment-fetch deferral persistence edges.
"""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from medre.adapters.matrix.errors import (
    MATRIX_ATTACHMENT_FETCH_MAX_DEFERRALS,
    MatrixAttachmentFetchDeferredError,
    MatrixConnectionError,
    MatrixMediaTransientError,
    MatrixMediaUnavailableError,
)
from medre.adapters.matrix.session import (
    _ATTACHMENT_FETCH_DEFERRALS_MAX,
    MatrixSession,
    _RoomRateLimitIntercept,
)
from medre.core.ingress.types import AdapterCheckpoint
from tests.helpers.matrix_session import make_matrix_config

MXC = "mxc://hs.example/media123"


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class StubResponseContent:
    def __init__(self, chunks: tuple[bytes, ...], error_body: bytes = b""):
        self._chunks = list(chunks)
        self._error_body = error_body

    async def iter_chunked(self, _size: int):
        for chunk in self._chunks:
            yield chunk

    async def read(self, _size: int) -> bytes:
        return self._error_body


class StubResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        chunks: tuple[bytes, ...] = (b"ab", b"cd"),
        error_body: bytes = b"",
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.content = StubResponseContent(chunks, error_body)


class StubHttpSession:
    def __init__(self, response: StubResponse | Exception) -> None:
        self._response = response
        self.requests: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: object):
        self.requests.append({"method": method, "url": url, **kwargs})
        response = self._response
        if isinstance(response, Exception):
            return _failing_cm(response)
        return _yielding_cm(response)


@asynccontextmanager
async def _yielding_cm(response: StubResponse):
    yield response


@asynccontextmanager
async def _failing_cm(exc: Exception):
    raise exc
    yield  # pragma: no cover - never reached


def _nio_module(**extra: object) -> SimpleNamespace:
    def download(server: str, media_id: str, access_token: object = None):
        return "GET", f"/_matrix/client/v3/download/{server}/{media_id}"

    base: dict[str, object] = {
        "Api": SimpleNamespace(download=download),
        "crypto": SimpleNamespace(
            attachments=SimpleNamespace(decrypt_attachment=lambda *_a: b"plain")
        ),
        "EncryptionError": type("EncryptionError", (Exception,), {}),
        "CallbackNotAcceptedError": type("CallbackNotAcceptedError", (Exception,), {}),
    }
    base.update(extra)
    return SimpleNamespace(**base)


def _media_session(monkeypatch: pytest.MonkeyPatch, http: StubHttpSession):
    monkeypatch.setitem(sys.modules, "nio", _nio_module())
    session = MatrixSession(make_matrix_config())
    session._client = SimpleNamespace(
        client_session=http,
        homeserver="https://hs.example",
        access_token="s3cret-token",
    )
    return session


# ---------------------------------------------------------------------------
# _parse_mxc_uri
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mxc", "expected"),
    [("mxc://hs.example/media123", ("hs.example", "media123"))],
)
def test_parse_mxc_uri_accepts_wellformed(mxc: str, expected: tuple[str, str]) -> None:
    from medre.adapters.matrix.session import _parse_mxc_uri

    assert _parse_mxc_uri(mxc) == expected


@pytest.mark.parametrize(
    "mxc",
    [
        "",
        "not-a-mxc",
        "https://hs.example/media",
        "mxc:///media",
        "mxc://user@hs.example/media",
        "mxc://hs.example/../secret",
        "mxc://hs.example/media?alt=small",
    ],
)
def test_parse_mxc_uri_rejects_malformed(mxc: str) -> None:
    from medre.adapters.matrix.session import _parse_mxc_uri

    with pytest.raises(MatrixMediaUnavailableError, match="locator"):
        _parse_mxc_uri(mxc)


# ---------------------------------------------------------------------------
# download_media
# ---------------------------------------------------------------------------


async def test_download_streams_bounded_body_with_header_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = StubHttpSession(StubResponse(chunks=(b"ab", b"cd")))
    session = _media_session(monkeypatch, http)

    data = await session.download_media(mxc=MXC, max_bytes=100, timeout_seconds=5.0)

    assert data == b"abcd"
    request = http.requests[0]
    assert request["headers"] == {"Authorization": "Bearer s3cret-token"}
    assert request["allow_redirects"] is False
    assert "access_token" not in str(request["url"])


async def test_download_rejects_declared_size_above_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = StubHttpSession(
        StubResponse(headers={"Content-Length": "101"}, chunks=(b"x",))
    )
    session = _media_session(monkeypatch, http)

    with pytest.raises(MatrixMediaUnavailableError, match="cap") as excinfo:
        await session.download_media(mxc=MXC, max_bytes=100, timeout_seconds=5.0)
    assert excinfo.value.reason == "oversized"


async def test_download_rejects_stream_crossing_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = StubHttpSession(StubResponse(chunks=(b"x" * 60, b"x" * 60)))
    session = _media_session(monkeypatch, http)

    with pytest.raises(MatrixMediaUnavailableError, match="crossed") as excinfo:
        await session.download_media(mxc=MXC, max_bytes=100, timeout_seconds=5.0)
    assert excinfo.value.reason == "oversized"


async def test_download_never_follows_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = StubHttpSession(StubResponse(status=301, chunks=()))
    session = _media_session(monkeypatch, http)

    with pytest.raises(MatrixMediaUnavailableError, match="redirect") as excinfo:
        await session.download_media(mxc=MXC, max_bytes=100, timeout_seconds=5.0)
    assert excinfo.value.reason == "malformed_source"
    assert http.requests[0]["allow_redirects"] is False


async def test_download_missing_media_is_permanent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = StubHttpSession(StubResponse(status=404, chunks=()))
    session = _media_session(monkeypatch, http)

    with pytest.raises(MatrixMediaUnavailableError, match="404") as excinfo:
        await session.download_media(mxc=MXC, max_bytes=100, timeout_seconds=5.0)
    assert excinfo.value.reason == "content_missing"


@pytest.mark.parametrize("status", [429, 502])
async def test_download_server_errors_are_transient(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    http = StubHttpSession(StubResponse(status=status, chunks=()))
    session = _media_session(monkeypatch, http)

    with pytest.raises(MatrixMediaTransientError):
        await session.download_media(mxc=MXC, max_bytes=100, timeout_seconds=5.0)


async def test_download_rejected_request_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps({"errcode": "M_UNKNOWN", "error": "nope"}).encode()
    http = StubHttpSession(StubResponse(status=400, chunks=(), error_body=body))
    session = _media_session(monkeypatch, http)

    with pytest.raises(MatrixMediaUnavailableError, match="M_UNKNOWN") as excinfo:
        await session.download_media(mxc=MXC, max_bytes=100, timeout_seconds=5.0)
    assert excinfo.value.reason == "malformed_source"


async def test_download_timeout_is_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    http = StubHttpSession(asyncio.TimeoutError())
    session = _media_session(monkeypatch, http)

    with pytest.raises(MatrixMediaTransientError, match="timed out"):
        await session.download_media(mxc=MXC, max_bytes=100, timeout_seconds=5.0)


async def test_download_requires_open_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "nio", _nio_module())
    session = MatrixSession(make_matrix_config())
    with pytest.raises(MatrixConnectionError, match="not connected"):
        await session.download_media(mxc=MXC, max_bytes=100, timeout_seconds=5.0)

    session._client = SimpleNamespace(homeserver="https://hs.example")
    with pytest.raises(MatrixConnectionError, match="session"):
        await session.download_media(mxc=MXC, max_bytes=100, timeout_seconds=5.0)


# ---------------------------------------------------------------------------
# _media_errcode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"", None),
        (b"not json", None),
        (b'["not","a","dict"]', None),
        (b'{"error": "no errcode"}', None),
        (b'{"errcode": "M_NOT_FOUND"}', "M_NOT_FOUND"),
    ],
)
def test_media_errcode_extraction(body: bytes, expected: str | None) -> None:
    session = MatrixSession(make_matrix_config())
    assert session._media_errcode(body) == expected


# ---------------------------------------------------------------------------
# decrypt_media_attachment
# ---------------------------------------------------------------------------


def _valid_file_info() -> dict[str, object]:
    return {
        "v": "v2",
        "key": {"kty": "oct", "alg": "A256CTR", "k": "k" * 43},
        "iv": "i" * 24,
        "hashes": {"sha256": "h" * 43},
    }


def test_decrypt_rejects_malformed_structure() -> None:
    session = MatrixSession(make_matrix_config())
    good = _valid_file_info()

    with pytest.raises(MatrixMediaUnavailableError, match="not an object"):
        session.decrypt_media_attachment(ciphertext=b"x", file_info=None)
    with pytest.raises(MatrixMediaUnavailableError, match="version"):
        session.decrypt_media_attachment(ciphertext=b"x", file_info={**good, "v": "v1"})
    with pytest.raises(MatrixMediaUnavailableError, match="JWK object"):
        session.decrypt_media_attachment(
            ciphertext=b"x", file_info={**good, "key": "nope"}
        )
    with pytest.raises(MatrixMediaUnavailableError, match="symmetric"):
        session.decrypt_media_attachment(
            ciphertext=b"x", file_info={**good, "key": {"kty": "RSA", "k": "k"}}
        )
    with pytest.raises(MatrixMediaUnavailableError, match="algorithm"):
        session.decrypt_media_attachment(
            ciphertext=b"x",
            file_info={**good, "key": {"kty": "oct", "alg": "X", "k": "k" * 43}},
        )
    with pytest.raises(MatrixMediaUnavailableError, match="iv/hashes"):
        session.decrypt_media_attachment(
            ciphertext=b"x", file_info={**good, "iv": None, "hashes": None}
        )
    with pytest.raises(MatrixMediaUnavailableError, match="sha256"):
        session.decrypt_media_attachment(
            ciphertext=b"x", file_info={**good, "hashes": {"sha256": 1}}
        )


def test_decrypt_delegates_to_sdk_and_maps_integrity_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[bytes, str, str, str]] = []

    class StubEncryptionError(Exception):
        pass

    def fake_decrypt(ciphertext: bytes, k: str, sha256: str, iv: str) -> bytes:
        calls.append((ciphertext, k, sha256, iv))
        if sha256 == "h" * 43:
            return b"plain"
        raise StubEncryptionError("digest mismatch")

    nio_stub = _nio_module()
    nio_stub.crypto.attachments.decrypt_attachment = fake_decrypt
    nio_stub.EncryptionError = StubEncryptionError
    monkeypatch.setitem(sys.modules, "nio", nio_stub)

    session = MatrixSession(make_matrix_config())
    plain = session.decrypt_media_attachment(
        ciphertext=b"cipher", file_info=_valid_file_info()
    )
    assert plain == b"plain"
    assert calls == [(b"cipher", "k" * 43, "h" * 43, "i" * 24)]

    bad = _valid_file_info()
    bad["hashes"] = {"sha256": "z" * 43}
    with pytest.raises(MatrixMediaUnavailableError, match="integrity") as excinfo:
        session.decrypt_media_attachment(ciphertext=b"cipher", file_info=bad)
    assert excinfo.value.reason == "integrity_failed"


def test_decrypt_maps_sdk_parameter_errors_to_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def bad_params(*_a: object) -> bytes:
        raise ValueError("bad parameters")

    nio_stub = _nio_module()
    nio_stub.crypto.attachments.decrypt_attachment = bad_params
    monkeypatch.setitem(sys.modules, "nio", nio_stub)

    session = MatrixSession(make_matrix_config())
    with pytest.raises(MatrixMediaUnavailableError, match="malformed") as excinfo:
        session.decrypt_media_attachment(
            ciphertext=b"cipher", file_info=_valid_file_info()
        )
    assert excinfo.value.reason == "malformed_source"


# ---------------------------------------------------------------------------
# upload_media / _on_upload_error_response
# ---------------------------------------------------------------------------


async def test_upload_returns_response_and_sync_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploaded: dict[str, object] = {}

    async def fake_upload(provider, **kwargs: object):
        uploaded["provider_bytes"] = provider(0, 0)
        uploaded.update(kwargs)
        return SimpleNamespace(content_uri="mxc://hs/up1"), None

    monkeypatch.setitem(sys.modules, "nio", _nio_module())
    session = MatrixSession(make_matrix_config())
    session._client = SimpleNamespace(upload=fake_upload)

    response, keys = await session.upload_media(
        data=b"payload",
        content_type="image/png",
        filename="photo.png",
        encrypt=False,
    )

    assert response.content_uri == "mxc://hs/up1"
    assert keys is None
    assert uploaded["provider_bytes"] == b"payload"
    assert uploaded["encrypt"] is False
    assert uploaded["filesize"] == len(b"payload")


async def test_upload_rate_limit_response_is_intercepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limited = SimpleNamespace(errcode="M_LIMIT_EXCEEDED", retry_after_ms=1500)

    async def fake_upload(_provider, **_kwargs: object):
        raise _RoomRateLimitIntercept(limited)

    monkeypatch.setitem(sys.modules, "nio", _nio_module())
    session = MatrixSession(make_matrix_config())
    session._client = SimpleNamespace(upload=fake_upload)

    response, keys = await session.upload_media(
        data=b"x", content_type="text/plain", filename=None, encrypt=False
    )
    assert response is limited
    assert keys is None


async def test_upload_requires_open_client() -> None:
    session = MatrixSession(make_matrix_config())
    with pytest.raises(MatrixConnectionError, match="not connected"):
        await session.upload_media(
            data=b"x", content_type="text/plain", filename=None, encrypt=False
        )


async def test_upload_error_response_intercepts_only_rate_limits() -> None:
    session = MatrixSession(make_matrix_config())
    limited = SimpleNamespace(errcode="M_LIMIT_EXCEEDED")
    plain = SimpleNamespace(event_id="$sent")

    with pytest.raises(_RoomRateLimitIntercept):
        await session._on_upload_error_response(limited)
    assert await session._on_upload_error_response(plain) is None


# ---------------------------------------------------------------------------
# Attachment-fetch deferral persistence edges
# ---------------------------------------------------------------------------


def test_attachment_fetch_deferral_key_rejects_incomplete_identity() -> None:
    assert MatrixSession._attachment_fetch_deferral_key({}) is None
    assert MatrixSession._attachment_fetch_deferral_key({"room_id": "!r:x"}) is None
    assert (
        MatrixSession._attachment_fetch_deferral_key(
            {"room_id": "!r:x", "event_id": "$e"}
        )
        is not None
    )


async def test_persist_and_load_without_checkpoint_endpoints_are_noops() -> None:
    session = MatrixSession(make_matrix_config())
    await session._record_attachment_fetch_deferral("a" * 64)
    await session._persist_attachment_fetch_deferrals()
    await session._load_attachment_fetch_deferrals()
    assert session._attachment_fetch_deferrals == {"a" * 64: 1}


async def test_prune_survives_checkpoint_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = MatrixSession(make_matrix_config())
    await session._record_attachment_fetch_deferral("b" * 64)

    async def fail_commit(_stream: str, _cursor: str, _metadata: str) -> None:
        raise OSError("disk full")

    session._checkpoint_committer = fail_commit

    caplog.set_level("WARNING", logger="medre.adapters.matrix.session")
    await session._prune_attachment_fetch_deferral("b" * 64)

    assert session._attachment_fetch_deferrals == {}
    assert any(
        "pruned Matrix attachment fetch deferral" in record.message
        for record in caplog.records
    )
    # Pruning an absent key is a no-op.
    await session._prune_attachment_fetch_deferral("c" * 64)


async def test_deferral_without_nio_rejection_class_reraises_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When nio cannot translate the deferral, the original error surfaces."""
    monkeypatch.setitem(sys.modules, "nio", SimpleNamespace(Other=object))
    monkeypatch.setitem(sys.modules, "nio.exceptions", SimpleNamespace())

    async def defer(_event: dict[str, object], _provenance: str) -> None:
        raise MatrixAttachmentFetchDeferredError("generated", ("network",))

    session = MatrixSession(make_matrix_config(), admission_callback=defer)
    room = SimpleNamespace(room_id="!room:example.org")
    event = SimpleNamespace(
        sender="@alice:example.org",
        event_id="$media",
        body="photo.png",
        source={
            "event_id": "$media",
            "sender": "@alice:example.org",
            "type": "m.room.message",
            "content": {"msgtype": "m.image", "body": "photo.png"},
        },
    )

    with pytest.raises(MatrixAttachmentFetchDeferredError):
        await session._on_nio_admission(room, event, SimpleNamespace(value="live"))


async def test_deferral_budget_counts_are_capped_per_identity() -> None:
    session = MatrixSession(make_matrix_config())
    key = "d" * 64
    for _ in range(MATRIX_ATTACHMENT_FETCH_MAX_DEFERRALS + 3):
        await session._record_attachment_fetch_deferral(key)
    assert session._attachment_fetch_deferrals[key] == (
        MATRIX_ATTACHMENT_FETCH_MAX_DEFERRALS - 1
    )
    assert len(session._attachment_fetch_deferrals) == 1
    assert len(session._attachment_fetch_deferrals) <= _ATTACHMENT_FETCH_DEFERRALS_MAX


async def test_load_restore_roundtrip_keeps_valid_entries() -> None:
    metadata = json.dumps(
        {"attempts": {"e" * 64: 2}},
        sort_keys=True,
        separators=(",", ":"),
    )
    checkpoint = AdapterCheckpoint(
        adapter_id="matrix-test",
        stream="matrix_attachment_fetch_deferrals",
        cursor="v1",
        metadata_json=metadata,
        updated_at="2026-09-27T00:00:00Z",
    )

    async def load(_stream: str) -> AdapterCheckpoint:
        return checkpoint

    session = MatrixSession(make_matrix_config(), checkpoint_loader=load)
    await session._load_attachment_fetch_deferrals()
    assert session._attachment_fetch_deferrals == {"e" * 64: 2}


# ---------------------------------------------------------------------------
# Admission provenance bookkeeping
# ---------------------------------------------------------------------------


async def test_admission_provenance_counters_track_recovered_and_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[dict[str, object], str]] = []

    async def admit(event: dict[str, object], provenance: str) -> None:
        seen.append((event, provenance))

    monkeypatch.setitem(sys.modules, "nio", _nio_module())
    session = MatrixSession(make_matrix_config(), admission_callback=admit)
    room = SimpleNamespace(room_id="!room:example.org")
    event = SimpleNamespace(
        sender="@alice:example.org",
        event_id="$media",
        body="photo.png",
        source={
            "event_id": "$media",
            "sender": "@alice:example.org",
            "type": "m.room.message",
            "content": {"msgtype": "m.image", "body": "photo.png"},
        },
    )

    await session._on_nio_admission(room, event, SimpleNamespace(value="recovered"))
    await session._on_nio_admission(room, event, SimpleNamespace(value="history"))

    assert session._recovered_event_count == 1
    assert session._history_event_count == 1
    assert [provenance for _event, provenance in seen] == ["recovered", "history"]


async def test_admission_without_native_identity_skips_deferral_tracking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An event lacking room/event identity has no stable key to track."""

    async def defer(_event: dict[str, object], _provenance: str) -> None:
        raise MatrixAttachmentFetchDeferredError("generated", ("network",))

    rejection = type("CallbackNotAcceptedError", (Exception,), {})
    monkeypatch.setitem(
        sys.modules, "nio", _nio_module(CallbackNotAcceptedError=rejection)
    )
    session = MatrixSession(make_matrix_config(), admission_callback=defer)
    room = SimpleNamespace(room_id="")
    event = SimpleNamespace(
        sender="@alice:example.org",
        event_id="$media",
        body="photo.png",
        source={
            "event_id": "$media",
            "sender": "@alice:example.org",
            "type": "m.room.message",
            "content": {},
        },
    )

    with pytest.raises(rejection):
        await session._on_nio_admission(room, event, SimpleNamespace(value="live"))

    # No stable native identity means no deferral state was recorded.
    assert session._attachment_fetch_deferrals == {}
