"""Database Failure Handling Tests.

Tests for graceful handling of database issues:
- Database file missing (should auto-create)
- Schema migration on startup
- Concurrent write handling (retry_on_db_lock)
- Transaction rollback on error
- Session cleanup on exception

Reference: mcp_agent_mail-aea
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from mcp_agent_mail.db import (
    QueryTracker,
    _extract_table_name,
    _validate_agent_execution_schema,
    ensure_schema,
    get_engine,
    get_query_tracker,
    get_session,
    get_session_factory,
    reset_database_state,
    retry_on_db_lock,
    track_queries,
)
from mcp_agent_mail.models import Agent, MessageDelivery, Project, UiUser
from tests.keys import pkey


def _install_released_message_delivery_tables(db_path: Path) -> None:
    """Replace only the delivery ledger with the schema shipped before 0.4.0."""
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        dependent_triggers = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'trigger' AND lower(sql) LIKE '%message_deliver%'"
        ).fetchall()
        for (trigger_name,) in dependent_triggers:
            quoted_name = str(trigger_name).replace('"', '""')
            connection.execute(f'DROP TRIGGER "{quoted_name}"')
        connection.executescript(
            """
            DROP TABLE message_delivery_recipients;
            DROP TABLE message_deliveries;
            CREATE TABLE message_deliveries (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                state VARCHAR(16) NOT NULL,
                project_id INTEGER NOT NULL,
                project_slug_snapshot VARCHAR(255) NOT NULL,
                project_generation_snapshot VARCHAR(64) NOT NULL,
                sender_id INTEGER NOT NULL,
                sender_name_snapshot VARCHAR(128) NOT NULL,
                sender_generation_snapshot VARCHAR(64) NOT NULL,
                actor_kind VARCHAR(16) NOT NULL,
                actor_agent_id INTEGER,
                actor_ui_user_id INTEGER,
                actor_name_snapshot VARCHAR(128) NOT NULL,
                actor_generation_snapshot VARCHAR(64) NOT NULL,
                actor_session_epoch_snapshot INTEGER,
                idempotency_scope VARCHAR(128) NOT NULL,
                idempotency_key VARCHAR(128) NOT NULL,
                request_sha256 VARCHAR(64) NOT NULL,
                thread_id VARCHAR(128),
                reply_to_message_id INTEGER,
                topic VARCHAR(64),
                subject VARCHAR(512) NOT NULL,
                body_md VARCHAR NOT NULL,
                importance VARCHAR(16) NOT NULL,
                ack_required BOOLEAN NOT NULL,
                attachments JSON DEFAULT '[]' NOT NULL,
                archive_document VARCHAR NOT NULL,
                archive_document_sha256 VARCHAR(64) NOT NULL,
                created_ts DATETIME NOT NULL,
                lease_token VARCHAR(128),
                lease_fence INTEGER NOT NULL,
                lease_expires_ts DATETIME,
                attempt_count INTEGER NOT NULL,
                next_attempt_ts DATETIME,
                last_attempt_ts DATETIME,
                last_error VARCHAR,
                archive_commit_sha VARCHAR(64),
                archive_receipt_path VARCHAR(1024),
                receipt_sha256 VARCHAR(64),
                published_message_id INTEGER,
                published_ts DATETIME,
                quarantined_ts DATETIME,
                quarantine_reason VARCHAR,
                CONSTRAINT ck_message_deliveries_state
                    CHECK (state IN ('pending', 'published', 'quarantined')),
                CONSTRAINT ck_message_deliveries_actor_provenance CHECK (
                    actor_kind IN ('agent', 'ui_user') AND (
                        (actor_kind = 'agent'
                         AND actor_agent_id IS NOT NULL
                         AND actor_ui_user_id IS NULL
                         AND actor_session_epoch_snapshot IS NULL)
                        OR
                        (actor_kind = 'ui_user'
                         AND actor_agent_id IS NULL
                         AND actor_ui_user_id IS NOT NULL
                         AND actor_session_epoch_snapshot >= 1)
                    )
                ),
                CONSTRAINT uq_message_deliveries_idempotency
                    UNIQUE (project_id, idempotency_scope, idempotency_key),
                CONSTRAINT uq_message_deliveries_published_message
                    UNIQUE (published_message_id),
                FOREIGN KEY(project_id) REFERENCES projects(id),
                FOREIGN KEY(sender_id) REFERENCES agents(id),
                FOREIGN KEY(actor_agent_id) REFERENCES agents(id),
                FOREIGN KEY(actor_ui_user_id) REFERENCES ui_users(id),
                FOREIGN KEY(reply_to_message_id) REFERENCES messages(id),
                FOREIGN KEY(published_message_id) REFERENCES messages(id)
            );
            CREATE TABLE message_delivery_recipients (
                delivery_id VARCHAR(36) NOT NULL,
                position INTEGER NOT NULL,
                kind VARCHAR(8) NOT NULL,
                agent_id INTEGER NOT NULL,
                agent_name_snapshot VARCHAR(128) NOT NULL,
                agent_generation_snapshot VARCHAR(64) NOT NULL,
                PRIMARY KEY (delivery_id, position),
                CONSTRAINT ck_message_delivery_recipients_position
                    CHECK (position >= 0),
                CONSTRAINT ck_message_delivery_recipients_kind
                    CHECK (kind IN ('to', 'cc', 'bcc')),
                CONSTRAINT uq_message_delivery_recipients_agent
                    UNIQUE (delivery_id, agent_id),
                FOREIGN KEY(delivery_id) REFERENCES message_deliveries(id),
                FOREIGN KEY(agent_id) REFERENCES agents(id)
            );
            CREATE INDEX idx_message_deliveries_project_created
                ON message_deliveries (project_id, created_ts);
            CREATE INDEX idx_message_deliveries_due
                ON message_deliveries (state, next_attempt_ts, lease_expires_ts);
            CREATE INDEX idx_message_deliveries_reply_pending
                ON message_deliveries (reply_to_message_id, state);
            CREATE INDEX idx_message_delivery_recipients_agent
                ON message_delivery_recipients (agent_id, delivery_id);
            """
        )


_RELEASED_DELIVERY_INSERT_COLUMNS = (
    "id",
    "state",
    "project_id",
    "project_slug_snapshot",
    "project_generation_snapshot",
    "sender_id",
    "sender_name_snapshot",
    "sender_generation_snapshot",
    "actor_kind",
    "actor_agent_id",
    "actor_ui_user_id",
    "actor_name_snapshot",
    "actor_generation_snapshot",
    "actor_session_epoch_snapshot",
    "idempotency_scope",
    "idempotency_key",
    "request_sha256",
    "thread_id",
    "reply_to_message_id",
    "topic",
    "subject",
    "body_md",
    "importance",
    "ack_required",
    "attachments",
    "archive_document",
    "archive_document_sha256",
    "created_ts",
    "lease_token",
    "lease_fence",
    "lease_expires_ts",
    "attempt_count",
    "next_attempt_ts",
    "last_attempt_ts",
    "last_error",
    "archive_commit_sha",
    "archive_receipt_path",
    "receipt_sha256",
    "published_message_id",
    "published_ts",
    "quarantined_ts",
    "quarantine_reason",
)


def _insert_released_delivery(
    connection: sqlite3.Connection,
    values: Mapping[str, object],
) -> None:
    columns = ", ".join(_RELEASED_DELIVERY_INSERT_COLUMNS)
    placeholders = ", ".join("?" for _ in _RELEASED_DELIVERY_INSERT_COLUMNS)
    connection.execute(
        f"INSERT INTO message_deliveries ({columns}) VALUES ({placeholders})",
        tuple(values[column] for column in _RELEASED_DELIVERY_INSERT_COLUMNS),
    )

# ============================================================================
# Database Auto-Creation Tests
# ============================================================================


class TestDatabaseAutoCreation:
    """Tests for automatic database and table creation."""

    @pytest.mark.asyncio
    async def test_ensure_schema_creates_database_file(self, tmp_path: Path, monkeypatch):
        """ensure_schema creates database file when it doesn't exist."""
        db_path = tmp_path / "new_database.sqlite3"
        assert not db_path.exists()

        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
        reset_database_state()

        await ensure_schema()

        assert db_path.exists()

    @pytest.mark.asyncio
    async def test_ensure_schema_creates_tables(self, isolated_env):
        """ensure_schema creates all required tables."""
        await ensure_schema()

        engine = get_engine()
        async with engine.begin() as conn:
            # Check that core tables exist by querying sqlite_master
            result = await conn.run_sync(
                lambda sync_conn: sync_conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
            )
            table_names = [row[0] for row in result]

        # Core tables should exist
        assert "projects" in table_names
        assert "agents" in table_names
        assert "messages" in table_names
        assert "message_recipients" in table_names
        assert "file_reservations" in table_names
        assert "agent_executions" in table_names
        assert "build_slot_artifact_projections" in table_names
        assert "build_slot_artifact_paths" in table_names
        assert "agent_links" in table_names

    @pytest.mark.asyncio
    async def test_ensure_schema_creates_fts_table(self, isolated_env):
        """ensure_schema creates FTS virtual table for message search."""
        await ensure_schema()

        engine = get_engine()
        async with engine.begin() as conn:
            result = await conn.run_sync(
                lambda sync_conn: sync_conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='fts_messages'"
                ).fetchall()
            )

        assert len(result) == 1
        assert result[0][0] == "fts_messages"

    @pytest.mark.asyncio
    async def test_ensure_schema_creates_indexes(self, isolated_env):
        """ensure_schema creates performance indexes."""
        await ensure_schema()

        engine = get_engine()
        async with engine.begin() as conn:
            result = await conn.run_sync(
                lambda sync_conn: sync_conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
                ).fetchall()
            )
            index_names = [row[0] for row in result]
            unique_indexes = await conn.run_sync(
                lambda sync_conn: sync_conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND name LIKE 'uq_agent_executions_%'"
                ).fetchall()
            )

        # Check for expected indexes
        assert "idx_messages_created_ts" in index_names
        assert "idx_messages_thread_id" in index_names
        assert "idx_file_reservations_expires_ts" in index_names
        assert "idx_message_recipients_agent_message" in index_names
        assert "idx_messages_project_sender_created" in index_names
        assert "idx_file_reservations_project_released_expires" in index_names
        assert "idx_file_reservations_project_agent_released" in index_names
        assert "idx_agent_executions_active" in index_names
        assert "idx_agent_executions_active_stale" in index_names
        assert "idx_agent_executions_project_active_stale" in index_names
        assert "idx_build_slot_artifact_projections_pending" in index_names
        assert "idx_build_slot_artifact_paths_project_execution" in index_names
        assert "idx_file_reservations_execution" in index_names
        assert "idx_file_reservations_archive_pending" in index_names
        assert "idx_product_project" in index_names
        assert {str(row[0]) for row in unique_indexes} == {
            "uq_agent_executions_session_external",
            "uq_agent_executions_subagent_external",
            "uq_agent_executions_token_hash",
        }

    @pytest.mark.asyncio
    async def test_reaper_queries_use_partial_pending_indexes(self, isolated_env):
        """Recovery plans seek only active/pending rows, never audit history."""
        await ensure_schema()
        engine = get_engine()
        async with engine.connect() as conn:
            stale_global = (
                await conn.exec_driver_sql(
                    "EXPLAIN QUERY PLAN "
                    "SELECT id FROM agent_executions "
                    "WHERE status = 'active' AND last_active_ts <= ? "
                    "ORDER BY last_active_ts, project_id, id LIMIT 256",
                    ("2026-08-14 00:00:00",),
                )
            ).fetchall()
            stale_project = (
                await conn.exec_driver_sql(
                    "EXPLAIN QUERY PLAN "
                    "SELECT id FROM agent_executions "
                    "WHERE status = 'active' AND project_id = ? "
                    "AND last_active_ts <= ? "
                    "ORDER BY last_active_ts, project_id, id LIMIT 256",
                    (1, "2026-08-14 00:00:00"),
                )
            ).fetchall()
            build_pending = (
                await conn.exec_driver_sql(
                    "EXPLAIN QUERY PLAN "
                    "SELECT project_id, execution_id "
                    "FROM build_slot_artifact_projections "
                    "WHERE reconciled_ts IS NULL "
                    "ORDER BY project_id, execution_id LIMIT 256"
                )
            ).fetchall()
            reservation_pending = (
                await conn.exec_driver_sql(
                    "EXPLAIN QUERY PLAN "
                    "SELECT project_id, id FROM file_reservations "
                    "WHERE archive_synced_revision < archive_revision "
                    "ORDER BY project_id, id LIMIT 256"
                )
            ).fetchall()
            artifact_paths = (
                await conn.exec_driver_sql(
                    "EXPLAIN QUERY PLAN "
                    "SELECT slot_path_component FROM build_slot_artifact_paths "
                    "WHERE project_id = ? AND execution_id IN (?, ?)",
                    (
                        1,
                        "11111111-1111-4111-8111-111111111111",
                        "22222222-2222-4222-8222-222222222222",
                    ),
                )
            ).fetchall()

        def rendered(plan: Sequence[object]) -> str:
            return " ".join(str(row) for row in plan)

        assert "idx_agent_executions_active_stale" in rendered(stale_global)
        assert "idx_agent_executions_project_active_stale" in rendered(
            stale_project
        )
        assert "idx_build_slot_artifact_projections_pending" in rendered(
            build_pending
        )
        assert "idx_file_reservations_archive_pending" in rendered(
            reservation_pending
        )
        assert "idx_build_slot_artifact_paths_project_execution" in rendered(
            artifact_paths
        )

    @pytest.mark.asyncio
    async def test_ensure_schema_is_idempotent(self, isolated_env):
        """ensure_schema can be called multiple times safely."""
        await ensure_schema()
        await ensure_schema()  # Second call should not raise
        await ensure_schema()  # Third call should not raise

        # Verify tables still exist
        engine = get_engine()
        async with engine.begin() as conn:
            result = await conn.run_sync(
                lambda sync_conn: sync_conn.exec_driver_sql(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='projects'"
                ).fetchone()
            )
            # fetchone() is Row | None; assert before subscripting so a missing
            # row fails as itself rather than as a TypeError one line later.
            assert result is not None
            assert result[0] == 1

    @pytest.mark.asyncio
    async def test_agent_execution_fresh_schema_contract(self, isolated_env):
        """Fresh databases expose execution ownership, metadata, FKs, and guards."""
        await ensure_schema()

        engine = get_engine()
        async with engine.connect() as conn:
            columns = await conn.exec_driver_sql("PRAGMA table_info(agent_executions)")
            column_rows = columns.fetchall()
            column_names = {str(row[1]) for row in column_rows}
            reservation_columns = await conn.exec_driver_sql(
                "PRAGMA table_info(file_reservations)"
            )
            reservation_column_names = {
                str(row[1]) for row in reservation_columns.fetchall()
            }
            foreign_keys = await conn.exec_driver_sql(
                "PRAGMA foreign_key_list(file_reservations)"
            )
            execution_foreign_keys = await conn.exec_driver_sql(
                "PRAGMA foreign_key_list(agent_executions)"
            )
            triggers = await conn.exec_driver_sql(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'trigger' AND "
                "(name LIKE '%execution%' "
                "OR name LIKE 'file_reservations_origin_guard_%' "
                "OR name LIKE 'file_reservations_archive_%')"
            )

        assert column_names == {
            "id",
            "project_id",
            "agent_id",
            "parent_execution_id",
            "external_id",
            "client_name",
            "execution_token_hash",
            "lifecycle_protocol_version",
            "turn_id",
            "agent_type",
            "model",
            "permission_mode",
            "kind",
            "status",
            "task_description",
            "cwd",
            "repo_root",
            "git_common_dir",
            "worktree_path",
            "branch",
            "head_sha",
            "started_ts",
            "last_active_ts",
            "ended_ts",
        }
        assert "execution_id" in reservation_column_names
        assert "origin" in reservation_column_names
        assert "archive_revision" in reservation_column_names
        assert "archive_synced_revision" in reservation_column_names
        token_column = next(
            row for row in column_rows if str(row[1]) == "execution_token_hash"
        )
        assert str(token_column[2]).upper() == "VARCHAR(64)"
        assert int(token_column[3]) == 1
        assert token_column[4] is None
        assert any(
            str(row[2]) == "agent_executions"
            and str(row[3]) == "execution_id"
            and str(row[4]) == "id"
            for row in foreign_keys.fetchall()
        )
        execution_fk_rows = execution_foreign_keys.fetchall()
        assert any(
            str(row[2]) == "agent_executions"
            and str(row[3]) == "parent_execution_id"
            and str(row[4]) == "id"
            for row in execution_fk_rows
        )
        assert any(
            str(row[2]) == "projects" and str(row[3]) == "project_id"
            for row in execution_fk_rows
        )
        assert any(
            str(row[2]) == "agents" and str(row[3]) == "agent_id"
            for row in execution_fk_rows
        )
        assert {str(row[0]) for row in triggers.fetchall()} >= {
            "agent_executions_project_agent_guard_bi",
            "agent_executions_project_agent_guard_bu",
            "agent_executions_parent_guard_bi",
            "agent_executions_parent_guard_bu",
            "agent_executions_terminal_guard_bu",
            "agent_executions_build_slot_projection_ai",
            "agent_executions_build_slot_projection_au",
            "build_slot_artifact_paths_active_execution_guard_bi",
            "agent_executions_capability_guard_bi",
            "agent_executions_capability_guard_bu",
            "agents_execution_project_guard_bu",
            "file_reservations_execution_guard_bi",
            "file_reservations_execution_guard_bu",
            "file_reservations_origin_guard_bi",
            "file_reservations_origin_guard_bu",
            "file_reservations_archive_version_guard_bi",
            "file_reservations_archive_version_guard_bu",
            "file_reservations_archive_revision_au",
        }

    @pytest.mark.asyncio
    async def test_agent_execution_schema_validator_rejects_definition_drift(
        self,
        isolated_env,
    ):
        """A canonical object name cannot hide a weakened trigger body."""
        await ensure_schema()
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "DROP TRIGGER agent_executions_capability_guard_bi"
            )
            await conn.exec_driver_sql(
                """
                CREATE TRIGGER agent_executions_capability_guard_bi
                BEFORE INSERT ON agent_executions
                BEGIN
                    SELECT 1;
                END
                """
            )
            with pytest.raises(RuntimeError, match="trigger definition drift"):
                await conn.run_sync(_validate_agent_execution_schema)

    @pytest.mark.asyncio
    async def test_agent_execution_checks_hierarchy_and_foreign_keys(self, isolated_env):
        """Execution rows enforce canonical shape, hierarchy, and Agent ownership."""
        await ensure_schema()
        engine = get_engine()
        project_one = Project(slug="execution-one", human_key=pkey("execution/one"))
        project_two = Project(slug="execution-two", human_key=pkey("execution/two"))
        async with get_session() as session:
            session.add_all([project_one, project_two])
            await session.flush()
            agent_one = Agent(
                project_id=int(project_one.id),
                name="ExecutionOne",
                program="test",
                model="test",
            )
            agent_sibling = Agent(
                project_id=int(project_one.id),
                name="ExecutionSibling",
                program="test",
                model="test",
            )
            agent_two = Agent(
                project_id=int(project_two.id),
                name="ExecutionTwo",
                program="test",
                model="test",
            )
            session.add_all([agent_one, agent_sibling, agent_two])
            await session.commit()

        parent_id = str(uuid.uuid4())
        child_id = str(uuid.uuid4())
        independent_id = str(uuid.uuid4())
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "INSERT INTO agent_executions "
                "(id, project_id, agent_id, external_id, client_name, execution_token_hash, "
                "lifecycle_protocol_version, kind, status, "
                "task_description, started_ts, last_active_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, 'session', 'active', '', ?, ?)",
                (
                    parent_id,
                    int(project_one.id),
                    int(agent_one.id),
                    "root-turn",
                    "codex",
                    "1" * 64,
                    "2026-08-13 10:00:00",
                    "2026-08-13 10:00:01",
                ),
            )
            await conn.exec_driver_sql(
                "INSERT INTO agent_executions "
                "(id, project_id, agent_id, parent_execution_id, external_id, "
                "client_name, execution_token_hash, lifecycle_protocol_version, "
                "turn_id, agent_type, model, permission_mode, kind, "
                "status, task_description, head_sha, started_ts, last_active_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 'subagent', 'active', '', ?, ?, ?)",
                (
                    child_id,
                    int(project_one.id),
                    int(agent_one.id),
                    parent_id,
                    "child-turn",
                    "codex",
                    "2" * 64,
                    "turn-123",
                    "worker",
                    "gpt-5",
                    "workspace-write",
                    "a" * 40,
                    "2026-08-13 10:00:02",
                    "2026-08-13 10:00:03",
                ),
            )
            await conn.exec_driver_sql(
                "INSERT INTO agent_executions "
                "(id, project_id, agent_id, external_id, client_name, "
                "execution_token_hash, lifecycle_protocol_version, kind, status, "
                "task_description, started_ts, last_active_ts) "
                "VALUES (?, ?, ?, 'independent-turn', 'codex', ?, 1, "
                "'session', 'active', '', ?, ?)",
                (
                    independent_id,
                    int(project_one.id),
                    int(agent_one.id),
                    "7" * 64,
                    "2026-08-13 10:00:00",
                    "2026-08-13 10:00:01",
                ),
            )

        invalid_rows = [
            (
                "INSERT INTO agent_executions "
                "(id, project_id, agent_id, external_id, client_name, "
                "execution_token_hash, kind, status, "
                "task_description, started_ts, last_active_ts) "
                "VALUES (?, ?, ?, 'bad-uuid', 'codex', lower(hex(randomblob(32))), "
                "'session', 'active', '', ?, ?)",
                ("NOT-A-UUID", int(project_one.id), int(agent_one.id), "2026-08-13 10:00:00", "2026-08-13 10:00:01"),
            ),
            (
                "INSERT INTO agent_executions "
                "(id, project_id, agent_id, external_id, client_name, "
                "execution_token_hash, kind, status, "
                "task_description, started_ts, last_active_ts) "
                "VALUES (?, ?, ?, 'wrong-project', 'codex', lower(hex(randomblob(32))), "
                "'session', 'active', '', ?, ?)",
                (str(uuid.uuid4()), int(project_two.id), int(agent_one.id), "2026-08-13 10:00:00", "2026-08-13 10:00:01"),
            ),
            (
                "INSERT INTO agent_executions "
                "(id, project_id, agent_id, external_id, client_name, "
                "execution_token_hash, kind, status, "
                "task_description, started_ts, last_active_ts) "
                "VALUES (?, ?, ?, 'missing-parent', 'codex', lower(hex(randomblob(32))), "
                "'subagent', 'active', '', ?, ?)",
                (str(uuid.uuid4()), int(project_one.id), int(agent_one.id), "2026-08-13 10:00:00", "2026-08-13 10:00:01"),
            ),
            (
                "INSERT INTO agent_executions "
                "(id, project_id, agent_id, parent_execution_id, external_id, "
                "client_name, execution_token_hash, kind, status, task_description, "
                "started_ts, last_active_ts) "
                "VALUES (?, ?, ?, ?, 'foreign-parent', 'codex', "
                "lower(hex(randomblob(32))), 'subagent', 'active', '', ?, ?)",
                (str(uuid.uuid4()), int(project_one.id), int(agent_sibling.id), parent_id, "2026-08-13 10:00:00", "2026-08-13 10:00:01"),
            ),
            (
                "INSERT INTO agent_executions "
                "(id, project_id, agent_id, external_id, client_name, "
                "execution_token_hash, kind, status, "
                "task_description, head_sha, started_ts, last_active_ts) "
                "VALUES (?, ?, ?, 'bad-sha', 'codex', lower(hex(randomblob(32))), "
                "'session', 'active', '', ?, ?, ?)",
                (str(uuid.uuid4()), int(project_one.id), int(agent_one.id), "A" * 40, "2026-08-13 10:00:00", "2026-08-13 10:00:01"),
            ),
            (
                "INSERT INTO agent_executions "
                "(id, project_id, agent_id, external_id, client_name, "
                "execution_token_hash, kind, status, "
                "task_description, started_ts, last_active_ts) "
                "VALUES (?, ?, ?, 'bad-time', 'codex', lower(hex(randomblob(32))), "
                "'session', 'active', '', ?, ?)",
                (str(uuid.uuid4()), int(project_one.id), int(agent_one.id), "2026-08-13 10:00:02", "2026-08-13 10:00:01"),
            ),
            (
                "INSERT INTO agent_executions "
                "(id, project_id, agent_id, external_id, client_name, "
                "execution_token_hash, kind, status, "
                "task_description, started_ts, last_active_ts, ended_ts) "
                "VALUES (?, ?, ?, 'bad-end', 'codex', lower(hex(randomblob(32))), "
                "'session', 'completed', '', ?, ?, NULL)",
                (str(uuid.uuid4()), int(project_one.id), int(agent_one.id), "2026-08-13 10:00:00", "2026-08-13 10:00:01"),
            ),
            (
                "INSERT INTO agent_executions "
                "(id, project_id, agent_id, external_id, client_name, "
                "execution_token_hash, kind, status, "
                "task_description, started_ts, last_active_ts) "
                "VALUES (?, ?, ?, 'missing-agent', 'codex', lower(hex(randomblob(32))), "
                "'session', 'active', '', ?, ?)",
                (str(uuid.uuid4()), int(project_one.id), 999999, "2026-08-13 10:00:00", "2026-08-13 10:00:01"),
            ),
            (
                "INSERT INTO agent_executions "
                "(id, project_id, agent_id, external_id, client_name, "
                "execution_token_hash, kind, status, "
                "task_description, cwd, started_ts, last_active_ts) "
                "VALUES (?, ?, ?, 'blank-cwd', 'codex', lower(hex(randomblob(32))), "
                "'session', 'active', '', '   ', ?, ?)",
                (str(uuid.uuid4()), int(project_one.id), int(agent_one.id), "2026-08-13 10:00:00", "2026-08-13 10:00:01"),
            ),
            (
                "INSERT INTO agent_executions "
                "(id, project_id, agent_id, external_id, client_name, "
                "execution_token_hash, kind, status, "
                "task_description, started_ts, last_active_ts) "
                "VALUES (?, ?, ?, 'long-task', 'codex', lower(hex(randomblob(32))), "
                "'session', 'active', ?, ?, ?)",
                (str(uuid.uuid4()), int(project_one.id), int(agent_one.id), "x" * 2049, "2026-08-13 10:00:00", "2026-08-13 10:00:01"),
            ),
            (
                "INSERT INTO agent_executions "
                "(id, project_id, agent_id, external_id, client_name, "
                "execution_token_hash, kind, status, "
                "task_description, started_ts, last_active_ts, ended_ts) "
                "VALUES (?, ?, ?, 'end-before-heartbeat', 'codex', "
                "lower(hex(randomblob(32))), 'session', "
                "'completed', '', ?, ?, ?)",
                (str(uuid.uuid4()), int(project_one.id), int(agent_one.id), "2026-08-13 10:00:00", "2026-08-13 10:00:02", "2026-08-13 10:00:01"),
            ),
            (
                "INSERT INTO agent_executions "
                "(id, project_id, agent_id, external_id, client_name, "
                "execution_token_hash, lifecycle_protocol_version, kind, status, "
                "task_description, started_ts, last_active_ts) "
                "VALUES (?, ?, ?, 'null-capability', 'codex', NULL, 1, "
                "'session', 'active', '', ?, ?)",
                (
                    str(uuid.uuid4()),
                    int(project_one.id),
                    int(agent_one.id),
                    "2026-08-13 10:00:00",
                    "2026-08-13 10:00:01",
                ),
            ),
            (
                "INSERT INTO agent_executions "
                "(id, project_id, agent_id, external_id, client_name, "
                "execution_token_hash, lifecycle_protocol_version, kind, status, "
                "task_description, started_ts, last_active_ts) "
                "VALUES (?, ?, ?, 'uppercase-capability', 'codex', ?, 1, "
                "'session', 'active', '', ?, ?)",
                (
                    str(uuid.uuid4()),
                    int(project_one.id),
                    int(agent_one.id),
                    "A" * 64,
                    "2026-08-13 10:00:00",
                    "2026-08-13 10:00:01",
                ),
            ),
            (
                "INSERT INTO agent_executions "
                "(id, project_id, agent_id, external_id, client_name, "
                "execution_token_hash, lifecycle_protocol_version, kind, status, "
                "task_description, started_ts, last_active_ts) "
                "VALUES (?, ?, ?, 'nonhex-capability', 'codex', ?, 1, "
                "'session', 'active', '', ?, ?)",
                (
                    str(uuid.uuid4()),
                    int(project_one.id),
                    int(agent_one.id),
                    "g" * 64,
                    "2026-08-13 10:00:00",
                    "2026-08-13 10:00:01",
                ),
            ),
        ]
        for statement, parameters in invalid_rows:
            with pytest.raises(IntegrityError):
                async with engine.begin() as conn:
                    await conn.exec_driver_sql(statement, parameters)

        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "UPDATE agents SET retired_at = ? WHERE id = ?",
                ("2026-08-13 10:00:04", int(agent_two.id)),
            )
        with pytest.raises(IntegrityError, match="retired"):
            async with engine.begin() as conn:
                await conn.exec_driver_sql(
                    "INSERT INTO agent_executions "
                    "(id, project_id, agent_id, external_id, client_name, "
                    "execution_token_hash, lifecycle_protocol_version, kind, status, "
                    "task_description, started_ts, last_active_ts) "
                    "VALUES (?, ?, ?, 'retired-owner', 'codex', ?, 1, "
                    "'session', 'active', '', ?, ?)",
                    (
                        str(uuid.uuid4()),
                        int(project_two.id),
                        int(agent_two.id),
                        "6" * 64,
                        "2026-08-13 10:00:00",
                        "2026-08-13 10:00:01",
                    ),
                )

        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.exec_driver_sql(
                    "INSERT INTO agent_executions "
                    "(id, project_id, agent_id, external_id, client_name, "
                    "execution_token_hash, kind, status, task_description, "
                    "started_ts, last_active_ts) "
                    "VALUES (?, ?, ?, 'root-turn', 'codex', lower(hex(randomblob(32))), "
                    "'session', 'active', '', ?, ?)",
                    (str(uuid.uuid4()), int(project_one.id), int(agent_one.id), "2026-08-13 10:00:00", "2026-08-13 10:00:01"),
                )
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.exec_driver_sql(
                    "INSERT INTO agent_executions "
                    "(id, project_id, agent_id, parent_execution_id, external_id, "
                    "client_name, execution_token_hash, kind, status, task_description, "
                    "started_ts, last_active_ts) "
                    "VALUES (?, ?, ?, ?, 'child-turn', 'codex', "
                    "lower(hex(randomblob(32))), 'subagent', 'active', '', ?, ?)",
                    (str(uuid.uuid4()), int(project_one.id), int(agent_one.id), parent_id, "2026-08-13 10:00:02", "2026-08-13 10:00:03"),
                )
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "INSERT INTO agent_executions "
                "(id, project_id, agent_id, external_id, client_name, execution_token_hash, "
                "lifecycle_protocol_version, kind, status, "
                "task_description, started_ts, last_active_ts) "
                "VALUES (?, ?, ?, 'root-turn', 'claude', ?, 1, 'session', 'active', '', ?, ?)",
                (
                    str(uuid.uuid4()),
                    int(project_one.id),
                    int(agent_one.id),
                    "3" * 64,
                    "2026-08-13 10:00:00",
                    "2026-08-13 10:00:01",
                ),
            )

        with pytest.raises(IntegrityError, match="owner is immutable"):
            async with engine.begin() as conn:
                await conn.exec_driver_sql(
                    "UPDATE agent_executions SET agent_id = ? WHERE id = ?",
                    (int(agent_sibling.id), independent_id),
                )

        with pytest.raises(IntegrityError, match="project-bound executions"):
            async with engine.begin() as conn:
                await conn.exec_driver_sql(
                    "UPDATE agents SET project_id = ? WHERE id = ?",
                    (int(project_two.id), int(agent_one.id)),
                )

        with pytest.raises(IntegrityError, match="active children"):
            async with engine.begin() as conn:
                await conn.exec_driver_sql(
                    "UPDATE agent_executions "
                    "SET status = 'completed', ended_ts = ? WHERE id = ?",
                    ("2026-08-13 10:00:04", parent_id),
                )
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            async with engine.begin() as conn:
                await conn.exec_driver_sql(
                    "DELETE FROM agent_executions WHERE id = ?",
                    (parent_id,),
                )

    @pytest.mark.asyncio
    async def test_execution_reservation_binding_and_terminal_guard(self, isolated_env):
        """Only active matching executions own claims, which must release before exit."""
        await ensure_schema()
        engine = get_engine()
        async with get_session() as session:
            project = Project(slug="execution-reservation", human_key=pkey("execution/reservation"))
            session.add(project)
            await session.flush()
            owner = Agent(project_id=int(project.id), name="Owner", program="test", model="test")
            other = Agent(project_id=int(project.id), name="Other", program="test", model="test")
            session.add_all([owner, other])
            await session.commit()

        execution_id = str(uuid.uuid4())
        completed_id = str(uuid.uuid4())
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "INSERT INTO agent_executions "
                "(id, project_id, agent_id, external_id, client_name, execution_token_hash, "
                "lifecycle_protocol_version, kind, status, "
                "task_description, started_ts, last_active_ts) "
                "VALUES (?, ?, ?, 'active-owner', 'codex', ?, 1, "
                "'session', 'active', '', ?, ?)",
                (
                    execution_id,
                    int(project.id),
                    int(owner.id),
                    "4" * 64,
                    "2026-08-13 10:00:00",
                    "2026-08-13 10:00:01",
                ),
            )
            await conn.exec_driver_sql(
                "INSERT INTO agent_executions "
                "(id, project_id, agent_id, external_id, client_name, execution_token_hash, "
                "lifecycle_protocol_version, kind, status, "
                "task_description, started_ts, last_active_ts, ended_ts) "
                "VALUES (?, ?, ?, 'completed-owner', 'codex', ?, 1, "
                "'session', 'completed', '', ?, ?, ?)",
                (
                    completed_id,
                    int(project.id),
                    int(owner.id),
                    "5" * 64,
                    "2026-08-13 09:00:00",
                    "2026-08-13 09:00:01",
                    "2026-08-13 09:00:02",
                ),
            )
            await conn.exec_driver_sql(
                "INSERT INTO file_reservations "
                "(project_id, agent_id, execution_id, origin, path_pattern, exclusive, reason, "
                "created_ts, expires_ts) "
                "VALUES (?, ?, ?, 'auto', 'src/**', 1, '', CURRENT_TIMESTAMP, "
                "datetime('now', '+1 hour'))",
                (int(project.id), int(owner.id), execution_id),
            )
            await conn.exec_driver_sql(
                "INSERT INTO file_reservations "
                "(project_id, agent_id, execution_id, origin, path_pattern, exclusive, reason, "
                "created_ts, expires_ts) "
                "VALUES (?, ?, ?, 'explicit', 'explicit/**', 1, '', CURRENT_TIMESTAMP, "
                "datetime('now', '+1 hour'))",
                (int(project.id), int(owner.id), execution_id),
            )

        terminal_mutations = [
            (
                "UPDATE agent_executions "
                "SET status = 'active', ended_ts = NULL WHERE id = ?",
                (completed_id,),
            ),
            (
                "UPDATE agent_executions SET status = 'failed' WHERE id = ?",
                (completed_id,),
            ),
            (
                "UPDATE agent_executions SET task_description = 'changed' WHERE id = ?",
                (completed_id,),
            ),
            (
                "UPDATE agent_executions "
                "SET lifecycle_protocol_version = 2 WHERE id = ?",
                (completed_id,),
            ),
        ]
        for statement, parameters in terminal_mutations:
            with pytest.raises(IntegrityError, match="terminal agent execution"):
                async with engine.begin() as conn:
                    await conn.exec_driver_sql(statement, parameters)

        invalid_claims = [
            (int(project.id), int(other.id), execution_id),
            (int(project.id), None, execution_id),
            (int(project.id), int(owner.id), completed_id),
            (int(project.id), int(owner.id), str(uuid.uuid4())),
        ]
        for project_id, agent_id, candidate_execution_id in invalid_claims:
            with pytest.raises(IntegrityError):
                async with engine.begin() as conn:
                    await conn.exec_driver_sql(
                        "INSERT INTO file_reservations "
                        "(project_id, agent_id, execution_id, path_pattern, exclusive, "
                        "reason, created_ts, expires_ts) "
                        "VALUES (?, ?, ?, 'bad/**', 1, '', CURRENT_TIMESTAMP, "
                        "datetime('now', '+1 hour'))",
                        (project_id, agent_id, candidate_execution_id),
                    )

        async with engine.connect() as conn:
            explicit_row = (
                await conn.exec_driver_sql(
                    "SELECT id, archive_revision, archive_synced_revision "
                    "FROM file_reservations "
                    "WHERE execution_id = ? AND origin = 'explicit'",
                    (execution_id,),
                )
            ).one()
            explicit_reservation_id = int(explicit_row[0])
            assert (int(explicit_row[1]), int(explicit_row[2])) == (1, 0)

        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "UPDATE file_reservations SET reason = 'versioned' WHERE id = ?",
                (explicit_reservation_id,),
            )
            versioned = (
                await conn.exec_driver_sql(
                    "SELECT archive_revision, archive_synced_revision "
                    "FROM file_reservations WHERE id = ?",
                    (explicit_reservation_id,),
                )
            ).one()
            assert (int(versioned[0]), int(versioned[1])) == (2, 0)
            await conn.exec_driver_sql(
                "UPDATE file_reservations SET archive_synced_revision = 2 "
                "WHERE id = ?",
                (explicit_reservation_id,),
            )

        invalid_archive_versions = [
            (
                "UPDATE file_reservations SET archive_synced_revision = 1 "
                "WHERE id = ?",
                "invalid reservation archive version",
            ),
            (
                "UPDATE file_reservations SET archive_revision = 4 WHERE id = ?",
                "invalid reservation archive version",
            ),
            (
                "UPDATE file_reservations "
                "SET reason = 'forged', archive_revision = 3, "
                "archive_synced_revision = 3 WHERE id = ?",
                "archive version is storage-managed",
            ),
        ]
        for statement, message in invalid_archive_versions:
            with pytest.raises(IntegrityError, match=message):
                async with engine.begin() as conn:
                    await conn.exec_driver_sql(
                        statement,
                        (explicit_reservation_id,),
                    )

        immutable_binding_mutations = [
            (
                "UPDATE file_reservations SET execution_id = NULL WHERE id = ?",
                (explicit_reservation_id,),
                "execution binding is immutable",
            ),
            (
                "UPDATE file_reservations SET execution_id = ? WHERE id = ?",
                (str(uuid.uuid4()), explicit_reservation_id),
                "execution binding is immutable",
            ),
            (
                "UPDATE file_reservations SET agent_id = ? WHERE id = ?",
                (int(other.id), explicit_reservation_id),
                "execution owner is immutable",
            ),
            (
                "UPDATE file_reservations SET origin = 'auto' WHERE id = ?",
                (explicit_reservation_id,),
                "origin cannot downgrade",
            ),
        ]
        for statement, parameters, message in immutable_binding_mutations:
            with pytest.raises(IntegrityError, match=message):
                async with engine.begin() as conn:
                    await conn.exec_driver_sql(statement, parameters)

        with pytest.raises(IntegrityError, match="active reservations"):
            async with engine.begin() as conn:
                await conn.exec_driver_sql(
                    "UPDATE agent_executions "
                    "SET status = 'completed', ended_ts = CURRENT_TIMESTAMP WHERE id = ?",
                    (execution_id,),
                )

        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "UPDATE file_reservations SET released_ts = CURRENT_TIMESTAMP "
                "WHERE execution_id = ? AND origin = 'auto'",
                (execution_id,),
            )
            await conn.exec_driver_sql(
                "UPDATE agent_executions "
                "SET status = 'completed', ended_ts = CURRENT_TIMESTAMP WHERE id = ?",
                (execution_id,),
            )

        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "UPDATE file_reservations SET released_ts = CURRENT_TIMESTAMP "
                "WHERE execution_id = ? AND origin = 'explicit'",
                (execution_id,),
            )

        with pytest.raises(IntegrityError, match="execution binding mismatch"):
            async with engine.begin() as conn:
                await conn.exec_driver_sql(
                    "UPDATE file_reservations "
                    "SET released_ts = NULL, expires_ts = datetime('now', '+1 hour') "
                    "WHERE execution_id = ?",
                    (execution_id,),
                )

    @pytest.mark.asyncio
    async def test_agent_execution_intermediate_schema_rebuilds_atomically(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        """Nullable intermediate capabilities rebuild to canonical DDL without loss."""
        db_path = tmp_path / "intermediate-executions.sqlite3"
        root_id = "11111111-1111-4111-8111-111111111111"
        child_id = "22222222-2222-4222-8222-222222222222"
        with closing(sqlite3.connect(db_path)) as legacy:
            legacy.executescript(
                f"""
                PRAGMA foreign_keys=ON;
                CREATE TABLE projects (
                    id INTEGER PRIMARY KEY,
                    slug VARCHAR(255) NOT NULL,
                    human_key VARCHAR(255) NOT NULL
                );
                CREATE TABLE agents (
                    id INTEGER PRIMARY KEY,
                    project_id INTEGER NOT NULL REFERENCES projects(id),
                    name VARCHAR(128) NOT NULL,
                    program VARCHAR(128) NOT NULL,
                    model VARCHAR(128) NOT NULL,
                    task_description VARCHAR(2048) NOT NULL DEFAULT '',
                    inception_ts DATETIME NOT NULL,
                    last_active_ts DATETIME NOT NULL,
                    attachments_policy VARCHAR(16) NOT NULL DEFAULT 'auto',
                    contact_policy VARCHAR(16) NOT NULL DEFAULT 'auto'
                );
                CREATE TABLE agent_executions (
                    id VARCHAR(36) PRIMARY KEY,
                    project_id INTEGER NOT NULL REFERENCES projects(id),
                    agent_id INTEGER NOT NULL REFERENCES agents(id),
                    parent_execution_id VARCHAR(36) REFERENCES agent_executions(id),
                    external_id VARCHAR(255) NOT NULL,
                    client_name VARCHAR(128) NOT NULL,
                    execution_token_hash VARCHAR(64),
                    lifecycle_protocol_version INTEGER NOT NULL DEFAULT 0,
                    turn_id VARCHAR(255),
                    agent_type VARCHAR(128),
                    model VARCHAR(128),
                    permission_mode VARCHAR(64),
                    kind VARCHAR(16) NOT NULL,
                    status VARCHAR(16) NOT NULL,
                    task_description VARCHAR(2048) NOT NULL DEFAULT '',
                    cwd VARCHAR(2048),
                    repo_root VARCHAR(2048),
                    git_common_dir VARCHAR(2048),
                    worktree_path VARCHAR(2048),
                    branch VARCHAR(512),
                    head_sha VARCHAR(40),
                    started_ts DATETIME NOT NULL,
                    last_active_ts DATETIME NOT NULL,
                    ended_ts DATETIME
                );
                CREATE TABLE file_reservations (
                    id INTEGER PRIMARY KEY,
                    project_id INTEGER NOT NULL REFERENCES projects(id),
                    agent_id INTEGER REFERENCES agents(id),
                    execution_id VARCHAR(36) REFERENCES agent_executions(id),
                    origin VARCHAR(16) NOT NULL DEFAULT 'explicit',
                    path_pattern VARCHAR(512) NOT NULL,
                    exclusive BOOLEAN NOT NULL,
                    reason VARCHAR(512) NOT NULL DEFAULT '',
                    created_ts DATETIME NOT NULL,
                    expires_ts DATETIME NOT NULL,
                    released_ts DATETIME
                );
                INSERT INTO projects (id, slug, human_key)
                VALUES (1, 'intermediate-execution', '/intermediate/execution');
                INSERT INTO agents (
                    id, project_id, name, program, model,
                    inception_ts, last_active_ts
                ) VALUES (
                    1, 1, 'IntermediateAgent', 'test', 'test',
                    '2026-08-13 10:00:00', '2026-08-13 10:00:00'
                );
                INSERT INTO agent_executions (
                    id, project_id, agent_id, external_id, client_name,
                    execution_token_hash, lifecycle_protocol_version, kind, status,
                    task_description, started_ts, last_active_ts
                ) VALUES (
                    '{root_id}', 1, 1, 'root', 'codex', NULL, 1,
                    'session', 'active', '',
                    '2026-08-13 10:00:00', '2026-08-13 10:00:01'
                );
                INSERT INTO agent_executions (
                    id, project_id, agent_id, parent_execution_id, external_id,
                    client_name, execution_token_hash, lifecycle_protocol_version,
                    kind, status, task_description, started_ts, last_active_ts
                ) VALUES (
                    '{child_id}', 1, 1, '{root_id}', 'child', 'codex', NULL, 1,
                    'subagent', 'active', '',
                    '2026-08-13 10:00:02', '2026-08-13 10:00:03'
                );
                INSERT INTO file_reservations (
                    id, project_id, agent_id, execution_id, origin, path_pattern,
                    exclusive, created_ts, expires_ts
                ) VALUES (
                    1, 1, 1, '{root_id}', 'explicit', 'legacy/**', 1,
                    '2026-08-13 10:00:00', '2026-08-14 10:00:00'
                );
                CREATE INDEX agent_executions_custom_status_idx
                ON agent_executions(status, external_id);
                CREATE TRIGGER agent_executions_custom_noop
                AFTER UPDATE OF task_description ON agent_executions
                BEGIN
                    SELECT new.id;
                END;
                """
            )

        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
        monkeypatch.setenv("APP_ENVIRONMENT", "test")
        reset_database_state()
        await ensure_schema()
        reset_database_state()
        await ensure_schema()

        engine = get_engine()
        async with engine.connect() as conn:
            columns = (
                await conn.exec_driver_sql("PRAGMA table_info(agent_executions)")
            ).fetchall()
            execution_rows = (
                await conn.exec_driver_sql(
                    "SELECT id, parent_execution_id, execution_token_hash "
                    "FROM agent_executions ORDER BY started_ts"
                )
            ).fetchall()
            reservation_row = (
                await conn.exec_driver_sql(
                    "SELECT execution_id, origin, path_pattern FROM file_reservations"
                )
            ).fetchone()
            custom_objects = {
                (str(row[0]), str(row[1]))
                for row in (
                    await conn.exec_driver_sql(
                        "SELECT type, name FROM sqlite_master "
                        "WHERE name IN ('agent_executions_custom_status_idx', "
                        "'agent_executions_custom_noop')"
                    )
                ).fetchall()
            }
            foreign_key_violations = (
                await conn.exec_driver_sql("PRAGMA foreign_key_check")
            ).fetchall()

        token_column = next(
            row for row in columns if str(row[1]) == "execution_token_hash"
        )
        assert int(token_column[3]) == 1
        assert [row[:2] for row in execution_rows] == [
            (root_id, None),
            (child_id, root_id),
        ]
        assert all(
            len(str(row[2])) == 64
            and str(row[2]) == str(row[2]).lower()
            and set(str(row[2])) <= set("0123456789abcdef")
            for row in execution_rows
        )
        assert len({str(row[2]) for row in execution_rows}) == 2
        assert reservation_row == (root_id, "explicit", "legacy/**")
        assert custom_objects == {
            ("index", "agent_executions_custom_status_idx"),
            ("trigger", "agent_executions_custom_noop"),
        }
        assert foreign_key_violations == []

    @pytest.mark.asyncio
    async def test_agent_execution_legacy_migration_is_idempotent(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        """A legacy reservation table gains execution ownership without data loss."""
        db_path = tmp_path / "legacy-executions.sqlite3"
        with closing(sqlite3.connect(db_path)) as legacy:
            legacy.executescript(
                """
                PRAGMA foreign_keys=ON;
                CREATE TABLE projects (
                    id INTEGER PRIMARY KEY,
                    slug VARCHAR(255) NOT NULL,
                    human_key VARCHAR(255) NOT NULL
                );
                CREATE TABLE agents (
                    id INTEGER PRIMARY KEY,
                    project_id INTEGER NOT NULL REFERENCES projects(id),
                    name VARCHAR(128) NOT NULL,
                    program VARCHAR(128) NOT NULL,
                    model VARCHAR(128) NOT NULL,
                    task_description VARCHAR(2048) NOT NULL DEFAULT '',
                    inception_ts DATETIME NOT NULL,
                    last_active_ts DATETIME NOT NULL,
                    attachments_policy VARCHAR(16) NOT NULL DEFAULT 'auto',
                    contact_policy VARCHAR(16) NOT NULL DEFAULT 'auto'
                );
                CREATE TABLE file_reservations (
                    id INTEGER PRIMARY KEY,
                    project_id INTEGER NOT NULL REFERENCES projects(id),
                    agent_id INTEGER REFERENCES agents(id),
                    path_pattern VARCHAR(512) NOT NULL,
                    exclusive BOOLEAN NOT NULL,
                    reason VARCHAR(512) NOT NULL DEFAULT '',
                    created_ts DATETIME NOT NULL,
                    expires_ts DATETIME NOT NULL,
                    released_ts DATETIME
                );
                INSERT INTO projects (id, slug, human_key)
                VALUES (1, 'legacy-execution', '/legacy/execution');
                INSERT INTO agents (
                    id, project_id, name, program, model, inception_ts, last_active_ts
                ) VALUES (
                    1, 1, 'LegacyAgent', 'test', 'test',
                    '2026-08-13 10:00:00', '2026-08-13 10:00:00'
                );
                INSERT INTO file_reservations (
                    id, project_id, agent_id, path_pattern, exclusive,
                    created_ts, expires_ts
                ) VALUES (
                    1, 1, 1, 'legacy/**', 1,
                    '2026-08-13 10:00:00', '2026-08-14 10:00:00'
                );
                """
            )

        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
        monkeypatch.setenv("APP_ENVIRONMENT", "test")
        reset_database_state()
        await ensure_schema()
        reset_database_state()
        await ensure_schema()

        engine = get_engine()
        async with engine.connect() as conn:
            columns = await conn.exec_driver_sql("PRAGMA table_info(file_reservations)")
            agent_columns = await conn.exec_driver_sql("PRAGMA table_info(agents)")
            rows = await conn.exec_driver_sql(
                "SELECT id, path_pattern, execution_id FROM file_reservations"
            )
            agent_state = await conn.exec_driver_sql(
                "SELECT id, provisioning_state FROM agents"
            )
            execution_table = await conn.exec_driver_sql(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = 'agent_executions'"
            )
            execution_index = await conn.exec_driver_sql(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'index' AND name = 'idx_file_reservations_execution'"
            )
            provisioning_triggers = await conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND (name LIKE 'agents_provisioning_state_guard_%' "
                "OR name LIKE '%_active_agent_guard_bi' "
                "OR name = 'agent_links_active_agents_guard_bi')"
            )

        assert [str(row[1]) for row in columns.fetchall()].count("execution_id") == 1
        provisioning_column = next(
            row
            for row in agent_columns.fetchall()
            if str(row[1]) == "provisioning_state"
        )
        assert int(provisioning_column[3]) == 1
        assert str(provisioning_column[4]).strip("'") == "active"
        assert rows.fetchall() == [(1, "legacy/**", None)]
        assert agent_state.fetchall() == [(1, "active")]
        assert execution_table.scalar_one() == 1
        assert execution_index.scalar_one() == 1
        assert {str(row[0]) for row in provisioning_triggers.fetchall()} >= {
            "agents_provisioning_state_guard_bi",
            "agents_provisioning_state_guard_bu",
            "agent_executions_active_agent_guard_bi",
            "file_reservations_active_agent_guard_bi",
            "agent_links_active_agents_guard_bi",
            "message_recipients_active_agent_guard_bi",
            "message_delivery_recipients_active_agent_guard_bi",
        }

        with pytest.raises(IntegrityError, match="invalid reservation origin"):
            async with engine.begin() as conn:
                await conn.exec_driver_sql(
                    "INSERT INTO file_reservations "
                    "(id, project_id, agent_id, origin, path_pattern, exclusive, "
                    "reason, created_ts, expires_ts) "
                    "VALUES (2, 1, 1, 'bogus', 'invalid/**', 1, '', "
                    "'2026-08-13 10:00:00', '2026-08-14 10:00:00')"
                )

    @pytest.mark.asyncio
    async def test_released_message_delivery_schema_upgrades_nonempty_ledger(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        """Released immutable rows and recipients survive the canonical rebuild."""
        db_path = tmp_path / "released-deliveries.sqlite3"
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
        monkeypatch.setenv("APP_ENVIRONMENT", "test")
        reset_database_state()
        await ensure_schema()

        project_generation = "1" * 64
        agent_generation = "2" * 64
        ui_generation = "3" * 64
        async with get_session() as session:
            project = Project(
                id=201,
                slug="released-ledger",
                human_key=pkey("released/ledger"),
                project_generation=project_generation,
            )
            sender = Agent(
                id=301,
                project_id=201,
                name="ReleasedSender",
                agent_generation=agent_generation,
                program="test",
                model="test",
            )
            recipient = Agent(
                id=302,
                project_id=201,
                name="ReleasedRecipient",
                agent_generation="4" * 64,
                program="test",
                model="test",
            )
            ui_user = UiUser(
                id=401,
                username="released-operator",
                password_hash="not-used",
                session_generation=ui_generation,
            )
            session.add(project)
            await session.flush()
            session.add_all([sender, recipient, ui_user])
            await session.commit()

        reset_database_state()
        _install_released_message_delivery_tables(db_path)
        agent_delivery_id = "11111111-1111-4111-8111-111111111111"
        ui_delivery_id = "22222222-2222-4222-8222-222222222222"
        common_values: dict[str, object] = {
            "state": "pending",
            "project_id": 201,
            "project_slug_snapshot": "released-ledger",
            "project_generation_snapshot": project_generation,
            "sender_id": 301,
            "sender_name_snapshot": "ReleasedSender",
            "sender_generation_snapshot": agent_generation,
            "idempotency_scope": "released-scope",
            "request_sha256": "5" * 64,
            "thread_id": None,
            "reply_to_message_id": None,
            "topic": "migration",
            "subject": "Released delivery",
            "body_md": "Immutable body",
            "importance": "normal",
            "ack_required": 0,
            "attachments": '[{"name":"proof.txt"}]',
            "archive_document": "Immutable archive document",
            "archive_document_sha256": "6" * 64,
            "created_ts": "2026-08-13 10:00:00",
            "lease_token": None,
            "lease_fence": 0,
            "lease_expires_ts": None,
            "attempt_count": 0,
            "next_attempt_ts": "2026-08-13 10:00:00",
            "last_attempt_ts": None,
            "last_error": None,
            "archive_commit_sha": None,
            "archive_receipt_path": None,
            "receipt_sha256": None,
            "published_message_id": None,
            "published_ts": None,
            "quarantined_ts": None,
            "quarantine_reason": None,
        }
        with closing(sqlite3.connect(db_path)) as released:
            released.execute(
                "INSERT INTO messages "
                "(id, project_id, sender_id, subject, body_md, importance, "
                "ack_required, created_ts, attachments, delivery_id) "
                "VALUES (501, 201, 301, 'Released delivery', 'Immutable body', "
                "'normal', 0, '2026-08-13 10:00:00', '[]', ?)",
                (agent_delivery_id,),
            )
            _insert_released_delivery(
                released,
                {
                    **common_values,
                    "id": agent_delivery_id,
                    "state": "published",
                    "actor_kind": "agent",
                    "actor_agent_id": 301,
                    "actor_ui_user_id": None,
                    "actor_name_snapshot": "ReleasedSender",
                    "actor_generation_snapshot": agent_generation,
                    "actor_session_epoch_snapshot": None,
                    "idempotency_key": "agent-key",
                    "next_attempt_ts": None,
                    "archive_commit_sha": "a" * 40,
                    "archive_receipt_path": (
                        "projects/released-ledger/message_deliveries/"
                        f"{agent_delivery_id}.md"
                    ),
                    "receipt_sha256": "b" * 64,
                    "published_message_id": 501,
                    "published_ts": "2026-08-13 10:00:01",
                },
            )
            _insert_released_delivery(
                released,
                {
                    **common_values,
                    "id": ui_delivery_id,
                    "actor_kind": "ui_user",
                    "actor_agent_id": None,
                    "actor_ui_user_id": 401,
                    "actor_name_snapshot": "released-operator",
                    "actor_generation_snapshot": ui_generation,
                    "actor_session_epoch_snapshot": 7,
                    "idempotency_key": "ui-key",
                },
            )
            released.execute(
                "INSERT INTO message_delivery_recipients "
                "(delivery_id, position, kind, agent_id, agent_name_snapshot, "
                "agent_generation_snapshot) VALUES (?, 0, 'to', 302, ?, ?)",
                (agent_delivery_id, "ReleasedRecipient", "4" * 64),
            )
            released.commit()

        reset_database_state()
        await ensure_schema()
        reset_database_state()
        await ensure_schema()

        engine = get_engine()
        async with engine.connect() as connection:
            delivery_columns = {
                str(row[1])
                for row in (
                    await connection.exec_driver_sql(
                        "PRAGMA table_info(message_deliveries)"
                    )
                ).fetchall()
            }
            recipient_columns = {
                str(row[1])
                for row in (
                    await connection.exec_driver_sql(
                        "PRAGMA table_info(message_delivery_recipients)"
                    )
                ).fetchall()
            }
            delivery_rows = (
                await connection.exec_driver_sql(
                    "SELECT id, actor_kind, actor_id, "
                    "actor_project_id_snapshot, actor_generation_snapshot, "
                    "actor_epoch_snapshot, sender_project_id_snapshot, "
                    "document_sha256, backoff_seconds, archive_relative_path, "
                    "archive_blob_sha, message_id "
                    "FROM message_deliveries ORDER BY id"
                )
            ).fetchall()
            recipient_row = (
                await connection.exec_driver_sql(
                    "SELECT delivery_id, ordinal, agent_id, project_id_snapshot "
                    "FROM message_delivery_recipients"
                )
            ).fetchone()
            violations = (
                await connection.exec_driver_sql("PRAGMA foreign_key_check")
            ).fetchall()

        assert "actor_id" in delivery_columns
        assert "actor_agent_id" not in delivery_columns
        assert "idempotency_scope" not in delivery_columns
        assert "ordinal" in recipient_columns
        assert "position" not in recipient_columns
        assert delivery_rows == [
            (
                agent_delivery_id,
                "agent",
                301,
                201,
                agent_generation,
                None,
                201,
                "6" * 64,
                0,
                (
                    "projects/released-ledger/message_deliveries/"
                    f"{agent_delivery_id}.md"
                ),
                "b" * 64,
                501,
            ),
            (
                ui_delivery_id,
                "ui_user",
                401,
                201,
                ui_generation,
                7,
                201,
                "6" * 64,
                0,
                None,
                None,
                None,
            ),
        ]
        assert recipient_row == (agent_delivery_id, 0, 302, 201)
        assert violations == []

    @pytest.mark.asyncio
    async def test_released_message_delivery_upgrade_rolls_back_on_invalid_row(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        """A row that cannot satisfy the canonical ledger leaves v1 untouched."""
        db_path = tmp_path / "invalid-released-delivery.sqlite3"
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
        monkeypatch.setenv("APP_ENVIRONMENT", "test")
        reset_database_state()
        await ensure_schema()
        reset_database_state()
        _install_released_message_delivery_tables(db_path)

        with closing(sqlite3.connect(db_path)) as released:
            released.execute("PRAGMA foreign_keys=OFF")
            released.execute("PRAGMA ignore_check_constraints=ON")
            invalid = {
                "id": "33333333-3333-4333-8333-333333333333",
                "state": "pending",
                "project_id": 999,
                "project_slug_snapshot": "missing-project",
                "project_generation_snapshot": "1" * 64,
                "sender_id": 999,
                "sender_name_snapshot": "MissingSender",
                "sender_generation_snapshot": "2" * 64,
                "actor_kind": "agent",
                "actor_agent_id": 999,
                "actor_ui_user_id": None,
                "actor_name_snapshot": "MissingSender",
                "actor_generation_snapshot": "2" * 64,
                "actor_session_epoch_snapshot": None,
                "idempotency_scope": "released-scope",
                "idempotency_key": "invalid-key",
                "request_sha256": "3" * 64,
                "thread_id": None,
                "reply_to_message_id": None,
                "topic": None,
                "subject": "Invalid",
                "body_md": "Cannot map sender project",
                "importance": "normal",
                "ack_required": 0,
                "attachments": "[]",
                "archive_document": "Archive",
                "archive_document_sha256": "4" * 64,
                "created_ts": "2026-08-13 10:00:00",
                "lease_token": None,
                "lease_fence": 0,
                "lease_expires_ts": None,
                "attempt_count": 0,
                "next_attempt_ts": "2026-08-13 10:00:00",
                "last_attempt_ts": None,
                "last_error": None,
                "archive_commit_sha": None,
                "archive_receipt_path": None,
                "receipt_sha256": None,
                "published_message_id": None,
                "published_ts": None,
                "quarantined_ts": None,
                "quarantine_reason": None,
            }
            _insert_released_delivery(released, invalid)
            released.commit()

        reset_database_state()
        with pytest.raises(
            RuntimeError,
            match="did not preserve every immutable row",
        ):
            await ensure_schema()
        reset_database_state()

        with closing(sqlite3.connect(db_path)) as connection:
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(message_deliveries)"
                ).fetchall()
            }
            rows = connection.execute(
                "SELECT id, idempotency_scope FROM message_deliveries"
            ).fetchall()
            temporary_tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE '%schema_v1'"
            ).fetchall()

        assert "actor_agent_id" in columns
        assert "actor_id" not in columns
        assert rows == [(invalid["id"], "released-scope")]
        assert temporary_tables == []

    @pytest.mark.asyncio
    async def test_terminal_delivery_survives_project_delete_but_pending_blocks(
        self,
        isolated_env,
    ):
        """Project deletion is blocked only by live delivery work, not its ledger."""
        await ensure_schema()
        engine = get_engine()
        pending_project = Project(
            slug="pending-delivery-project",
            human_key=pkey("delivery/pending-project"),
        )
        terminal_project = Project(
            slug="terminal-delivery-project",
            human_key=pkey("delivery/terminal-project"),
        )
        async with get_session() as session:
            session.add_all([pending_project, terminal_project])
            await session.flush()
            pending_agent = Agent(
                project_id=int(pending_project.id),
                name="PendingDeliveryAgent",
                program="test",
                model="test",
            )
            terminal_agent = Agent(
                project_id=int(terminal_project.id),
                name="TerminalDeliveryAgent",
                program="test",
                model="test",
            )
            session.add_all([pending_agent, terminal_agent])
            await session.flush()
            pending_delivery = MessageDelivery(
                project_id=int(pending_project.id),
                project_slug_snapshot=pending_project.slug,
                project_generation_snapshot=pending_project.project_generation,
                sender_project_id_snapshot=int(pending_project.id),
                sender_project_slug_snapshot=pending_project.slug,
                sender_project_generation_snapshot=pending_project.project_generation,
                sender_id=int(pending_agent.id),
                sender_name_snapshot=pending_agent.name,
                sender_generation_snapshot=pending_agent.agent_generation,
                actor_kind="agent",
                actor_id=int(pending_agent.id),
                actor_name_snapshot=pending_agent.name,
                actor_project_id_snapshot=int(pending_project.id),
                actor_project_slug_snapshot=pending_project.slug,
                actor_project_generation_snapshot=pending_project.project_generation,
                actor_generation_snapshot=pending_agent.agent_generation,
                idempotency_key="pending-delivery",
                request_sha256="a" * 64,
                subject="Pending",
                body_md="Body",
                archive_document="Pending archive",
                document_sha256="b" * 64,
            )
            terminal_delivery = MessageDelivery(
                project_id=int(terminal_project.id),
                project_slug_snapshot=terminal_project.slug,
                project_generation_snapshot=terminal_project.project_generation,
                sender_project_id_snapshot=int(terminal_project.id),
                sender_project_slug_snapshot=terminal_project.slug,
                sender_project_generation_snapshot=terminal_project.project_generation,
                sender_id=int(terminal_agent.id),
                sender_name_snapshot=terminal_agent.name,
                sender_generation_snapshot=terminal_agent.agent_generation,
                actor_kind="agent",
                actor_id=int(terminal_agent.id),
                actor_name_snapshot=terminal_agent.name,
                actor_project_id_snapshot=int(terminal_project.id),
                actor_project_slug_snapshot=terminal_project.slug,
                actor_project_generation_snapshot=terminal_project.project_generation,
                actor_generation_snapshot=terminal_agent.agent_generation,
                idempotency_key="terminal-delivery",
                request_sha256="c" * 64,
                subject="Terminal",
                body_md="Body",
                archive_document="Terminal archive",
                document_sha256="d" * 64,
            )
            session.add_all([pending_delivery, terminal_delivery])
            await session.commit()

        with pytest.raises(IntegrityError, match="pending message delivery"):
            async with engine.begin() as conn:
                await conn.exec_driver_sql(
                    "DELETE FROM projects WHERE id = ?",
                    (int(pending_project.id),),
                )

        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "UPDATE message_deliveries "
                "SET state = 'quarantined', quarantined_ts = CURRENT_TIMESTAMP, "
                "quarantine_reason = 'test', next_attempt_ts = NULL "
                "WHERE id = ?",
                (terminal_delivery.id,),
            )
            await conn.exec_driver_sql(
                "DELETE FROM agents WHERE id = ?",
                (int(terminal_agent.id),),
            )
            await conn.exec_driver_sql(
                "DELETE FROM projects WHERE id = ?",
                (int(terminal_project.id),),
            )

        async with engine.connect() as conn:
            ledger_row = (
                await conn.exec_driver_sql(
                    "SELECT state, project_id, project_slug_snapshot "
                    "FROM message_deliveries WHERE id = ?",
                    (terminal_delivery.id,),
                )
            ).fetchone()
        assert ledger_row == (
            "quarantined",
            int(terminal_project.id),
            terminal_project.slug,
        )


# ============================================================================
# Concurrent Write Handling Tests (retry_on_db_lock)
# ============================================================================


class TestRetryOnDbLock:
    """Tests for the retry_on_db_lock decorator."""

    @pytest.mark.asyncio
    async def test_retry_on_db_lock_succeeds_without_error(self):
        """Decorator passes through successful function calls."""
        call_count = {"value": 0}

        @retry_on_db_lock(max_retries=3)
        async def success_func() -> str:
            call_count["value"] += 1
            return "success"

        result = await success_func()
        assert result == "success"
        assert call_count["value"] == 1

    @pytest.mark.asyncio
    async def test_retry_on_db_lock_retries_on_lock_error(self):
        """Decorator retries on database lock errors."""
        call_count = {"value": 0}

        @retry_on_db_lock(max_retries=3, base_delay=0.01)
        async def retry_func() -> str:
            call_count["value"] += 1
            if call_count["value"] < 3:
                raise OperationalError("statement", {}, Exception("database is locked"))
            return "success after retries"

        result = await retry_func()
        assert result == "success after retries"
        assert call_count["value"] == 3

    @pytest.mark.asyncio
    async def test_retry_on_db_lock_exhausts_retries(self):
        """Decorator raises after exhausting retries."""
        call_count = {"value": 0}

        @retry_on_db_lock(max_retries=2, base_delay=0.01)
        async def always_fails() -> str:
            call_count["value"] += 1
            raise OperationalError("statement", {}, Exception("database is locked"))

        with pytest.raises(OperationalError):
            await always_fails()

        # Should have tried max_retries + 1 times
        assert call_count["value"] == 3

    @pytest.mark.asyncio
    async def test_retry_on_db_lock_ignores_non_lock_errors(self):
        """Decorator does not retry on non-lock operational errors."""
        call_count = {"value": 0}

        @retry_on_db_lock(max_retries=3, base_delay=0.01)
        async def other_error_func() -> str:
            call_count["value"] += 1
            raise OperationalError("statement", {}, Exception("connection refused"))

        with pytest.raises(OperationalError):
            await other_error_func()

        # Should have only tried once (no retry for non-lock errors)
        assert call_count["value"] == 1

    @pytest.mark.asyncio
    async def test_retry_on_db_lock_detects_busy_error(self):
        """Decorator retries on 'database is busy' errors."""
        call_count = {"value": 0}

        @retry_on_db_lock(max_retries=3, base_delay=0.01)
        async def busy_func() -> str:
            call_count["value"] += 1
            if call_count["value"] < 2:
                raise OperationalError("statement", {}, Exception("database is busy"))
            return "success"

        result = await busy_func()
        assert result == "success"
        assert call_count["value"] == 2


# ============================================================================
# Query Tracking Helper Tests
# ============================================================================


class TestQueryTrackingHelpers:
    """Tests for query tracking utilities and normalization helpers."""

    def test_extract_table_name_variants(self):
        assert _extract_table_name("SELECT * FROM messages") == "messages"
        assert _extract_table_name('select * from "agents"') == "agents"
        assert _extract_table_name("UPDATE projects SET name = 'x'") == "projects"
        assert _extract_table_name("INSERT INTO `message_recipients` (id) VALUES (1)") == "message_recipients"
        assert _extract_table_name("SELECT * FROM main.file_reservations") == "file_reservations"
        assert _extract_table_name('SELECT * FROM "main"."messages"') == "messages"
        assert _extract_table_name("BEGIN") is None

    def test_query_tracker_records_counts_and_slow_queries(self):
        tracker = QueryTracker(slow_query_ms=5.0)
        tracker.record("SELECT * FROM messages", 3.0)
        tracker.record("SELECT * FROM messages", 7.5)

        assert tracker.total == 2
        assert tracker.per_table["messages"] == 2
        assert tracker.slow_queries == [{"table": "messages", "duration_ms": 7.5}]

        payload = tracker.to_dict()
        assert payload["total"] == 2
        assert payload["per_table"]["messages"] == 2
        assert payload["slow_query_ms"] == 5.0
        assert payload["slow_queries"] == [{"table": "messages", "duration_ms": 7.5}]

    def test_track_queries_context_manages_contextvar(self):
        assert get_query_tracker() is None
        with track_queries() as tracker:
            assert get_query_tracker() is tracker
            tracker.record("SELECT * FROM agents", 1.2)
        assert get_query_tracker() is None


# ============================================================================
# Transaction Rollback Tests
# ============================================================================


class TestTransactionRollback:
    """Tests for transaction rollback on errors."""

    @pytest.mark.asyncio
    async def test_session_rollback_on_exception(self, isolated_env):
        """Session rolls back uncommitted changes when exception occurs."""
        await ensure_schema()

        # Create a project successfully first
        async with get_session() as session:
            project = Project(slug="rollback-test", human_key=pkey("rollback/test"))
            session.add(project)
            await session.commit()

        # Now try to create a duplicate (which should fail)
        # and verify the transaction is rolled back
        try:
            async with get_session() as session:
                # This should succeed
                agent = Agent(
                    project_id=1,
                    name="TestAgent",
                    program="test",
                    model="test",
                )
                session.add(agent)

                # Simulate an error before commit
                raise ValueError("Simulated error")
        except ValueError:
            pass

        # Verify the agent was NOT persisted due to rollback
        async with get_session() as session:
            from sqlalchemy import text

            result = await session.execute(text("SELECT COUNT(*) FROM agents WHERE name='TestAgent'"))
            count = result.scalar()
            assert count == 0

    @pytest.mark.asyncio
    async def test_explicit_rollback_discards_changes(self, isolated_env):
        """Explicit session.rollback() discards uncommitted changes."""
        await ensure_schema()

        async with get_session() as session:
            project = Project(slug="explicit-rollback", human_key=pkey("explicit/rollback"))
            session.add(project)
            # Don't commit, just rollback
            await session.rollback()

        # Verify project was not persisted
        async with get_session() as session:
            from sqlalchemy import text

            result = await session.execute(text("SELECT COUNT(*) FROM projects WHERE slug='explicit-rollback'"))
            count = result.scalar()
            assert count == 0


# ============================================================================
# Session Cleanup Tests
# ============================================================================


class TestSessionCleanup:
    """Tests for proper session cleanup on exceptions."""

    @pytest.mark.asyncio
    async def test_session_closed_after_context(self, isolated_env):
        """Session is properly closed after context manager exits."""
        await ensure_schema()

        async with get_session() as session:
            # Create a project to verify session works
            project = Project(slug="cleanup-test", human_key=pkey("cleanup/test"))
            session.add(project)
            await session.commit()

        # Verify the data was committed and new sessions work correctly
        async with get_session() as new_session:
            from sqlalchemy import text

            result = await new_session.execute(
                text("SELECT COUNT(*) FROM projects WHERE slug='cleanup-test'")
            )
            count = result.scalar()
            assert count == 1

    @pytest.mark.asyncio
    async def test_session_closed_on_exception(self, isolated_env):
        """Session is properly closed even when exception occurs."""
        await ensure_schema()

        # First create a project we can verify
        async with get_session() as session:
            project = Project(slug="exception-test", human_key=pkey("exception/test"))
            session.add(project)
            await session.commit()

        try:
            async with get_session() as session:
                # Simulate an error mid-transaction
                raise RuntimeError("Test exception")
        except RuntimeError:
            pass

        # Verify database is still accessible after exception (session cleaned up)
        async with get_session() as new_session:
            from sqlalchemy import text

            result = await new_session.execute(
                text("SELECT COUNT(*) FROM projects WHERE slug='exception-test'")
            )
            count = result.scalar()
            assert count == 1

    @pytest.mark.asyncio
    async def test_multiple_concurrent_sessions(self, isolated_env):
        """Multiple concurrent sessions work independently."""
        await ensure_schema()

        # Create initial project
        async with get_session() as session:
            project = Project(slug="concurrent-test", human_key=pkey("concurrent/test"))
            session.add(project)
            await session.commit()

        async def worker(worker_id: int) -> str:
            async with get_session() as session:
                agent = Agent(
                    project_id=1,
                    name=f"Worker{worker_id}",
                    program="test",
                    model="test",
                )
                session.add(agent)
                await session.commit()
                await session.refresh(agent)
                return f"Worker{worker_id} created agent {agent.id}"

        # Run multiple workers concurrently
        results = await asyncio.gather(*[worker(i) for i in range(5)])

        assert len(results) == 5
        for i, result in enumerate(results):
            assert f"Worker{i}" in result

        # Verify all agents were created
        async with get_session() as session:
            from sqlalchemy import text

            result = await session.execute(text("SELECT COUNT(*) FROM agents"))
            count = result.scalar()
            assert count == 5


# ============================================================================
# Engine and Session Factory Tests
# ============================================================================


class TestEngineAndFactory:
    """Tests for engine and session factory initialization."""

    def test_reset_database_state_clears_globals(self, isolated_env):
        """reset_database_state clears all global state."""
        # First initialize the database
        asyncio.run(ensure_schema())

        # Verify engine is initialized
        engine = get_engine()
        assert engine is not None

        # Reset state
        reset_database_state()

        # After reset, calling get_engine should re-initialize
        # (the engine will be recreated on next access)

    @pytest.mark.asyncio
    async def test_reset_database_state_while_loop_running(self, isolated_env):
        """reset_database_state should safely dispose the engine even inside an active event loop."""
        await ensure_schema()
        engine = get_engine()
        assert engine is not None

        reset_database_state()

        await ensure_schema()
        async with get_session() as session:
            from sqlalchemy import text

            result = await session.execute(text("SELECT 1"))
            assert result.scalar() == 1

    @pytest.mark.asyncio
    async def test_get_session_factory_creates_factory(self, isolated_env):
        """get_session_factory creates and returns session factory."""
        factory = get_session_factory()
        assert factory is not None

        # Factory should produce working sessions
        async with factory() as session:
            # Just verify we can execute a simple query
            from sqlalchemy import text

            result = await session.execute(text("SELECT 1"))
            assert result.scalar() == 1


# ============================================================================
# WAL Mode and SQLite Configuration Tests
# ============================================================================


class TestSQLiteConfiguration:
    """Tests for SQLite-specific configuration."""

    @pytest.mark.asyncio
    async def test_wal_mode_enabled(self, isolated_env):
        """WAL mode is enabled for SQLite databases."""
        await ensure_schema()

        engine = get_engine()
        async with engine.begin() as conn:
            result = await conn.run_sync(
                lambda sync_conn: sync_conn.exec_driver_sql("PRAGMA journal_mode").fetchone()
            )
            assert result is not None
            journal_mode = result[0].lower()

        assert journal_mode == "wal"

    @pytest.mark.asyncio
    async def test_busy_timeout_set(self, isolated_env):
        """SQLite busy_timeout is set for lock handling."""
        await ensure_schema()

        engine = get_engine()
        async with engine.begin() as conn:
            result = await conn.run_sync(
                lambda sync_conn: sync_conn.exec_driver_sql("PRAGMA busy_timeout").fetchone()
            )
            assert result is not None
            timeout = result[0]

        # Should be 60000ms (60 seconds) to handle sustained write contention
        assert timeout == 60000

    @pytest.mark.asyncio
    async def test_synchronous_mode_full(self, isolated_env):
        """SQLite commits are durable across OS crashes and power loss."""
        await ensure_schema()

        engine = get_engine()
        async with engine.begin() as conn:
            result = await conn.run_sync(
                lambda sync_conn: sync_conn.exec_driver_sql("PRAGMA synchronous").fetchone()
            )
            assert result is not None
            sync_mode = result[0]

        # 2 = FULL
        assert sync_mode == 2
