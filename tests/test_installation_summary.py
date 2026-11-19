"""Startup header lines report installed core and provider provenance."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from medre.runtime import installation_summary


class _FakeDist:
    def __init__(self, name: str, version: str, direct: dict[str, Any] | None = None):
        self._name = name
        self._version = version
        self._direct = direct
        self.metadata = {"Name": name}

    @property
    def version(self) -> str:
        return self._version

    def read_text(self, filename: str) -> str | None:
        if filename == "direct_url.json" and self._direct is not None:
            return json.dumps(self._direct)
        return None


def _entry(dist: _FakeDist, name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, dist=dist)


GIT_MATRIX = _FakeDist(
    "medre-adapter-matrix",
    "0.1.0",
    {
        "url": "https://github.com/jeremiah-k/medre-adapter-matrix",
        "vcs_info": {
            "vcs": "git",
            "commit_id": "c987da62baea6b31627d4f8fc5f5b6c69d07f844",
        },
    },
)
INDEX_MESHTASTIC = _FakeDist("medre-adapter-meshtastic", "0.1.1")


def _install_environment(
    monkeypatch: pytest.MonkeyPatch, providers: list[SimpleNamespace]
) -> None:
    def version(name: str) -> str:
        assert name == "medre"
        return "0.1.1"

    monkeypatch.setattr(installation_summary.importlib.metadata, "version", version)
    monkeypatch.setattr(
        installation_summary.importlib.metadata,
        "distribution",
        lambda name: _FakeDist(name, "0.1.1"),
    )

    def entry_points(*, group: str):
        assert group == "medre.adapters"
        return providers

    monkeypatch.setattr(
        installation_summary.importlib.metadata, "entry_points", entry_points
    )


def test_banner_reports_core_python_and_provider_provenance(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_environment(
        monkeypatch,
        [
            _entry(GIT_MATRIX, "matrix"),
            _entry(INDEX_MESHTASTIC, "meshtastic"),
            _entry(_FakeDist("medre", "0.1.1"), "builtin"),
        ],
    )
    monkeypatch.setattr(
        installation_summary.platform, "python_version", lambda: "3.14.8"
    )
    monkeypatch.setattr(installation_summary.platform, "system", lambda: "Linux")
    monkeypatch.setattr(installation_summary.platform, "machine", lambda: "x86_64")
    lines = installation_summary.installation_lines()
    assert lines == [
        "  Core:    medre 0.1.1 (pypi)",
        "  Python:  3.14.8 (Linux x86_64)",
        "  Adapter: matrix 0.1.0 (github:jeremiah-k/medre-adapter-matrix@c987da62)",
        "  Adapter: meshtastic 0.1.1 (pypi)",
    ]


def test_provider_identities_share_the_banner_rows(monkeypatch: pytest.MonkeyPatch):
    _install_environment(
        monkeypatch,
        [_entry(GIT_MATRIX, "matrix"), _entry(INDEX_MESHTASTIC, "meshtastic")],
    )
    rows, incomplete = installation_summary.provider_identities()
    assert not incomplete
    assert [(row.transport, row.version, row.source) for row in rows] == [
        ("matrix", "0.1.0", "github:jeremiah-k/medre-adapter-matrix@c987da62"),
        ("meshtastic", "0.1.1", "pypi"),
    ]
    banner = installation_summary.installation_lines()
    assert banner[2:] == [
        f"  Adapter: {row.transport} {row.version} ({row.source})" for row in rows
    ]


def test_community_distribution_names_are_surfaced(monkeypatch: pytest.MonkeyPatch):
    _install_environment(
        monkeypatch, [_entry(_FakeDist("community-radio", "1.2.3"), "radio")]
    )
    rows, _incomplete = installation_summary.provider_identities()
    assert rows[0].distribution != installation_summary.standard_distribution("radio")
    assert (
        "  Adapter: radio 1.2.3 (pypi) [community-radio]"
        in installation_summary.installation_lines()
    )


def test_first_party_distribution_names_omit_brackets(monkeypatch: pytest.MonkeyPatch):
    _install_environment(
        monkeypatch,
        [_entry(_FakeDist("Medre.Adapter.MeshCore", "1.0.0"), "meshcore")],
    )
    assert installation_summary.standard_distribution("meshcore") == (
        "medre-adapter-meshcore"
    )
    assert installation_summary.installation_lines()[-1] == (
        "  Adapter: meshcore 1.0.0 (pypi)"
    )


def test_editable_and_url_labels_redact_credentials(monkeypatch: pytest.MonkeyPatch):
    editable = _FakeDist(
        "medre-adapter-lxmf",
        "0.1.0",
        {"url": "file:///home/operator/dev/lxmf", "dir_info": {"editable": True}},
    )
    wheel = _FakeDist(
        "community-wheel",
        "2.0.0",
        {
            "url": "https://operator:fixture-pass@example.invalid/pkg.whl?token=fixture-secret-sentinel#fixture-fragment",
            "archive_info": {},
        },
    )
    _install_environment(
        monkeypatch, [_entry(editable, "lxmf"), _entry(wheel, "extra")]
    )
    text = "\n".join(installation_summary.installation_lines())
    assert "editable:file:///home/operator/dev/lxmf" in text
    assert "fixture-secret-sentinel" not in text
    assert "fixture-pass" not in text
    assert "fixture-fragment" not in text
    assert "url:https://example.invalid/pkg.whl?redacted" in text


def test_local_snapshot_installs_use_the_local_label():
    assert (
        installation_summary.provenance_from_direct(
            {"url": "file:///opt/providers/pkg", "dir_info": {}}
        )
        == "local:file:///opt/providers/pkg"
    )


def test_git_forge_urls_compact_to_owner_repo_at_short_commit():
    assert (
        installation_summary.provenance_from_direct(
            {
                "url": "https://github.com/jeremiah-k/medre-adapter-matrix.git",
                "vcs_info": {
                    "vcs": "git",
                    "commit_id": "d2619ed8efa621984591d2a50466e4c32a952de6",
                },
            }
        )
        == "github:jeremiah-k/medre-adapter-matrix@d2619ed8"
    )


def test_non_forge_git_hosts_keep_host_and_repo():
    direct = {
        "url": "https://git.example.com/group/subgroup/repo",
        "vcs_info": {"vcs": "git", "commit_id": "a" * 40},
    }
    assert (
        installation_summary.provenance_from_direct(direct)
        == f"git:git.example.com/group/subgroup/repo@{'a' * 8}"
    )


def test_vcs_scheme_prefix_and_uppercase_host_resolve_to_the_forge():
    assert (
        installation_summary.provenance_from_direct(
            {
                "url": "git+ssh://git@GitHub.com/owner/repo.git",
                "vcs_info": {"vcs": "git", "commit_id": "B" * 64},
            }
        )
        == f"github:owner/repo@{'B' * 8}"
    )


def test_git_credentials_never_reach_the_label():
    assert (
        installation_summary.provenance_from_direct(
            {
                "url": "https://user:fixture-pass@github.com/owner/repo",
                "vcs_info": {"vcs": "git", "commit_id": "c" * 40},
            }
        )
        == "github:owner/repo@cccccccc"
    )


def test_sha256_git_commit_is_abbreviated():
    assert (
        installation_summary.provenance_from_direct(
            {
                "url": "https://github.com/owner/repo",
                "vcs_info": {"vcs": "git", "commit_id": "d" * 64},
            }
        )
        == "github:owner/repo@dddddddd"
    )


def test_corrupt_direct_metadata_degrades_per_distribution(
    monkeypatch: pytest.MonkeyPatch,
):
    broken = _FakeDist("medre-adapter-xmpp", "0.1.0", {"unexpected": "shape"})
    broken.read_text = lambda filename: "{not json"  # type: ignore[method-assign]
    _install_environment(
        monkeypatch, [_entry(broken, "xmpp"), _entry(INDEX_MESHTASTIC, "meshtastic")]
    )
    lines = installation_summary.installation_lines()
    assert "  Adapter: xmpp 0.1.0 (unknown)" in lines
    assert "  Adapter: meshtastic 0.1.1 (pypi)" in lines


def test_provider_failures_never_block_the_banner(monkeypatch: pytest.MonkeyPatch):
    def entry_points(*, group: str):
        raise RuntimeError("metadata exploded")

    monkeypatch.setattr(
        installation_summary.importlib.metadata, "entry_points", entry_points
    )
    rows, incomplete = installation_summary.provider_identities()
    assert rows == [] and incomplete
    lines = installation_summary.installation_lines()
    assert lines and lines[0].startswith("  Core:")


def test_core_distribution_missing_reports_source_tree(
    monkeypatch: pytest.MonkeyPatch,
):
    missing = installation_summary.importlib.metadata.PackageNotFoundError

    def version(name: str):
        raise missing(name)

    monkeypatch.setattr(installation_summary.importlib.metadata, "version", version)
    monkeypatch.setattr(
        installation_summary.importlib.metadata,
        "distribution",
        lambda name: (_ for _ in ()).throw(missing(name)),
    )
    monkeypatch.setattr(
        installation_summary.importlib.metadata, "entry_points", lambda **kw: []
    )
    assert installation_summary.core_source_label() == "source"
    lines = installation_summary.installation_lines()
    expected = (
        f"  Core:    medre {installation_summary._CORE_FALLBACK_VERSION} (source)"
    )
    assert expected in lines


def test_core_source_label_shares_the_installed_formatter(monkeypatch):
    _install_environment(monkeypatch, [])
    assert installation_summary.core_source_label() == "pypi"


@pytest.mark.parametrize("scheme", ["ftp", "HTTPS", "sftp"])
def test_direct_source_redaction_all_schemes(scheme: str):
    dist = _FakeDist(
        "community",
        "1.0",
        {
            "url": f"{scheme}://user:fixture-pass@example.invalid/pkg.whl?token=fixture-query#fixture-fragment",
            "archive_info": {},
        },
    )
    assert (
        installation_summary._source_label(dist)
        == f"url:{scheme.lower()}://example.invalid/pkg.whl?redacted"
    )


@pytest.mark.parametrize(
    ("direct", "expected"),
    [
        ({}, "unknown"),
        ([], "unknown"),
        (None, "unknown"),
        ("file:///work/repo", "unknown"),
        ({"url": "https://user:fixture-pass@example.invalid/pkg.whl"}, "unknown"),
        ({"url": "https://example.invalid/pkg.whl", "archive_info": []}, "unknown"),
        (
            {
                "url": "https://example.invalid/pkg.whl",
                "dir_info": {"editable": True},
            },
            "unknown",
        ),
        (
            {
                "url": "file:///work/repo",
                "dir_info": {"editable": True},
                "vcs_info": {"vcs": "git", "commit_id": "a" * 40},
            },
            "unknown",
        ),
        ({"url": "file:///work/repo", "dir_info": {"editable": "false"}}, "unknown"),
        (
            {
                "url": "https://example.invalid/repo",
                "vcs_info": {"vcs": "git", "commit_id": "fixture-secret"},
            },
            "unknown",
        ),
        (
            {
                "url": "https://user:fixture pass@example.invalid/pkg.whl",
                "archive_info": {},
            },
            "unknown",
        ),
        ({"url": "https://[invalid", "archive_info": {}}, "unknown"),
        ({"url": "relative/pkg.whl", "archive_info": {}}, "unknown"),
        ({"url": "https:///pkg.whl", "archive_info": {}}, "unknown"),
        (
            {"url": "https://example.invalid/repo", "vcs_info": {"vcs": "git"}},
            "unknown",
        ),
        (
            {
                "url": "https://example.invalid/repo",
                "vcs_info": {"vcs": "perforce", "commit_id": "a" * 40},
            },
            "unknown",
        ),
        (
            {
                "url": "https://github.com/owner/repo",
                "vcs_info": {"vcs": "git", "commit_id": 12345},
            },
            "unknown",
        ),
    ],
)
def test_invalid_direct_metadata_is_not_reported_as_provenance(direct, expected):
    assert installation_summary.provenance_from_direct(direct) == expected


def _metadata_dist(
    tmp_path: Path, name: str, metadata: bytes, direct: bytes | None = None
):
    path = tmp_path / f"{name}-1.0.dist-info"
    path.mkdir()
    (path / "METADATA").write_bytes(metadata)
    if direct is not None:
        (path / "direct_url.json").write_bytes(direct)
    return installation_summary.importlib.metadata.PathDistribution(path)


def test_invalid_core_metadata_is_best_effort(monkeypatch, tmp_path):
    dist = _metadata_dist(tmp_path, "medre", b"\xff", b"\xff")
    monkeypatch.setattr(
        installation_summary.importlib.metadata, "version", lambda name: dist.version
    )
    monkeypatch.setattr(
        installation_summary.importlib.metadata, "distribution", lambda name: dist
    )
    monkeypatch.setattr(
        installation_summary.importlib.metadata, "entry_points", lambda **kw: []
    )
    assert installation_summary.installation_lines()[0] == (
        "  Core:    medre ? (unknown)"
    )


def test_damaged_provider_identity_and_provenance_are_isolated(monkeypatch, tmp_path):
    bad_name = _metadata_dist(tmp_path, "bad", b"\xff")
    bad_url = _metadata_dist(
        tmp_path,
        "community",
        b"Name: community\nVersion: 1.0\n",
        b"\xff",
    )
    _install_environment(
        monkeypatch,
        [
            SimpleNamespace(name="bad", dist=bad_name),
            SimpleNamespace(name="community", dist=bad_url),
            _entry(INDEX_MESHTASTIC, "meshtastic"),
            _entry(_FakeDist("MEDRE", "1.0"), "builtin"),
            _entry(_FakeDist("MEDRE_Adapter.Matrix", "1.0"), "matrix"),
        ],
    )
    assert installation_summary.installation_lines()[2:] == [
        "  Adapter: community 1.0 (unknown) [community]",
        "  Adapter: matrix 1.0 (pypi)",
        "  Adapter: meshtastic 0.1.1 (pypi)",
        "  Adapter inventory incomplete (metadata unavailable)",
    ]


def test_partial_entry_point_discovery_preserves_visited_entries(monkeypatch):
    _install_environment(monkeypatch, [])

    def discover(**kwargs):
        yield _entry(INDEX_MESHTASTIC, "meshtastic")
        raise RuntimeError("fixture-secret")

    monkeypatch.setattr(
        installation_summary.importlib.metadata, "entry_points", discover
    )
    rows, incomplete = installation_summary.provider_identities()
    assert [(row.transport, row.source) for row in rows] == [("meshtastic", "pypi")]
    assert incomplete
    assert installation_summary.installation_lines()[2:] == [
        "  Adapter: meshtastic 0.1.1 (pypi)",
        "  Adapter inventory incomplete (metadata unavailable)",
    ]


def test_missing_or_invalid_versions_do_not_hide_provenance(monkeypatch):
    dist = _FakeDist("community-radio", "1.0\nforged log line")
    _install_environment(monkeypatch, [_entry(dist, "radio")])
    assert installation_summary.installation_lines()[2:] == [
        "  Adapter: radio ? (pypi) [community-radio]"
    ]


def test_version_read_exception_preserves_provenance(monkeypatch):
    class UnreadableVersion(_FakeDist):
        @property
        def version(self) -> str:
            raise OSError("fixture-secret")

    dist = UnreadableVersion("community-radio", "1.0")
    _install_environment(monkeypatch, [_entry(dist, "radio")])
    rows, _incomplete = installation_summary.provider_identities()
    assert rows == [
        installation_summary.ProviderIdentity(
            transport="radio",
            distribution="community-radio",
            version="?",
            source="pypi",
        )
    ]


def test_core_source_lookup_exception_preserves_version(monkeypatch):
    _install_environment(monkeypatch, [])

    def failed(name):
        raise OSError("fixture-secret")

    monkeypatch.setattr(installation_summary.importlib.metadata, "distribution", failed)
    assert installation_summary.installation_lines()[0] == (
        "  Core:    medre 0.1.1 (unknown)"
    )


def test_missing_entry_identity_marks_inventory_incomplete(monkeypatch):
    missing = _FakeDist("community-radio", "1.0")
    missing.metadata = {}
    _install_environment(
        monkeypatch,
        [
            SimpleNamespace(name="missing", dist=None),
            _entry(missing, "missing-name"),
            _entry(_FakeDist("invalid name", "1.0"), "bad-name"),
            _entry(INDEX_MESHTASTIC, "meshtastic"),
        ],
    )
    assert installation_summary.installation_lines()[2:] == [
        "  Adapter: meshtastic 0.1.1 (pypi)",
        "  Adapter inventory incomplete (metadata unavailable)",
    ]


def test_platform_and_discovery_failure_never_leak_exception_details(monkeypatch):
    _install_environment(monkeypatch, [])

    def failed():
        raise OSError("fixture-secret")

    monkeypatch.setattr(installation_summary.platform, "machine", failed)
    monkeypatch.setattr(installation_summary, "_provider_lines", failed)
    assert installation_summary.installation_lines()[1:] == [
        "  Python:  unknown",
        "  Adapter inventory incomplete (metadata unavailable)",
    ]


@pytest.mark.parametrize(
    ("kind", "commit", "expected"),
    [("hg", "a" * 40, "hg:aaaaaaaa"), ("svn", "12345", "svn:12345")],
)
def test_recognized_non_git_vcs_labels(kind, commit, expected):
    dist = _FakeDist(
        "community",
        "1.0",
        {
            "url": "https://example.invalid/repo",
            "vcs_info": {"vcs": kind, "commit_id": commit},
        },
    )
    assert installation_summary._source_label(dist) == expected


def test_hg_sha256_commit_is_abbreviated():
    assert (
        installation_summary.provenance_from_direct(
            {
                "url": "https://example.invalid/repo",
                "vcs_info": {"vcs": "hg", "commit_id": "e" * 64},
            }
        )
        == "hg:eeeeeeee"
    )


def test_gitlab_nested_repository_label():
    assert (
        installation_summary.provenance_from_direct(
            {
                "url": "https://gitlab.com/team/subgroup/provider.git",
                "vcs_info": {"vcs": "git", "commit_id": "a" * 40},
            }
        )
        == "gitlab:team/subgroup/provider@aaaaaaaa"
    )


@pytest.mark.parametrize(
    "url", ["file:///srv/repo.git", "file://server/share/repo.git"]
)
def test_file_git_provenance_preserves_record(url):
    from medre.runtime.installation_summary import describe_direct_source

    commit = "a" * 40
    direct = {"url": url, "vcs_info": {"vcs": "git", "commit_id": commit}}
    label, record = describe_direct_source(direct)
    assert label == f"git:{url}@aaaaaaaa"
    assert record == {"kind": "git", "url": url, "commit": commit, "editable": False}
    direct["vcs_info"]["commit_id"] = "invalid"
    assert describe_direct_source(direct) == ("unknown", None)
