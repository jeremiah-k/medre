"""Tests for MatrixRelationHandler: reply target extraction, reaction
extraction, and reply body construction.
"""

from __future__ import annotations

from medre.adapters.matrix.relations import (
    MatrixRelationHandler,
    build_reply_body,
    extract_reaction,
    extract_reply_target,
)


class TestMatrixRelationHandler:
    """MatrixRelationHandler delegates to module-level helpers."""

    def test_extract_reply_target(self) -> None:
        source = {
            "content": {
                "m.relates_to": {
                    "m.in_reply_to": {
                        "event_id": "$orig-001",
                    }
                }
            }
        }
        result = extract_reply_target(source)
        assert result == "$orig-001"

    def test_extract_reply_target_missing(self) -> None:
        source = {"content": {"body": "no reply"}}
        result = extract_reply_target(source)
        assert result is None

    def test_extract_reaction(self) -> None:
        source = {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$orig-001",
                    "key": "👍",
                }
            }
        }
        result = extract_reaction(source)
        assert result is not None
        assert result == ("$orig-001", "👍")

    def test_extract_reaction_missing(self) -> None:
        source = {"content": {"body": "no reaction"}}
        result = extract_reaction(source)
        assert result is None

    def test_build_reply_body(self) -> None:
        result = build_reply_body("my reply", "@alice:server", "original msg")
        assert result == "> <@alice:server> original msg\n\nmy reply"

    # -- Handler class delegation ----------------------------------------

    def test_handler_extract_reply_target(self) -> None:
        handler = MatrixRelationHandler()
        source = {
            "content": {
                "m.relates_to": {
                    "m.in_reply_to": {"event_id": "$evt-1"}
                }
            }
        }
        assert handler.extract_reply_target(source) == "$evt-1"

    def test_handler_extract_reaction(self) -> None:
        handler = MatrixRelationHandler()
        source = {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$evt-1",
                    "key": "🔥",
                }
            }
        }
        assert handler.extract_reaction(source) == ("$evt-1", "🔥")

    def test_handler_build_reply_body(self) -> None:
        handler = MatrixRelationHandler()
        result = handler.build_reply_body("reply", "@bob:server", "orig")
        assert result == "> <@bob:server> orig\n\nreply"
