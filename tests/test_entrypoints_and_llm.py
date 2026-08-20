from __future__ import annotations

import asyncio
import contextlib
import runpy
from pathlib import Path
from typing import Any

from mcp_agent_mail import config as _config


def test_main_module_dispatch(monkeypatch):
    # __main__.main() dispatches straight to the Typer app on the current argv
    # (GH#146: it previously force-rewrote argv to ['--help'], hiding every
    # subcommand). With argv = ['<prog>', '--help'] Typer prints help and exits
    # with code 0, which surfaces as SystemExit(0) — that *is* the correct CLI
    # contract; we just pin the exit code so future regressions don't silently
    # swap it for a crash.
    import sys as _sys

    import pytest

    import mcp_agent_mail.__main__ as entry
    from mcp_agent_mail.cli import app as real_app

    monkeypatch.setattr(_sys, "argv", ["mcp-agent-mail", "--help"])
    monkeypatch.setattr(entry, "app", real_app)
    with pytest.raises(SystemExit) as exc_info:
        entry.main()
    assert exc_info.value.code == 0


def test_http_module_main_invokes_uvicorn(isolated_env, monkeypatch):
    # Verify http.main uses settings + argparse defaults to run uvicorn
    from mcp_agent_mail import http as http_mod

    monkeypatch.setenv("HTTP_FORWARDED_ALLOW_IPS", "127.0.0.1")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    captured: dict[str, Any] = {}

    def fake_run(
        app,
        host,
        port,
        log_level="info",
        access_log=True,
        forwarded_allow_ips="127.0.0.1",
    ):
        captured["host"] = host
        captured["port"] = port
        captured["log_level"] = log_level
        captured["access_log"] = access_log
        captured["forwarded_allow_ips"] = forwarded_allow_ips

    monkeypatch.setattr("uvicorn.run", fake_run)
    # Simulate no CLI args beyond program name
    monkeypatch.setattr(http_mod, "get_settings", _config.get_settings)
    monkeypatch.setattr("sys.argv", ["mcp-agent-mail-http"])
    http_mod.main()
    assert captured["host"] == _config.get_settings().http.host
    assert captured["port"] == _config.get_settings().http.port
    assert captured["access_log"] is False
    assert captured["forwarded_allow_ips"] == "127.0.0.1"


def test_shipped_service_commands_do_not_log_oauth_callback_queries() -> None:
    root = Path(__file__).resolve().parents[1]
    systemd_unit = (root / "deploy/systemd/mcp-agent-mail.service").read_text(
        encoding="utf-8"
    )
    assert "mcp_agent_mail.http:create_app --factory" in systemd_unit
    assert "--no-access-log" in systemd_unit
    assert "mcp_agent_mail.http:build_http_app --factory" not in systemd_unit

    gunicorn_config = runpy.run_path(str(root / "deploy/gunicorn.conf.py"))
    assert gunicorn_config["workers"] == 1
    assert gunicorn_config["worker_class"] == "uvicorn_worker.UvicornWorker"
    assert gunicorn_config["accesslog"] is None


def test_llm_env_bridge_and_callbacks(monkeypatch):
    # Ensure provider envs are bridged and success callback can be installed
    from mcp_agent_mail import llm as llm_mod

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    # Force cost logging on
    monkeypatch.setenv("LLM_COST_LOGGING_ENABLED", "true")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    # Bridge provider envs
    llm_mod._bridge_provider_env()
    import os

    assert os.environ.get("GOOGLE_API_KEY") == "g-key"

    # Stub litellm behaviors to avoid network and heavy imports
    class _StubResp:
        def __init__(self) -> None:
            self.model = "stub-model"
            self.provider = "stub"
            self.choices = [{"message": {"content": "ok"}}]

    class _StubRouter:
        def completion(self, **kwargs):
            # Emulate LiteLLM Router interface
            return _StubResp()

    import litellm as litellm_pkg

    # Install stubs and capture success_callback list
    monkeypatch.setattr(litellm_pkg, "Router", _StubRouter)
    monkeypatch.setattr(litellm_pkg, "enable_cache", lambda **_: None)
    monkeypatch.setattr(litellm_pkg, "completion", lambda **_: _StubResp())
    # Ensure attribute exists for callbacks
    monkeypatch.setattr(litellm_pkg, "success_callback", [], raising=False)

    # Running a simple completion should succeed and return normalized output
    out = asyncio.run(llm_mod.complete_system_user("sys", "user"))
    # content may vary by stub path; assert at least model populated
    assert isinstance(out.model, str) and len(out.model) > 0
