"""Runtime submission honors content authority after rendering."""

from datetime import datetime, timezone


async def test_expiry_observed_during_render_prevents_adapter_submission(
    temp_storage, monkeypatch
):
    from medre.runtime.retry import RetryWorker
    from tests.helpers.initial_outbox import _pipeline, _stage

    runner, _, adapters = _pipeline(temp_storage)
    await _stage(temp_storage, runner)
    render = runner._rendering_pipeline.render

    async def expire_after_render(*args, **kwargs):
        result = await render(*args, **kwargs)
        await temp_storage._write(
            "UPDATE event_content_dependencies SET released_at=? "
            "WHERE event_id='event' AND owner_kind IN ('ingress', 'append')",
            (datetime.now(timezone.utc).isoformat(timespec="microseconds"),),
        )
        return result

    monkeypatch.setattr(runner._rendering_pipeline, "render", expire_after_render)
    dispatcher = RetryWorker(
        temp_storage, runner, None, initial_dispatch=True, enabled=False
    )
    await dispatcher._process_due(datetime.now(timezone.utc))
    assert all(not adapter.delivered_payloads for adapter in adapters.values())
    receipts = await temp_storage.list_receipts_for_event("event")
    assert receipts
    assert any(
        "content_authority_denied" in (receipt.error or "") for receipt in receipts
    )
