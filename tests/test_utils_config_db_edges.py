"""Edge cases in the two thinnest layers: environment parsing and database wiring.

The configuration half pins one distinction and nothing else. A variable that is
absent, or present but blank, is *not configured*: the compile-time default
applies and start-up continues, which is what lets a fresh deployment boot with
no ``.env`` at all. A variable holding a non-empty value that will not parse is
an operator mistake instead, and it has to stop start-up with the variable's own
name in the message -- folding it into the default would run the server with the
opposite of the setting its operator believed they had written.

The database half pins the SQLite-only policy (refused early, and refused without
repeating the credential the URL carried) and the filename arithmetic that the
backup/restore path depends on.
"""

from __future__ import annotations

import asyncio
import pathlib

from pytest import approx, mark, raises

from mcp_agent_mail import config as config_module, db as db_module, utils as utils_module

# Planted in every rejected database URL. The refusal has to name the backend
# and nothing else: the URL it was handed routinely carries a password, and an
# exception that quotes it turns a configuration mistake into a credential in
# the terminal scrollback and in whatever ships the logs.
URL_SECRET = "hunter2-never-print-me"


def _settings_from(monkeypatch, **environment: str):
    """Settings loaded fresh after ``environment`` is installed over the real one."""
    for variable, written in environment.items():
        monkeypatch.setenv(variable, written)
    config_module.clear_settings_cache()
    return config_module.get_settings()


def _refusal_for(monkeypatch, variable: str, written: str) -> str:
    """The ConfigError text produced when ``variable`` holds an unparseable value."""
    monkeypatch.setenv(variable, written)
    config_module.clear_settings_cache()
    with raises(config_module.ConfigError) as refusal:
        config_module.get_settings()
    return str(refusal.value)


def test_slugify_folds_punctuation_and_names_the_nameless():
    assert utils_module.slugify("  Hello World!!  ") == "hello-world"
    assert utils_module.slugify("") == "project"


def test_agent_name_sanitiser_keeps_word_characters_and_refuses_pure_punctuation():
    assert utils_module.sanitize_agent_name(" A!@#$ ") == "A"
    assert utils_module.sanitize_agent_name("!!!") is None


def test_reader_roles_split_on_commas_with_empty_positions_dropped(monkeypatch):
    settings = _settings_from(
        monkeypatch,
        HTTP_RBAC_READER_ROLES="reader, ro ,, read ",
        HTTP_RATE_LIMIT_ENABLED="true",
    )
    roles = list(settings.http.rbac_reader_roles)
    assert {"reader", "ro", "read"}.issubset(roles)
    assert "" not in roles
    assert settings.http.rate_limit_enabled is True


def test_explicit_values_reach_settings_in_their_typed_form(monkeypatch):
    settings = _settings_from(
        monkeypatch,
        HTTP_PORT="9999",
        HTTP_RATE_LIMIT_ENABLED="yes",
        LLM_TEMPERATURE="0.7",
        HTTP_RATE_LIMIT_BACKEND="redis",
    )
    assert settings.http.port == 9999
    assert settings.http.rate_limit_enabled is True
    assert settings.llm.temperature == approx(0.7)
    assert settings.http.rate_limit_backend == "redis"


def test_a_blank_value_means_unconfigured_rather_than_wrong(monkeypatch):
    settings = _settings_from(
        monkeypatch,
        HTTP_PORT="",
        HTTP_RATE_LIMIT_ENABLED="",
        DATABASE_POOL_SIZE="",
    )
    assert settings.http.port == 8765
    assert settings.http.rate_limit_enabled is False
    assert settings.database.pool_size == 50


def test_an_absent_variable_takes_the_compiled_default(monkeypatch):
    monkeypatch.delenv("DATABASE_POOL_SIZE", raising=False)
    config_module.clear_settings_cache()
    assert config_module.get_settings().database.pool_size == 50


# (variable, what the operator typed, anything else the message must carry)
UNPARSEABLE_SETTINGS = (
    ("HTTP_PORT", "not-a-number", ()),
    ("HTTP_RATE_LIMIT_ENABLED", "maybe", ()),
    ("LLM_TEMPERATURE", "hot", ()),
    ("DATABASE_POOL_SIZE", "lots", ()),
    ("HTTP_RATE_LIMIT_BACKEND", "cassandra", ()),
    # A near-miss on a closed vocabulary is unguessable from a bare rejection,
    # so this one also has to print the vocabulary.
    ("TOOLS_FILTER_MODE", "bogus", ("include",)),
)


@mark.parametrize(("variable", "written", "also_reported"), UNPARSEABLE_SETTINGS)
def test_an_unparseable_value_stops_startup_naming_its_variable(monkeypatch, variable, written, also_reported):
    message = _refusal_for(monkeypatch, variable, written)
    assert variable in message
    assert repr(written) in message
    for token in also_reported:
        assert token in message


PORT_VARIABLE = "HTTP_PORT"


def _dotenv_is_the_only_source_of_port(monkeypatch, directory, port: int) -> None:
    """Leave ``directory``'s ``.env`` as the one place a port can be read from."""
    (directory / ".env").write_text(f"{PORT_VARIABLE}={port}\n", encoding="utf-8")
    monkeypatch.delenv(PORT_VARIABLE, raising=False)
    monkeypatch.chdir(directory)


def test_settings_are_cached_until_the_cache_is_cleared(tmp_path, monkeypatch):
    _dotenv_is_the_only_source_of_port(monkeypatch, tmp_path, 1111)
    config_module.clear_settings_cache()
    assert config_module.get_settings().http.port == 1111

    _dotenv_is_the_only_source_of_port(monkeypatch, tmp_path, 2222)
    # Still the first reading: the snapshot is taken once, and an edit on disk
    # stays invisible until something says otherwise.
    assert config_module.get_settings().http.port == 1111
    config_module.get_settings.cache_clear()
    assert config_module.get_settings().http.port == 2222


SQLITE_URLS = (
    "sqlite+aiosqlite:///:memory:",
    "sqlite+aiosqlite:////var/lib/mailbox/storage.sqlite3",
    "sqlite:///:memory:",
    # SQLAlchemy keeps whatever casing the environment used, so the guard has
    # to case-fold before it compares.
    "SQLite+aiosqlite:///:memory:",
)


@mark.parametrize("database_url", SQLITE_URLS)
def test_every_sqlite_spelling_passes_the_backend_guard(database_url):
    # Returning at all is the assertion: this guard speaks only by raising.
    assert db_module._assert_supported_backend(database_url) is None


UNREADABLE_URLS = ("", "this is not a url")


@mark.parametrize("database_url", UNREADABLE_URLS)
def test_a_string_that_is_not_a_url_is_left_for_the_engine_to_report(database_url):
    # Backend policy has nothing to say about a value it cannot parse, and two
    # components reporting the same mistake with different words is worse than
    # one reporting it late.
    assert db_module._assert_supported_backend(database_url) is None


FOREIGN_URLS = (
    (f"postgresql+asyncpg://mcp:{URL_SECRET}@example.invalid:5432/mail", "postgresql"),
    (f"POSTGRESQL+asyncpg://mcp:{URL_SECRET}@example.invalid:5432/mail", "postgresql"),
    (f"mysql+aiomysql://mcp:{URL_SECRET}@example.invalid/mail", "mysql"),
)


@mark.parametrize(("database_url", "backend"), FOREIGN_URLS)
def test_a_backend_we_do_not_support_is_refused_without_echoing_the_url(database_url, backend):
    with raises(db_module.UnsupportedDatabaseBackendError) as refusal:
        db_module._assert_supported_backend(database_url)
    message = str(refusal.value)
    assert backend in message.lower()
    assert "SQLite" in message
    assert "142" in message  # the tracking issue, so the operator can follow it
    assert URL_SECRET not in message
    assert "example.invalid" not in message


def test_engine_init_refuses_postgres_before_it_reaches_the_schema(isolated_env, monkeypatch):
    # Without this the failure surfaces inside ``CREATE VIRTUAL TABLE ... fts5``,
    # far from the setting that caused it.
    monkeypatch.setenv("DATABASE_URL", f"postgresql+asyncpg://mcp:{URL_SECRET}@example.invalid:5432/mail")
    config_module.clear_settings_cache()
    db_module.reset_database_state()
    with raises(db_module.UnsupportedDatabaseBackendError):
        db_module.init_engine()


def test_engine_is_rebuilt_lazily_after_a_reset(isolated_env):
    db_module.reset_database_state()
    assert db_module.get_engine() is not None
    asyncio.run(db_module.ensure_schema())


def test_sidecar_and_snapshot_names_append_to_the_whole_filename():
    live = pathlib.Path("/var/lib/mailbox/mail.db")

    write_ahead_log, shared_memory = db_module.get_sqlite_sidecar_paths(live)
    assert write_ahead_log == pathlib.Path("/var/lib/mailbox/mail.db-wal")
    assert shared_memory == pathlib.Path("/var/lib/mailbox/mail.db-shm")

    parked = db_module.get_sqlite_pre_restore_path(live)
    assert parked == pathlib.Path("/var/lib/mailbox/mail.db.pre-restore")
    # Both sets exist at once during a restore, which deletes the stale snapshot
    # sidecars; were the two naming rules to collide, that cleanup would take the
    # freshly restored write-ahead log with it.
    assert set(db_module.get_sqlite_sidecar_paths(parked)).isdisjoint({write_ahead_log, shared_memory})
