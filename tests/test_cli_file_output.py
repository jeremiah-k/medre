"""Exclusive topology output preserves other writers and releases failed writes."""

from __future__ import annotations

import os

import pytest

from medre.cli import file_output


def test_existing_file_is_never_replaced(tmp_path):
    output = tmp_path / "graph"
    output.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        file_output.write_private_output(output, b"new")
    assert output.read_bytes() == b"existing"


@pytest.mark.parametrize("failure", [OSError("disk full"), KeyboardInterrupt()])
def test_failed_and_interrupted_writes_remove_only_their_file(
    tmp_path, monkeypatch, failure
):
    output = tmp_path / "graph"

    def fail(_descriptor):
        raise failure

    monkeypatch.setattr(file_output.os, "fsync", fail)
    with pytest.raises(type(failure)):
        file_output.write_private_output(output, b"partial")
    assert not output.exists()


def test_failed_export_keeps_concurrent_replacement(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("replacing an open file is a POSIX race")
    output = tmp_path / "graph"

    def replace_then_fail(_descriptor):
        output.unlink()
        output.write_bytes(b"another writer")
        raise OSError("disk full")

    monkeypatch.setattr(file_output.os, "fsync", replace_then_fail)
    with pytest.raises(OSError):
        file_output.write_private_output(output, b"ours")
    assert output.read_bytes() == b"another writer"


def test_descriptor_setup_failure_does_not_leak_descriptor(tmp_path, monkeypatch):
    output = tmp_path / "graph"
    opened = []
    original_open = os.open
    original_fstat = os.fstat

    def track(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def fail(_descriptor):
        raise OSError("setup failed")

    with pytest.raises(OSError, match="setup failed"):
        with monkeypatch.context() as patch:
            patch.setattr(file_output.os, "open", track)
            patch.setattr(file_output.os, "fstat", fail)
            file_output.write_private_output(output, b"payload")
    with pytest.raises(OSError):
        original_fstat(opened[0])
    assert not output.exists()
