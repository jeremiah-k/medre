"""The fixture predecessor matches the pinned released wheel exactly.

Every declared catalog object — tables, indexes, triggers, views — is
compared as its full ``(type, name, sql)`` triple between the released
0.1.0 wheel source and the committed fixture, so a renamed index or a
re-columned table cannot hide behind a matching name set.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import zipfile
from pathlib import Path

import pytest

_FIXTURE = Path(__file__).parent / "fixtures/released-storage-0.1.0.sql"
_FIXTURE_SHA256 = "623dceec6e92745222c98dac94de976c66bdb50834fe588fa373520822295811"
_RELEASED_010_WHEEL = "medre-0.1.0-py3-none-any.whl"


def _catalog_objects(ddl: str) -> set[tuple[str, str, str]]:
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(ddl)
        return {
            (str(row[0]), str(row[1]), str(row[2]))
            for row in reference.execute(
                "SELECT type,name,sql FROM sqlite_master"
                " WHERE name NOT GLOB 'sqlite_*' AND sql IS NOT NULL"
            )
        }
    finally:
        reference.close()


@pytest.fixture(scope="module")
def wheel_catalog() -> set[tuple[str, str, str]]:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts/verify_released_artifact_continuity.py"
    )
    spec = importlib.util.spec_from_file_location(
        "medre_verify_released_artifact_continuity", script
    )
    assert spec is not None and spec.loader is not None
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    cache = driver.default_artifact_cache()
    if _RELEASED_010_WHEEL not in driver.cache_status(cache):
        pytest.skip("released 0.1.0 wheel absent from the local artifact cache")
    wheel = cache / _RELEASED_010_WHEEL
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert digest == driver._PINNED_ARTIFACTS[_RELEASED_010_WHEEL]
    with zipfile.ZipFile(wheel) as archive:
        source = archive.read("medre/core/storage/sqlite/schema.py").decode("utf-8")
    namespace: dict[str, object] = {"__name__": "released_schema"}
    exec(
        compile(source, "released_schema.py", "exec"), namespace
    )  # nosec B102 # Pinned, digest-verified released wheel source.
    return _catalog_objects(str(namespace["_SCHEMA"]) + str(namespace["_INDEXES"]))


def test_fixture_matches_wheel_object_for_object(
    wheel_catalog: set[tuple[str, str, str]],
) -> None:
    ddl = _FIXTURE.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(ddl).hexdigest() == _FIXTURE_SHA256
    fixture_catalog = _catalog_objects(ddl.decode("utf-8"))
    assert fixture_catalog == wheel_catalog
