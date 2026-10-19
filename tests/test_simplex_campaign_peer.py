"""Native peer observations must identify the independent group and sender."""

from copy import deepcopy

import pytest

from scripts.adapter_campaign.simplex_peer import group_message


def record():
    return {
        "chatInfo": {"type": "group", "groupInfo": {"groupId": 3}},
        "chatItem": {
            "meta": {"itemId": 7},
            "chatDir": {"type": "groupRcv", "groupMember": {"groupMemberId": 2}},
            "content": {
                "type": "rcvMsgContent",
                "msgContent": {"type": "text", "text": "MR-proof café ✓"},
            },
        },
    }


@pytest.mark.parametrize("scope", [None, {"type": "main"}])
def test_group_receipt_preserves_native_identity_and_utf8(scope):
    row = record()
    row["chatInfo"]["groupChatScope"] = scope
    assert group_message(row, 3) == {
        "text": "MR-proof café ✓",
        "sender": "2",
        "channel": "group:3",
        "native_id": "7",
    }


@pytest.mark.parametrize("change", ["room", "echo", "file", "control", "live"])
def test_nonfixture_and_incomplete_items_cannot_qualify_delivery(change):
    row = deepcopy(record())
    item = row["chatItem"]
    if change == "room":
        row["chatInfo"]["groupInfo"]["groupId"] = 8
    elif change == "echo":
        item["chatDir"]["type"] = "groupSnd"
    elif change == "file":
        item["file"] = {"fileId": 1}
    elif change == "control":
        item["content"]["type"] = "rcvGroupEvent"
    elif change == "live":
        item["meta"]["itemLive"] = True
    assert group_message(row, 3) is None


def test_private_support_scope_cannot_qualify_main_group_delivery():
    row = record()
    row["chatInfo"]["groupChatScope"] = {
        "type": "memberSupport",
        "groupMember_": {"groupMemberId": 2},
    }
    assert group_message(row, 3) is None


async def test_peer_environment_overrides_precede_sdk_import(tmp_path, monkeypatch):
    import builtins
    import os

    from scripts.adapter_campaign.simplex_peer import simplexchat

    class ImportObserved(Exception):
        pass

    prefix = tmp_path / "profile"
    (tmp_path / "profile_chat.db").touch()
    libs, cache = str(tmp_path / "libs"), str(tmp_path / "cache")
    monkeypatch.setenv("SIMPLEX_LIBS_DIR", "prior-libs")
    monkeypatch.setenv("XDG_CACHE_HOME", "prior-cache")
    original = builtins.__import__

    def observe(name, *args, **kwargs):
        if name == "simplex_chat":
            observed_libs = os.environ.get("SIMPLEX_LIBS_DIR")
            observed_cache = os.environ.get("XDG_CACHE_HOME")
            assert (observed_libs, observed_cache) == (libs, cache)
            raise ImportObserved
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", observe)
    with pytest.raises(ImportObserved):
        await simplexchat(
            {
                "channel": "group:3",
                "peer": {
                    "database_prefix": str(prefix),
                    "libs_dir": libs,
                    "native_cache_dir": cache,
                },
            }
        )
