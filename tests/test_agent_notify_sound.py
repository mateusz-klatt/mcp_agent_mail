"""A tone an agent picks for itself, and the reasons it is a closed set.

The operator asked for two things alongside the hard identifier: a preferred
name and a preferred sound. The name landed earlier; this is the sound.

The vocabulary is closed on purpose, and these tests pin that rather than the
particular five values — a URL here would let any agent point the operator's
browser at a host of its choosing, and a raw frequency invites values that are
silence or pain rather than notification.
"""

from __future__ import annotations

import contextlib

import pytest
from fastmcp import Client

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import NOTIFY_SOUND_NAMES, build_mcp_server

KEY = "/test/sound"
AGENT_NAME = "codex-linux-sound-1"


def _data(result):
    return getattr(result, "data", None) or getattr(result, "structured_content", {})


@pytest.fixture
def server(isolated_env, monkeypatch):
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    return build_mcp_server()


async def _register(client, name: str = AGENT_NAME):
    await client.call_tool("ensure_project", {"human_key": KEY})
    me = _data(
        await client.call_tool(
            "register_agent",
            {"project_key": KEY, "name": name, "program": "probe", "model": "probe"},
        )
    )
    return me["registration_token"]


async def _register_payload(client, project_key: str, name: str):
    await client.call_tool("ensure_project", {"human_key": project_key})
    return _data(
        await client.call_tool(
            "register_agent",
            {
                "project_key": project_key,
                "name": name,
                "program": "probe",
                "model": "probe",
            },
        )
    )


@pytest.mark.asyncio
async def test_new_agents_get_dense_project_local_sounds(server):
    """Global sparse ids must not make colleagues collide in a fresh project."""
    other_key = "/test/sound-other"
    async with Client(server) as client:
        first = await _register_payload(client, KEY, "codex-linux-sound-1")
        for slot in range(1, 6):
            await _register_payload(
                client,
                other_key,
                f"claude-linux-other-{slot}",
            )
        sounds = [first["notify_sound"]]
        for slot in range(2, 14):
            created = await _register_payload(
                client,
                KEY,
                f"codex-linux-sound-{slot}",
            )
            sounds.append(created["notify_sound"])

    assert sounds == [*NOTIFY_SOUND_NAMES, NOTIFY_SOUND_NAMES[0]]
    assert len(set(sounds[:12])) == 12


@pytest.mark.asyncio
async def test_create_agent_identity_uses_the_same_sound_assignment(server):
    project_key = "/test/sound-create"
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        first = _data(
            await client.call_tool(
                "create_agent_identity",
                {
                    "project_key": project_key,
                    "name_hint": "codex-linux-create-1",
                    "program": "probe",
                    "model": "probe",
                },
            )
        )
        second = _data(
            await client.call_tool(
                "create_agent_identity",
                {
                    "project_key": project_key,
                    "name_hint": "codex-linux-create-2",
                    "program": "probe",
                    "model": "probe",
                },
            )
        )

    assert first["notify_sound"] == NOTIFY_SOUND_NAMES[0]
    assert second["notify_sound"] == NOTIFY_SOUND_NAMES[1]


@pytest.mark.asyncio
async def test_an_agent_can_choose_and_clear_its_tone(server):
    async with Client(server) as client:
        token = await _register(client)

        out = _data(
            await client.call_tool(
                "set_agent_notify_sound",
                {"project_key": KEY, "agent_name": AGENT_NAME,
                 "notify_sound": "high", "registration_token": token},
            )
        )
        assert out["notify_sound"] == "high"
        # The whole vocabulary comes back, so a caller never has to guess.
        assert "chime" in out["available"] and "high" in out["available"]

        cleared = _data(
            await client.call_tool(
                "set_agent_notify_sound",
                {"project_key": KEY, "agent_name": AGENT_NAME,
                 "notify_sound": "", "registration_token": token},
            )
        )
        assert cleared["notify_sound"] is None, "empty string must clear, not store"


@pytest.mark.asyncio
async def test_an_unknown_tone_is_refused_out_loud(server):
    """Silently ignoring it would be worse than refusing.

    A tone that never plays is indistinguishable from a viewer with sound
    switched off, so an agent that set a typo would have no way to learn which
    of the two it was looking at.
    """
    async with Client(server) as client:
        token = await _register(client)
        with pytest.raises(Exception) as excinfo:
            await client.call_tool(
                "set_agent_notify_sound",
                {"project_key": KEY, "agent_name": AGENT_NAME,
                 "notify_sound": "airhorn", "registration_token": token},
            )
        assert "airhorn" in str(excinfo.value)
        # The message must carry the valid set, or the next attempt is another guess.
        assert "chime" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_url_is_refused_like_any_other_unknown_value(server):
    """The reason the set is closed, pinned as a test rather than a comment.

    This is the case that motivated the design: a URL would turn a preference
    into a request to a host the operator never chose, made by their browser,
    every time a message arrives.
    """
    async with Client(server) as client:
        token = await _register(client)
        with pytest.raises(Exception) as excinfo:
            await client.call_tool(
                "set_agent_notify_sound",
                {"project_key": KEY, "agent_name": AGENT_NAME,
                 "notify_sound": "https://example.invalid/ping.mp3",
                 "registration_token": token},
            )
        # Assert it was refused FOR BEING UNKNOWN, not merely that something
        # raised. The first version of this test asserted `raises(Exception)`
        # and passed while the code was throwing from a broken constructor —
        # green for a reason that had nothing to do with the URL.
        assert "example.invalid" in str(excinfo.value)
        assert "chime" in str(excinfo.value)


@pytest.mark.asyncio
async def test_the_tone_is_not_a_credential_and_not_an_identity(server):
    """It rides on the agent payload, and changes nothing else about it."""
    async with Client(server) as client:
        token = await _register(client)
        await client.call_tool(
            "set_agent_notify_sound",
            {"project_key": KEY, "agent_name": AGENT_NAME,
             "notify_sound": "soft", "registration_token": token},
        )
        who = _data(
            await client.call_tool(
                "whois",
                {"project_key": KEY, "agent_name": AGENT_NAME,
                 "registration_token": token, "include_recent_commits": False},
            )
        )
        payload = who.get("agent", who)
        assert payload.get("notify_sound") == "soft"
        assert payload.get("name") == AGENT_NAME, "the address must be untouched"
