# MCP Agent Mail — Ticketing v1 Specification

**Status:** committed. Supersedes the circulated v1 proposal and the three rival designs.
**Baseline commit:** `949cded` on `fix/isolate-oauth-config-tests`; measurements taken against that tree and against `data/mailbox/storage.sqlite3` read-only.
**Effort key:** Quick ≈ <1 h · Short ≈ half day · Medium ≈ 1–2 days · Large ≈ >2 days.

Every claim below carries `file:line`. Where the adversarial phase refuted a design's claim, the correction wins and is marked **[CORRECTED]** with its evidence. Section 9 is the settled-facts ledger — do not re-litigate anything in it.

---

## 1. Decisions

### P1 — beads

**Ticketing is a product feature of MCP Agent Mail; `.beads/` remains this fleet's own process tool, no importer ships in v1, and no code reads `.beads/` — but `AGENTS.md` MUST be rewritten in the same release, because it currently forbids the feature.**

*Rationale.* Beads is git-native JSONL: repo-scoped, offline, merges on branches. Agent Mail tickets are project-scoped rows in one SQLite file behind one server that has been corrupted twice this month and whose restart kills every other agent's MCP session. Moving the fleet's own worklist there is a reliability downgrade and couples "can we see our TODO list" to "is the mail server up". Two-way sync is a distributed-systems project, not a feature; refused outright.

The blocking finding is Design 2's, verified verbatim: `AGENTS.md:246` reads *"**Single source of truth**: Use **Beads** for task status/priority/dependencies"* and `AGENTS.md:275` reads *"Don't create or manage tasks in Mail; treat Beads as the single task queue."* Several providers auto-load `AGENTS.md` into agent context with no off switch. Shipping a tracker while our own agent instructions forbid using it is a defect, not documentation debt. Slice 6a rewrites `AGENTS.md:239-277` into **"Native tickets (this server)"** and **"External trackers (beads, Jira, Linear)"**, keeping the beads↔mail cheat-sheet verbatim under the second — it is already honoured in code (`app.py:10534-10541` admits dots in `topic` specifically so `br-abc.1` passes unmangled).

A later one-way import stays free because `ck_tickets_key` constrains a **superset** (§2) and `tickets.external_ref` carries a partial unique index, so re-import updates instead of duplicating. No schema change would be needed.

*What would make this wrong:* if the operator decides the fleet's own backlog must be visible to agents that cannot read the checkout (a cloud/remote agent). Then write a `tickets import-beads` CLI command against `tickets.py` — one module, no migration. Cost of being wrong: Quick-to-Short. Cost of the opposite error (shipping sync in v1) is owning a two-writer consistency problem against a tool we do not control.

### P2 — comments

**A ticket comment is an ordinary `Message` with `topic = <ticket key>` and `thread_id = <ticket key>`; there is no `ticket_comments` table — and the durability objection is answered instead by `ticket_events`, an append-only in-database mutation log that records that a comment happened even if the message is later purged.**

*Rationale.* Reuse buys, at zero persistence cost: inbox delivery through the durable outbox, per-recipient `read_ts`/`ack_ts` (`models.py:523-524`), FTS searchability (`fts_messages` is message-only, `db.py:2346`), the Git archive receipt, and `fetch_topic` as an already-registered tool. `Message.topic` is already indexed for exactly this read — `Index("idx_messages_project_topic", "project_id", "topic")` at **`models.py:533`** (not 527 — **[CORRECTED]**, Design 1 mis-cited it twice).

Design 3's case for a separate table rested on retention, and that argument is **[CORRECTED]**: `purge_old_messages` is not unqualified and not automatic. It defaults to `dry_run: bool = True` (`app.py:11719`), its predicate is triple-scoped — `Message.project_id == project.id`, `Message.created_ts < cutoff`, `Message.id NOT IN (pending reply targets)` (`app.py:11737-11741`) — there is no scheduler and no CLI command, and the archive is never trimmed. Design 3's two *other* reasons survive and are the ones on record: (i) every mail delivery is a serialised commit through one process-wide queue and one repo-wide `.commit.lock` shared by all four projects, so a Jira-shaped comment stream throttles real mail fleet-wide; (ii) a delivered message is immutable by receipt CHECK (`models.py:696-711`) and there is **no content-mutation path for `messages` anywhere in `src/`** — the only two `update(Message)` sites are `app.py:11753` (nulls `reply_to`) and `cli.py:6524` (repoints `project_id`).

The mitigation for (i) is restraint, and it is normative: **only `comment_ticket` and an assignee change emit mail.** `update_ticket`, `link_ticket` and closure write SQLite and nothing else.

The mitigation for retention and for immutability is `ticket_events`: a `commented` event row carrying the message id survives any purge, so the ticket's history is never lost even where the prose is.

Known unmitigated cost, stated honestly: `fetch_inbox(topic=...)` is a **positive** filter only — *"filter to messages with this topic tag"* (`app.py:13061`), passed through at `app.py:13128`; there is no negative/exclusion topic predicate anywhere. **[CORRECTED]** — Design 2 claimed inbox pollution was "already solved by machinery that exists"; it is not. An agent can ask for *only* ticket traffic; it cannot ask for its inbox *minus* ticket traffic. If that becomes intolerable, the fix is a read-side parameter on `fetch_inbox` (a `topic NOT IN`/`topic IS NULL` branch) that changes no stored data. Budget it as a follow-up, not as v1.

*What would make this wrong:* if ticket chatter measurably degrades inbox usability before the exclusion filter ships, or if commit-lock contention shows up in delivery latency. Both are observable; neither requires a data migration to fix.

### P3 — keys

**`<PREFIX>-<n>`, globally unique, minted from a `ticket_sequences` row by a bootstrap-`INSERT`-then-`UPDATE … RETURNING` executed inside the same `get_immediate_session()` transaction that inserts the ticket, with the candidate key checked against the existing `messages.topic` namespace before it is taken.**

*Rationale.* The key's whole value is off-system: "blocked on `AM-12`" pasted into a subject, a commit message, or a reservation `reason`. It must therefore be a legal mail `topic` verbatim — `app.py:10546` validates `len(topic) <= 64 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", topic)`, and `models.py:550` caps `topic` at 64, `models.py:545` caps `thread_id` at 128. §2's `ck_tickets_key` is exactly that charset.

Three sub-decisions, each against a refuted alternative:

- **Not the integer PK.** SQLite reuses rowids; this repo has paid for that twice (`models.py:560-562` gives `MessageDelivery` a UUID beside its rowid; `Project`/`Agent` carry random 32-byte `*_generation` columns, `models.py:180-187`).
- **Not `MAX(seq)+1`.** **[CORRECTED]** vs. the circulated proposal. It is correct only while no ticket is ever deleted, and it cannot express a prefix. A stored counter survives any future retention of closed tickets.
- **Globally unique prefix, not per-project.** **[CORRECTED]** vs. Design 3. Global→per-project is a constraint *relaxation* and is always free; per-project→global requires renaming keys that are already frozen (no `messages` content-mutation path exists, and archive documents embed `topic` verbatim at `delivery.py:544-556`). Four projects can otherwise each mint `AM-1`, and cross-project mail is implemented and supported.

**New requirement none of the three designs had:** the topic namespace is already occupied. 1683 of 4119 production messages carry a topic, and they are already in the PREFIX-WORD shape being proposed (`HERMES-REACT-MARKDOWN`, `IRIS-DOMAIN`, `IRIS-LOGIN-ORIGIN-HOTFIX`). `topic` is free-form with no reservation mechanism. A newly minted key that collides silently adopts unrelated historical conversation into `get_ticket`, and cannot be undone. The allocator must therefore reject a candidate that already exists as a topic in that project (§3).

*What would make this wrong:* if keys turn out to be unused by humans, the sequence table is three columns and one row per project and can be ignored. Changing the key *format* after keys are in mail is effectively impossible — which is why the CHECK constrains a superset rather than the canonical shape, making a format change a generator change and not a schema change.

### P4 — surfaces

**v1 ships MCP tools + a new `src/mcp_agent_mail/tickets.py` service module + a read-only `tickets` CLI group. No HTTP endpoint, no `/mail` SPA route, no locale key.**

*Rationale.* `/api/` is not a REST bridge: it is `fastapi_app.mount()` of the MCP Streamable-HTTP ASGI app (`http.py:6510-6528`), authenticated by the static service bearer, and `MailUiAuthMiddleware` returns early for any non-`/mail` path (`http.py:3983-3984`), so a session cookie is never consulted there. A new MCP tool is reachable by `POST /api/` with a bearer (that is how `scripts/hooks/agent_mail_common.sh:952-959` calls tools) and is **completely unusable from the browser SPA**. A ticket page therefore costs the entire API layer: measured at ~3200–4100 lines across 8 hand-edited files plus 44 mechanical locale catalogs, gated by `ui/vite.config.ts:51-56` (100% branches/functions/lines/statements) and `ui/src/i18n.test.ts:66` (45-catalog key-set equality). Deferring costs nothing: that price does not change with time.

Shipping no `/mail` GET route also means the suite's one **set-equality** contract test is never touched (`tests/test_mail_ui_auth.py:5699`, `assert actual == set(classification)`).

The service module is not optional. All 54 MCP tools are nested closures inside `build_mcp_server()` (`app.py:7370`; `_deliver_message` at `app.py:8312`, 4-space indent) and are physically unimportable, which is why the same FTS query already exists three times (`app.py:16705`, `cli.py:1516`, `http.py:8257`). The sanctioned answer is `ui_access.py` — 431 lines, typed `RuntimeError` subclasses carrying `.code` (`ui_access.py:62-69`), frozen slotted result dataclasses (`ui_access.py:81`), imported by **both** `cli.py:923` and `http.py:150`. `tickets.py` follows it exactly.

**[CORRECTED] — the reason the CLI is read-only is not the one Designs 1 and 2 gave.** Both claimed mail "cannot" be emitted outside `build_mcp_server()`. That is false: `accept_message_delivery` (`delivery.py:1057`), `process_message_delivery` (`delivery.py:1867`) and `emit_published_delivery_notifications` (`delivery.py:1544`) are module-level, and `http.py:8482-8483` already builds a `MessageDeliveryRequest` and delivers a complete, archived, notified message from outside `app.py`. The real blocker is narrower and different: **contact policy lives in the tool bodies, not in the delivery helper** — `_deliver_message` (`app.py:8312-8486`) contains no `contact_policy` reference at all; enforcement is at `app.py:10916-10925` (`CONTACT_BLOCKED`) inside `send_message`. A service-layer send would silently skip it. The v1 CLI is therefore read-only until that check is factored to module scope (tracked as a follow-up, §8.7).

*What would make this wrong:* if a human operator needs to file or close tickets from a browser. Then the endpoint reads through `tickets.py` and the only unavoidable extra work is adding the new `/mail/**` GET route to the `classification` dict at `tests/test_mail_ui_auth.py:5642-5698`.

---

## 2. Data model

Append verbatim to `src/mcp_agent_mail/models.py` after `ProjectSiblingSuggestion` (ends line 1145). **No import changes are required**: `CheckConstraint, Column, Index, Integer, String, UniqueConstraint, text` (`models.py:11`), `Field, SQLModel` (`models.py:13`), `Optional` (`models.py:9`) and `_utcnow_naive` (`models.py:123-130`) are all in scope.

Two rules govern the whole block and must survive the author:

1. **The big table constrains SHAPE, not VOCABULARY.** `op.batch_alter_table` — enabled at `migrations/env.py:45` and prescribed by `script.py.mako:7-8` — is the only way to widen a table-level CHECK, and it is CREATE-tmp + INSERT…SELECT + DROP + RENAME. So `status_key`, `kind_key`, `resolution_key`, `relation` and `target_kind` carry length/charset CHECKs only; membership is a module constant in `tickets.py`. In-file precedent for a documented-by-comment vocabulary string: `ProjectSiblingSuggestion.status` (`models.py:1139`) and `Agent.contact_policy` (`models.py:243`). This is Design 3's principle without Design 3's seven tables.
2. **`priority` is open-ended upward (`>= 0`), never `BETWEEN`.** A ceiling would need a rewrite the first time somebody wants a sixth band.

```python
# =================================================================================================
# Ticketing
# =================================================================================================
#
# Design rule, and it is a SQLite rule rather than a taste: a table-level CHECK is a promise that
# its vocabulary is closed forever, because widening one requires ``op.batch_alter_table`` -- a full
# copy of the table that also silently drops dependent triggers and indexes (migrations/env.py:45,
# migrations/script.py.mako:7-8). The ticket table therefore constrains SHAPE only: lengths,
# character classes, temporal ordering, non-negativity -- predicates that are true for any
# vocabulary. Membership (`open` / `in_progress` / `closed`, `epic` / `task` / ...) is a module
# constant in ``tickets.py`` and is enforced on write, exactly as ``ProjectSiblingSuggestion.status``
# (models.py:1139) and ``Agent.contact_policy`` (models.py:243) already are.


class TicketSequence(SQLModel, table=True):
    """Per-project allocator for human-readable ticket keys (``AM-12``).

    A stored counter rather than ``MAX(seq) + 1`` over ``tickets``: SQLite reuses integer row ids
    after a delete, which is why ``MessageDelivery`` carries a UUID beside its rowid
    (models.py:560-562) and why ``Project``/``Agent`` carry random ``*_generation`` columns
    (models.py:180-187, 230-237). A ticket key is quoted into mail subjects, ``topic`` tags,
    reservation ``reason`` strings and commit messages, and neither ``messages`` nor the Git archive
    has any content-rewrite path -- so the number must be minted once and never reissued, even if a
    future retention command deletes closed tickets.

    A dedicated table rather than a column on ``projects``: adding a column to the live ``projects``
    table is an ALTER this design otherwise never needs.

    ``prefix`` is GLOBALLY unique, not unique per project. A key pasted into cross-project mail
    carries no project context, so a per-project prefix would let two of the four projects both mint
    ``AM-1``. Global uniqueness is also the reversible direction: global -> per-project is a
    constraint relaxation and is free; per-project -> global would require renaming keys that are
    already frozen in immutable archive documents.

    ``prefix`` is STORED, never derived from ``Project.slug``. Deriving it would make a mutable field
    load-bearing -- rename the project and every memorised key points at nothing. That is the exact
    failure reasoned about for ``Agent.display_name`` at models.py:258-276.
    """

    __tablename__ = "ticket_sequences"
    __table_args__ = (
        UniqueConstraint("prefix", name="uq_ticket_sequences_prefix"),
        CheckConstraint(
            "length(prefix) BETWEEN 2 AND 12 "
            "AND upper(prefix) = prefix "
            "AND substr(prefix, 1, 1) GLOB '[A-Z]' "
            "AND prefix NOT GLOB '*[^A-Z0-9]*'",
            name="ck_ticket_sequences_prefix",
        ),
        CheckConstraint("next_seq >= 1", name="ck_ticket_sequences_next_seq"),
    )

    project_id: int = Field(foreign_key="projects.id", primary_key=True)
    prefix: str = Field(max_length=12)
    next_seq: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False, server_default="1"),
    )
    created_ts: datetime = Field(default_factory=_utcnow_naive)
    updated_ts: datetime = Field(default_factory=_utcnow_naive)


class Ticket(SQLModel, table=True):
    """One tracked unit of work: an epic, a task, a bug, or any future kind.

    There is deliberately no ``epics`` table. An epic is a ticket whose ``kind_key`` is ``'epic'``
    and whose children point at it through ``parent_id``. Two tables would have duplicated every
    link, event, filter and authorization predicate, and would have made a third level (sub-task,
    initiative, spike) a third table. Here a third level is a new member of one module constant.

    Discussion is NOT stored here. A ticket's conversation is ordinary mail whose ``Message.topic``
    equals this ``key``: already indexed (``idx_messages_project_topic``, models.py:533), already
    delivered to inboxes, already carrying read receipts and ACK (models.py:523-524), already
    committed to the Git archive, and already searchable through ``fts_messages`` (db.py:2346). A
    private comment table would have had to grow all of that from scratch and would still have been
    the one unsearchable corpus in a server built for searchable coordination.

    ``key`` is the character class ``send_message`` accepts for ``topic`` (app.py:10546:
    ``[A-Za-z0-9][A-Za-z0-9._-]*``, length <= 64), so every key is a legal ``topic`` and a legal
    ``thread_id`` (models.py:545, 128 chars) verbatim, with no escaping anywhere. The class is
    deliberately a SUPERSET of the shape we generate: it also admits ``bd-10s`` and ``br-abc.1``, so
    a future one-way import is a generator change and never a schema change.

    Uniqueness is enforced twice on purpose. ``uq_tickets_key`` is the exact-match constraint and
    provides the lookup index; ``uq_tickets_key_nocase`` closes a collision the first cannot see --
    ``fetch_topic`` matches case-INSENSITIVELY (app.py:13243), so ``AM-12`` and ``am-12`` would
    otherwise be two tickets with permanently indistinguishable discussions.

    ``closed_ts IS NOT NULL`` is the sole DATABASE-level meaning of "finished", which is why the hot
    worklist indexes are partial on it rather than on a list of status names. The status word is a
    label the service layer keeps in step; the physical invariant is that a resolution and a close
    time are both present or both absent.
    """

    __tablename__ = "tickets"
    __table_args__ = (
        UniqueConstraint("key", name="uq_tickets_key"),
        # Expression unique index; a UniqueConstraint cannot express case folding. Same technique as
        # uq_message_deliveries_idempotency (models.py:713-723).
        Index("uq_tickets_key_nocase", text("lower(key)"), unique=True),
        # THE hot query: what is open in this project, most urgent first. Partial so closed history
        # never enters the index, in the idiom of idx_agent_executions_active (models.py:399-405).
        Index(
            "idx_tickets_project_open",
            "project_id",
            "priority",
            "updated_ts",
            sqlite_where=text("closed_ts IS NULL"),
        ),
        Index(
            "idx_tickets_project_assignee_open",
            "project_id",
            "assignee_agent_id",
            "priority",
            sqlite_where=text("closed_ts IS NULL"),
        ),
        Index("idx_tickets_project_status", "project_id", "status_key", "updated_ts"),
        Index("idx_tickets_parent", "parent_id"),
        # Makes a later one-way import idempotent: re-importing an upstream issue updates rather
        # than duplicating. Partial, so the overwhelming majority of rows never enter it.
        Index(
            "uq_tickets_project_external_ref",
            "project_id",
            "external_ref",
            unique=True,
            sqlite_where=text("external_ref IS NOT NULL"),
        ),
        # Shape, never vocabulary -- see the section header. The class is exactly app.py:10546's.
        CheckConstraint(
            "length(key) BETWEEN 3 AND 64 "
            "AND substr(key, 1, 1) GLOB '[A-Za-z0-9]' "
            "AND key NOT GLOB '*[^A-Za-z0-9._-]*'",
            name="ck_tickets_key",
        ),
        CheckConstraint(
            "length(kind_key) BETWEEN 1 AND 32 "
            "AND lower(kind_key) = kind_key "
            "AND kind_key NOT GLOB '*[^a-z0-9_]*'",
            name="ck_tickets_kind_key",
        ),
        CheckConstraint(
            "length(status_key) BETWEEN 1 AND 32 "
            "AND lower(status_key) = status_key "
            "AND status_key NOT GLOB '*[^a-z0-9_]*'",
            name="ck_tickets_status_key",
        ),
        CheckConstraint(
            "resolution_key IS NULL OR (length(resolution_key) BETWEEN 1 AND 32 "
            "AND lower(resolution_key) = resolution_key "
            "AND resolution_key NOT GLOB '*[^a-z0-9_]*')",
            name="ck_tickets_resolution_key",
        ),
        CheckConstraint(
            "length(trim(title)) BETWEEN 1 AND 512",
            name="ck_tickets_title",
        ),
        # A real ceiling on free text, in the idiom of ck_agent_executions_task_description
        # (models.py:357-360): far above any honest description, far below what a runaway writer
        # needs to bloat a single-writer database every other agent's mail waits behind.
        CheckConstraint("length(description_md) <= 65536", name="ck_tickets_description_md"),
        # Open-ended upward on purpose. 0 is most urgent, matching the convention already in
        # .beads/issues.jsonl (priorities 0-4), so an imported priority needs no remapping.
        CheckConstraint("priority >= 0", name="ck_tickets_priority"),
        CheckConstraint("parent_id IS NULL OR parent_id != id", name="ck_tickets_parent_not_self"),
        # The two halves of "finished" cannot drift apart. Vocabulary-free by construction, so
        # renaming or adding a terminal status never touches this table. Mirrors the cross-column
        # shape of ck_agent_executions_status_end (models.py:373-377).
        CheckConstraint(
            "(closed_ts IS NULL AND resolution_key IS NULL) "
            "OR (closed_ts IS NOT NULL AND resolution_key IS NOT NULL)",
            name="ck_tickets_closure",
        ),
        CheckConstraint(
            "external_ref IS NULL OR length(trim(external_ref)) BETWEEN 1 AND 256",
            name="ck_tickets_external_ref",
        ),
        CheckConstraint("revision >= 1", name="ck_tickets_revision"),
        CheckConstraint(
            "updated_ts >= created_ts AND (closed_ts IS NULL OR closed_ts >= created_ts)",
            name="ck_tickets_timestamps",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    # Stored, never derived. SQLite reuses row ids, so a public identifier can never be a function
    # of the primary key.
    key: str = Field(max_length=64)
    # Vocabulary in tickets.TICKET_KINDS: epic | task | bug | chore.
    kind_key: str = Field(default="task", max_length=32)
    # Self-reference: the task's epic. NULL for an epic and for a loose task. The complementary rule
    # -- a parent must itself be an epic -- spans two rows and lives in tickets.py; SQLite CHECK
    # cannot see another row.
    parent_id: Optional[int] = Field(default=None, foreign_key="tickets.id")
    title: str = Field(max_length=512)
    description_md: str = Field(default="")
    # Vocabulary in tickets.TICKET_STATUSES: open | in_progress | closed.
    status_key: str = Field(default="open", max_length=32, index=True)
    # Vocabulary in tickets.TICKET_RESOLUTIONS: done | wontfix | duplicate | obsolete.
    resolution_key: Optional[str] = Field(default=None, max_length=32)
    priority: int = Field(
        default=3,
        sa_column=Column(Integer, nullable=False, server_default="3"),
    )
    # Nullable for the FileReservation.agent_id reason (models.py:862-866): a ticket must outlive
    # its assignee. An agent that is retired, swept or renamed must not take open work out of the
    # worklist; an assignee whose Agent row is gone reads as unassigned, which is the correct
    # *current* state.
    assignee_agent_id: Optional[int] = Field(default=None, foreign_key="agents.id", index=True)
    reporter_agent_id: Optional[int] = Field(default=None, foreign_key="agents.id")
    # Actor snapshot beside the FK, because not every writer is an Agent: ui_access.py:28 answers the
    # same question with the literal "cli". Without it a CLI-created ticket reads as "created by
    # nobody" forever. Same shape as message_delivery_recipients' name snapshots (models.py:826-828).
    reporter_label: str = Field(default="", max_length=128)
    # The message in which this work was decided; it normally predates the ticket, so the topic
    # convention cannot recover it. ondelete="SET NULL" is load-bearing rather than defensive:
    # purge_old_messages deletes Message rows (app.py:11760-11770) while PRAGMA foreign_keys=ON is
    # set on every pooled connection (db.py:449), and the same command already NULLs the
    # Message.reply_to self-FK for retained replies (app.py:11751-11758) -- i.e. SET NULL is this
    # codebase's own answer to exactly this coupling. The only existing ondelete precedent is
    # models.py:1049-1050.
    origin_message_id: Optional[int] = Field(
        default=None,
        foreign_key="messages.id",
        ondelete="SET NULL",
    )
    external_ref: Optional[str] = Field(default=None, max_length=256)
    # Compare-and-swap token for concurrent editors, in the idiom of ui_users.profile_revision
    # (models.py:1010). Optional on the wire; every write returns the new value.
    revision: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False, server_default="1"),
    )
    created_ts: datetime = Field(default_factory=_utcnow_naive)
    updated_ts: datetime = Field(default_factory=_utcnow_naive)
    closed_ts: Optional[datetime] = Field(default=None)


class TicketLink(SQLModel, table=True):
    """One directed edge from a ticket into the graph this server already owns.

    This is the capability a general-purpose tracker structurally cannot have, because it has
    neither this mail archive nor these file reservations: a ticket can point at the exact delivered
    message where the decision was made and at the file reservation realising it.

    ``target_ref`` is TEXT and carries NO foreign key, deliberately and uniformly. Three targets of
    three different shapes cannot share one integer FK, and a polymorphic triple of nullable FK
    columns would need an "exactly one" CHECK that then forces ``ondelete="CASCADE"`` on the message
    column -- which deletes the whole edge (the relation, the source, the author, the timestamp)
    rather than degrading the pointer. TEXT also survives ``purge_old_messages`` (app.py:11760-11770)
    with no coupling at all. The precedent is ``UiAccessAuditEvent`` (models.py:1056-1127), which
    carries no foreign keys for the same survive-the-referent reason.

    For ``target_kind = 'ticket'`` the ref is the target ticket's ``key``, not its row id: keys are
    globally unique and never rewritten, and a key-addressed edge may legitimately cross projects --
    the case a fleet coordinating four repositories actually hits.

    Existence is verified at write time by ``tickets.py``; a later dangling ref renders as "no longer
    available" rather than as an error.

    ``relation`` and ``target_kind`` are shape-checked strings with no membership CHECK: they are
    server-defined and extensible, and extending them must not be a rewrite of this table.
    """

    __tablename__ = "ticket_links"
    __table_args__ = (
        UniqueConstraint(
            "ticket_id", "relation", "target_kind", "target_ref", name="uq_ticket_links_edge"
        ),
        # The reverse read: "what blocks this ticket".
        Index("idx_ticket_links_target", "target_kind", "target_ref"),
        Index("idx_ticket_links_ticket", "ticket_id", "relation"),
        CheckConstraint(
            "length(relation) BETWEEN 1 AND 32 "
            "AND lower(relation) = relation "
            "AND relation NOT GLOB '*[^a-z_]*'",
            name="ck_ticket_links_relation",
        ),
        CheckConstraint(
            "length(target_kind) BETWEEN 1 AND 32 "
            "AND lower(target_kind) = target_kind "
            "AND target_kind NOT GLOB '*[^a-z_]*'",
            name="ck_ticket_links_target_kind",
        ),
        CheckConstraint(
            "length(trim(target_ref)) BETWEEN 1 AND 128",
            name="ck_ticket_links_target_ref",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    ticket_id: int = Field(foreign_key="tickets.id", index=True)
    # Vocabulary in tickets.TICKET_RELATIONS: blocks | relates | duplicates | decided_by | touches.
    # Inverses are derived at read time.
    relation: str = Field(max_length=32)
    # Vocabulary in tickets.TICKET_TARGET_KINDS: ticket | message | file_reservation.
    # Reserved without any schema change: commit | execution | build_slot | url.
    target_kind: str = Field(max_length=32)
    target_ref: str = Field(max_length=128)
    created_by_agent_id: Optional[int] = Field(default=None, foreign_key="agents.id")
    created_by_label: str = Field(default="", max_length=128)
    created_ts: datetime = Field(default_factory=_utcnow_naive)


class TicketEvent(SQLModel, table=True):
    """Append-only record of one ticket mutation.

    This table ships in v1 for one reason: history is the only part of the design that CANNOT be
    added retroactively. Every other deferral here -- claims, full-text search, the web UI, a beads
    importer -- is a new table or an additive index later at identical cost. A change log that was
    not written on the day of the change has no source to backfill from.

    It is also what makes the P2 decision safe. Ticket *discussion* is mail and is therefore subject
    to ``purge_old_messages``; a ``commented`` event carrying the message id is a durable
    in-database record that the comment existed, independent of whatever retention later removes.

    No foreign keys and snapshot columns throughout, matching ``UiAccessAuditEvent``
    (models.py:1056-1127) exactly: an audit row must outlive its subject and must read correctly
    without a join.

    Known v1 gap, stated rather than hidden: append-only is enforced by the service layer, which has
    no update path, and not yet by a ``ticket_events_immutable_bu`` trigger of the kind db.py:3931
    installs for ``ui_access_audit_events``. That trigger is additive and belongs in a later change,
    because the first non-empty Alembic revision in this repository should not also reopen the
    "nothing new is added to db.py" question (versions/20260814_1900_0001baseline_baseline.py:19-20).
    """

    __tablename__ = "ticket_events"
    __table_args__ = (
        Index("idx_ticket_events_ticket_created", "ticket_id", "created_ts"),
        Index("idx_ticket_events_project_created", "project_id", "created_ts"),
        CheckConstraint(
            "length(event_type) BETWEEN 1 AND 32 "
            "AND lower(event_type) = event_type "
            "AND event_type NOT GLOB '*[^a-z_]*'",
            name="ck_ticket_events_event_type",
        ),
        CheckConstraint(
            "field_name IS NULL OR (length(field_name) BETWEEN 1 AND 64 "
            "AND lower(field_name) = field_name "
            "AND field_name NOT GLOB '*[^a-z0-9_]*')",
            name="ck_ticket_events_field_name",
        ),
        CheckConstraint(
            "(old_value IS NULL OR length(old_value) <= 1024) "
            "AND (new_value IS NULL OR length(new_value) <= 1024)",
            name="ck_ticket_events_values",
        ),
        CheckConstraint("revision_after >= 1", name="ck_ticket_events_revision_after"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    ticket_id: int = Field(index=True)
    ticket_key_snapshot: str = Field(max_length=64)
    project_id: int = Field(index=True)
    project_slug_snapshot: str = Field(max_length=255)
    # Vocabulary in tickets.TICKET_EVENT_TYPES:
    # created | field_changed | commented | linked | unlinked | closed | reopened.
    event_type: str = Field(max_length=32)
    field_name: Optional[str] = Field(default=None, max_length=64)
    old_value: Optional[str] = Field(default=None, max_length=1024)
    new_value: Optional[str] = Field(default=None, max_length=1024)
    actor_agent_id: Optional[int] = Field(default=None)
    actor_label: str = Field(default="", max_length=128)
    revision_after: int = Field(default=1)
    created_ts: datetime = Field(default_factory=_utcnow_naive)
```

**Timestamps.** Every `*_ts` uses `_utcnow_naive` (`models.py:123-130`). `tickets.py` imports and uses only that helper. Reaching for `datetime.now(timezone.utc)` produces aware datetimes and raises *"can't compare offset-naive and offset-aware"* in the ORM evaluator — the exact failure `_utcnow_naive` exists to prevent. The SQLite converter returns `None` on a parse failure rather than raising (`db.py:380-399`), so a corrupt timestamp reads back as NULL silently. Never a `server_default` for a timestamp; the codebase has none.

**Service-layer vocabularies** (module constants in `tickets.py`, the single source of membership):

```
TICKET_KINDS        = ("epic", "task", "bug", "chore")
TICKET_STATUSES     = ("open", "in_progress", "closed")
TICKET_RESOLUTIONS  = ("done", "wontfix", "duplicate", "obsolete")
TICKET_RELATIONS    = ("blocks", "relates", "duplicates", "decided_by", "touches")
TICKET_TARGET_KINDS = ("ticket", "message", "file_reservation")
TICKET_EVENT_TYPES  = ("created", "field_changed", "commented", "linked", "unlinked",
                       "closed", "reopened")
```

`blocked` and `review` are deliberately absent from `TICKET_STATUSES`: `blocked` is derivable from an unresolved `blocks` edge and would be a second source of truth that can disagree with the first; `review` is a stage this fleet performs by mail. Adding either later is a one-line change to a tuple, not a migration — that is the whole point of the shape/vocabulary split.

Priority→importance mapping used for notifications (`tickets.priority_importance`): `0,1 → "high"`, `2,3 → "normal"`, `>=4 → "low"`.

---

## 3. Key generation

**Format.** `<PREFIX>-<n>` where `PREFIX` matches `[A-Z][A-Z0-9]{1,11}` and `n` is a positive decimal. Globally unique across all projects, case-insensitively.

**Prefix derivation** (first ticket in a project only). From `Project.slug` (`models.py:159`, already canonical lowercase `[a-z0-9-]`): uppercase, strip non-alphanumerics, take the first 4 characters, pad/refuse below 2. `uq_ticket_sequences_prefix` makes a collision a hard error; the allocator then appends `2`…`9` in a bounded loop inside the same transaction and, if all are taken, raises `TICKET_PREFIX_UNAVAILABLE`. `create_ticket(key_prefix=...)` overrides the derivation, and is honoured **only** when the project has no `ticket_sequences` row yet.

**Allocation.** One function, `tickets.allocate_ticket_key(session, *, project, prefix_hint=None) -> str`, called with an `AsyncSession` that the caller opened via `db.get_immediate_session()` (`db.py:704`) — the same transaction that inserts the `Ticket` row.

```
1. bootstrap:  SELECT ... FROM ticket_sequences WHERE project_id = :pid
               if missing -> derive prefix, INSERT (project_id, prefix, next_seq=1)
2. loop (<= 64 iterations):
       UPDATE ticket_sequences
          SET next_seq = next_seq + 1, updated_ts = :now
        WHERE project_id = :pid
       RETURNING prefix, next_seq - 1
       candidate = f"{prefix}-{n}"
       if EXISTS (SELECT 1 FROM messages
                   WHERE project_id = :pid AND lower(topic) = lower(:candidate))
           continue          # the topic namespace already owns this string
       return candidate
   raise TicketError("TICKET_KEY_NAMESPACE_EXHAUSTED")
3. the ticket INSERT follows in the same transaction; uq_tickets_key /
   uq_tickets_key_nocase are the invariant, the sequence is only the allocator.
```

**Why the bootstrap INSERT is not optional.** All three rival designs specified the allocator as "a single `UPDATE … RETURNING`". On a project's first ticket the row does not exist, the UPDATE matches zero rows, and `RETURNING` yields nothing. **[CORRECTED]** — verified: `UPDATE seq SET next_value = next_value + 1 WHERE project_id=99 RETURNING next_value - 1` on a missing row returns no rows at all.

**Why `RETURNING next_seq - 1` is the allocated number.** SQLite `RETURNING` sees post-update values. Verified on the runtime's SQLite 3.46.1: seeded at 1, two successive statements returned `1` then `2`, leaving the stored value at `3`. The repo already uses `.returning()` for the "did my statement apply" purpose at `app.py:6537-6547`.

**Why it is race-free on this database.** `get_immediate_session` issues `BEGIN IMMEDIATE` *before* SQLAlchemy's autobegin can issue a plain `BEGIN` (`db.py:735-738`), taking SQLite's RESERVED lock before the first read. Its docstring names the two defects this prevents — *"Phantom conflicts after a release (#130) / Missed conflicts before an insert (#129)"* (`db.py:709-712`) — and a counter read-modify-write is exactly the second. Two concurrent creators serialise on the RESERVED lock against `busy_timeout=60000` (`db.py:469`). Because the allocation shares the ticket's own transaction, a rollback returns the number rather than burning it, so keys are gap-free as well as collision-free.

**[CORRECTED] — a separate table buys no contention reduction.** Design 3 justified `ticket_key_sequences` partly as "keeps ticket creation from contending with project writes". The RESERVED lock is database-wide, not row- or table-scoped (`db.py:705-712`, and the docstring's own warning about *"unnecessary write-lock contention"* at `db.py:727-731`). The separate table is still correct — for the stated reason about not ALTERing a live table — but the contention argument must not be repeated.

**Session discipline, normative.** `get_immediate_session` for `create_ticket`, `update_ticket` and `link_ticket` (the last because its `blocks`-cycle reachability walk followed by an insert **is** a read-then-write — **[CORRECTED]** vs. Design 1, which exempted it). Plain `get_session` for `get_ticket`, `list_tickets`, the discussion read, the event read and every CLI command. A RESERVED lock on a plain listing adds write contention to a single-writer 63 MB database that already has 29 immediate-session call sites in `app.py` and 13 in `delivery.py`.

**[CORRECTED] — a composite primary key is not an idempotency mechanism.** Design 1 claimed re-linking is "idempotent by the composite primary key rather than by a read-then-write". A duplicate insert raises `IntegrityError`, which poisons the SQLAlchemy transaction and is *not* an `OperationalError`, so `retry_on_db_lock` (`db.py:294`) never sees it. The house idiom is an explicit `except IntegrityError: await session.rollback()` then re-read — `app.py:4162-4172`, and six other call sites. `set_ticket_link` uses that shape.

---

## 4. Migration plan

### What happens to the live 63 MB database

`ensure_schema` (`db.py:1390`) is the only schema path in the project and **is** the migration runner: there is no `alembic` invocation in any Dockerfile, compose file, deploy script or the Makefile, and `make migrate` → `cli.py`'s `migrate` command → `ensure_schema()`. Its order, verified:

1. `db.py:1417-1419` — records `was_fresh = not has_table("agents")` **before** anything.
2. `db.py:1422` — `await conn.run_sync(SQLModel.metadata.create_all)` (checkfirst=True). **This is what actually creates the four ticketing tables, on every database, fresh or live.**
3. `db.py:1426-1443` — three hand-written rebuilds + `_setup_fts`.
4. `db.py:1445-1448` — `_align_alembic_version(...)`, **last**, which at `db.py:1382-1386` skips the stamp when a version row already exists and then runs `alembic_command.upgrade(config, "head")` **unconditionally**.

Cost of step 2 for the new tables: `CREATE TABLE` inserts one `sqlite_master` row and allocates one root page; each index on an empty table allocates one more. Four tables plus nine indexes ≈ tens of kilobytes appended to the WAL, O(1) in database size, touching zero existing rows. `synchronous=FULL` costs one or two fsyncs. `wal_autocheckpoint=1000` means the frames ride along in an ordinary later checkpoint. The one non-free effect is a schema-cookie bump, invalidating prepared statements on the 50-connection pool; they re-prepare on next use. **No existing table is altered, so `op.batch_alter_table` never runs and the three `fts_messages` triggers are never at risk.**

### The show-stopper, and the guard

Production is stamped `0001baseline` (measured read-only). Because `create_all` runs *before* `upgrade head`, an unguarded `op.create_table("tickets")` in revision 0002 hits a table `create_all` built moments earlier: `OperationalError: table tickets already exists`. The `create_all` commit is a separate transaction and is **not** rolled back; `alembic_version` stays at `0001baseline`; `ensure_schema` raises; and `@retry_on_db_lock` does not intervene because `_is_lock_error` (`db.py:218-230`) matches only *locked / busy / unable to open / disk i/o error*. Every restart reproduces it identically, and `ensure_schema()` is called from ~30 CLI entry points and at server start — so it takes down every entry point at once.

### Is a migration file needed at all?

Strictly, no: head would remain `0001baseline`, `upgrade head` would remain a no-op, and `create_all` would create the tables everywhere. We ship one anyway, because deviating silently from the repository's own stated doctrine (*"from here on, schema changes are Alembic revisions"*, `versions/20260814_1900_0001baseline_baseline.py:19-20`) on the first opportunity is worse governance than accepting a documented rollback procedure — and that procedure has to be written before *any* future revision regardless.

### The revision

`src/mcp_agent_mail/migrations/versions/20260831_1200_0002ticketing_ticketing.py`, `revision = "0002ticketing"`, `down_revision = "0001baseline"` (filename per `alembic.ini:12`).

**[CORRECTED] — do not hand-write the DDL.** All three designs specified `op.create_table("tickets", ...)  # full column/CHECK/index set`. Once correctly guarded, that body is **unreachable on every database shape** — verified on fresh, already-tracked-at-baseline, and pre-Alembic/untracked-but-populated — because `create_all` always ran first. Hundreds of lines of duplicate DDL that can never execute, can never be covered by a test, and are guaranteed to drift from `models.py`. Drive it from metadata instead:

```python
"""Bring the Alembic ledger up to the ticketing tables.

Reconciliation, not creation. ``ensure_schema`` runs ``SQLModel.metadata.create_all``
(db.py:1422) and only afterwards ``alembic upgrade head`` (db.py:1445-1448 -> db.py:1386),
so on every database that is not brand new -- production included, stamped 0001baseline --
these tables already exist by the time this body runs. A bare ``op.create_table`` would
raise "table tickets already exists", the create_all commit would not roll back, and the
server would fail to start on every restart.

This is STRUCTURAL, not a workaround for this one change: while create_all runs first,
every table-creating revision forever must be a no-op when the table is present. And the
index loop is not decoration -- ``create_all(checkfirst=True)`` skips an EXISTING table
wholesale and will NOT add a model-declared Index to it. db.py:2559-2593 is thirteen
hand-written ``CREATE INDEX IF NOT EXISTS`` statements standing as proof of what happens
when nobody notices.
"""

_TICKETING_TABLES: tuple[str, ...] = (
    "ticket_sequences",
    "tickets",
    "ticket_links",
    "ticket_events",
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    present = set(inspector.get_table_names())

    missing = [SQLModel.metadata.tables[name] for name in _TICKETING_TABLES if name not in present]
    if missing:
        SQLModel.metadata.create_all(bind, tables=missing, checkfirst=False)

    for name in _TICKETING_TABLES:
        if name not in present:
            continue  # just created above, indexes came with it
        existing = {ix["name"] for ix in inspector.get_indexes(name)}
        for index in SQLModel.metadata.tables[name].indexes:
            if index.name not in existing:
                index.create(bind)


def downgrade() -> None:
    """Unreachable from the runtime (nothing invokes ``alembic downgrade`` anywhere in this
    repository); correct for a manual CLI invocation. Foreign-key order."""
    bind = op.get_bind()
    present = set(sa.inspect(bind).get_table_names())
    for name in ("ticket_events", "ticket_links", "tickets", "ticket_sequences"):
        if name in present:
            op.drop_table(name)
```

Importing `SQLModel.metadata` in a revision is consistent with this repo — `migrations/env.py` already imports `models` for metadata registration and sets `target_metadata = SQLModel.metadata`. The "frozen snapshot" property that normally argues against it is already not held by anything here, because the runtime schema comes from models.

**No data seed may ever live in a revision body.** On a fresh database `was_fresh` is True, `db.py:1385` stamps `head`, and the subsequent `upgrade head` is a no-op — so a seed placed in `upgrade()` would run on production and on **no** developer machine. This was Design 3's uniquely correct observation and it is now a standing rule. (v1 seeds nothing; vocabularies are module constants.)

### Rollback — the gap none of the three designs had

**[CORRECTED] — `downgrade()` is not the rollback story, and reverting the image after 0002 has stamped the database bricks every entry point.** `alembic_command` appears in `src/` at exactly two lines, `db.py:1385` (stamp) and `db.py:1386` (upgrade). With `0002ticketing` recorded and the old image's `versions/` directory lacking it, `_align_alembic_version` sees `already_tracked`, skips the stamp, and `upgrade(config, "head")` raises `CommandError: Can't locate revision identified by '0002ticketing'` — measured — on every entry point, repeating on every restart. On a fleet whose deploy is a container restart and whose reflex on a bad deploy is an image revert, this is the *more probable* outage of the two.

**Runbook (goes in the revision docstring, README deploy section, and the fleet's restart protocol):**

*Before deploy* — record the stamp and take the repo's own backup:
```
sqlite3 -readonly <db> "SELECT version_num FROM alembic_version;"
# plus: git bundle --all + create_sqlite_snapshot (storage.py:5692-5717)
```

*If the ticketing release must be rolled back*, either:
- **roll forward** — redeploy the ticketing image; the guarded revision is a no-op and startup succeeds; or
- **revert the image, then re-stamp before starting the container:**
  ```
  sqlite3 <db> "UPDATE alembic_version SET version_num='0001baseline';"
  ```
  Measured to restore startup. The orphan ticketing tables are harmless: `create_all` never drops, and nothing in the old image reads them.

### Test fixes

**[CORRECTED] — the suite already covers the production-shaped path; do not write a new fixture.** All three designs asserted otherwise (Design 1: "the entire test suite takes the fresh path"; Designs 2 and 3 each budgeted a slice for a hand-built already-tracked fixture). `tests/test_alembic_baseline.py:108-131` does **not** use `isolated_env` — it takes `tmp_path, monkeypatch` directly and builds its own database through `_build_schema` (which calls `dbmod.ensure_schema()` at line 104). At lines 125-127 it drops `alembic_version` and at line 130 re-runs `ensure_schema` against a database that already contains the new tables. With an unguarded revision it **errors at line 130**, before its own assertion. It is the real exerciser and it costs nothing.

Two changes, both in slice 1:

- `tests/test_alembic_baseline.py:131` — `assert _stamp_in(database) == dbmod._BASELINE_REVISION` becomes an assertion against the **current head** read from the script directory (`ScriptDirectory.from_config(...).get_current_head()`), not a hardcoded `"0002ticketing"`, so it never needs editing again. The module docstring at lines 9-11 already anticipated this (*"Today the baseline happens to be head, so both paths land on the same string"*). The three unit tests above it (lines 53-84) monkeypatch both `stamp` and `upgrade` and assert only the stamp *decision*; they survive unchanged.
- `tests/test_database_failures.py:246-257` — additive: assert `tickets`, `ticket_sequences`, `ticket_links`, `ticket_events` are present after schema init.

---

## 5. MCP tool surface

Six tools in a new cluster. `CLUSTER_TICKETING = "ticketing"` added at `app.py:436` (after `CLUSTER_PRODUCT`). All six registered as a contiguous 4-space-indent block inside `build_mcp_server()`, inserted at `app.py:15954` — after the file-reservation tools end and **before** the `# --- Build slots` comment at `app.py:15955`. **Not** inside any `if settings.*_enabled:` guard: 8 of the existing 54 tools vanish on a default install because `WORKTREES_ENABLED` defaults to False (`config.py:808-811`), and a headline feature nobody can find has not shipped.

Decorator stack, fixed order (reversing it registers the uninstrumented function): `@mcp.tool(name=…, description=…)` → `@_instrument_tool(name, cluster=CLUSTER_TICKETING, capabilities={"ticketing"}, project_arg="project_key", agent_arg="agent_name")` → body. No `@retry_on_db_lock` on any of them.

Signature order is house law: `ctx: Context` first, then required, then optional, then `registration_token`, then `format` **last**.

| Tool | Signature | Returns |
|---|---|---|
| `create_ticket` | `(ctx, project_key, agent_name, title, kind="task", description_md="", priority=3, parent_key=None, assignee=None, status=None, origin_message_id=None, external_ref=None, key_prefix=None, notify_assignee=True, registration_token=None, format=None)` | `dict[str, Any]` |
| `get_ticket` | `(ctx, project_key, agent_name, ticket_key, include_discussion=True, discussion_limit=20, event_limit=50, registration_token=None, format=None)` | `dict[str, Any]` |
| `list_tickets` | `(ctx, project_key, agent_name, status=None, kind=None, assignee=None, parent_key=None, priority_max=None, include_closed=False, query=None, updated_before=None, limit=50, registration_token=None, format=None)` | **`ToonableList`** |
| `update_ticket` | `(ctx, project_key, agent_name, ticket_key, title=None, description_md=None, status=None, resolution=None, priority=None, assignee=None, parent_key=None, expected_revision=None, notify_assignee=True, registration_token=None, format=None)` | `dict[str, Any]` |
| `link_ticket` | `(ctx, project_key, agent_name, ticket_key, relation, target_ref, target_kind="ticket", remove=False, registration_token=None, format=None)` | `dict[str, Any]` |
| `comment_ticket` | `(ctx, project_key, agent_name, ticket_key, body_md, idempotency_key, to=None, cc=None, importance=None, ack_required=False, auto_contact_if_blocked=None, registration_token=None, format=None)` | `dict[str, Any]` |

**Semantics, one line each:**

- **`create_ticket`** — Mint a ticket or an epic (`kind="epic"`), allocate its globally unique key, write a `created` event, and — when an assignee is given — mail them. Absorbs the circulated proposal's separate `create_epic`.
- **`get_ticket`** — One ticket in full: own fields, parent, children if it is an epic, links in both directions, the discussion read by `topic`, and the event history; `discussion_truncated` / `events_truncated` booleans rather than silent elision.
- **`list_tickets`** — The worklist. Filter by status, kind, assignee (`"me"` resolves to the caller), epic, priority ceiling or a `LIKE` title query; ordered `updated_ts DESC, id DESC`; keyset-paged by `updated_before`, never `OFFSET`.
- **`update_ticket`** — Every field mutation in one verb — retitle, re-describe, reprioritise, reassign (`assignee=""` unassigns), reparent (`parent_key=""` detaches), transition status — with optional `expected_revision` compare-and-swap; absorbs the proposal's `assign_ticket` and `close_ticket`.
- **`link_ticket`** — Assert or retract one typed edge: `blocks`/`relates`/`duplicates` → a ticket key, `decided_by` → a message id, `touches` → a file-reservation id; `remove=True` absorbs an `unlink_ticket` tool.
- **`comment_ticket`** — Post a comment as a real Agent Mail message with `topic=<key>` and `thread_id=<key>`, so it is delivered, acknowledgeable, searchable and archived, and record a `commented` event carrying the message id.

**Binding rules for the tool bodies:**

- **Return annotation.** `list_tickets` **must** be `ToonableList` (`app.py:409`), never `list[dict[str, Any]]`. FastMCP derives the output schema from the annotation, and a list annotation makes `format="toon"` *error out entirely* rather than degrade — and would break every tool at once if `TOON_DEFAULT_FORMAT` is ever set server-side (`app.py:392-408`).
- **Argument validation.** `limit`, `discussion_limit` and `event_limit` go through `_validate_limit` (`app.py:1918`, *"the single source of truth for limit bounds"*); `updated_before` through `_validate_iso_timestamp` (`app.py:1871`).
- **Resolution and auth.** `await _get_project_by_identifier(project_key)` (`app.py:3126`) as the first statement after argument validation, then `_authenticate_agent(ctx, project, agent_name, registration_token, token_param="registration_token", action="<tool_name>")` (`app.py:7932`).
- **Errors.** `ToolExecutionError(error_type, message, recoverable=…, data={…})` with SCREAMING_SNAKE types: `TICKET_NOT_FOUND`, `UNKNOWN_TICKET_KIND`, `UNKNOWN_TICKET_STATUS`, `UNKNOWN_TICKET_RESOLUTION`, `UNKNOWN_TICKET_RELATION`, `PARENT_NOT_FOUND`, `PARENT_NOT_AN_EPIC`, `TICKET_CYCLE`, `TICKET_REVISION_CONFLICT` (recoverable, `data` carries the current revision), `TICKET_PREFIX_UNAVAILABLE`, `TICKET_KEY_NAMESPACE_EXHAUSTED`, `LINK_TARGET_NOT_FOUND`, `CLOSED_REQUIRES_RESOLUTION`.
- **Ordering — normative.** DB-first, always. Commit and **close** the write transaction, then send mail, then report `notification: {delivered, delivery_id, error}` in the response. A failed notification never rolls back a ticket. **[CORRECTED]** vs. Designs 2 and 3, which promised the opposite: `accept_message_delivery` opens its **own** `get_immediate_session` (`delivery.py:1070`) on a fresh pooled connection (`db.py:735-738`), so holding a ticket write transaction across a send blocks on a database-wide RESERVED lock for `busy_timeout=60000` (`db.py:469`) and then — because `_is_lock_error` matches bare `"locked"` — `retry_on_db_lock` re-runs the whole body and repeats the stall. `force_release_file_reservation` commits and exits its immediate session at `app.py:15648` before mailing at `app.py:15734`; that is the house rule.
- **Idempotency.** `comment_ticket` takes a **required explicit** `idempotency_key` (1–128 chars, stripped, validated in the tool body before anything else), matching `send_message` (`app.py:10383`). **[CORRECTED]** vs. Design 1's content-hashed `_internal_delivery_idempotency_key`: two genuinely distinct comments with identical text from the same sender would collide, and `delivery.py:1072-1076` returns `reused=True` — silently swallowing the second comment with no error. The assignment notification, being a server-originated event, keeps the internal-key form (`app.py:8299`, precedent at `app.py:14059` and `app.py:15721`) with the ticket revision folded into the payload so successive assignments do not collide.
- **Contact policy.** `comment_ticket` is agent-authored content and **must** apply the same policy branch `send_message` applies at `app.py:10916-10925`; blocked recipients are dropped, and if the surviving set is empty the comment is addressed to the sender (self-send is always permitted, `app.py:11226-11233`) rather than refused — `_deliver_message` raises `INVALID_ARGUMENT` on an empty recipient set at `app.py:8343-8349`. The assignment notification is a coordination event: if the assignee blocks the assigner, the ticket is still assigned and `notification.delivered=false` with `reason="CONTACT_BLOCKED"` is reported. **[CORRECTED]** vs. Design 2's "you cannot assign work to an agent that will not take your mail" — `_deliver_message` contains no `contact_policy` reference anywhere in `app.py:8312-8486`, and fail-closed atomicity across a delivery is unimplementable here (see Ordering).
- **EMFILE retry.** Add only `get_ticket` and `list_tickets` to `_EMFILE_RETRY_TOOLS` (`app.py:411-425`) — pure reads, consistent with `fetch_inbox`/`search_messages` already being there. **Never** `create_ticket` or `comment_ticket`; the comment on that frozenset says why.
- **Discussion read.** Exact match `Message.topic == ticket.key`, which uses `idx_messages_project_topic` (`models.py:533`). `fetch_topic` folds case (`app.py:13243`) and would defeat the index; we control the topic we write, and the allocator has already excluded a case-insensitive collision.

**Registration side effects that are not automatic and must be done in the same commit:**

- `TOOL_FILTER_PROFILES` (`app.py:458-482`): add `CLUSTER_TICKETING` to the **`core`** profile's `clusters` list (which already carries `CLUSTER_FILE_RESERVATIONS` at `app.py:463`). `minimal` and `messaging` unchanged — a recorded decision, not an oversight.
- `config.py:696-699`: add `get_ticket,list_tickets` to the `HTTP_RBAC_READONLY_TOOLS` default (currently `health_check,fetch_inbox,whois,search_messages,summarize_thread`), consumed at `http.py:5294`; otherwise reader-role JWTs get 403 on ticket reads.
- `resource://tooling/directory` (`app.py:17059-17469`): one new cluster dict with all six tools plus a playbook (`create_ticket → file_reservation_paths → comment_ticket → update_ticket(status='closed')`). `tests/test_tool_filter_and_notifications.py:164` asserts `directory_names <= filtered_names` — **one-way**: a typo breaks CI loudly, an omission passes silently. Copy each name from its `@mcp.tool(name=…)` line rather than retyping.
- `resource://tooling/schemas` (`app.py:17494-17553`): entries for `create_ticket` and `update_ticket` only — the two with enough optional arguments to be misread. `tests/test_rotate_registration_token.py:317` asserts documented `required` equals the input schema's `required` exactly.
- **No** `resource://tickets/vocabulary` in v1: the vocabularies live in the tool docstrings and the schema hints. Deferred as a Quick follow-up if agents demonstrably guess wrong.

**Capabilities declare, they do not gate.** `capabilities={"ticketing"}` is documentation only: `_enforce_capabilities` returns early unless `ctx.metadata["allowed_capabilities"]` is populated, and nothing in the codebase sets it (`app.py:632-641`). Authorization is `_authenticate_agent`.

---

## 6. Other surfaces

| Surface | v1 | Why |
|---|---|---|
| **MCP tools** | Ships | Registration is a side effect of decoration; no list to append to. |
| **`POST /api/` JSON-RPC** | Ships **for free** | `/api/` is an ASGI mount of the MCP app (`http.py:6510-6528`); a new tool is immediately callable as `params.name="create_ticket"` by anything holding the **static service bearer** — which is how `scripts/hooks/agent_mail_common.sh:952-959` already calls tools. No code. |
| **`tickets.py` service module** | Ships | The only thing preventing ticketing from being written three times (`app.py:16705` / `cli.py:1516` / `http.py:8257` are the same FTS query, three times). |
| **CLI `tickets list` / `tickets show`** | Ships, **read-only** | Operator audit path when the MCP session is down — which on this deployment is every restart. Read-only because contact policy is not yet factored out of the tool bodies (§1 P4). |
| **`/mail/api/v1/tickets` HTTP endpoints** | **Deferred** | The SPA holds a cookie and no bearer, and `MailUiAuthMiddleware` ignores non-`/mail` paths (`http.py:3983-3984`), so the `/api/` mount gives the browser **nothing**. |
| **`/mail` SPA page** | **Deferred** | ~3200–4100 lines across 8 hand-edited files + 44 locale catalogs, under 100% vitest thresholds (`ui/vite.config.ts:51-56`) and 45-catalog key-set equality (`ui/src/i18n.test.ts:66`). Same price now or later. |
| **Git archive mirror of ticket rows** | **Never** (v1 or later) | 14 of 19 tables have none; the mirrored ones share one property — an off-server reader needs them from disk. A row whose status changes 20 times does not belong in append-only history as 20 rewrites of one JSON file. Ticket *discussion* reaches the archive for free as mail. |
| **FTS5 for tickets** | **Deferred** | `v1` uses `LIKE` via `_like_escape`/`_extract_like_terms` (already imported at `cli.py:54-61`), bounded by `project_id` and the partial index. Adding FTS later is `CREATE VIRTUAL TABLE IF NOT EXISTS` + three triggers — the exact additive shape already at `db.py:2346-2377` — with zero rewrite. |
| **`TicketClaim` / derived file surface** | **Deferred, and needs redesign first** | See §8.6 — the mechanism as specified is measurably incomplete on this deployment. |
| **Viewer/share export** | **Excluded, correctly, with no work** | `share.py` builds the snapshot from an allowlist and raises on unexpected tables (`share.py:565-580`); `tests/test_share_export.py:450` pins deny-by-default. Do **not** add ticket tables to `allowed_base_columns` unless tickets are meant to be publicly shareable. |

CLI registration: `tickets_app = typer.Typer(help="Inspect tickets and epics")` declared beside the other twelve and registered with `app.add_typer(tickets_app, name="tickets")` in the block at `cli.py:441-467`. Invocation is `python -m mcp_agent_mail.cli tickets …`; there is no `[project.scripts]` entry point, and the `mcp-agent-mail` binary on PATH is a stub that always exits 1.

---

## 7. Implementation slices

Dependency-ordered. Slices marked **‖** can run in parallel with their siblings. `pytestmark = pytest.mark.usefixtures("isolated_env")` at module level on **every** new test file (`tests/test_product_bus.py:13` precedent) — a test that forgets it writes to the operator's real `./storage.sqlite3` and `~/.mcp_agent_mail_git_mailbox_repo`, and the two most recent commits on this branch (`a70ab79`, `949cded`) are both one-line fixes for exactly that leak.

---

### Slice 1 — Schema and ledger · **Medium** · no dependencies

**Files.** `src/mcp_agent_mail/models.py` (append after line 1145) · `src/mcp_agent_mail/migrations/versions/20260831_1200_0002ticketing_ticketing.py` (new) · `tests/test_alembic_baseline.py:131` · `tests/test_database_failures.py:246-257` · `tests/test_ticket_schema.py` (new)

**Deliverable.** Four tables exist on every database shape; the ledger advances to `0002ticketing` without collision; nothing reads or writes them. Separately deployable and the **only** slice that can break production, so it ships alone.

**Tests.** Every CHECK exercised with a rejecting case **and** an accepting control — a closed ticket with no resolution refused while a closed ticket with one is accepted; a lowercase prefix refused while an uppercase one is accepted; `AM-12`, `bd-10s`, `br-abc.1`, `mcp_agent_mail-123` all accepted by `ck_tickets_key` while `../etc` and a leading dot are refused; `am-12` refused after `AM-12` exists (the `uq_tickets_key_nocase` control). A `sqlite_master` assertion that `idx_tickets_project_open` still carries its `WHERE closed_ts IS NULL` clause — a partial index that silently became total would pass every functional test. `tests/test_alembic_baseline.py:131` retargeted to the script-directory head. Table-presence assertions appended to `tests/test_database_failures.py`.

**Do not** hand-verify against a production copy: `tests/test_alembic_baseline.py:113-131` is the production-shaped exerciser and it fires (§4).

---

### Slice 2 — `tickets.py` service module · **Large** · depends on 1

**Files.** `src/mcp_agent_mail/tickets.py` (new, ~550 lines) · `tests/test_tickets_service.py` (new)

**Deliverable.** `TicketError(RuntimeError)` with a `Literal`-typed `.code`, frozen slotted result dataclasses, and `async def` functions taking an open `AsyncSession` — the `ui_access.py:62-81` shape line for line. Exports: `allocate_ticket_key`, `create_ticket`, `load_ticket`, `list_tickets`, `apply_ticket_update`, `set_ticket_link`, `record_ticket_event`, `ticket_to_dict`, the vocabulary constants and their normalizers, `priority_importance`. No MCP, HTTP or Typer import; **no mail**; **no `functools.lru_cache`** (three uncleared ones already exist at `app.py:1054`, `1279`, `5868` and `tests/conftest.py` resets only four named caches — an order-dependent cache is this repo's documented recurring defect class).

**Lint constraint that will bite.** `app.py:2` carries a file-level `# ruff: noqa: I001, A002`, which is the only reason 48 tools can declare a parameter named `format`. There are **no** `per-file-ignores` anywhere in `pyproject.toml`, `select` includes `A` and `PTH`, and `make check` runs lint before pytest. So `tickets.py` signatures use `ticket_key`, `ticket_id`, `kind`, `status_filter` — never `format`, `id`, `type` or `filter`. Same rule for the test file.

**Tests.** Key allocation under concurrency: N overlapping creators in one project yield N distinct keys, `next_seq` advances by exactly N, no gaps; a rolled-back transaction returns its number rather than burning it. Bootstrap on a project with no sequence row. Prefix derivation, the `2`…`9` collision suffix, and the bounded-loop exhaustion path asserted to *raise* rather than spin. Topic-namespace reservation: seed a message with `topic="AM-1"`, mint, assert the allocator skips to `AM-2`; control — with no such message it takes `AM-1`. The cross-row rules SQLite cannot express: a parent that is not an epic is refused; a `blocks` cycle is refused. Idempotent re-link via `IntegrityError` rollback. Closed↔open round-trip leaving `resolution_key` and `closed_ts` consistent in both directions. Every emitted timestamp asserted naive.

---

### Slice 3 — MCP read/write tools, no mail · **Medium** · depends on 2

**Files.** `src/mcp_agent_mail/app.py` (`CLUSTER_TICKETING` at :436; `core` profile at :463; four tool stacks inserted at :15954; directory cluster entry at :17059-17469; schema hints at :17494-17553) · `src/mcp_agent_mail/config.py:696-699` · `tests/test_ticketing_tools.py` (new)

**Deliverable.** `create_ticket`, `get_ticket`, `list_tickets`, `update_ticket` registered unconditionally and callable. Assignment changes the row and returns it; no notification behaviour yet.

**Tests.** Driven through `fastmcp.Client(build_mcp_server())`, reading `result.data` for object tools and `result.structured_content["result"]` for `list_tickets`. **`format="toon"` on `list_tickets` must succeed** — the one assertion that catches a `list[dict[str, Any]]` annotation. Project keys from `tests.keys.pkey()`, never `Path.cwd()`. Wrong `registration_token` refused for every tool. Cross-project isolation: an agent in project A cannot read or mutate a ticket in project B even though keys are globally unique. `_validate_limit` bounds. Tools present under `TOOLS_FILTER_PROFILE=core`, absent under `minimal`. Every new tool name appears in the tooling-directory payload (closing the one-way gap by hand, since no test does it for us).

---

### Slice 4 ‖ — Links · **Short** · depends on 3

**Files.** `src/mcp_agent_mail/app.py` (`link_ticket`; reverse-link reads in `get_ticket`) · `tests/test_ticketing_links.py` (new)

**Deliverable.** The cross-domain edge: `blocks`/`relates`/`duplicates` → ticket key, `decided_by` → message id, `touches` → file-reservation id. `get_ticket` resolves both directions into readable summaries.

**Tests.** Cycle refusal in the `blocks` graph, with the control that a non-cyclic chain is accepted. Re-linking is idempotent. A `decided_by` link to a message the calling agent cannot see is refused. A dangling `target_ref` (message purged) renders as `available: false` rather than raising — control test: purge the message via `purge_old_messages(dry_run=False)` and assert the link row survives and the ticket still loads.

> ⚠ Slices 4 and 5 both edit the same contiguous region of `app.py`. Assign them to one agent, or serialise the merge.

---

### Slice 5 ‖ — Mail integration · **Medium** · depends on 3

**Files.** `src/mcp_agent_mail/app.py` (`comment_ticket`; notification calls inside `create_ticket` and `update_ticket`) · `tests/test_ticketing_mail.py` (new)

**Deliverable.** `comment_ticket` sends through the tool-body delivery path with `topic=<key>`, `thread_id=<key>`, required `idempotency_key`, and contact policy applied as `send_message` applies it. `get_ticket` returns the discussion. Assignment mails the assignee at the priority-derived importance. `commented` events recorded with the message id. **DB-first ordering, session closed before every send.**

**Tests.** A comment lands in the assignee's `fetch_inbox` and in `fetch_topic(topic=<key>)`. A retried `comment_ticket` with the same `idempotency_key` reuses the delivery (`reused=True`); a *different* key with identical body creates a second message (the control that proves the key is not content-derived). The empty-recipient edge: a reporter commenting on their own unassigned ticket gets a recorded self-addressed comment, not `INVALID_ARGUMENT`. A blocked assignee leaves the ticket assigned with `notification.delivered=false` and `reason="CONTACT_BLOCKED"` — control: an unblocked assignee gets `delivered=true`. A ticket survives a delivery failure with its row intact and readable. **Timing control:** assert `comment_ticket` returns in well under `busy_timeout` — a regression that reintroduces holding the write transaction across the send would show as a 60-second stall, not a wrong answer.

---

### Slice 6a ‖ — Documentation reconciliation · **Quick** · no code dependency

**Files.** `AGENTS.md:239-277` · `README.md` (a `tickets` section + tool-table rows) · `SKILL.md` tool table

**Deliverable.** `AGENTS.md` split into **"Native tickets (this server)"** and **"External trackers (beads, Jira, Linear)"**, the beads mapping cheat-sheet preserved verbatim under the second. This removes the standing instruction (`AGENTS.md:275`) telling every agent — including auto-loading providers — not to use the feature. **Not optional and not follow-up.** No test enforces the README table, which is already ~13 tools behind; update it for humans.

---

### Slice 6b ‖ — CLI, read-only · **Short** · depends on 2

**Files.** `src/mcp_agent_mail/cli.py` (`tickets_app` at :441-467, banner comment, two commands) · `tests/test_cli_ticket_commands.py` (new)

**Deliverable.** `tickets list <project> [--status --kind --assignee --epic --include-closed --limit --json]` and `tickets show <ticket-key> [--json]`, both resolving the project through `_get_project_record` (`cli.py:1191`) and rendering with `rich.table.Table`.

**Tests.** `CliRunner().invoke(app, [...], env={"COLUMNS": "400"})` — mandatory, or substring assertions become wrap-dependent (`tests/test_cli.py:131-140`). Help-text contract asserting each subcommand and each option flag after ANSI stripping (`tests/test_cli_archive_commands.py:549-588` shape). `--json` failure emits exactly `{"error": "..."}` with exit code 1 (`tests/test_cli.py:1151-1170`). Assert no `tickets delete` / `hard-delete-ticket` command exists (`tests/test_cli.py:1174-1180` pins that irreversible hard-deletes must be absent; ticket removal is a transition to `closed` with `resolution_key='wontfix'`). New SQLAlchemy WHERE clauses need the `cast(ColumnElement[bool], …)` wrapper the module uses throughout (`cli.py:400-416`) or `make typecheck` fails.

---

### Slice 6c — e2e golden · **Short** · depends on 3, 4, 5

**Files.** `tests/e2e/test_isomorphism_e2e.py:1179-1186` · `tests/e2e/golden/isomorphism_e2e.json` · `.test_durations`

**Deliverable.** A `ticketing` phase in the isomorphism golden alongside `product_bus`, which set the precedent that a new entity domain gets one. `.test_durations` regenerated in the same PR (it is already stale: 275 of 2206 entries name tests that no longer exist).

**Tests.** Regenerate with `E2E_UPDATE=1` (`tests/e2e/test_isomorphism_e2e.py:1205`) and **diff the golden line by line before committing** — regeneration rewrites the whole file and would silently bless an unrelated regression.

---

### Slice 7 ‖ — `projects adopt` repointing · **Quick** · depends on 1

**Files.** `src/mcp_agent_mail/cli.py` (the adopt transaction around `cli.py:6524`) · `tests/test_cli.py` (extend the adopt cases)

**Deliverable.** `projects adopt` currently repoints `messages.project_id` (`cli.py:6524`) and knows nothing about tickets. Once tickets exist, an adopted project orphans them. Add `tickets` and `ticket_sequences` to the repoint, inside the same `BEGIN IMMEDIATE` window (`cli.py:6328-6332`), or refuse adoption when the source project has tickets — matching the existing immutable-delivery-history refusal at `cli.py:6358-6361` and its fail-before-touching test (`tests/test_cli.py:923`). **Decide which**; refusing is cheaper and consistent, repointing is friendlier. Recommend: **refuse**, with a message naming the ticket count, because a repointed `ticket_sequences` row could collide with the destination's prefix.

---

**Parallelism summary.** Slice 1 alone. Then 2. Then 3. Then {4, 5} serialised against each other on `app.py`, with {6a, 6b, 7} genuinely parallel to all of them. 6c last.

---

## 8. Open risks

**8.1 — Image rollback after the ledger advances.** *Uncertainty:* whether the fleet's restart protocol can carry a pre-flight stamp record and a re-stamp step. *Cheapest experiment:* on a scratch copy of a stamped database, apply the ticketing tree, then swap in the pre-ticketing `versions/` directory and run `ensure_schema` — confirm `CommandError`, then confirm `UPDATE alembic_version SET version_num='0001baseline'` restores startup. ~15 minutes; both halves already measured once, this re-confirms on the final revision file. **Never against `data/`.**

**8.2 — Topic-namespace collision rate.** *Uncertainty:* whether the allocator's skip loop will ever fire, and whether a derived prefix collides with an established topic family (`IRIS-`, `HERMES-`). *Cheapest experiment:* one read-only query — `SELECT substr(topic, 1, instr(topic,'-')-1) AS p, count(*) FROM messages WHERE topic LIKE '%-%' GROUP BY p ORDER BY 2 DESC;` — and compare against the prefix each of the four project slugs would derive. Minutes. If a collision exists, pass `key_prefix` explicitly on that project's first ticket.

**8.3 — `projects adopt`.** *Uncertainty:* refuse vs. repoint (slice 7). *Cheapest experiment:* read the adopt transaction at `cli.py:6320-6400` end to end and count how many tables it already repoints; if it repoints only `messages`, refuse.

**8.4 — Windows CI chunk placement.** *Uncertainty:* the Windows leg needs ~4.5 h against Ubuntu's ~19 min and is split 8 ways against a stale `.test_durations`; a slow ticketing test lands wherever it hashes with no local warning, against a 90-minute per-chunk kill. *Cheapest experiment:* run the new test files locally with `--durations=20` and add their entries to `.test_durations` in the same PR. Keep every new test free of wall-clock assertions, or gate with `skip_if_cpu_overloaded()` (`tests/conftest.py:31-42`).

**8.5 — SonarCloud new-code coverage.** *Uncertainty:* `sonar.coverage.exclusions` does **not** exclude `src/`, so untested ticketing code in `app.py` and `cli.py` counts against a blocking gate even when pytest is green. *Cheapest experiment:* none — mitigate by writing the tool-body and CLI tests **in** slices 3/5/6b rather than deferring them.

**8.6 — Whether the `TicketClaim` idea is salvageable at all.** Design 2's derived file surface is the best idea in any of the three proposals and it is **measurably incomplete on this deployment**: 1029 of 1307 production `file_reservations` have `execution_id IS NULL` (79%), and 57 of the most recent 100 still do; `AGENT_EXECUTION_ENFORCEMENT_MODE` defaults to `"observe"` (`config.py:827-832`) and `file_reservation_paths` explicitly accepts an execution-less claim with a warning (`app.py:15272-15277`); and 912 of 1323 executions are `kind='subagent'`, whose reservations a session-level claim could not see under a one-open-claim-per-execution index. It fails **silently** — an empty file list, never an error — which is the worst failure shape for a claim about who holds what. *Cheapest experiment before any redesign:* (a) re-run the coverage query restricted to the last 30 days and grouped by `kind`, to see whether coverage is improving; (b) set `AGENT_EXECUTION_ENFORCEMENT_MODE=enforce` in a scratch environment and observe what refuses. Only then decide between "also match `agent_id` within the claim window" and "declare a hard dependency on enforce mode". Note also that a `ticket_claims.execution_id` FK would put the new table inside the blast radius of `_migrate_agent_executions_schema`, which does `DROP TABLE agent_executions` (`db.py:2145`) followed by a **global** `PRAGMA foreign_key_check` that raises on any dangling FK anywhere (`db.py:2190-2194`).

**8.7 — Factoring contact policy to module scope.** *Uncertainty:* whether `send_message`'s policy branch (`app.py:10916-10925`) can be lifted out of the closure without touching `send_message` — it is the precondition for a CLI or HTTP write path. *Cheapest experiment:* read `app.py:10880-10960` and enumerate the closure variables it uses. Half an hour, and it determines whether the read-only CLI stays read-only.

**8.8 — Inbox pollution.** *Uncertainty:* whether ticket traffic in the default inbox becomes a real complaint before the exclusion filter exists (there is no negative topic predicate; `app.py:13061`, `13128`). *Cheapest experiment:* after slice 5 ships, count ticket-topic messages as a fraction of each agent's unread over a week. If it exceeds ~30%, add the `fetch_inbox` exclusion parameter (a read-side change, no stored data).

**8.9 — Module-global tool registry across test servers.** `TOOL_CLUSTER_MAP` / `TOOL_METADATA` are module-level globals mutated at decoration time and accumulate across every `build_mcp_server()` call in a session; `_apply_tool_filter` (`app.py:18899`) iterates the accumulated global rather than that server's own tool set. Not new, not caused by ticketing, but a ticketing filter-profile test is exactly where it would surface. *Cheapest experiment:* run the new profile assertions both first and last in the file and check for a difference.

---

## 9. Corrections — settled facts, do not re-litigate

### A. Claims from the circulated v1 proposal

| # | Claim | Verdict and evidence |
|---|---|---|
| A1 | "Two entities: Epic and Ticket." | **Refuted structurally.** One table with `kind_key`+`parent_id`. Two tables would have duplicated links, events, filters and every authorization predicate, and made a third level a third table. `create_epic` is deleted from the surface. |
| A2 | `status: open \| in_progress \| blocked \| review \| closed` as a stored column. | **Refuted twice.** `blocked` is derivable from an unresolved `blocks` edge and would be a second source of truth; `review` is a stage this fleet performs by mail. And a *membership* CHECK on the domain's largest table is the `batch_alter_table` rewrite trap (`migrations/env.py:45`, `script.py.mako:7-8`). v1 stores a shape-checked string with membership in `tickets.py`. |
| A3 | `TicketLink(ticket_id, relation, target_kind, target_id)` polymorphic with `target_id`. | **Kept in spirit, refuted in shape.** Three targets of three shapes cannot share one integer FK; the "exactly one of three nullable FKs" alternative forces `ondelete="CASCADE"` on the message column, which deletes the whole edge rather than degrading the pointer. v1 uses a uniform TEXT `target_ref` with no FK, on the `UiAccessAuditEvent` precedent (`models.py:1056-1127`). |
| A4 | `relation: blocks \| relates \| duplicates \| fixes` | `fixes` absorbed; v1 vocabulary is `blocks \| relates \| duplicates \| decided_by \| touches`, extensible without a migration because it is shape-checked only. |
| A5 | P3 option "MAX(seq)+1 in a transaction". | **Refuted.** Correct only while no ticket is ever deleted, and it cannot express a prefix. Rowid/number reuse is a defect this repo has paid for twice (`models.py:560-562`, `180-187`). v1 uses a stored counter. |
| A6 | Implicit: a second Alembic revision containing `op.create_table`. | **Refuted — production outage.** `ensure_schema` runs `create_all` (`db.py:1422`) **before** `alembic upgrade head` (`db.py:1445-1448` → `db.py:1386`), and production is stamped `0001baseline`. Unguarded, every restart raises `table tickets already exists`, unmatched by `_is_lock_error` (`db.py:218-230`). |
| A7 | "P4: v1 = MCP only" implying no other surface is needed. | **Confirmed for HTTP/UI, incomplete otherwise.** `/api/` is an ASGI mount of the MCP app (`http.py:6510-6528`) authenticated by the static bearer, so MCP tools yield **no** browser-usable endpoint. But without a shared service module the CLI must reimplement everything — the same FTS query already exists three times (`app.py:16705`, `cli.py:1516`, `http.py:8257`). v1 adds `tickets.py`. |
| A8 | Nine tools. | Six. `create_epic` → `create_ticket(kind="epic")`; `assign_ticket` and `close_ticket` → `update_ticket`. |
| A9 | P1 default assumption (a), beads stays the fleet's tool. | **Confirmed, with a blocker the proposal missed:** `AGENTS.md:246` and `AGENTS.md:275` currently instruct every agent not to create or manage tasks in Mail. The rewrite is part of the release. |
| A10 | Beads facts implied by the proposal. | **Corrected by measurement of `.beads/issues.jsonl`:** 167 records, of which **only 4 are open** (110 closed, 53 tombstone). There is **no `blocked_by` field** — the dependency field is `dependencies` (45 records); `labels` appears on 3, `external_ref` on 5. The dominant id prefix is **`mcp_agent_mail-` (136)**, not `bd-` (31). Any future importer must be written against these names, not against the ones the designs assumed. |

### B. Claims from the rival designs

| # | Claim | Verdict and evidence |
|---|---|---|
| B1 | *(Designs 1, 2)* "`_deliver_message` is a closure and mail therefore **cannot** be sent outside `build_mcp_server()`" — used as the sole reason for a read-only CLI. | **Refuted.** The closure is real (`app.py:8312` under `app.py:7370`), but `accept_message_delivery` (`delivery.py:1057`), `process_message_delivery` (`delivery.py:1867`) and `emit_published_delivery_notifications` (`delivery.py:1544`) are module-level, and `http.py:8482-8483` already delivers a complete archived message from outside `app.py`. The real blocker is that **contact policy lives in the tool bodies** — `_deliver_message` (`app.py:8312-8486`) contains no `contact_policy` reference; enforcement is at `app.py:10916-10925`. The read-only CLI stands; the reason changes, and so does the remedy (§8.7). |
| B2 | *(Design 2)* "`assign_ticket` inherits contact policy verbatim through `_deliver_message`; you cannot assign work to an agent that will not take your mail." | **Refuted.** `_deliver_message` enforces nothing. v1 applies the policy explicitly in the tool body for `comment_ticket`, and reports a blocked assignment notification rather than refusing the assignment. |
| B3 | *(Design 2)* "`assignee_agent_id` is written **only** if the delivery is accepted — fail-closed and atomic." *(Design 3)* "hold `get_immediate_session` across the whole decision", applied to `comment_ticket`. | **Refuted — deadlock.** `accept_message_delivery` opens its **own** `get_immediate_session` (`delivery.py:1070`) on a fresh pooled connection (`db.py:735-738`); holding a ticket write transaction across it blocks database-wide for `busy_timeout=60000` (`db.py:469`), and `_is_lock_error` matches bare `"locked"` so `retry_on_db_lock` re-runs the whole body and repeats the stall. Every existing `_deliver_message` call site has no session open; `force_release_file_reservation` commits and exits at `app.py:15648` before mailing at `app.py:15734`. **v1 is DB-first, session closed before every send.** |
| B4 | *(Design 3)* "`purge_old_messages` issues an unqualified `delete(Message)` on age, so a ticket's discussion silently evaporates at the retention horizon" — its self-declared *decisive* reason for a `ticket_comments` table. | **Refuted.** `dry_run: bool = True` by default (`app.py:11719`); the predicate is `project_id` + `created_ts < cutoff` + `id NOT IN (pending reply targets)` (`app.py:11737-11741`); no scheduler, no CLI command, and the archive is never trimmed. Design 3's **other two** reasons — commit-lock throughput and the immutability of a delivered message — are confirmed and are the ones on record. |
| B5 | *(Design 3)* Per-project key uniqueness (`uq_tickets_project_key`) while arguing keys are cross-references. | **Refuted as the worst reversibility choice in any design.** Global→per-project is a free relaxation; per-project→global requires renaming already-frozen keys. There is **no content-mutation path for `messages` anywhere in `src/`** — the only `update(Message)` sites are `app.py:11753` (nulls `reply_to`) and `cli.py:6524` (repoints `project_id`) — and archive documents embed `topic` verbatim (`delivery.py:544-556`). v1 is globally unique, case-insensitively. |
| B6 | *(Designs 2, 3; Design 1 most strongly)* "The test suite **cannot** catch the create_all/upgrade collision; a green suite is not evidence; hand-verify against a production copy / write a new already-tracked fixture." | **Refuted.** `tests/test_alembic_baseline.py:108-131` does not use `isolated_env`; it builds its own DB via `_build_schema` (`ensure_schema()` at line 104), drops `alembic_version` at line 125, and re-runs at line 130 against a database that already contains the new tables. With an unguarded revision it **errors at line 130**. The work is **one changed assertion at line 131**, not a new slice. |
| B7 | *(All three)* "`downgrade()` is the rollback; slice 1 is independently revertible." | **Refuted.** `alembic_command` appears in `src/` at exactly `db.py:1385` and `db.py:1386`; there is no `alembic` invocation in any deploy surface. Reverting the image after `0002ticketing` is stamped raises `CommandError: Can't locate revision identified by '0002ticketing'` on every entry point, forever (measured), recoverable only by a manual `UPDATE alembic_version`. §4 carries the runbook. |
| B8 | *(All three)* The revision must reproduce the full DDL (`op.create_table("tickets", …)  # full column/CHECK/index set`). | **Refuted.** Once guarded, the body is unreachable on fresh, already-tracked and pre-Alembic databases alike (measured on all three), because `create_all` always ran first. That is untestable duplicate DDL guaranteed to drift. v1 drives it from `SQLModel.metadata`. |
| B9 | *(Design 3, Design 2)* "Every table-creating revision forever must be a no-op when the table is present." | **Correct for tables, dangerous if generalised to indexes.** `create_all(checkfirst=True)` skips an **existing** table wholesale and will not add a model-declared `Index` to it — `db.py:2559-2593` is thirteen hand-written `CREATE INDEX IF NOT EXISTS` statements proving it. Design 1's separately-guarded index loop is the shape the rule must take, and v1 adopts it. |
| B10 | *(Design 2)* "Inbox pollution is already solved: `fetch_inbox(topic=...)` separates ticket traffic without a single new line of filtering code." | **Refuted.** `topic` is an **inclusive** filter — *"filter to messages with this topic tag"* (`app.py:13061`), passed at `app.py:13128`; there is no negative predicate anywhere. An agent can ask for *only* ticket traffic, never for its inbox *minus* it. Priced as a follow-up (§8.8). |
| B11 | *(Design 2)* `TicketClaim` — "every reservation opened during the claim window is unambiguously work on that ticket… no drift possible." | **Refuted by this deployment's own data.** `FileReservation.execution_id` is nullable ("Legacy reservations remain nullable", `models.py:867-868`); 1029/1307 production reservations have it NULL, 57 of the most recent 100 still do; `AGENT_EXECUTION_ENFORCEMENT_MODE` defaults to `"observe"` (`config.py:827-832`) and `app.py:15272-15277` accepts execution-less claims; 912/1323 executions are subagents. The derivation would silently under-report, with an empty list and no error. Deferred pending redesign (§8.6). |
| B12 | *(Design 2)* "Skipping the beads importer keeps the CHECK constraint free to be ours." | **Refuted by its own schema.** Design 2's `_TICKET_KEY_SHAPE_SQL` requires `upper(key)=key`, `NOT GLOB '*[^A-Z0-9-]*'` and a trailing digit — which rejects `bd-10s` and `br-abc.1`, the only real ids in the export. v1 takes Design 3's superset breadth instead, plus case-insensitive uniqueness rather than a case pin, so mixed-case imported ids remain admissible. |
| B13 | *(Design 2)* `TicketLink.target_message_id` with `ondelete="CASCADE"` described as "losing the precise pointer is acceptable". | **Refuted.** CASCADE deletes the whole edge row, not the pointer. `SET NULL` would match the prose but would violate Design 2's own exactly-one-target CHECK. Moot in v1 (TEXT `target_ref`, no FK). |
| B14 | *(Design 1)* "`link_ticket` needs no immediate session at all — the composite primary key makes re-linking idempotent" while also performing a cycle-reachability walk before insert. | **Refuted twice.** A read-then-write is exactly the `#129` "missed conflicts before an insert" case (`db.py:709-715`), so `link_ticket` **must** take `get_immediate_session`. And a duplicate insert raises `IntegrityError`, which poisons the transaction and is not an `OperationalError` (`db.py:294`); the house idiom is explicit `except IntegrityError: rollback` + re-read (`app.py:4162-4172`). |
| B15 | *(Design 1)* `comment_ticket` idempotency via `_internal_delivery_idempotency_key('ticket_comment', {...})`. | **Refuted.** It hashes content, so two genuinely distinct comments with identical text collide and `delivery.py:1072-1076` returns `reused=True` — the second comment is swallowed with no error. v1 requires an explicit `idempotency_key`, as `send_message` does (`app.py:10383`). |
| B16 | *(Design 1)* P2 answer omits retention entirely, calling Message reuse "zero persistence cost". | **Partially refuted, then re-founded.** Retention is real but manual and unscheduled (B4), so the risk is smaller than Design 3 claimed and larger than Design 1 implied. v1 answers it with `ticket_events` rather than a comment table. |
| B17 | *(Design 3)* "A separate `ticket_key_sequences` table keeps ticket creation from contending with project writes." | **Refuted.** `BEGIN IMMEDIATE` takes a **database-wide** RESERVED lock (`db.py:705-712`, `727-731`). The separate table is still correct — for the not-ALTERing-a-live-table reason — but the contention argument is false. |
| B18 | *(Designs 2, 3)* Key allocation as "a single `UPDATE … RETURNING`". | **Incomplete.** On a project's first ticket the row does not exist, the UPDATE matches zero rows and `RETURNING` yields nothing (verified). A bootstrap INSERT in the same transaction is mandatory. The `RETURNING next_seq - 1` arithmetic itself is **confirmed** on SQLite 3.46.1. |
| B19 | *(Design 3, uniquely correct — adopted)* "A migration's `upgrade()` body never runs on a fresh install, so a data seed there would execute on production and on no developer machine." | **Confirmed** (`db.py:1385` stamps `head` when `was_fresh`). Now a standing rule: **no data seed may ever live in a revision body.** Designs 1 and 2 both missed this. |
| B20 | *(Design 1, uniquely correct — adopted)* "A new `tickets.py` inherits none of `app.py`'s lint waivers." | **Confirmed.** `app.py:2` carries `# ruff: noqa: I001, A002`; `pyproject.toml` has **no** `per-file-ignores`; `select` includes `A` and `PTH`; `make check` runs lint before pytest. A service function argument named `format`/`id`/`type` fails the gate before any test runs. |

### C. Citation corrections

| # | Correction |
|---|---|
| C1 | `idx_messages_project_topic` is at **`models.py:533`**, not 527 (Design 1 cited 527 twice; Design 2 cited 533 correctly). |
| C2 | `MessageRecipient.read_ts` / `ack_ts` are at **`models.py:523-524`**, not 524-525. |
| C3 | `FileReservation.reason` is at **`models.py:878`**; `models.py:877` is `exclusive`. |
| C4 | Design 1's "keys are globally unique, so `get_ticket` and `link_ticket` accept a bare key with no project argument" contradicts its own tool signatures, which all take `project_key`. v1 keeps `project_key` on every tool (it is the auth and RBAC scope) and uses the global key only for cross-project link targets. |
| C5 | Production being stamped `0001baseline` does **not** prove it took the pre-existing path: because baseline == head today, a *fresh* database stamped at `"head"` also records `0001baseline`. It is `already_tracked` simply because a version row exists — which is what makes the unconditional `upgrade` at `db.py:1386` run. The conclusion is unchanged; the mechanism is not what a reader reasoning from the stamp value alone would infer. |