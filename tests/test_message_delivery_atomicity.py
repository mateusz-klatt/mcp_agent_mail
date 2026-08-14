"""Schema-level guarantees for durable, idempotent message delivery."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import uuid
from collections.abc import Coroutine
from contextlib import closing
from pathlib import Path
from typing import Any, TypeVar

import pytest
from sqlmodel import SQLModel

from mcp_agent_mail import db as database_module
from mcp_agent_mail.db import ensure_schema, reset_database_state
from mcp_agent_mail.models import MessageDelivery, MessageDeliveryRecipient

T = TypeVar("T")

PROJECT_GENERATION = "a" * 64
SOURCE_PROJECT_GENERATION = "1" * 64
SENDER_GENERATION = "b" * 64
SOURCE_SENDER_GENERATION = "2" * 64
RECIPIENT_GENERATION = "c" * 64
USER_GENERATION = "d" * 64
DOCUMENT_SHA256 = "e" * 64
REQUEST_SHA256 = "f" * 64


def _run_database(coroutine: Coroutine[Any, Any, T]) -> T:
    """Run one database coroutine and close the process-global engine."""
    try:
        return asyncio.run(coroutine)
    finally:
        reset_database_state()


def _database_path() -> Path:
    prefix = "sqlite+aiosqlite:///"
    database_url = os.environ["DATABASE_URL"]
    assert database_url.startswith(prefix)
    return Path(database_url.removeprefix(prefix))


def _open_database() -> sqlite3.Connection:
    connection = sqlite3.connect(_database_path())
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _install_legacy_ui_locale_schema(connection: sqlite3.Connection) -> None:
    """Replace ``ui_users`` with the historical two-locale schema."""
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        """
        CREATE TABLE ui_users_legacy (
            id INTEGER PRIMARY KEY,
            username VARCHAR(64) NOT NULL,
            password_hash VARCHAR(256) NOT NULL,
            role VARCHAR(16) NOT NULL,
            disabled BOOLEAN NOT NULL,
            session_epoch INTEGER NOT NULL,
            session_generation VARCHAR(64) NOT NULL,
            display_name VARCHAR(128),
            profile_revision INTEGER NOT NULL DEFAULT 1,
            preferred_ui_locale VARCHAR(2) NOT NULL DEFAULT 'en',
            preferred_correspondence_locale VARCHAR(2),
            created_ts DATETIME NOT NULL,
            last_login_ts DATETIME,
            CONSTRAINT ck_ui_users_preferred_ui_locale
                CHECK (preferred_ui_locale IN ('en', 'pl')),
            CONSTRAINT ck_ui_users_preferred_correspondence_locale
                CHECK (
                    preferred_correspondence_locale IS NULL
                    OR preferred_correspondence_locale IN ('en', 'pl')
                ),
            UNIQUE (username)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO ui_users_legacy (
            id, username, password_hash, role, disabled, session_epoch,
            session_generation, display_name, profile_revision,
            preferred_ui_locale, preferred_correspondence_locale,
            created_ts, last_login_ts
        )
        SELECT
            id, username, password_hash, role, disabled, session_epoch,
            session_generation, display_name, profile_revision,
            'pl', 'en', created_ts, last_login_ts
        FROM ui_users
        """
    )
    dependent_triggers = connection.execute(
        """
        SELECT name, sql
        FROM sqlite_master
        WHERE type = 'trigger'
          AND sql IS NOT NULL
          AND (tbl_name = 'ui_users' OR instr(lower(sql), 'ui_users') > 0)
        ORDER BY name
        """
    ).fetchall()
    for trigger_name, _trigger_sql in dependent_triggers:
        quoted_name = '"' + str(trigger_name).replace('"', '""') + '"'
        connection.execute(f"DROP TRIGGER {quoted_name}")
    connection.execute("DROP TABLE ui_users")
    connection.execute("ALTER TABLE ui_users_legacy RENAME TO ui_users")
    for _trigger_name, trigger_sql in dependent_triggers:
        connection.execute(str(trigger_sql))
    connection.execute(
        "CREATE INDEX ui_users_custom_role_idx ON ui_users(role, disabled)"
    )
    connection.execute(
        """
        CREATE TRIGGER ui_users_custom_noop
        AFTER UPDATE OF disabled ON ui_users
        BEGIN
            SELECT new.id;
        END
        """
    )
    connection.execute(
        """
        CREATE VIEW ui_users_public_names AS
        SELECT id, username FROM ui_users
        """
    )
    connection.commit()


def _seed_identities(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO projects
            (id, slug, human_key, project_generation, created_at)
        VALUES (1, 'delivery-project', '/delivery/project', ?, CURRENT_TIMESTAMP)
        """,
        (PROJECT_GENERATION,),
    )
    connection.execute(
        """
        INSERT INTO projects
            (id, slug, human_key, project_generation, created_at)
        VALUES (2, 'source-project', '/source/project', ?, CURRENT_TIMESTAMP)
        """,
        (SOURCE_PROJECT_GENERATION,),
    )
    connection.executemany(
        """
        INSERT INTO agents (
            id, project_id, name, agent_generation, program, model,
            task_description, inception_ts, last_active_ts,
            attachments_policy, contact_policy
        ) VALUES (?, 1, ?, ?, 'codex', 'test', '', CURRENT_TIMESTAMP,
                  CURRENT_TIMESTAMP, 'auto', 'open')
        """,
        [
            (10, "SenderAgent", SENDER_GENERATION),
            (11, "RecipientAgent", RECIPIENT_GENERATION),
        ],
    )
    connection.execute(
        """
        INSERT INTO agents (
            id, project_id, name, agent_generation, program, model,
            task_description, inception_ts, last_active_ts,
            attachments_policy, contact_policy
        ) VALUES (12, 2, 'SourceSender', ?, 'codex', 'test', '',
                  CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'auto', 'open')
        """,
        (SOURCE_SENDER_GENERATION,),
    )
    connection.execute(
        """
        INSERT INTO ui_users (
            id, username, password_hash, role, disabled, session_epoch,
            session_generation, profile_revision, preferred_ui_locale, created_ts
        ) VALUES (20, 'operator', 'unused', 'member', 0, 7, ?, 1, 'en',
                  CURRENT_TIMESTAMP)
        """,
        (USER_GENERATION,),
    )
    connection.executemany(
        """
        INSERT INTO ui_project_assignments (
            user_id, project_id, role, created_ts, updated_ts
        ) VALUES (20, ?, 'operator', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        [(1,), (2,)],
    )
    connection.execute(
        """
        INSERT INTO agent_links (
            a_project_id, a_agent_id, b_project_id, b_agent_id, status,
            reason, created_ts, updated_ts
        ) VALUES (2, 12, 1, 11, 'approved', 'test route',
                  CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    connection.execute(
        """
        INSERT INTO messages (
            id, project_id, sender_id, subject, body_md, importance,
            ack_required, created_ts, attachments
        ) VALUES (100, 1, 10, 'Parent', 'Parent body', 'normal', 0,
                  CURRENT_TIMESTAMP, '[]')
        """
    )
    connection.execute(
        "INSERT INTO message_recipients (message_id, agent_id, kind) "
        "VALUES (100, 11, 'to')"
    )
    connection.commit()


def _insert_delivery(
    connection: sqlite3.Connection,
    *,
    delivery_id: str | None = None,
    delivery_kind: str = "message",
    idempotency_key: str = "request-1",
    actor_kind: str = "system",
    actor_id: int = 0,
    actor_name: str = "system",
    actor_generation: str | None = None,
    actor_epoch: int | None = None,
    reply_to_message_id: int | None = 100,
    request_sha256: str = REQUEST_SHA256,
    sender_id: int = 10,
    sender_name: str = "SenderAgent",
    sender_generation: str = SENDER_GENERATION,
    sender_project_id: int = 1,
    sender_project_slug: str = "delivery-project",
    sender_project_generation: str = PROJECT_GENERATION,
    actor_project_id: int | None = None,
    actor_project_slug: str | None = None,
    actor_project_generation: str | None = None,
    thread_id: str = "delivery-thread",
) -> str:
    delivery_id = delivery_id or str(uuid.uuid4())
    if actor_kind in {"agent", "ui_user"} and actor_project_id is None:
        actor_project_id = sender_project_id
        actor_project_slug = sender_project_slug
        actor_project_generation = sender_project_generation
    connection.execute(
        """
        INSERT INTO message_deliveries (
            id, state, delivery_kind, project_id, project_slug_snapshot,
            project_generation_snapshot, sender_project_id_snapshot,
            sender_project_slug_snapshot, sender_project_generation_snapshot,
            sender_id, sender_name_snapshot, sender_generation_snapshot,
            actor_kind, actor_id,
            actor_name_snapshot, actor_generation_snapshot,
            actor_epoch_snapshot, actor_project_id_snapshot,
            actor_project_slug_snapshot, actor_project_generation_snapshot,
            idempotency_key, request_sha256, thread_id,
            reply_to_message_id, subject, body_md, importance, ack_required,
            attachments, archive_document, document_sha256, created_ts,
            lease_fence, attempt_count, backoff_seconds, next_attempt_ts
        ) VALUES (
            ?, 'pending', ?, 1, 'delivery-project', ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Subject', 'Body',
            'normal', 0, '[]', '# Canonical delivery', ?, CURRENT_TIMESTAMP,
            0, 0, 0, CURRENT_TIMESTAMP
        )
        """,
        (
            delivery_id,
            delivery_kind,
            PROJECT_GENERATION,
            sender_project_id,
            sender_project_slug,
            sender_project_generation,
            sender_id,
            sender_name,
            sender_generation,
            actor_kind,
            actor_id,
            actor_name,
            actor_generation,
            actor_epoch,
            actor_project_id,
            actor_project_slug,
            actor_project_generation,
            idempotency_key,
            request_sha256,
            thread_id,
            reply_to_message_id,
            DOCUMENT_SHA256,
        ),
    )
    return delivery_id


def _insert_recipient(
    connection: sqlite3.Connection,
    delivery_id: str,
    *,
    ordinal: int = 0,
    kind: str = "to",
    agent_id: int = 11,
    agent_name: str = "RecipientAgent",
    agent_generation: str = RECIPIENT_GENERATION,
    project_id: int = 1,
) -> None:
    connection.execute(
        """
        INSERT INTO message_delivery_recipients (
            delivery_id, ordinal, kind, agent_id, agent_name_snapshot,
            agent_generation_snapshot, project_id_snapshot
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            delivery_id,
            ordinal,
            kind,
            agent_id,
            agent_name,
            agent_generation,
            project_id,
        ),
    )


def _materialize_delivery_message(
    connection: sqlite3.Connection,
    delivery_id: str,
    message_id: int,
    *,
    sender_id: int = 10,
) -> None:
    created_ts = connection.execute(
        "SELECT created_ts FROM message_deliveries WHERE id = ?",
        (delivery_id,),
    ).fetchone()[0]
    connection.execute(
        """
        INSERT INTO messages (
            id, project_id, sender_id, delivery_id, thread_id, reply_to, subject, body_md,
            importance, ack_required, created_ts, attachments
        ) VALUES (?, 1, ?, ?, 'delivery-thread', 100, 'Subject', 'Body',
                  'normal', 0, ?, '[]')
        """,
        (message_id, sender_id, delivery_id, created_ts),
    )
    connection.execute(
        "INSERT INTO message_recipients (message_id, agent_id, kind) "
        "VALUES (?, 11, 'to')",
        (message_id,),
    )


def test_legacy_agents_gain_stable_immutable_generation(isolated_env) -> None:
    database_path = _database_path()
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            """
            CREATE TABLE projects (
                id INTEGER PRIMARY KEY,
                slug VARCHAR(255) NOT NULL UNIQUE,
                human_key VARCHAR(255) NOT NULL,
                created_at DATETIME NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                name VARCHAR(128) NOT NULL,
                program VARCHAR(128) NOT NULL,
                model VARCHAR(128) NOT NULL,
                task_description VARCHAR(2048) NOT NULL,
                inception_ts DATETIME NOT NULL,
                last_active_ts DATETIME NOT NULL,
                attachments_policy VARCHAR(16) NOT NULL,
                contact_policy VARCHAR(16) NOT NULL,
                UNIQUE (project_id, name)
            )
            """
        )
        connection.execute(
            "INSERT INTO projects (id, slug, human_key, created_at) "
            "VALUES (1, 'legacy', '/legacy', CURRENT_TIMESTAMP)"
        )
        connection.execute(
            """
            INSERT INTO agents (
                id, project_id, name, program, model, task_description,
                inception_ts, last_active_ts, attachments_policy, contact_policy
            ) VALUES (1, 1, 'LegacyAgent', 'codex', 'test', '',
                      CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'auto', 'open')
            """
        )
        connection.commit()

    _run_database(ensure_schema())
    with closing(_open_database()) as connection:
        generation = connection.execute(
            "SELECT agent_generation FROM agents WHERE id = 1"
        ).fetchone()[0]
        assert len(generation) == 64
        assert not set(generation) - set("0123456789abcdef")
        assert connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'message_deliveries'"
        ).fetchone() == ("message_deliveries",)
        delivery_columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info('message_deliveries')")
        }
        assert delivery_columns["sender_project_id_snapshot"][3] == 1
        assert delivery_columns["sender_project_slug_snapshot"][3] == 1
        assert delivery_columns["sender_project_generation_snapshot"][3] == 1
        assert delivery_columns["actor_project_id_snapshot"][3] == 0
        assert list(
            connection.execute("PRAGMA foreign_key_list('message_deliveries')")
        ) == []
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE agents SET agent_generation = ? WHERE id = 1",
                ("9" * 64,),
            )
        connection.rollback()

    _run_database(ensure_schema())
    with closing(_open_database()) as connection:
        assert connection.execute(
            "SELECT agent_generation FROM agents WHERE id = 1"
        ).fetchone()[0] == generation


def test_legacy_ui_locale_schema_is_rebuilt_without_losing_account_links(
    isolated_env,
) -> None:
    _run_database(ensure_schema())
    with closing(_open_database()) as connection:
        _seed_identities(connection)
        _install_legacy_ui_locale_schema(connection)

        legacy_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'ui_users'"
        ).fetchone()[0]
        assert "VARCHAR(2)" in legacy_sql
        assert "'fr'" not in legacy_sql
        assert connection.execute(
            "SELECT role FROM ui_project_assignments WHERE user_id = 20 AND project_id = 1"
        ).fetchone() == ("operator",)

    _run_database(ensure_schema())
    with closing(_open_database()) as connection:
        table_info = {
            row[1]: row for row in connection.execute("PRAGMA table_info('ui_users')")
        }
        assert table_info["preferred_ui_locale"][2].casefold() == "varchar(16)"
        assert (
            table_info["preferred_correspondence_locale"][2].casefold()
            == "varchar(16)"
        )
        create_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'ui_users'"
        ).fetchone()[0]
        assert "'fr'" in create_sql
        assert "'my-MM'" in create_sql
        assert "'zh-Hant'" in create_sql
        assert connection.execute(
            """
            SELECT username, preferred_ui_locale, preferred_correspondence_locale
            FROM ui_users WHERE id = 20
            """
        ).fetchone() == ("operator", "pl", "en")
        assert connection.execute(
            "SELECT role FROM ui_project_assignments WHERE user_id = 20 AND project_id = 1"
        ).fetchone() == ("operator",)
        assert list(connection.execute("PRAGMA foreign_key_check")) == []
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        preserved_objects = {
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE name IN (
                    'ui_users_custom_role_idx',
                    'ui_users_custom_noop',
                    'ui_users_public_names'
                )
                """
            )
        }
        assert preserved_objects == {
            "ui_users_custom_role_idx",
            "ui_users_custom_noop",
            "ui_users_public_names",
        }
        assert connection.execute(
            "SELECT username FROM ui_users_public_names WHERE id = 20"
        ).fetchone() == ("operator",)

        connection.execute(
            """
            UPDATE ui_users
            SET preferred_ui_locale = 'my-MM',
                preferred_correspondence_locale = 'zh-Hant'
            WHERE id = 20
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="invalid preferred_ui_locale"):
            connection.execute(
                "UPDATE ui_users SET preferred_ui_locale = 'MY-mm' WHERE id = 20"
            )
        connection.rollback()

    _run_database(ensure_schema())
    with closing(_open_database()) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM ui_users WHERE id = 20"
        ).fetchone() == (1,)


def test_legacy_ui_locale_schema_rebuild_rolls_back_after_late_failure(
    isolated_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_database(ensure_schema())
    with closing(_open_database()) as connection:
        _seed_identities(connection)
        _install_legacy_ui_locale_schema(connection)
        legacy_table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'ui_users'"
        ).fetchone()[0]
        legacy_account = connection.execute(
            """
            SELECT id, username, password_hash, role, disabled, session_epoch,
                   session_generation, display_name, profile_revision,
                   preferred_ui_locale, preferred_correspondence_locale,
                   created_ts, last_login_ts
            FROM ui_users WHERE id = 20
            """
        ).fetchone()
        legacy_objects = connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE name IN (
                'ui_users_custom_role_idx',
                'ui_users_custom_noop',
                'ui_users_public_names'
            )
            ORDER BY type, name
            """
        ).fetchall()
        legacy_assignments = connection.execute(
            """
            SELECT user_id, project_id, role
            FROM ui_project_assignments
            WHERE user_id = 20
            ORDER BY project_id
            """
        ).fetchall()

    original_rebuild = database_module._rebuild_ui_users_locale_schema
    rebuild_completed = False

    def fail_after_rebuild(connection: Any) -> None:
        nonlocal rebuild_completed
        original_rebuild(connection)
        assert connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE name = 'ui_users_locale_v2'"
        ).fetchone() is None
        migrated_table_sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'ui_users'"
        ).scalar_one()
        assert "'fr'" in str(migrated_table_sql)
        rebuild_completed = True
        raise RuntimeError("injected failure after ui_users locale rebuild")

    monkeypatch.setattr(
        database_module,
        "_rebuild_ui_users_locale_schema",
        fail_after_rebuild,
    )

    async def fail_migration_and_read_foreign_keys() -> int:
        with pytest.raises(
            RuntimeError,
            match="injected failure after ui_users locale rebuild",
        ):
            await ensure_schema()
        async with database_module.get_engine().connect() as connection:
            result = await connection.exec_driver_sql("PRAGMA foreign_keys")
            return int(result.scalar_one())

    assert _run_database(fail_migration_and_read_foreign_keys()) == 1
    assert rebuild_completed is True

    with closing(_open_database()) as connection:
        assert connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'ui_users'"
        ).fetchone()[0] == legacy_table_sql
        assert connection.execute(
            """
            SELECT id, username, password_hash, role, disabled, session_epoch,
                   session_generation, display_name, profile_revision,
                   preferred_ui_locale, preferred_correspondence_locale,
                   created_ts, last_login_ts
            FROM ui_users WHERE id = 20
            """
        ).fetchone() == legacy_account
        assert connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE name IN (
                'ui_users_custom_role_idx',
                'ui_users_custom_noop',
                'ui_users_public_names'
            )
            ORDER BY type, name
            """
        ).fetchall() == legacy_objects
        assert connection.execute(
            """
            SELECT user_id, project_id, role
            FROM ui_project_assignments
            WHERE user_id = 20
            ORDER BY project_id
            """
        ).fetchall() == legacy_assignments
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'ui_users_locale_v2'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT username FROM ui_users_public_names WHERE id = 20"
        ).fetchone() == ("operator",)
        assert list(connection.execute("PRAGMA foreign_key_check")) == []
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)


def test_schema_exposes_due_idempotency_and_audit_guards(isolated_env) -> None:
    _run_database(ensure_schema())
    with closing(_open_database()) as connection:
        index_sql = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'message_deliveries'"
            )
        }
        assert "coalesce(actor_generation_snapshot, '')" in (
            index_sql["uq_message_deliveries_idempotency"] or ""
        ).lower()
        assert "coalesce(actor_project_generation_snapshot, '')" in (
            index_sql["uq_message_deliveries_idempotency"] or ""
        ).lower()
        assert "project_id, project_generation_snapshot" in (
            index_sql["uq_message_deliveries_idempotency"] or ""
        ).lower()
        assert "where state = 'pending'" in (
            index_sql["idx_message_deliveries_due"] or ""
        ).lower()
        trigger_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND (name LIKE 'message_deliveries_%' "
                "OR name LIKE 'message_delivery_recipients_%')"
            )
        }
        assert {
            "message_deliveries_guard_bi",
            "message_deliveries_snapshots_bu",
            "message_deliveries_receipt_bu",
            "message_deliveries_lease_fence_bu",
            "message_deliveries_terminal_bu",
            "message_deliveries_transition_bu",
            "message_deliveries_immutable_bd",
            "message_delivery_recipients_guard_bi",
            "message_delivery_recipients_immutable_bu",
            "message_delivery_recipients_immutable_bd",
            "message_deliveries_project_guard_bd",
            "message_deliveries_project_guard_bu",
            "message_deliveries_agent_pending_bd",
            "message_deliveries_agent_pending_bu",
            "message_deliveries_ui_user_pending_bd",
            "message_deliveries_ui_user_pending_bu",
            "message_deliveries_reply_target_pending_bd",
            "message_deliveries_reply_target_pending_bu",
            "message_deliveries_reply_target_pending_bi",
        } <= trigger_names
        assert list(
            connection.execute("PRAGMA foreign_key_list('message_deliveries')")
        ) == []
        recipient_fks = list(
            connection.execute(
                "PRAGMA foreign_key_list('message_delivery_recipients')"
            )
        )
        assert [(row[2], row[3]) for row in recipient_fks] == [
            ("message_deliveries", "delivery_id")
        ]


def test_agent_replace_and_delivery_idempotency_collisions_fail_closed(isolated_env) -> None:
    _run_database(ensure_schema())
    with closing(_open_database()) as connection:
        _seed_identities(connection)
        _insert_delivery(connection)
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="agents identity collision"):
            connection.execute(
                """
                INSERT OR REPLACE INTO agents (
                    id, project_id, name, agent_generation, program, model,
                    task_description, inception_ts, last_active_ts,
                    attachments_policy, contact_policy
                ) VALUES (10, 1, 'HostileAgent', ?, 'hostile', 'hostile', '',
                          CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'auto', 'open')
                """,
                ("9" * 64,),
            )
        connection.rollback()
        assert connection.execute(
            "SELECT name, agent_generation FROM agents WHERE id = 10"
        ).fetchone() == ("SenderAgent", SENDER_GENERATION)

        with pytest.raises(sqlite3.IntegrityError, match="identity collision"):
            _insert_delivery(
                connection,
                delivery_id=str(uuid.uuid4()),
                idempotency_key="request-1",
                request_sha256="0" * 64,
            )
        connection.rollback()
        assert connection.execute(
            "SELECT count(*) FROM message_deliveries"
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    ("delivery_id", "idempotency_key", "request_sha256"),
    [
        (str(uuid.uuid4()).upper(), "bad-uuid", REQUEST_SHA256),
        ("-0000000-0000-0000-0000-000000000000", "extra-hyphen", REQUEST_SHA256),
        (None, "bad-request-hash", "F" * 64),
    ],
)
def test_delivery_uuid_and_hashes_are_canonical_lowercase_hex(
    isolated_env,
    delivery_id: str | None,
    idempotency_key: str,
    request_sha256: str,
) -> None:
    _run_database(ensure_schema())
    with closing(_open_database()) as connection:
        _seed_identities(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_delivery(
                connection,
                delivery_id=delivery_id,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
            )
        connection.rollback()
        assert connection.execute(
            "SELECT count(*) FROM message_deliveries"
        ).fetchone() == (0,)


def test_delivery_kind_is_closed_and_immutable(isolated_env) -> None:
    _run_database(ensure_schema())
    with closing(_open_database()) as connection:
        _seed_identities(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_delivery(
                connection,
                delivery_kind="authorization_bypass",
            )
        connection.rollback()
        delivery_id = _insert_delivery(
            connection,
            delivery_kind="reply",
            actor_kind="agent",
            actor_id=10,
            actor_name="SenderAgent",
            actor_generation=SENDER_GENERATION,
        )
        connection.commit()
        with pytest.raises(
            sqlite3.IntegrityError,
            match="message delivery snapshots are immutable",
        ):
            connection.execute(
                "UPDATE message_deliveries SET delivery_kind = 'message' WHERE id = ?",
                (delivery_id,),
            )
        connection.rollback()
        assert connection.execute(
            "SELECT delivery_kind FROM message_deliveries WHERE id = ?",
            (delivery_id,),
        ).fetchone() == ("reply",)

        contact_request_id = _insert_delivery(
            connection,
            delivery_kind="contact_request",
            idempotency_key="contact-request-1",
            request_sha256="9" * 64,
            actor_kind="agent",
            actor_id=10,
            actor_name="SenderAgent",
            actor_generation=SENDER_GENERATION,
        )
        connection.commit()
        assert connection.execute(
            "SELECT delivery_kind FROM message_deliveries WHERE id = ?",
            (contact_request_id,),
        ).fetchone() == ("contact_request",)


@pytest.mark.parametrize(
    "slug",
    ["../escape", "/absolute", "back\\slash", "Uppercase", ".", "-edge", "edge-"],
)
def test_project_slug_is_one_canonical_filesystem_segment(
    isolated_env,
    slug: str,
) -> None:
    _run_database(ensure_schema())
    with closing(_open_database()) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="canonical project slug"):
            connection.execute(
                "INSERT INTO projects (slug, human_key, project_generation, created_at) "
                "VALUES (?, '/hostile', ?, CURRENT_TIMESTAMP)",
                (slug, "1" * 64),
            )
        connection.rollback()


@pytest.mark.parametrize(
    ("actor_kind", "actor_id", "actor_name", "actor_generation", "actor_epoch"),
    [
        ("agent", 10, "SenderAgent", SENDER_GENERATION, None),
        ("ui_user", 20, "operator", USER_GENERATION, 7),
        ("system", 0, "system", None, None),
    ],
)
def test_actor_provenance_variants_are_bound_to_identity_lifetimes(
    isolated_env,
    actor_kind: str,
    actor_id: int,
    actor_name: str,
    actor_generation: str | None,
    actor_epoch: int | None,
) -> None:
    _run_database(ensure_schema())
    with closing(_open_database()) as connection:
        _seed_identities(connection)
        _insert_delivery(
            connection,
            delivery_kind="reply" if actor_kind == "ui_user" else "message",
            actor_kind=actor_kind,
            actor_id=actor_id,
            actor_name=actor_name,
            actor_generation=actor_generation,
            actor_epoch=actor_epoch,
        )
        connection.commit()
        assert connection.execute(
            "SELECT actor_kind, actor_id, actor_generation_snapshot "
            "FROM message_deliveries"
        ).fetchone() == (actor_kind, actor_id, actor_generation)

        with pytest.raises(sqlite3.IntegrityError):
            _insert_delivery(
                connection,
                idempotency_key="bad-provenance",
                actor_kind=actor_kind,
                actor_id=actor_id,
                actor_name=actor_name,
                actor_generation="9" * 64,
                actor_epoch=actor_epoch,
            )
        connection.rollback()


def test_system_and_agent_actor_provenance_cannot_be_forged(isolated_env) -> None:
    _run_database(ensure_schema())
    with closing(_open_database()) as connection:
        _seed_identities(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_delivery(
                connection,
                idempotency_key="forged-system-name",
                actor_name="Administrator",
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            _insert_delivery(
                connection,
                idempotency_key="forged-agent-actor",
                actor_kind="agent",
                actor_id=11,
                actor_name="RecipientAgent",
                actor_generation=RECIPIENT_GENERATION,
                actor_project_id=1,
                actor_project_slug="delivery-project",
                actor_project_generation=PROJECT_GENERATION,
            )
        connection.rollback()
        assert connection.execute(
            "SELECT count(*) FROM message_deliveries"
        ).fetchone() == (0,)


def test_ui_actor_authorization_is_enforced_by_database(isolated_env) -> None:
    _run_database(ensure_schema())
    with closing(_open_database()) as connection:
        _seed_identities(connection)
        reply_id = _insert_delivery(
            connection,
            delivery_kind="reply",
            idempotency_key="operator-reply",
            request_sha256="7" * 64,
            actor_kind="ui_user",
            actor_id=20,
            actor_name="operator",
            actor_generation=USER_GENERATION,
            actor_epoch=7,
        )
        connection.commit()
        assert connection.execute(
            "SELECT actor_project_id_snapshot FROM message_deliveries WHERE id = ?",
            (reply_id,),
        ).fetchone() == (1,)

        with pytest.raises(sqlite3.IntegrityError, match="not authorized"):
            _insert_delivery(
                connection,
                idempotency_key="operator-compose",
                request_sha256="6" * 64,
                actor_kind="ui_user",
                actor_id=20,
                actor_name="operator",
                actor_generation=USER_GENERATION,
                actor_epoch=7,
            )
        connection.rollback()

        connection.execute(
            "UPDATE ui_project_assignments SET role = 'viewer' "
            "WHERE user_id = 20 AND project_id = 1"
        )
        with pytest.raises(sqlite3.IntegrityError, match="not authorized"):
            _insert_delivery(
                connection,
                delivery_kind="reply",
                idempotency_key="viewer-reply",
                request_sha256="5" * 64,
                actor_kind="ui_user",
                actor_id=20,
                actor_name="operator",
                actor_generation=USER_GENERATION,
                actor_epoch=7,
            )
        connection.rollback()

        connection.execute("UPDATE ui_users SET role = 'admin' WHERE id = 20")
        admin_message_id = _insert_delivery(
            connection,
            idempotency_key="admin-compose",
            request_sha256="4" * 64,
            actor_kind="ui_user",
            actor_id=20,
            actor_name="operator",
            actor_generation=USER_GENERATION,
            actor_epoch=7,
        )
        connection.commit()
        assert connection.execute(
            "SELECT delivery_kind FROM message_deliveries WHERE id = ?",
            (admin_message_id,),
        ).fetchone() == ("message",)


def test_cross_project_sender_and_agent_actor_keep_independent_source_lifetime(
    isolated_env,
) -> None:
    _run_database(ensure_schema())
    with closing(_open_database()) as connection:
        _seed_identities(connection)
        delivery_id = _insert_delivery(
            connection,
            idempotency_key="cross-project-request",
            sender_id=12,
            sender_name="SourceSender",
            sender_generation=SOURCE_SENDER_GENERATION,
            sender_project_id=2,
            sender_project_slug="source-project",
            sender_project_generation=SOURCE_PROJECT_GENERATION,
            actor_kind="agent",
            actor_id=12,
            actor_name="SourceSender",
            actor_generation=SOURCE_SENDER_GENERATION,
        )
        _insert_recipient(connection, delivery_id)
        connection.commit()
        canonical_path = (
            f"projects/delivery-project/message_deliveries/{delivery_id}.md"
        )

        for wrong_path in (
            f"projects/other/message_deliveries/{delivery_id}.md",
            "../message-deliveries/escape.md",
            f"projects/delivery-project/message_deliveries/{delivery_id.upper()}.md",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    UPDATE message_deliveries
                    SET archive_relative_path = ?, archive_blob_sha = ?,
                        archive_commit_sha = ?
                    WHERE id = ?
                    """,
                    (wrong_path, "1" * 40, "2" * 40, delivery_id),
                )
            connection.rollback()

        connection.execute(
            """
            UPDATE message_deliveries
            SET archive_relative_path = ?, archive_blob_sha = ?,
                archive_commit_sha = ?
            WHERE id = ?
            """,
            (canonical_path, "1" * 40, "2" * 40, delivery_id),
        )
        connection.commit()

        for receipt_update in (
            "archive_blob_sha = '3' || substr(archive_blob_sha, 2), "
            "archive_commit_sha = '4' || substr(archive_commit_sha, 2)",
            "archive_relative_path = NULL, archive_blob_sha = NULL, "
            "archive_commit_sha = NULL",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="receipt is immutable"):
                connection.execute(
                    f"UPDATE message_deliveries SET {receipt_update} WHERE id = ?",
                    (delivery_id,),
                )
            connection.rollback()

        assert connection.execute(
            """
            SELECT project_id, sender_project_id_snapshot,
                   actor_project_id_snapshot
            FROM message_deliveries
            WHERE id = ?
            """,
            (delivery_id,),
        ).fetchone() == (1, 2, 2)
        with pytest.raises(sqlite3.IntegrityError, match="project has pending message delivery"):
            connection.execute("DELETE FROM projects WHERE id = 2")
        connection.rollback()

        _materialize_delivery_message(
            connection,
            delivery_id,
            101,
            sender_id=12,
        )
        connection.execute(
            """
            UPDATE message_deliveries
            SET state = 'published', message_id = 101,
                archive_relative_path = ?,
                archive_blob_sha = ?, archive_commit_sha = ?,
                published_ts = CURRENT_TIMESTAMP, next_attempt_ts = NULL
            WHERE id = ?
            """,
            (
                f"projects/delivery-project/message_deliveries/{delivery_id}.md",
                "1" * 40,
                "2" * 40,
                delivery_id,
            ),
        )
        connection.commit()
        assert connection.execute(
            "SELECT project_id, sender_id FROM messages WHERE id = 101"
        ).fetchone() == (1, 12)


def test_recipient_trigger_enforces_cross_project_route_and_contact_shape(
    isolated_env,
) -> None:
    _run_database(ensure_schema())
    with closing(_open_database()) as connection:
        _seed_identities(connection)
        connection.execute("DELETE FROM agent_links")
        unauthorized_id = _insert_delivery(
            connection,
            idempotency_key="unapproved-message",
            request_sha256="3" * 64,
            sender_id=12,
            sender_name="SourceSender",
            sender_generation=SOURCE_SENDER_GENERATION,
            sender_project_id=2,
            sender_project_slug="source-project",
            sender_project_generation=SOURCE_PROJECT_GENERATION,
            actor_kind="agent",
            actor_id=12,
            actor_name="SourceSender",
            actor_generation=SOURCE_SENDER_GENERATION,
        )
        with pytest.raises(sqlite3.IntegrityError, match="route is not approved"):
            _insert_recipient(connection, unauthorized_id)
        connection.rollback()

        connection.execute(
            "UPDATE agent_links SET status = 'pending', reason = 'contact request', "
            "updated_ts = CURRENT_TIMESTAMP "
            "WHERE a_project_id = 2 AND a_agent_id = 12 "
            "AND b_project_id = 1 AND b_agent_id = 11"
        )
        contact_id = _insert_delivery(
            connection,
            delivery_kind="contact_request",
            idempotency_key="pending-contact",
            request_sha256="2" * 64,
            sender_id=12,
            sender_name="SourceSender",
            sender_generation=SOURCE_SENDER_GENERATION,
            sender_project_id=2,
            sender_project_slug="source-project",
            sender_project_generation=SOURCE_PROJECT_GENERATION,
            actor_kind="agent",
            actor_id=12,
            actor_name="SourceSender",
            actor_generation=SOURCE_SENDER_GENERATION,
        )
        _insert_recipient(connection, contact_id)
        with pytest.raises(sqlite3.IntegrityError, match="exactly one to recipient"):
            _insert_recipient(
                connection,
                contact_id,
                ordinal=1,
                kind="cc",
                agent_id=10,
                agent_name="SenderAgent",
                agent_generation=SENDER_GENERATION,
            )
        connection.rollback()

        connection.execute("DELETE FROM agent_links")
        connection.execute(
            """
            INSERT INTO messages (
                id, project_id, sender_id, subject, body_md, importance,
                ack_required, thread_id, created_ts, attachments
            ) VALUES (101, 2, 11, 'Inbound', 'Question', 'normal', 0,
                      'delivery-thread', CURRENT_TIMESTAMP, '[]')
            """
        )
        connection.execute(
            "INSERT INTO message_recipients (message_id, agent_id, kind) "
            "VALUES (101, 12, 'to')"
        )
        reply_id = _insert_delivery(
            connection,
            delivery_kind="reply",
            idempotency_key="thread-reply",
            request_sha256="1" * 64,
            sender_id=12,
            sender_name="SourceSender",
            sender_generation=SOURCE_SENDER_GENERATION,
            sender_project_id=2,
            sender_project_slug="source-project",
            sender_project_generation=SOURCE_PROJECT_GENERATION,
            actor_kind="agent",
            actor_id=12,
            actor_name="SourceSender",
            actor_generation=SOURCE_SENDER_GENERATION,
        )
        _insert_recipient(connection, reply_id)

        forged_reply_id = _insert_delivery(
            connection,
            delivery_kind="reply",
            idempotency_key="forged-thread-reply",
            request_sha256="0" * 64,
            sender_id=12,
            sender_name="SourceSender",
            sender_generation=SOURCE_SENDER_GENERATION,
            sender_project_id=2,
            sender_project_slug="source-project",
            sender_project_generation=SOURCE_PROJECT_GENERATION,
            actor_kind="agent",
            actor_id=12,
            actor_name="SourceSender",
            actor_generation=SOURCE_SENDER_GENERATION,
            thread_id="forged-thread",
        )
        with pytest.raises(sqlite3.IntegrityError, match="reply recipient route"):
            _insert_recipient(connection, forged_reply_id)
        connection.rollback()


@pytest.mark.parametrize(
    ("include_path", "include_blob", "include_commit"),
    [
        (True, False, False),
        (False, True, False),
        (False, False, True),
        (True, True, False),
        (True, False, True),
        (False, True, True),
    ],
)
def test_delivery_receipt_is_all_null_or_all_complete(
    isolated_env,
    include_path: bool,
    include_blob: bool,
    include_commit: bool,
) -> None:
    _run_database(ensure_schema())
    with closing(_open_database()) as connection:
        _seed_identities(connection)
        delivery_id = _insert_delivery(connection)
        connection.commit()
        canonical_path = (
            f"projects/delivery-project/message_deliveries/{delivery_id}.md"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE message_deliveries
                SET archive_relative_path = ?, archive_blob_sha = ?,
                    archive_commit_sha = ?
                WHERE id = ?
                """,
                (
                    canonical_path if include_path else None,
                    "1" * 40 if include_blob else None,
                    "2" * 40 if include_commit else None,
                    delivery_id,
                ),
            )
        connection.rollback()
        assert connection.execute(
            "SELECT archive_relative_path, archive_blob_sha, archive_commit_sha "
            "FROM message_deliveries WHERE id = ?",
            (delivery_id,),
        ).fetchone() == (None, None, None)


def test_quarantined_delivery_requires_a_nonempty_reason(isolated_env) -> None:
    _run_database(ensure_schema())
    with closing(_open_database()) as connection:
        _seed_identities(connection)
        delivery_id = _insert_delivery(connection)
        connection.commit()
        for reason in (None, "", "   "):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    UPDATE message_deliveries
                    SET state = 'quarantined', quarantined_ts = CURRENT_TIMESTAMP,
                        quarantine_reason = ?, next_attempt_ts = NULL
                    WHERE id = ?
                    """,
                    (reason, delivery_id),
                )
            connection.rollback()
        assert connection.execute(
            "SELECT state, quarantine_reason FROM message_deliveries WHERE id = ?",
            (delivery_id,),
        ).fetchone() == ("pending", None)


def test_recipient_snapshots_are_ordered_unique_and_immutable(isolated_env) -> None:
    _run_database(ensure_schema())
    with closing(_open_database()) as connection:
        _seed_identities(connection)
        delivery_id = _insert_delivery(connection)
        _insert_recipient(connection, delivery_id)
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="identity collision"):
            _insert_recipient(connection, delivery_id, ordinal=1)
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="snapshot mismatch"):
            _insert_recipient(
                connection,
                delivery_id,
                ordinal=1,
                agent_id=10,
                agent_name="SenderAgent",
                agent_generation="9" * 64,
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE message_delivery_recipients SET kind = 'cc' "
                "WHERE delivery_id = ?",
                (delivery_id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM message_delivery_recipients WHERE delivery_id = ?",
                (delivery_id,),
            )
        connection.rollback()


def test_lease_fence_and_attempt_counters_are_monotonic(isolated_env) -> None:
    _run_database(ensure_schema())
    with closing(_open_database()) as connection:
        _seed_identities(connection)
        delivery_id = _insert_delivery(connection)
        connection.commit()
        connection.execute(
            """
            UPDATE message_deliveries
            SET lease_token = 'worker-a', lease_fence = 1,
                lease_expires_ts = datetime('now', '+5 minutes'),
                attempt_count = 1, last_attempt_ts = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (delivery_id,),
        )
        connection.execute(
            "UPDATE message_deliveries "
            "SET lease_expires_ts = datetime('now', '+10 minutes') WHERE id = ?",
            (delivery_id,),
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="must advance"):
            connection.execute(
                "UPDATE message_deliveries SET lease_token = 'worker-b' WHERE id = ?",
                (delivery_id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="not monotonic"):
            connection.execute(
                "UPDATE message_deliveries SET attempt_count = 0 WHERE id = ?",
                (delivery_id,),
            )
        connection.rollback()
        connection.execute(
            """
            UPDATE message_deliveries
            SET lease_token = 'worker-b', lease_fence = 2,
                lease_expires_ts = datetime('now', '+5 minutes'),
                attempt_count = 2
            WHERE id = ?
            """,
            (delivery_id,),
        )
        connection.commit()
        assert connection.execute(
            "SELECT lease_token, lease_fence, attempt_count "
            "FROM message_deliveries WHERE id = ?",
            (delivery_id,),
        ).fetchone() == ("worker-b", 2, 2)


def test_publish_requires_materialized_message_exact_recipients_and_receipt(
    isolated_env,
) -> None:
    _run_database(ensure_schema())
    with closing(_open_database()) as connection:
        _seed_identities(connection)
        delivery_id = _insert_delivery(connection)
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="ordered recipients"):
            connection.execute(
                """
                UPDATE message_deliveries
                SET state = 'published', message_id = 101,
                    archive_relative_path = ?,
                    archive_blob_sha = ?, archive_commit_sha = ?,
                    published_ts = CURRENT_TIMESTAMP, next_attempt_ts = NULL
                WHERE id = ?
                """,
                (
                    f"projects/delivery-project/message_deliveries/{delivery_id}.md",
                    "1" * 40,
                    "2" * 40,
                    delivery_id,
                ),
            )
        connection.rollback()

        _insert_recipient(connection, delivery_id)
        _materialize_delivery_message(connection, delivery_id, 101)
        connection.execute(
            """
            UPDATE message_deliveries
            SET state = 'published', message_id = 101,
                archive_relative_path = ?,
                archive_blob_sha = ?, archive_commit_sha = ?,
                published_ts = CURRENT_TIMESTAMP, next_attempt_ts = NULL
            WHERE id = ?
            """,
            (
                f"projects/delivery-project/message_deliveries/{delivery_id}.md",
                "1" * 40,
                "2" * 40,
                delivery_id,
            ),
        )
        connection.commit()
        assert connection.execute(
            "SELECT state, message_id FROM message_deliveries WHERE id = ?",
            (delivery_id,),
        ).fetchone() == ("published", 101)
        with pytest.raises(sqlite3.IntegrityError, match="terminal"):
            connection.execute(
                "UPDATE message_deliveries SET last_error = 'late' WHERE id = ?",
                (delivery_id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM message_deliveries WHERE id = ?",
                (delivery_id,),
            )
        connection.rollback()

        connection.execute(
            "DELETE FROM message_recipients WHERE message_id IN (100, 101)"
        )
        connection.execute("DELETE FROM messages WHERE id IN (100, 101)")
        connection.execute("DELETE FROM agent_links")
        connection.execute("DELETE FROM agents WHERE id IN (10, 11)")
        connection.commit()
        connection.execute("DELETE FROM projects WHERE id = 1")
        connection.commit()
        assert connection.execute(
            "SELECT id FROM projects WHERE id = 1"
        ).fetchone() is None
        assert connection.execute(
            """
            SELECT state, project_slug_snapshot, sender_name_snapshot,
                   message_id, reply_to_message_id
            FROM message_deliveries
            WHERE id = ?
            """,
            (delivery_id,),
        ).fetchone() == (
            "published",
            "delivery-project",
            "SenderAgent",
            101,
            100,
        )
        assert connection.execute(
            "SELECT agent_name_snapshot FROM message_delivery_recipients "
            "WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone() == ("RecipientAgent",)

        connection.execute(
            """
            INSERT INTO messages (
                id, project_id, sender_id, subject, body_md, importance,
                ack_required, created_ts, attachments
            ) VALUES (101, 2, 12, 'Reused row id', 'New lifetime', 'normal', 0,
                      CURRENT_TIMESTAMP, '[]')
            """
        )
        connection.commit()
        assert connection.execute(
            "SELECT id, delivery_id FROM messages WHERE subject = 'Reused row id'"
        ).fetchone() == (101, None)
        connection.execute("DELETE FROM messages WHERE id = 101")
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="binding mismatch"):
            connection.execute(
                """
                INSERT INTO messages (
                    id, project_id, sender_id, delivery_id, subject, body_md,
                    importance, ack_required, created_ts, attachments
                ) VALUES (101, 2, 12, ?, 'Hijack', 'New lifetime', 'normal', 0,
                          CURRENT_TIMESTAMP, '[]')
                """,
                (delivery_id,),
            )
        connection.rollback()


def test_pending_delivery_blocks_parent_deletion_and_snapshot_mutation(isolated_env) -> None:
    _run_database(ensure_schema())
    with closing(_open_database()) as connection:
        _seed_identities(connection)
        delivery_id = _insert_delivery(
            connection,
            delivery_kind="reply",
            actor_kind="ui_user",
            actor_id=20,
            actor_name="operator",
            actor_generation=USER_GENERATION,
            actor_epoch=7,
        )
        _insert_recipient(connection, delivery_id)
        connection.commit()

        for statement, expected in [
            ("DELETE FROM projects WHERE id = 1", "project has pending message delivery"),
            ("DELETE FROM agents WHERE id = 10", "agent has pending"),
            ("DELETE FROM agents WHERE id = 11", "agent has pending"),
            ("DELETE FROM ui_users WHERE id = 20", "user has pending"),
            ("DELETE FROM messages WHERE id = 100", "reply target"),
        ]:
            with pytest.raises(sqlite3.IntegrityError, match=expected):
                connection.execute(statement)
            connection.rollback()

        for statement, expected in [
            (
                "UPDATE projects SET id = 3 WHERE id = 1",
                "immutable message delivery",
            ),
            (
                "UPDATE projects SET slug = 'moved-project' WHERE id = 1",
                "immutable message delivery",
            ),
            ("UPDATE agents SET id = 13 WHERE id = 10", "agent has pending"),
            (
                "UPDATE agents SET project_id = 2 WHERE id = 11",
                "agent has pending",
            ),
            ("UPDATE ui_users SET id = 21 WHERE id = 20", "user has pending"),
            ("UPDATE messages SET id = 102 WHERE id = 100", "reply target"),
            (
                "UPDATE messages SET project_id = 2 WHERE id = 100",
                "reply target",
            ),
        ]:
            with pytest.raises(sqlite3.IntegrityError, match=expected):
                connection.execute(statement)
            connection.rollback()

        connection.execute("PRAGMA recursive_triggers=OFF")
        with pytest.raises(sqlite3.IntegrityError, match="reply target"):
            connection.execute(
                """
                INSERT OR REPLACE INTO messages (
                    id, project_id, sender_id, subject, body_md, importance,
                    ack_required, created_ts, attachments
                ) VALUES (100, 1, 10, 'Hostile parent', 'Changed', 'urgent', 0,
                          CURRENT_TIMESTAMP, '[]')
                """
            )
        connection.rollback()
        assert connection.execute(
            "SELECT subject FROM messages WHERE id = 100"
        ).fetchone() == ("Parent",)

        with pytest.raises(sqlite3.IntegrityError, match="snapshots are immutable"):
            connection.execute(
                "UPDATE message_deliveries SET subject = 'Changed' WHERE id = ?",
                (delivery_id,),
            )
        connection.rollback()
        assert connection.execute(
            "SELECT subject FROM message_deliveries WHERE id = ?",
            (delivery_id,),
        ).fetchone() == ("Subject",)

        with pytest.raises(sqlite3.IntegrityError, match="session_generation is immutable"):
            connection.execute(
                "UPDATE ui_users SET session_generation = ? WHERE id = 20",
                ("8" * 64,),
            )
        connection.rollback()


def test_update_or_replace_cannot_transplant_protected_identities(isolated_env) -> None:
    _run_database(ensure_schema())
    with closing(_open_database()) as connection:
        _seed_identities(connection)
        delivery_id = _insert_delivery(
            connection,
            delivery_kind="reply",
            actor_kind="ui_user",
            actor_id=20,
            actor_name="operator",
            actor_generation=USER_GENERATION,
            actor_epoch=7,
            sender_id=12,
            sender_name="SourceSender",
            sender_generation=SOURCE_SENDER_GENERATION,
            sender_project_id=2,
            sender_project_slug="source-project",
            sender_project_generation=SOURCE_PROJECT_GENERATION,
            reply_to_message_id=100,
        )
        _insert_recipient(connection, delivery_id)
        connection.execute(
            "INSERT INTO projects (id, slug, human_key, project_generation, created_at) "
            "VALUES (3, 'replacement-project', '/replacement', ?, CURRENT_TIMESTAMP)",
            ("3" * 64,),
        )
        connection.execute(
            """
            INSERT INTO ui_users (
                id, username, password_hash, role, disabled, session_epoch,
                session_generation, profile_revision, preferred_ui_locale, created_ts
            ) VALUES (21, 'replacement-user', 'unused', 'member', 0, 1, ?, 1,
                      'en', CURRENT_TIMESTAMP)
            """,
            ("4" * 64,),
        )
        connection.execute(
            """
            INSERT INTO agents (
                id, project_id, name, agent_generation, program, model,
                task_description, inception_ts, last_active_ts,
                attachments_policy, contact_policy
            ) VALUES (13, 2, 'ReplacementAgent', ?, 'codex', 'test', '',
                      CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'auto', 'open')
            """,
            ("5" * 64,),
        )
        connection.execute(
            """
            INSERT INTO messages (
                id, project_id, sender_id, subject, body_md, importance,
                ack_required, created_ts, attachments
            ) VALUES (102, 2, 12, 'Replacement message', 'Body', 'normal', 0,
                      CURRENT_TIMESTAMP, '[]')
            """
        )
        connection.commit()
        connection.execute("PRAGMA recursive_triggers=OFF")

        for statement, expected in (
            (
                "UPDATE OR REPLACE projects SET id = 1 WHERE id = 3",
                "projects identity collision",
            ),
            (
                "UPDATE OR REPLACE projects SET slug = 'delivery-project' WHERE id = 3",
                "projects identity collision",
            ),
            (
                "UPDATE OR REPLACE ui_users SET id = 20 WHERE id = 21",
                "ui_users identity collision",
            ),
            (
                "UPDATE OR REPLACE ui_users SET username = 'operator' WHERE id = 21",
                "ui_users identity collision",
            ),
            (
                "UPDATE OR REPLACE agents SET id = 12 WHERE id = 13",
                "agents identity collision",
            ),
            (
                "UPDATE OR REPLACE agents SET name = 'SourceSender' WHERE id = 13",
                "agents identity collision",
            ),
            (
                "UPDATE OR REPLACE messages SET id = 100 WHERE id = 102",
                "messages identity collision",
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError, match=expected):
                connection.execute(statement)
            connection.rollback()

        assert connection.execute(
            "SELECT slug, project_generation FROM projects WHERE id = 1"
        ).fetchone() == ("delivery-project", PROJECT_GENERATION)
        assert connection.execute(
            "SELECT username, session_generation FROM ui_users WHERE id = 20"
        ).fetchone() == ("operator", USER_GENERATION)
        assert connection.execute(
            "SELECT name, agent_generation FROM agents WHERE id = 12"
        ).fetchone() == ("SourceSender", SOURCE_SENDER_GENERATION)
        assert connection.execute(
            "SELECT subject FROM messages WHERE id = 100"
        ).fetchone() == ("Parent",)


def test_identity_generations_cannot_be_reused_from_delivery_history(isolated_env) -> None:
    _run_database(ensure_schema())
    with closing(_open_database()) as connection:
        _seed_identities(connection)
        delivery_id = _insert_delivery(
            connection,
            delivery_kind="reply",
            actor_kind="ui_user",
            actor_id=20,
            actor_name="operator",
            actor_generation=USER_GENERATION,
            actor_epoch=7,
            sender_id=12,
            sender_name="SourceSender",
            sender_generation=SOURCE_SENDER_GENERATION,
            sender_project_id=2,
            sender_project_slug="source-project",
            sender_project_generation=SOURCE_PROJECT_GENERATION,
        )
        _insert_recipient(connection, delivery_id)
        connection.execute(
            """
            UPDATE message_deliveries
            SET state = 'quarantined', quarantined_ts = CURRENT_TIMESTAMP,
                quarantine_reason = 'identity lifetime test', next_attempt_ts = NULL
            WHERE id = ?
            """,
            (delivery_id,),
        )
        connection.commit()
        connection.execute("DELETE FROM ui_users WHERE id = 20")
        connection.execute("DELETE FROM agent_links")
        connection.execute("DELETE FROM agents WHERE id = 12")
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="lifetime was already used"):
            connection.execute(
                """
                INSERT INTO ui_users (
                    id, username, password_hash, role, disabled, session_epoch,
                    session_generation, profile_revision, preferred_ui_locale, created_ts
                ) VALUES (20, 'replacement', 'unused', 'member', 0, 1, ?, 1, 'en',
                          CURRENT_TIMESTAMP)
                """,
                (USER_GENERATION,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="lifetime was already used"):
            connection.execute(
                """
                INSERT INTO agents (
                    id, project_id, name, agent_generation, program, model,
                    task_description, inception_ts, last_active_ts,
                    attachments_policy, contact_policy
                ) VALUES (12, 2, 'ReplacementSender', ?, 'codex', 'test', '',
                          CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'auto', 'open')
                """,
                (SOURCE_SENDER_GENERATION,),
            )
        connection.rollback()

        connection.execute(
            """
            INSERT INTO ui_users (
                id, username, password_hash, role, disabled, session_epoch,
                session_generation, profile_revision, preferred_ui_locale, created_ts
            ) VALUES (20, 'replacement', 'unused', 'member', 0, 1, ?, 1, 'en',
                      CURRENT_TIMESTAMP)
            """,
            ("8" * 64,),
        )
        connection.execute(
            """
            INSERT INTO agents (
                id, project_id, name, agent_generation, program, model,
                task_description, inception_ts, last_active_ts,
                attachments_policy, contact_policy
            ) VALUES (12, 2, 'ReplacementSender', ?, 'codex', 'test', '',
                      CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'auto', 'open')
            """,
            ("9" * 64,),
        )
        connection.commit()


def test_quarantine_is_terminal_and_clears_due_state(isolated_env) -> None:
    _run_database(ensure_schema())
    with closing(_open_database()) as connection:
        _seed_identities(connection)
        delivery_id = _insert_delivery(connection)
        connection.execute(
            """
            UPDATE message_deliveries
            SET state = 'quarantined', quarantined_ts = CURRENT_TIMESTAMP,
                quarantine_reason = 'archive hash conflict', next_attempt_ts = NULL
            WHERE id = ?
            """,
            (delivery_id,),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="terminal"):
            connection.execute(
                "UPDATE message_deliveries SET state = 'pending' WHERE id = ?",
                (delivery_id,),
            )
        connection.rollback()
        assert connection.execute(
            "SELECT state, next_attempt_ts FROM message_deliveries WHERE id = ?",
            (delivery_id,),
        ).fetchone() == ("quarantined", None)


def test_model_metadata_contains_delivery_foundation() -> None:
    assert MessageDelivery.__tablename__ == "message_deliveries"
    assert MessageDeliveryRecipient.__tablename__ == "message_delivery_recipients"
    assert {
        column.name for column in SQLModel.metadata.tables["message_deliveries"].columns
    } >= {
        "id",
        "state",
        "request_sha256",
        "archive_document",
        "document_sha256",
        "lease_token",
        "lease_fence",
        "attempt_count",
        "backoff_seconds",
        "archive_relative_path",
        "archive_blob_sha",
        "archive_commit_sha",
        "message_id",
    }
