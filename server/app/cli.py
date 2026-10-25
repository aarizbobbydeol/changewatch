import argparse
import asyncio
import json
import secrets
import sys
from typing import Optional
from app.auth import hash_api_key
from app.crypto import (
    decrypt_data_key,
    decrypt_or_plain,
    encrypt,
    encrypt_or_keep,
    encrypt_data_key,
    generate_master_key,
    get_master_key_bytes,
    get_previous_master_key_bytes,
    invalidate_project_data_key_cache,
)
from app.config import settings
from app.db import close_db_pool, get_connection, init_db_pool
from app.migrations import run_migrations
from app.retention import cleanup_old_records
from app.metering import load_price_book_yaml, publish_price_book, set_price_lock
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def generate_api_key() -> str:
    """Generate a cryptographically secure random API key (legacy)."""
    return f"cw_live_{secrets.token_urlsafe(24)}"


def generate_ingest_key() -> str:
    """Generate a cryptographically secure random ingest API key."""
    return f"cw_ingest_{secrets.token_urlsafe(24)}"


def generate_read_key() -> str:
    """Generate a cryptographically secure random read API key."""
    return f"cw_read_{secrets.token_urlsafe(24)}"


def _mask_url(url: str) -> str:
    """Show only scheme and host of a URL; webhook paths are credentials."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/******" if parsed.netloc else "******"


async def migrate_cmd() -> None:
    """Apply pending migrations; the API also does this on startup."""
    pool = await init_db_pool()
    try:
        applied = await run_migrations(pool)
        print(f"Applied {len(applied)} migration(s)" + (f": {', '.join(applied)}" if applied else " (database up to date)"))
    finally:
        await close_db_pool()


async def create_project_cmd(name: str, slack_webhook: Optional[str] = None):
    """Create a new project, generate data encryption key, and print hashed keys once."""
    pool = await init_db_pool()
    # Ensure migrations have run
    await run_migrations(pool)

    ingest_key = generate_ingest_key()
    ingest_hash = hash_api_key(ingest_key)
    ingest_prefix = ingest_key[:14] + "..."

    read_key = generate_read_key()
    read_hash = hash_api_key(read_key)
    read_prefix = read_key[:14] + "..."

    # Generate 256-bit project data key and encrypt with master key
    try:
        master_key = get_master_key_bytes()
        data_key = AESGCM.generate_key(bit_length=256)
        data_key_enc = encrypt_data_key(data_key, master_key)
    except Exception as exc:
        # Say why: without a data key, stored stack traces and diffs cannot be encrypted
        print(f"Warning: could not generate the project's encryption key: {exc}", file=sys.stderr)
        data_key_enc = None

    try:
        async with get_connection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO projects (
                    name,
                    data_key_enc,
                    ingest_key_hash,
                    ingest_key_prefix,
                    read_key_hash,
                    read_key_prefix
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id, name, created_at
                """,
                name,
                data_key_enc,
                ingest_hash,
                ingest_prefix,
                read_hash,
                read_prefix,
            )
            if slack_webhook:
                # The webhook URL is a credential: store it encrypted with the project's data key
                webhook_plain, webhook_enc = await encrypt_or_keep(
                    row["id"], "projects", "slack_webhook_url", slack_webhook
                )
                await conn.execute(
                    "UPDATE projects SET slack_webhook_url = $1, slack_webhook_url_enc = $2 WHERE id = $3",
                    webhook_plain,
                    webhook_enc,
                    row["id"],
                )

        print("\n==========================================")
        print(" ChangeWatch Project Created Successfully ")
        print("==========================================")
        print(f"Project ID:        {row['id']}")
        print(f"Project Name:      {row['name']}")
        print(f"Ingest Key:        {ingest_key}")
        print(f"Read Key:          {read_key}")
        print(f"Slack Webhook URL: {_mask_url(slack_webhook) if slack_webhook else '(None)'}")
        print(f"Envelope Security: {'Enabled (AES-256-GCM)' if data_key_enc else 'Pending'}")
        print("------------------------------------------")
        print("IMPORTANT: Save these keys now. They are hashed with SHA-256 and will NEVER be shown again.")
        print("  - Ingest Key: Use in Python SDK & GitHub Actions (can only POST events and deploys).")
        print("  - Read Key:   Use in Dashboard & Verdict API (can only read status and timelines).")
        print("==========================================\n")

    finally:
        await close_db_pool()


async def list_projects_cmd():
    """List existing projects (names and IDs only, keys masked)."""
    await init_db_pool()
    try:
        async with get_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT id, name, ingest_key_prefix, read_key_prefix, api_key, slack_webhook_url,
                       (slack_webhook_url_enc IS NOT NULL) AS webhook_encrypted, created_at,
                       (data_key_enc IS NOT NULL) as has_crypto
                FROM projects
                ORDER BY id
                """
            )

        if not rows:
            print("No projects found.")
            return

        print("\nID  | Name                 | Ingest Prefix   | Read Prefix     | Encrypted | Slack Webhook")
        print("----+----------------------+-----------------+-----------------+-----------+----------------------")
        for r in rows:
            ingest_p = r["ingest_key_prefix"] or (r["api_key"][:12] + "..." if r["api_key"] else "-")
            read_p = r["read_key_prefix"] or "-"
            if r["webhook_encrypted"]:
                webhook = "(encrypted)"
            else:
                webhook = _mask_url(r["slack_webhook_url"]) if r["slack_webhook_url"] else "-"
            enc = "YES" if r["has_crypto"] else "NO"
            print(f"{r['id']:<3} | {r['name']:<20} | {ingest_p:<15} | {read_p:<15} | {enc:<9} | {webhook}")
        print()
    finally:
        await close_db_pool()


async def rotate_master_key_cmd():
    """Re-wrap all project data keys from MASTER_KEY_PREVIOUS to MASTER_KEY."""
    current_key = get_master_key_bytes()
    prev_key = get_previous_master_key_bytes()

    if not prev_key:
        print("ERROR: MASTER_KEY_PREVIOUS is not configured. Set MASTER_KEY_PREVIOUS before running key rotation.")
        sys.exit(1)

    await init_db_pool()
    try:
        async with get_connection() as conn:
            rows = await conn.fetch(
                "SELECT id, data_key_enc FROM projects WHERE data_key_enc IS NOT NULL"
            )
            count = skipped = 0
            for r in rows:
                old_enc = r["data_key_enc"]
                try:
                    data_key = decrypt_data_key(old_enc, prev_key)
                except Exception:
                    # Already wrapped with the new key (an earlier run stopped part-way): skip it
                    decrypt_data_key(old_enc, current_key)  # raises if neither key opens it
                    skipped += 1
                    continue
                new_enc = encrypt_data_key(data_key, current_key)
                await conn.execute(
                    "UPDATE projects SET data_key_enc = $1 WHERE id = $2",
                    new_enc,
                    r["id"],
                )
                invalidate_project_data_key_cache(r["id"])
                count += 1

            print(f"\nSuccessfully re-wrapped {count} project data keys with new MASTER_KEY"
                  f" ({skipped} already used it).\n")
    finally:
        await close_db_pool()


async def rotate_project_key_cmd(project_id: int):
    """Refuse: replacing a project's data key in place would make its stored data unreadable.

    The old command wrote a new key without re-encrypting anything, while the running services
    kept encrypting with the old key held in memory. Every encrypted column of the project
    (traces, diffs, AI results, the alert webhook) became undecryptable, so alerts silently
    stopped. Rotation needs versioned project keys; until then, rotate MASTER_KEY instead.
    """
    print(
        f"\nrotate-project-key is not available: it would make every encrypted record of project "
        f"{project_id} unreadable.\n"
        "To change the key that protects all project keys, set MASTER_KEY_PREVIOUS to the current "
        "key and MASTER_KEY to a new one, then run: python -m app.cli rotate-master-key\n",
        file=sys.stderr,
    )
    sys.exit(2)


async def delete_project_cmd(project_id: int, purge: bool):
    """Delete a project and purge its data and encryption keys (crypto-shred)."""
    await init_db_pool()
    try:
        async with get_connection() as conn:
            await conn.execute("DELETE FROM projects WHERE id = $1", project_id)
            invalidate_project_data_key_cache(project_id)

        print(f"\nProject {project_id} and all associated data keys permanently deleted (crypto-shredded).\n")
    finally:
        await close_db_pool()


async def cleanup_retention_cmd(
    days: Optional[int] = None,
    batch_size: int = 5000,
    code_days: Optional[int] = None,
) -> dict:
    """Trigger retention cleanup for expired events, traffic, and code contents."""
    pool = await init_db_pool()
    try:
        kwargs = {"retention_days": days, "batch_size": batch_size}
        if code_days is not None:
            kwargs["code_retention_days"] = code_days
        stats = await cleanup_old_records(pool, **kwargs)
        effective_days = days if days is not None else settings.RETENTION_DAYS
        effective_code_days = code_days if code_days is not None else settings.CODE_RETENTION_DAYS
        print("\n==========================================")
        print(" ChangeWatch Retention Cleanup Completed  ")
        print("==========================================")
        print(f"Retention Window:  {effective_days} days")
        if code_days is not None:
            print(f"Code Retention Window:  {effective_code_days} days")
        print(f"Batch Size:        {batch_size}")
        print(f"Events Deleted:    {stats['events_deleted']}")
        print(f"Traffic Deleted:   {stats['traffic_deleted']}")
        if "code_records_cleaned" in stats:
            print(f"Code Records Cleaned: {stats['code_records_cleaned']}")
        print(f"Batches Executed:  {stats['batches']}")
        print("==========================================\n")

        return stats
    finally:
        await close_db_pool()




async def encrypt_existing_cmd(batch_size: int = 1000) -> dict:
    """
    One-time resumable CLI to convert existing plaintext columns to encrypted BYTEA columns.
    Converts:
    - deploys.diff -> deploys.diff_enc
    - deploys.codeowners -> deploys.codeowners_enc
    - deploy_files.content -> deploy_files.content_enc
    - events.stack_trace -> events.stack_trace_enc
    - events.extra -> events.extra_enc
    - projects.slack_webhook_url -> projects.slack_webhook_url_enc
    - notification_channels.config -> notification_channels.config_enc
    Safe to re-run and resumable.
    """
    await init_db_pool()
    stats = {
        "deploys_encrypted": 0,
        "deploy_files_encrypted": 0,
        "events_encrypted": 0,
        "projects_encrypted": 0,
        "channels_encrypted": 0,
    }
    try:
        async with get_connection() as conn:
            # 1. Projects
            proj_rows = await conn.fetch(
                "SELECT id, slack_webhook_url FROM projects WHERE slack_webhook_url IS NOT NULL AND slack_webhook_url_enc IS NULL"
            )
            for pr in proj_rows:
                enc = await encrypt(pr["id"], "projects", "slack_webhook_url", pr["slack_webhook_url"])
                await conn.execute(
                    "UPDATE projects SET slack_webhook_url_enc = $1, slack_webhook_url = NULL WHERE id = $2",
                    enc, pr["id"]
                )
                stats["projects_encrypted"] += 1

            # 1b. Notification channel configs (credentials)
            ch_rows = await conn.fetch(
                "SELECT id, project_id, config FROM notification_channels WHERE config_enc IS NULL AND config <> '{}'::jsonb"
            )
            for cr in ch_rows:
                cfg = cr["config"] if isinstance(cr["config"], str) else json.dumps(cr["config"])
                enc = await encrypt(cr["project_id"], "notification_channels", "config", cfg)
                await conn.execute(
                    "UPDATE notification_channels SET config_enc = $1, config = '{}'::jsonb WHERE id = $2",
                    enc, cr["id"]
                )
                stats["channels_encrypted"] += 1

            # 2. Deploys: diff and CODEOWNERS (batched)
            while True:
                dep_rows = await conn.fetch(
                    """
                    SELECT id, project_id, diff, codeowners FROM deploys
                    WHERE (diff IS NOT NULL AND diff_enc IS NULL)
                       OR (codeowners IS NOT NULL AND codeowners_enc IS NULL)
                    LIMIT $1
                    """,
                    batch_size
                )
                if not dep_rows:
                    break
                for dr in dep_rows:
                    if dr.get("diff") is not None:
                        enc = await encrypt(dr["project_id"], "deploys", "diff", dr["diff"])
                        await conn.execute("UPDATE deploys SET diff_enc = $1, diff = NULL WHERE id = $2", enc, dr["id"])
                    if dr.get("codeowners") is not None:
                        enc = await encrypt(dr["project_id"], "deploys", "codeowners", dr["codeowners"])
                        await conn.execute(
                            "UPDATE deploys SET codeowners_enc = $1, codeowners = NULL WHERE id = $2", enc, dr["id"]
                        )
                    stats["deploys_encrypted"] += 1

            # 2b. Deploy file contents (batched)
            while True:
                file_rows = await conn.fetch(
                    """
                    SELECT id, project_id, content FROM deploy_files
                    WHERE content IS NOT NULL AND content_enc IS NULL
                    LIMIT $1
                    """,
                    batch_size
                )
                if not file_rows:
                    break
                for fr in file_rows:
                    enc = await encrypt(fr["project_id"], "deploy_files", "content", fr["content"])
                    await conn.execute(
                        "UPDATE deploy_files SET content_enc = $1, content = NULL WHERE id = $2", enc, fr["id"]
                    )
                    stats["deploy_files_encrypted"] += 1

            # 3. Events (batched)
            while True:
                ev_rows = await conn.fetch(
                    """
                    SELECT id, project_id, stack_trace, extra FROM events
                    WHERE (stack_trace IS NOT NULL AND stack_trace_enc IS NULL)
                       OR (extra <> '{}'::jsonb AND extra_enc IS NULL)
                    LIMIT $1
                    """,
                    batch_size
                )
                if not ev_rows:
                    break
                for er in ev_rows:
                    updates = []
                    args = []
                    arg_idx = 1
                    if er["stack_trace"] is not None:
                        enc_st = await encrypt(er["project_id"], "events", "stack_trace", er["stack_trace"])
                        updates.append(f"stack_trace_enc = ${arg_idx}, stack_trace = NULL")
                        args.append(enc_st)
                        arg_idx += 1
                    extra_val = er["extra"] if isinstance(er["extra"], (dict, type(None))) else json.loads(er["extra"])
                    if extra_val:
                        enc_ex = await encrypt(er["project_id"], "events", "extra", json.dumps(extra_val))
                        # extra is NOT NULL: leave an empty object behind
                        updates.append(f"extra_enc = ${arg_idx}, extra = '{{}}'::jsonb")
                        args.append(enc_ex)
                        arg_idx += 1
                    if updates:
                        args.append(er["id"])
                        await conn.execute(
                            f"UPDATE events SET {', '.join(updates)} WHERE id = ${arg_idx}",
                            *args
                        )
                    stats["events_encrypted"] += 1

        print("\n==========================================")
        print(" ChangeWatch Encrypt-Existing Completed   ")
        print("==========================================")
        print(f"Projects Encrypted: {stats['projects_encrypted']}")
        print(f"Deploys Encrypted:  {stats['deploys_encrypted']}")
        print(f"Files Encrypted:    {stats['deploy_files_encrypted']}")
        print(f"Events Encrypted:   {stats['events_encrypted']}")
        print(f"Channels Encrypted: {stats['channels_encrypted']}")
        print("==========================================\n")
        return stats
    finally:
        await close_db_pool()


async def publish_price_book_cmd(file_path: str):
    """Publish a new rate card version from a YAML file."""
    await init_db_pool()
    try:
        data = load_price_book_yaml(file_path)
        async with get_connection() as conn:
            version = await publish_price_book(conn, data)
        print("\n==========================================")
        print(" ChangeWatch Price Book Published         ")
        print("==========================================")
        print(f"Version:        {version}")
        print(f"Source File:    {file_path}")
        print(f"Effective From: {data.get('effective_from', 'now')}")
        print(f"Meters Defined: {list(data.get('meters', {}).keys())}")
        print("==========================================\n")
        return version
    finally:
        await close_db_pool()


async def price_lock_cmd(project_id: int, days: int):
    """Lock rate card version for a project for N days."""
    await init_db_pool()
    try:
        async with get_connection() as conn:
            version, lock_until = await set_price_lock(conn, project_id, days)
        print("\n==========================================")
        print(" ChangeWatch Price Lock Activated         ")
        print("==========================================")
        print(f"Project ID:      {project_id}")
        print(f"Pinned Version:  {version}")
        print(f"Locked Until:    {lock_until.isoformat()}")
        return version, lock_until
    finally:
        await close_db_pool()


async def add_channel_cmd(project_id: int, channel_type: str, config_json: str, enabled: bool = True):

    """Add a notification channel for a project."""
    await init_db_pool()
    try:
        config = json.loads(config_json)
    except Exception as exc:
        print(f"Error: invalid JSON in config: {exc}")
        await close_db_pool()
        return

    try:
        async with get_connection() as conn:
            # Channel config holds credentials (webhook URLs, tokens, SMTP passwords): store it encrypted
            config_plain, config_enc = await encrypt_or_keep(
                project_id, "notification_channels", "config", json.dumps(config)
            )
            row = await conn.fetchrow(
                """
                INSERT INTO notification_channels (project_id, type, config, config_enc, enabled)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, type, enabled, created_at
                """,
                project_id,
                channel_type.lower(),
                config_plain or "{}",
                config_enc,
                enabled,
            )
            print(f"Channel created: ID={row['id']}, Type={row['type']}, Enabled={row['enabled']}")
    finally:
        await close_db_pool()


async def list_channels_cmd(project_id: int):
    """List notification channels for a project."""
    await init_db_pool()
    try:
        async with get_connection() as conn:
            rows = await conn.fetch(
                "SELECT id, type, config, config_enc, enabled, created_at FROM notification_channels WHERE project_id = $1 ORDER BY id",
                project_id,
            )
            if not rows:
                print(f"No notification channels found for project {project_id}.")
            else:
                print(f"\nNotification Channels for Project {project_id}:")
                for r in rows:
                    cfg = await decrypt_or_plain(
                        project_id, "notification_channels", "config", r.get("config_enc"), r.get("config")
                    )
                    cfg_display = dict(cfg) if isinstance(cfg, dict) else json.loads(cfg or "{}")
                    for k in cfg_display:
                        if any(s in k.lower() for s in ("token", "secret", "password", "key")):
                            cfg_display[k] = "******"
                        elif "url" in k.lower() and isinstance(cfg_display[k], str):
                            cfg_display[k] = _mask_url(cfg_display[k])
                    print(f"  [{r['id']}] {r['type'].upper()} (enabled={r['enabled']}): {cfg_display}")
    finally:
        await close_db_pool()


async def add_member_cmd(
    project_id: int,
    name: str,
    git_emails: Optional[str] = None,
    slack_user_id: Optional[str] = None,
    discord_user_id: Optional[str] = None,
    email: Optional[str] = None,
):
    """Add a team member to a project."""
    await init_db_pool()
    emails = [e.strip() for e in git_emails.split(",") if e.strip()] if git_emails else []
    try:
        async with get_connection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO team_members (project_id, name, git_emails, slack_user_id, discord_user_id, email)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id, name
                """,
                project_id,
                name,
                emails,
                slack_user_id,
                discord_user_id,
                email,
            )
            print(f"Team member created: ID={row['id']}, Name={row['name']}")
    finally:
        await close_db_pool()


async def list_members_cmd(project_id: int):
    """List team members for a project."""
    await init_db_pool()
    try:
        async with get_connection() as conn:
            rows = await conn.fetch(
                "SELECT id, name, git_emails, slack_user_id, discord_user_id, email FROM team_members WHERE project_id = $1 ORDER BY id",
                project_id,
            )
            if not rows:
                print(f"No team members found for project {project_id}.")
            else:
                print(f"\nTeam Members for Project {project_id}:")
                for r in rows:
                    print(f"  [{r['id']}] {r['name']} | Slack={r['slack_user_id'] or '-'} | Discord={r['discord_user_id'] or '-'} | Emails={r['git_emails']}")
    finally:
        await close_db_pool()


async def requeue_dead_cmd(limit: int = 0) -> int:
    """Move dead-lettered events back to the events queue with a fresh retry count."""
    import json

    from app.queue import close_redis, init_redis
    from app.worker import DEAD_LETTER_QUEUE, EVENTS_QUEUE

    redis = await init_redis()
    moved = 0
    try:
        while not limit or moved < limit:
            raw = await redis.lpop(DEAD_LETTER_QUEUE)
            if raw is None:
                break
            payload = json.loads(raw)
            payload["_tries"] = 0
            await redis.rpush(EVENTS_QUEUE, json.dumps(payload))
            moved += 1
        left = await redis.llen(DEAD_LETTER_QUEUE)
        print(f"Re-queued {moved} dead-lettered event(s); {left} left in {DEAD_LETTER_QUEUE}.")
    finally:
        await close_redis()
    return moved


def main():

    parser = argparse.ArgumentParser(description="ChangeWatch CLI Management Utility")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: create-project
    create_parser = subparsers.add_parser("create-project", help="Create a new project")
    create_parser.add_argument("name", help="Name of the project")
    create_parser.add_argument(
        "--slack-webhook",
        dest="slack_webhook",
        help="Optional Slack incoming webhook URL for alerts",
        default=None,
    )

    # Subcommand: list-projects
    subparsers.add_parser("list-projects", help="List all projects")

    # Subcommand: migrate
    subparsers.add_parser("migrate", help="Apply pending database migrations (and the dev seed if SEED_DEV_PROJECT=1)")

    # Subcommand: generate-master-key
    subparsers.add_parser("generate-master-key", help="Generate a new 256-bit AES-GCM master key")

    # Subcommand: rotate-master-key
    subparsers.add_parser("rotate-master-key", help="Re-wrap project data keys with new MASTER_KEY")

    # Subcommand: rotate-project-key
    rotate_proj_parser = subparsers.add_parser("rotate-project-key", help="Rotate data key for a single project")
    rotate_proj_parser.add_argument("--project-id", type=int, required=True, help="Project ID")

    # Subcommand: delete-project
    del_parser = subparsers.add_parser("delete-project", help="Delete project and crypto-shred data")
    del_parser.add_argument("--project-id", type=int, required=True, help="Project ID")
    del_parser.add_argument("--purge", action="store_true", help="Confirm permanent crypto-shredding")

    # Subcommand: cleanup-retention
    cleanup_parser = subparsers.add_parser(
        "cleanup-retention", help="Clean up expired events and traffic records"
    )
    cleanup_parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Retention period in days (defaults to RETENTION_DAYS setting)",
    )
    cleanup_parser.add_argument(
        "--code-days",
        type=int,
        default=None,
        help="Code retention period in days (defaults to CODE_RETENTION_DAYS setting)",
    )
    cleanup_parser.add_argument(
        "--batch-size",
        type=int,
        default=5000,
        help="Batch size for batch deletions (default: 5000)",
    )

    # Subcommand: encrypt-existing
    enc_parser = subparsers.add_parser(
        "encrypt-existing", help="Convert existing plaintext columns to encrypted BYTEA columns in batches"
    )
    enc_parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Batch size for encryption updates (default: 1000)",
    )

    # Subcommand: price-book
    pb_parser = subparsers.add_parser("price-book", help="Price book management")
    pb_sub = pb_parser.add_subparsers(dest="subaction", required=True)
    pb_pub = pb_sub.add_parser("publish", help="Publish a new price book version from YAML")
    pb_pub.add_argument("file", help="Path to price book YAML file")

    # Subcommand: price-lock
    pl_parser = subparsers.add_parser("price-lock", help="Lock rate card version for a project")
    pl_parser.add_argument("project_id", type=int, help="Project ID")
    pl_parser.add_argument("--days", type=int, required=True, help="Lock duration in days")

    # Subcommand: add-channel
    ch_add_parser = subparsers.add_parser("add-channel", help="Add notification channel")
    ch_add_parser.add_argument("project_id", type=int, help="Project ID")
    ch_add_parser.add_argument("type", choices=["slack", "discord", "teams", "email", "webhook"], help="Channel type")
    ch_add_parser.add_argument("--config", required=True, help="JSON configuration for channel")
    ch_add_parser.add_argument("--disabled", action="store_true", help="Create channel in disabled state")

    # Subcommand: list-channels
    ch_list_parser = subparsers.add_parser("list-channels", help="List notification channels for a project")
    ch_list_parser.add_argument("project_id", type=int, help="Project ID")

    # Subcommand: add-member
    mem_add_parser = subparsers.add_parser("add-member", help="Add team member to project")
    mem_add_parser.add_argument("project_id", type=int, help="Project ID")
    mem_add_parser.add_argument("name", help="Member display name")
    mem_add_parser.add_argument("--git-emails", default=None, help="Comma-separated git author emails")
    mem_add_parser.add_argument("--slack-user-id", default=None, help="Slack member ID (e.g. U123456)")
    mem_add_parser.add_argument("--discord-user-id", default=None, help="Discord user ID")
    mem_add_parser.add_argument("--email", default=None, help="Notification email address")

    # Subcommand: list-members
    mem_list_parser = subparsers.add_parser("list-members", help="List team members for a project")
    mem_list_parser.add_argument("project_id", type=int, help="Project ID")

    # Subcommand: requeue-dead
    rq_parser = subparsers.add_parser(
        "requeue-dead", help="Replay events that failed 3 times (events:dead), for example after fixing their cause"
    )
    rq_parser.add_argument("--limit", type=int, default=0, help="At most this many (default: all)")

    args = parser.parse_args()

    if args.command == "create-project":
        asyncio.run(create_project_cmd(args.name, args.slack_webhook))
    elif args.command == "migrate":
        asyncio.run(migrate_cmd())
    elif args.command == "list-projects":
        asyncio.run(list_projects_cmd())
    elif args.command == "generate-master-key":
        key = generate_master_key()
        print("\nGenerated ChangeWatch Master Key (AES-256-GCM):")
        print(key)
        print("Set this in your environment as: MASTER_KEY=" + key + "\n")
    elif args.command == "rotate-master-key":
        asyncio.run(rotate_master_key_cmd())
    elif args.command == "rotate-project-key":
        asyncio.run(rotate_project_key_cmd(args.project_id))
    elif args.command == "delete-project":
        asyncio.run(delete_project_cmd(args.project_id, args.purge))
    elif args.command == "cleanup-retention":
        if getattr(args, "code_days", None) is not None:
            asyncio.run(cleanup_retention_cmd(args.days, args.batch_size, args.code_days))
        else:
            asyncio.run(cleanup_retention_cmd(args.days, args.batch_size))
    elif args.command == "encrypt-existing":
        asyncio.run(encrypt_existing_cmd(args.batch_size))
    elif args.command == "price-book":
        if args.subaction == "publish":
            asyncio.run(publish_price_book_cmd(args.file))
    elif args.command == "price-lock":
        asyncio.run(price_lock_cmd(args.project_id, args.days))
    elif args.command == "add-channel":
        asyncio.run(add_channel_cmd(args.project_id, args.type, args.config, enabled=not args.disabled))
    elif args.command == "list-channels":
        asyncio.run(list_channels_cmd(args.project_id))
    elif args.command == "add-member":
        asyncio.run(
            add_member_cmd(
                args.project_id,
                args.name,
                git_emails=args.git_emails,
                slack_user_id=args.slack_user_id,
                discord_user_id=args.discord_user_id,
                email=args.email,
            )
        )
    elif args.command == "list-members":
        asyncio.run(list_members_cmd(args.project_id))
    elif args.command == "requeue-dead":
        asyncio.run(requeue_dead_cmd(args.limit))
    else:
        parser.print_help()
        sys.exit(1)



if __name__ == "__main__":
    main()
