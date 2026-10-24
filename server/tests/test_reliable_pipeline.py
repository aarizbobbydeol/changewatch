from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
import pytest
from app.outbox import (
    annotate_delayed_message,
    process_outbox_item,
    queue_notification,
)
from app.worker import reap_stuck_processing_events


def test_sdk_event_id_generation():
    from changewatch import Client
    client = Client(api_key="test_key", endpoint="http://localhost:8000")
    client._running = False

    try:
        raise ValueError("test exception with unique id")
    except Exception as exc:
        client.capture_exception(exc)

    assert not client._queue.empty()
    item = client._queue.get_nowait()
    assert "event_id" in item
    assert len(item["event_id"]) == 32  # uuid4 hex


def test_outbox_delayed_annotation():
    payload = {"text": "Spike detected in deploy abc"}
    # 1. Immediate delivery (< 60s) -> no annotation
    annotated_fast = annotate_delayed_message(payload, delay_sec=15.0)
    assert "[delayed by" not in annotated_fast["text"]

    # 2. Delayed delivery (12 minutes = 720s) -> annotated
    annotated_delayed = annotate_delayed_message(payload, delay_sec=720.0)
    assert "[delayed by 12 min (channel was unreachable)]" in annotated_delayed["text"]


@pytest.mark.asyncio
async def test_outbox_queue_deduplication():
    mock_conn = AsyncMock()
    # First insert succeeds
    mock_conn.fetchrow.return_value = {"id": 1}
    q1 = await queue_notification(
        conn=mock_conn,
        project_id=1,
        dedupe_key="deploy_100_flag",
        payload={"alert": "regression"},
    )
    assert q1 is True

    # Second insert with same dedupe_key returns None (ON CONFLICT DO NOTHING)
    mock_conn.fetchrow.return_value = None
    q2 = await queue_notification(
        conn=mock_conn,
        project_id=1,
        dedupe_key="deploy_100_flag",
        payload={"alert": "regression"},
    )
    assert q2 is False


@pytest.mark.asyncio
async def test_outbox_process_success():
    mock_conn = AsyncMock()
    mock_sender = AsyncMock()

    item = {
        "id": 42,
        "project_id": 1,
        "attempts": 0,
        "created_at": datetime.now(timezone.utc),
        "payload": {"text": "Deploy safe"},
    }

    success = await process_outbox_item(mock_conn, item, sender_func=mock_sender)
    assert success is True
    mock_sender.assert_called_once()
    mock_conn.execute.assert_called_once()
    assert "UPDATE outbox" in mock_conn.execute.call_args[0][0]
    assert "status = 'sent'" in mock_conn.execute.call_args[0][0]


@pytest.mark.asyncio
async def test_outbox_process_failure_and_backoff():
    mock_conn = AsyncMock()
    mock_failing_sender = AsyncMock(side_effect=RuntimeError("Slack 503 Service Unavailable"))

    item = {
        "id": 42,
        "project_id": 1,
        "attempts": 1,
        "created_at": datetime.now(timezone.utc),
        "payload": {"text": "Deploy alert"},
    }

    success = await process_outbox_item(mock_conn, item, sender_func=mock_failing_sender)
    assert success is False
    mock_conn.execute.assert_called_once()
    sql_call = mock_conn.execute.call_args[0][0]
    assert "attempts = $1" in sql_call
    assert "next_attempt_at = $2" in sql_call


@pytest.mark.asyncio
async def test_reliable_queue_reaper():
    """Verify reaper scans events:processing:* and returns stuck items to events queue."""
    mock_redis = AsyncMock()
    # Mock SCAN returning one worker processing queue
    mock_redis.scan.return_value = (0, ["events:processing:crashed_worker_123"])
    # Mock RPOPLPUSH returning one stuck item then None
    mock_redis.rpoplpush.side_effect = ['{"event_id": "stuck_1"}', None]

    with patch("app.worker.get_redis", return_value=mock_redis):
        reaped = await reap_stuck_processing_events(timeout_sec=60)
        assert reaped == 1
        mock_redis.rpoplpush.assert_called_with("events:processing:crashed_worker_123", "events")
