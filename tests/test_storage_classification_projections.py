"""Classification follows the mutable conversation projection contract."""

from medre.core.storage.backend import ConversationMembership
from medre.core.storage.classification import ColumnClass, column_class
from tests.helpers.storage import make_storage_event


async def test_rebuildable_membership_fields_are_operational_authority(temp_storage):
    """Late-parent repair rewrites projection facts, never immutable identity."""
    await temp_storage.append(make_storage_event(event_id="membership"))
    await temp_storage.append(make_storage_event(event_id="parent"))
    for root, target, relation, depth, state in [
        ("membership", None, "reply", 0, "unresolved"),
        ("parent", "parent", "reply", 1, "resolved"),
    ]:
        membership = ConversationMembership(
            event_id="membership",
            root_event_id=root,
            conversation_id=root,
            resolved_target_event_id=target,
            relation_type=relation,
            depth=depth,
            resolution_state=state,
        )
        assert await temp_storage.put_conversation_membership(membership)
        assert (
            await temp_storage.get_conversation_membership("membership") == membership
        )
    for column in (
        "root_event_id",
        "conversation_id",
        "resolved_target_event_id",
        "relation_type",
        "depth",
        "resolution_state",
        "updated_at",
    ):
        assert (
            column_class("conversation_membership", column)
            == ColumnClass.OPERATIONAL_AUTHORITY
        )
