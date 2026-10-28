"""Explicit stopped capture keeps ownership across dynamic native resolution."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from medre.maintenance import stopped_declare as sd


def _config(root: Path):
    return SimpleNamespace(storage=SimpleNamespace(path=str(root / "core.sqlite")))


def _core_and_dynamic(monkeypatch, tmp_path, declaration):
    monkeypatch.setattr(
        sd,
        "_prepare_instances",
        lambda *_: iter(
            [("test", "one", "real", SimpleNamespace(), SimpleNamespace())]
        ),
    )
    monkeypatch.setattr(sd, "declaration_from_spec", lambda _spec: declaration)
    monkeypatch.setattr(
        "medre.maintenance.stopped_snapshot.create_stopped_snapshot",
        lambda *_args, **_kwargs: None,
    )


def test_resolver_runs_under_held_exclusion_and_releases_after_capture(
    monkeypatch, tmp_path
):
    calls = []

    class Exclusion(sd.LocalExclusion):
        def __init__(self):
            super().__init__("test", note="probe")
            self.scope = "local_only"

        def close(self):
            calls.append("close")
            super().close()

    class Dynamic:
        provider = "test"

        def coverage(self, _config, *, adapter_id):
            calls.append("preview")
            return sd.declared(
                provider="test",
                adapter_id=adapter_id,
                files=(),
                state_class="unsupported",
            )

        def local_exclusion(self, _config, *, adapter_id):
            calls.append("acquire")
            return Exclusion()

        def resolve_coverage(self, _config, *, adapter_id):
            calls.append("resolve")
            return sd.declared(
                provider="test",
                adapter_id=adapter_id,
                files=(
                    sd.DeclaredFile(tmp_path / "native", "native_identity", "opaque"),
                ),
            )

    _core_and_dynamic(monkeypatch, tmp_path, Dynamic())
    monkeypatch.setattr(
        "medre.maintenance.stopped_snapshot.create_stopped_snapshot",
        lambda *_args, **_kwargs: calls.append("capture"),
    )
    result = sd.capture_configured_stopped_state(
        _config(tmp_path),
        SimpleNamespace(),
        common_root=tmp_path,
        snapshot=tmp_path / "snapshot",
        runtime_stopped=True,
    )
    assert calls == ["preview", "acquire", "resolve", "capture", "close"]
    assert any(entry.path == "native" for entry in result.entries)


def test_required_export_refuses_complete_capture_and_closes(monkeypatch, tmp_path):
    closed = []

    class Exclusion(sd.LocalExclusion):
        def __init__(self):
            super().__init__("test", note="probe")
            self.scope = "local_only"

        def close(self):
            closed.append(True)
            super().close()

    class Dynamic:
        provider = "test"

        def coverage(self, _config, *, adapter_id):
            return sd.declared(
                provider="test",
                adapter_id=adapter_id,
                files=(),
                state_class="unsupported",
            )

        def local_exclusion(self, _config, *, adapter_id):
            return Exclusion()

        def resolve_coverage(self, _config, *, adapter_id):
            return sd.declared(
                provider="test",
                adapter_id=adapter_id,
                state_class="externally_managed",
                files=(
                    sd.DeclaredFile(
                        tmp_path / "secret",
                        "native_identity",
                        "opaque",
                        presence="export_only",
                    ),
                ),
            )

    _core_and_dynamic(monkeypatch, tmp_path, Dynamic())
    with pytest.raises(sd.StoppedPlanError, match="unsupported native export"):
        sd.capture_configured_stopped_state(
            _config(tmp_path),
            SimpleNamespace(),
            common_root=tmp_path,
            snapshot=tmp_path / "snapshot",
            runtime_stopped=True,
        )
    assert closed == [True]


def test_dynamic_provider_without_exclusion_is_refused(monkeypatch, tmp_path):
    class Dynamic:
        provider = "test"

        def coverage(self, _config, *, adapter_id):
            return sd.declared(
                provider="test",
                adapter_id=adapter_id,
                files=(),
                state_class="unsupported",
            )

        def local_exclusion(self, _config, *, adapter_id):
            return sd.LocalExclusion("test", note="unsupported")

        def resolve_coverage(self, _config, *, adapter_id):
            raise AssertionError("must not resolve without exclusion")

    _core_and_dynamic(monkeypatch, tmp_path, Dynamic())
    with pytest.raises(sd.ExclusionRefused):
        sd.capture_configured_stopped_state(
            _config(tmp_path),
            SimpleNamespace(),
            common_root=tmp_path,
            snapshot=tmp_path / "snapshot",
            runtime_stopped=True,
        )


def test_externally_managed_zero_file_state_is_not_complete(monkeypatch, tmp_path):
    class External:
        provider = "test"

        def coverage(self, _config, *, adapter_id):
            return sd.declared(
                provider="test",
                adapter_id=adapter_id,
                files=(),
                state_class="externally_managed",
            )

        def local_exclusion(self, _config, *, adapter_id):
            raise AssertionError("externally managed provider has no host lease")

    _core_and_dynamic(monkeypatch, tmp_path, External())
    with pytest.raises(sd.StoppedPlanError, match="unsupported native export"):
        sd.capture_configured_stopped_state(
            _config(tmp_path),
            SimpleNamespace(),
            common_root=tmp_path,
            snapshot=tmp_path / "snapshot",
            runtime_stopped=True,
        )


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_capture_failure_releases_all_providers_in_reverse_order(
    monkeypatch, tmp_path, failure
):
    closed = []

    class Held(sd.LocalExclusion):
        def __init__(self, label):
            super().__init__("test", note="probe")
            self.label = label
            self.scope = "local_only"

        def close(self):
            closed.append(self.label)
            super().close()

    class Static:
        provider = "test"

        def coverage(self, config, *, adapter_id):
            return sd.declared(
                provider=self.provider,
                adapter_id=adapter_id,
                files=(
                    sd.DeclaredFile(tmp_path / adapter_id, "native_identity", "opaque"),
                ),
            )

        def local_exclusion(self, config, *, adapter_id):
            return Held(adapter_id)

    _core_and_dynamic(monkeypatch, tmp_path, Static())
    monkeypatch.setattr(
        sd,
        "_prepare_instances",
        lambda *_: iter(
            [("test", label, "real", None, None) for label in ("first", "second")]
        ),
    )

    def failed_capture(*args, **kwargs):
        assert closed == []
        raise failure()

    monkeypatch.setattr(
        "medre.maintenance.stopped_snapshot.create_stopped_snapshot", failed_capture
    )
    with pytest.raises(failure):
        sd.capture_configured_stopped_state(
            _config(tmp_path),
            SimpleNamespace(),
            common_root=tmp_path,
            snapshot=tmp_path / "snapshot",
            runtime_stopped=True,
        )
    assert closed == ["second", "first"]
    assert not (tmp_path / "snapshot").exists()


@pytest.mark.parametrize("provider, adapter_id", [("other", "one"), ("test", "other")])
def test_resolved_coverage_cannot_rebind_provider_or_instance(
    monkeypatch, tmp_path, provider, adapter_id
):
    class Dynamic:
        provider = "test"

        def coverage(self, config, *, adapter_id):
            return sd.declared(
                provider="test",
                adapter_id=adapter_id,
                files=(),
                state_class="unsupported",
            )

        def local_exclusion(self, config, *, adapter_id):
            held = sd.LocalExclusion("test", note="probe")
            held.scope = "local_only"
            return held

        def resolve_coverage(self, config, **kwargs):
            return sd.declared(
                provider=provider,
                adapter_id=adapter_id,
                files=(
                    sd.DeclaredFile(tmp_path / "native", "native_identity", "opaque"),
                ),
            )

    _core_and_dynamic(monkeypatch, tmp_path, Dynamic())
    with pytest.raises(sd.StoppedPlanError, match="another instance"):
        sd.capture_configured_stopped_state(
            _config(tmp_path),
            SimpleNamespace(),
            common_root=tmp_path,
            snapshot=tmp_path / "snapshot",
            runtime_stopped=True,
        )


def test_flock_refuses_fifo_without_waiting_for_a_writer(tmp_path):
    import os
    import subprocess
    import sys

    if not hasattr(os, "mkfifo"):
        pytest.skip("platform has no FIFO primitive")
    fifo = tmp_path / "not-a-lock"
    os.mkfifo(fifo)
    # A subprocess timeout makes a blocking-open regression fail without
    # leaving pytest itself stuck inside the operating-system open call.
    code = """
import sys
from pathlib import Path
from medre.maintenance.stopped_declare import ExclusionRefused, LocalExclusion
try:
    LocalExclusion('probe', note='test').retain_flock(Path(sys.argv[1]), purpose='lock')
except ExclusionRefused:
    pass
else:
    raise AssertionError('FIFO accepted as lock')
"""
    subprocess.run([sys.executable, "-c", code, str(fifo)], check=True, timeout=10)
