"""Synthetic native campaign records, independent of provider renderers."""

from copy import deepcopy

from scripts.adapter_campaign.baseline import FIXTURES, SUITE, text


def report() -> dict:
    """Build two independently scoped native and durable source observations."""
    cases = []
    for source, target in (("left", "right"), ("right", "left")):
        marker = f"MR-private-{source}"
        fields = {
            "event_id": f"event-{source}",
            "delivery_plan_id": f"plan-{source}",
            "outbox_id": f"outbox-{source}",
            "target_adapter": target,
            "target_channel": "group:1",
            "route_id": f"{source}_to_{target}",
            "attempt_number": 1,
            "source": "live",
            "replay_run_id": None,
            "status": "sent",
        }
        cases.append(
            {
                "source": source,
                "marker": marker,
                "native_receipts": {
                    target: {
                        "kind": "received",
                        "text": marker + " private body",
                        "sender": "bot-" + target,
                        "channel": "group:peer-" + target,
                    }
                },
                "core": {
                    "event": {
                        "event_id": fields["event_id"],
                        "source_adapter": source,
                        "source_channel_id": "group:1",
                        "payload": {"body": marker},
                    },
                    "receipts": [{**fields, "adapter_message_id": None}],
                    "outbox": [{**fields, "dispatch_source": "live"}],
                    "refs": [],
                },
            }
        )
    return {
        "status": "passed",
        "coverage": "partial",
        "native_expectations": {
            name: {
                "transport": "matrix" if name == "left" else "xmpp",
                "source_channel": "group:1",
                "receipt_sender": "bot-" + name,
                "receipt_channel": "group:peer-" + name,
            }
            for name in ("left", "right")
        },
        "expected_pairs": [["left", "right"], ["right", "left"]],
        "completed_pairs": [["left", "right"], ["right", "left"]],
        "cleanup_errors": [],
        "cases": cases,
    }


def baseline_report():
    """Create native/durable observations without calling a production renderer."""
    data = report()
    originals = data["cases"]
    data["cases"] = []
    data["suite"] = SUITE
    data["expected_source_attribution"] = {
        name: {
            "sender_name": "Known",
            "origin_label": "Network",
            "verification": "unknown",
        }
        for name in ("left", "right")
    }
    data["presentation_expectations"] = {
        target: {
            "prefixes": {
                source: "[Known/Network]: "
                for source in ("left", "right")
                if source != target
            },
            "max_text_bytes": None,
        }
        for target in ("left", "right")
    }
    for original in originals:
        source = original["source"]
        target = next(iter(original["native_receipts"]))
        for index, name in enumerate(FIXTURES):
            case = deepcopy(original)
            event_id = f"event-{source}-{index}"
            case["core"]["event"]["event_id"] = event_id
            for row in case["core"]["receipts"] + case["core"]["outbox"]:
                row.update(
                    event_id=event_id,
                    outbox_id=f"outbox-{source}-{index}",
                    delivery_plan_id=f"plan-{source}-{index}",
                )
            case["fixture"] = name
            case["marker"] = original["marker"] + f"-{index} "
            case["sent_text"] = text(
                name, case["marker"], data["native_expectations"][source]["transport"]
            )
            case["core"]["event"]["payload"]["body"] = case["sent_text"]
            case["native_receipts"][target]["text"] = (
                "[Known/Network]: " + case["sent_text"]
            )
            case["core"]["event"].update(
                source_transport_id="native-peer", source_native_ref=None
            )
            if target == "left":
                case["native_receipts"][target]["native_details"] = {
                    "bridge_context": {
                        "schema_version": 1,
                        "source": {
                            "adapter": source,
                            "platform": "xmpp",
                            "canonical_event_id": event_id,
                            "channel": "group:1",
                            "sender_id": "native-peer",
                            "sender_name": "Known",
                            "origin_label": "Network",
                            "sender_verification": "unknown",
                            "native_ref": None,
                        },
                        "relations": [],
                    }
                }
            data["cases"].append(case)
    return data
