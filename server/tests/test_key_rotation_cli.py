"""Key rotation commands never make stored data unreadable."""
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.cli as cli
from app.crypto import decrypt_data_key, encrypt_data_key


@pytest.mark.asyncio
async def test_rotate_project_key_refuses_without_touching_the_database(capsys):
    with patch("app.cli.init_db_pool", new=AsyncMock()) as init_pool, pytest.raises(SystemExit) as exit_info:
        await cli.rotate_project_key_cmd(1)
    assert exit_info.value.code == 2
    init_pool.assert_not_awaited()
    assert "rotate-master-key" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_rotate_master_key_can_be_run_again_after_stopping_part_way(capsys):
    old, new = os.urandom(32), os.urandom(32)
    key_a, key_b = os.urandom(32), os.urandom(32)
    rows = [
        {"id": 1, "data_key_enc": encrypt_data_key(key_a, new)},  # re-wrapped by the earlier run
        {"id": 2, "data_key_enc": encrypt_data_key(key_b, old)},  # not yet
    ]
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    conn.execute = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("app.cli.get_master_key_bytes", return_value=new), \
         patch("app.cli.get_previous_master_key_bytes", return_value=old), \
         patch("app.cli.init_db_pool", new=AsyncMock()), patch("app.cli.close_db_pool", new=AsyncMock()), \
         patch("app.cli.get_connection", return_value=ctx):
        await cli.rotate_master_key_cmd()

    (sql, new_enc, pid), _ = conn.execute.await_args
    assert pid == 2 and conn.execute.await_count == 1
    assert decrypt_data_key(new_enc, new) == key_b
    assert "re-wrapped 1 project data keys" in capsys.readouterr().out
