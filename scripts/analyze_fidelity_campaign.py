"""Recheck retained native conversation fidelity without contacting services."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import re

from medre.config.identifiers import adapter_id_problem
from medre.core.storage.sqlite.storage import SQLiteStorage

from scripts.adapter_campaign.fidelity import CASES
from scripts.adapter_campaign.presentation import (
    CASES as PRESENTATION_CASES,
    example as presentation_example,
)
from scripts.adapter_campaign.fidelity_evidence import verify_case
from scripts.adapter_campaign.recovery import read_event
from scripts.adapter_campaign.lab.log_review import analyze as analyze_log


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


async def analyze(directory: Path) -> dict:
    if not __debug__:
        raise RuntimeError("Fidelity analysis requires enabled assertions")
    report = json.loads((directory / "results.json").read_text())
    assert (
        type(report["version"]) is int
        and report["version"] in {1, 2}
        and report["status"] == "passed"
        and not report["cleanup_errors"]
    ), "Incomplete fidelity campaign"
    cases = CASES + PRESENTATION_CASES if report["version"] == 2 else CASES
    prefix = report["run_prefix"]
    assert re.fullmatch(r"MF-[0-9a-f]{12}-", prefix), "Invalid fidelity nonce"
    topology = report["expectations"]
    assert len(topology) == 2 and {v["transport"] for v in topology.values()} == {
        "matrix",
        "xmpp",
    }, "Invalid native topology"
    peers = {}
    for name, endpoint in topology.items():
        assert not adapter_id_problem(name), "Invalid adapter identity"
        assert all(
            isinstance(endpoint.get(key), str) and endpoint[key]
            for key in ("channel", "peer_identity", "bot_identity")
        ), "Missing native expectation"
        peers[name] = records(directory / (name + ".jsonl"))
        assert not any(r["kind"] == "error" for r in peers[name]), "Native peer failed"
        ready = [r for r in peers[name] if r["kind"] == "ready"]
        assert (
            len(ready) == 1
            and ready[0]["identity"]
            == endpoint["peer_identity"]
            != endpoint["bot_identity"]
        )
    runtime_ready = {}
    for name in ("runtime-0", "runtime-1"):
        control = records(directory / (name + ".jsonl"))
        assert sum(r["kind"] == "ready" for r in control) == 1 and not any(
            r["kind"] == "error" for r in control
        ), "Runtime generation failed"
        runtime_ready[name] = next(r["at"] for r in control if r["kind"] == "ready")
    assert (
        runtime_ready["runtime-1"] > runtime_ready["runtime-0"]
    ), "Restart generation order differs"
    for name in (*topology, "runtime-0", "runtime-1"):
        for suffix in ("stdout", "stderr"):
            capture = directory / (name + "." + suffix)
            assert capture.is_file(), "Missing native process capture"
            audit = analyze_log(capture.read_text(errors="replace"))
            assert (
                not audit["resource_warning_lines"]
                and not audit["records_without_timestamp"]
            ), "Native process warnings or missing timestamps"
            if name.startswith("runtime-") and suffix == "stdout":
                assert audit["levels"], "Missing runtime log records"
    assert [case["name"] for case in report["cases"]] == [
        case[0] for case in cases
    ], "Fidelity scenario coverage differs"
    parents = {}
    verdicts = []
    db = await SQLiteStorage.open_readonly(str(directory / "medre.sqlite3"))
    try:
        for (name, transport, parent), case in zip(cases, report["cases"], strict=True):
            styled = presentation_example(name, prefix + name + " ")
            assert case["marker"] == prefix + name + " " and case["text"] == (
                styled["text"] if styled else case["marker"] + "café 🦉"
            )
            assert (
                case["parent"] == parent
                and topology[case["source"]]["transport"] == transport
                and case["target"] != case["source"]
            )
            assert case["mode"] == ("missing" if parent == "missing" else "native")
            assert (
                sum(
                    r["kind"] == "sent" and r.get("native_id") == case["source_ack"]
                    for r in peers[case["source"]]
                )
                == 1
                and case["source_ack"]
            ), "Native source acknowledgement missing"
            ack = next(
                r
                for r in peers[case["source"]]
                if r["kind"] == "sent" and r.get("native_id") == case["source_ack"]
            )
            if styled or name in {
                "matrix_after_restart",
                "xmpp_after_restart",
                "matrix_missing",
                "xmpp_missing",
            }:
                assert (
                    ack["at"] > runtime_ready["runtime-1"]
                ), "Reply predates the claimed restart"
            else:
                assert (
                    runtime_ready["runtime-0"] < ack["at"] < runtime_ready["runtime-1"]
                ), "Initial message is outside its runtime generation"
            current = await read_event(db, case["marker"])
            assert current == case["core"], "Persisted fidelity evidence differs"
            expected = verify_case(
                case,
                current,
                peers[case["source"]],
                peers[case["target"]],
                topology,
                parents,
            )
            assert all(
                case[key] == value for key, value in expected.items()
            ), "Recorded native references differ"
            parents[name] = case
            verdicts.append({"name": name, "status": "passed"})
    finally:
        await db.close()
    return {"version": 1, "status": "passed", "cases": verdicts}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(analyze(args.directory))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "message": "Fidelity evidence is invalid or incomplete",
                }
            )
        )
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
