"""Synthetic native campaign records, independent of provider renderers."""


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
