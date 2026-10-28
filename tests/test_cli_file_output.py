"""Exclusive topology output preserves other writers and publishes atomically."""

from __future__ import annotations

import os
import stat

import pytest

from medre.cli import file_output


def test_existing_file_is_never_replaced(tmp_path):
    output = tmp_path / "graph"
    output.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        file_output.write_private_output(output, b"new")
    assert output.read_bytes() == b"existing"
    assert not list(tmp_path.glob(".medre-export-*"))


@pytest.mark.parametrize("failure", [OSError("disk full"), KeyboardInterrupt()])
def test_failed_and_interrupted_writes_never_publish_partial_file(
    tmp_path, monkeypatch, failure
):
    output = tmp_path / "graph"

    def fail(_descriptor):
        assert not output.exists()
        raise failure

    monkeypatch.setattr(file_output.os, "fsync", fail)
    with pytest.raises(type(failure)):
        file_output.write_private_output(output, b"partial")
    assert not output.exists()
    assert not list(tmp_path.glob(".medre-export-*"))


def test_concurrent_destination_creator_is_not_overwritten(tmp_path, monkeypatch):
    output = tmp_path / "graph"
    original_link = os.link

    def replace_then_link(source, destination):
        output.write_bytes(b"another writer")
        original_link(source, destination)

    monkeypatch.setattr(file_output.os, "link", replace_then_link)
    with pytest.raises(FileExistsError):
        file_output.write_private_output(output, b"ours")
    assert output.read_bytes() == b"another writer"
    assert not list(tmp_path.glob(".medre-export-*"))


def test_descriptor_setup_failure_does_not_leak_descriptor(tmp_path, monkeypatch):
    output = tmp_path / "graph"
    opened = []
    original_mkstemp = file_output.tempfile.mkstemp
    original_fstat = os.fstat

    def track(*args, **kwargs):
        descriptor, name = original_mkstemp(*args, **kwargs)
        opened.append(descriptor)
        return descriptor, name

    def fail(_descriptor, _mode):
        raise OSError("setup failed")

    with pytest.raises(OSError, match="setup failed"):
        with monkeypatch.context() as patch:
            patch.setattr(file_output.tempfile, "mkstemp", track)
            patch.setattr(file_output.os, "fdopen", fail)
            file_output.write_private_output(output, b"payload")
    with pytest.raises(OSError):
        original_fstat(opened[0])
    assert not output.exists()
    assert not list(tmp_path.glob(".medre-export-*"))


def test_atomic_publication_is_private_and_complete(tmp_path):
    path = tmp_path / "diagram.svg"
    file_output.write_private_output(path, b"<svg>complete</svg>")
    assert path.read_bytes() == b"<svg>complete</svg>"
    assert not list(tmp_path.glob(".medre-export-*"))
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.stat().st_nlink == 1


def test_existing_symlink_is_never_replaced(tmp_path):
    if os.name == "nt":
        pytest.skip("symlink privilege varies by Windows installation")
    output = tmp_path / "graph"
    other = tmp_path / "other"
    other.write_bytes(b"original")
    output.symlink_to(other)
    with pytest.raises(FileExistsError):
        file_output.write_private_output(output, b"new")
    assert output.is_symlink()
    assert other.read_bytes() == b"original"
    assert not list(tmp_path.glob(".medre-export-*"))


@pytest.mark.skipif(os.name == "nt", reason="directory fsync is POSIX-only")
def test_publication_syncs_the_parent_after_complete_file_contents(
    tmp_path, monkeypatch
):
    path = tmp_path / "graph.svg"
    calls = []
    original = os.fsync

    def sync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            assert path.read_bytes() == b"complete"
            calls.append("directory")
        else:
            assert not path.exists()
            calls.append("contents")
        original(descriptor)

    monkeypatch.setattr(file_output.os, "fsync", sync)
    file_output.write_private_output(path, b"complete")
    assert calls == ["contents", "directory"]


@pytest.mark.skipif(os.name == "nt", reason="directory fsync is POSIX-only")
@pytest.mark.parametrize("failure", ["open", "sync"])
def test_unsupported_directory_sync_retains_a_complete_export(
    tmp_path, monkeypatch, failure
):
    path = tmp_path / "graph.svg"
    original_open, original_sync = os.open, os.fsync

    def open_directory(name, *args, **kwargs):
        if name == tmp_path and failure == "open":
            raise OSError("directory open unsupported")
        return original_open(name, *args, **kwargs)

    def sync(descriptor):
        if failure == "sync" and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("directory sync unsupported")
        original_sync(descriptor)

    monkeypatch.setattr(file_output.os, "open", open_directory)
    monkeypatch.setattr(file_output.os, "fsync", sync)
    file_output.write_private_output(path, b"complete")
    assert path.read_bytes() == b"complete"
    assert not list(tmp_path.glob(".medre-export-*"))
