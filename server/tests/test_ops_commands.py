"""Operator commands do what their help says."""
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings


@pytest.mark.asyncio
async def test_cleanup_retention_cleans_code_by_default():
    from app.cli import cleanup_retention_cmd

    stats = {"events_deleted": 0, "traffic_deleted": 0, "batches": 0, "code_records_cleaned": 0}
    with patch("app.cli.init_db_pool", new=AsyncMock(return_value="pool")), \
         patch("app.cli.close_db_pool", new=AsyncMock()), \
         patch("app.cli.cleanup_old_records", new=AsyncMock(return_value=stats)) as cleanup:
        await cleanup_retention_cmd(days=30)
    assert cleanup.await_args.kwargs["code_retention_days"] == settings.CODE_RETENTION_DAYS


def test_api_starts_the_configured_number_of_workers(monkeypatch):
    import app.serve as serve

    monkeypatch.setattr(settings, "API_WORKERS", "3")
    with patch("app.serve.uvicorn.run") as run:
        serve.main()
    assert run.call_args.kwargs["workers"] == 3
    assert run.call_args.args[0] == "app.main:app"
