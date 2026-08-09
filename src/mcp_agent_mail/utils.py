"""Utility helpers for the MCP Agent Mail service."""

from __future__ import annotations

import importlib
import os
import random
import re
from typing import Any, Iterable, Optional, cast

# Agent name word lists - used to generate memorable adjective+noun combinations
# These lists are designed to provide a large namespace (62 x 69 = 4278 combinations)
# while keeping names easy to remember, spell, and distinguish.
#
# Design principles:
# - All words are capitalized for consistent CamelCase output (e.g., "GreenLake")
# - Adjectives are colors, weather, materials, and nature-themed descriptors
# - Nouns are nature, geography, animals, and simple objects
# - No offensive, controversial, or confusing words
# - No words that could be easily misspelled or confused with each other

ADJECTIVES: Iterable[str] = (
    # Colors (original + expanded)
    "Red",
    "Orange",
    "Pink",
    "Black",
    "Purple",
    "Blue",
    "Brown",
    "White",
    "Green",
    "Chartreuse",
    "Lilac",
    "Fuchsia",
    "Azure",
    "Amber",
    "Coral",
    "Crimson",
    "Cyan",
    "Gold",
    "Gray",
    "Indigo",
    "Ivory",
    "Jade",
    "Lavender",
    "Magenta",
    "Maroon",
    "Navy",
    "Olive",
    "Pearl",
    "Rose",
    "Ruby",
    "Sage",
    "Scarlet",
    "Silver",
    "Teal",
    "Topaz",
    "Violet",
    "Cobalt",
    "Copper",
    "Bronze",
    "Emerald",
    "Sapphire",
    "Turquoise",
    # Weather and nature
    "Sunny",
    "Misty",
    "Foggy",
    "Stormy",
    "Windy",
    "Frosty",
    "Dusty",
    "Hazy",
    "Cloudy",
    "Rainy",
    # Descriptive
    "Swift",
    "Quiet",
    "Bold",
    "Calm",
    "Bright",
    "Dark",
    "Wild",
    "Silent",
    "Gentle",
    "Rustic",
)

NOUNS: Iterable[str] = (
    # Original nouns
    "Stone",
    "Lake",
    "Dog",
    "Creek",
    "Pond",
    "Cat",
    "Bear",
    "Mountain",
    "Hill",
    "Snow",
    "Castle",
    # Geography and nature
    "River",
    "Forest",
    "Valley",
    "Canyon",
    "Meadow",
    "Prairie",
    "Desert",
    "Island",
    "Cliff",
    "Cave",
    "Glacier",
    "Waterfall",
    "Spring",
    "Stream",
    "Reef",
    "Dune",
    "Ridge",
    "Peak",
    "Gorge",
    "Marsh",
    "Brook",
    "Glen",
    "Grove",
    "Hollow",
    "Basin",
    "Cove",
    "Bay",
    "Harbor",
    # Animals
    "Fox",
    "Wolf",
    "Hawk",
    "Eagle",
    "Owl",
    "Deer",
    "Elk",
    "Moose",
    "Falcon",
    "Raven",
    "Heron",
    "Crane",
    "Otter",
    "Beaver",
    "Badger",
    "Finch",
    "Robin",
    "Sparrow",
    "Lynx",
    "Puma",
    # Objects and structures
    "Tower",
    "Bridge",
    "Forge",
    "Mill",
    "Barn",
    "Gate",
    "Anchor",
    "Lantern",
    "Beacon",
    "Compass",
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_AGENT_NAME_RE = re.compile(r"[^A-Za-z0-9]+")
_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CLIENT_PLATFORM_HOST_AGENT_ID_RE = re.compile(
    r"^(?:claude|codex|copilot|gemini)"
    r"-(?:linux|wsl|win|mac|other)"
    r"-[A-Za-z0-9][A-Za-z0-9._-]{0,95}"
    r"-[1-9][0-9]*$",
)

# Pre-built frozenset of all valid agent names (lowercase) for O(1) validation lookup.
# This is computed once at module load time rather than O(n*m) per validation call.
_VALID_AGENT_NAMES: frozenset[str] = frozenset(
    f"{adj}{noun}".lower() for adj in ADJECTIVES for noun in NOUNS
)


def slugify(value: str) -> str:
    """Normalize a human-readable value into a slug."""
    normalized = value.strip().lower()
    slug = _SLUG_RE.sub("-", normalized).strip("-")
    return slug or "project"


def generate_agent_name() -> str:
    """Return a random adjective+noun combination."""
    adjective = random.choice(tuple(ADJECTIVES))
    noun = random.choice(tuple(NOUNS))
    return f"{adjective}{noun}"


def validate_agent_name_format(name: str) -> bool:
    """
    Validate that an agent name matches the required adjective+noun format.

    CRITICAL: Agent names MUST be randomly generated two-word combinations
    like "GreenLake" or "BlueDog", NOT descriptive names like "BackendHarmonizer".

    Names should be:
    - Unique and easy to remember
    - NOT descriptive of the agent's role or task
    - One of the predefined adjective+noun combinations

    Note: This validation is case-insensitive to match the database behavior
    where "GreenLake", "greenlake", and "GREENLAKE" are treated as the same.

    Returns True if valid, False otherwise.
    """
    if not name:
        return False

    # O(1) lookup using pre-built frozenset (vs O(n*m) iteration)
    return name.lower() in _VALID_AGENT_NAMES


_EXPLICIT_ID_SEPARATOR_RE = re.compile(r"[._-]")


def validate_explicit_agent_id(name: str) -> bool:
    """Validate that a caller-supplied identity is safe for use as an agent name.

    Explicit IDs allow stable, human-chosen identities like ``cc-0``,
    ``alpha-one``, or ``worker_42`` — useful for swarm workflows where agents
    are relaunched onto the same identity.  The format mirrors thread IDs:
    ASCII alphanumerics plus ``._-``, starting with an alphanumeric, max 128
    characters.

    To distinguish explicit IDs from adjective+noun names, the ID must
    contain at least one separator character (``-``, ``_``, or ``.``).
    Purely alphanumeric strings go through the adjective+noun validation
    path instead.
    """
    if not name:
        return False
    if not _THREAD_ID_RE.fullmatch(name):
        return False
    # Require at least one separator so purely-alphanumeric strings like
    # "BackendHarmonizer" still go through adjective+noun validation.
    return _EXPLICIT_ID_SEPARATOR_RE.search(name) is not None


def parse_client_platform_host_agent_id(
    name: str,
) -> tuple[str, str, str, str] | None:
    """Parse the stable ``client-os-host-slot`` identity contract.

    Client and OS are closed, single lexical tokens at the start, the slot is
    the final positive integer, and the entire middle remainder is the host.
    Consequently host names may contain hyphens without making the identity
    ambiguous.  A future client family may use an underscore in its one token
    (for example ``claude_desktop``) after being added to the closed vocabulary;
    client or OS tokens themselves must never contain hyphens.

    The returned client and platform values are case-folded vocabulary values;
    the host spelling and decimal slot are preserved.  ``None`` means the name
    is not a canonical identity.
    """
    if not validate_explicit_agent_id(name):
        return None
    if _CLIENT_PLATFORM_HOST_AGENT_ID_RE.fullmatch(name) is None:
        return None
    client, platform, host_and_slot = name.split("-", 2)
    host, slot = host_and_slot.rsplit("-", 1)
    return client.casefold(), platform.casefold(), host, slot


def validate_client_platform_host_agent_id(name: str) -> bool:
    """Return whether *name* follows the stable client/OS/host contract.

    Integrators use ``<client>-<os>-<host>-<slot>`` so several coding
    clients on one machine have separate mailboxes and reservations while each
    client keeps the same identity across process restarts.  Client and platform
    are deliberately closed vocabularies; the slot is a positive integer chosen
    by the operator rather than an automatically allocated session number.  The
    client token is the program family (for example ``claude`` or ``codex``),
    while host/platform distinguish native Windows, WSL, Linux and macOS use.
    """
    return parse_client_platform_host_agent_id(name) is not None


def sanitize_agent_name(value: str) -> Optional[str]:
    """Normalize user-provided agent name; return None if nothing remains."""
    cleaned = _AGENT_NAME_RE.sub("", value.strip())
    if not cleaned:
        return None
    return cleaned[:128]


def pid_is_alive(pid: int) -> bool:
    """Report whether ``pid`` is a running process, without signalling it.

    The single implementation for the whole package. It exists here, in a module
    that imports nothing from the package, because there used to be two of these
    under different names (``cli._pid_is_alive``, ``storage.AsyncFileLock._pid_alive``)
    with different mechanisms and — the part that mattered — **opposite failure
    policies**. Two properties are easy to get wrong and both were, in different
    files:

    **The probe must not send anything.** ``os.kill(pid, 0)`` is a pure query on
    POSIX because signal 0 is defined to perform only the error checks. On
    Windows ``0`` is ``CTRL_C_EVENT``, so the same line stops being an observer
    and starts delivering Ctrl+C to a process group. That is why the Windows
    branch goes through ``OpenProcess`` and never touches ``os.kill``.

    **A failed probe is not evidence of death.** ``OpenProcess`` is refused for
    any process owned by another user or running at higher integrity, and it
    reports that refusal the same way it reports a genuine absence: no handle.
    Only ``ERROR_INVALID_PARAMETER`` (87) actually means "no such process".
    Reading anything else as a corpse means releasing a live process's lock,
    which is a correctness failure and not a tidy one — the second holder is
    told it acquired something.

    Measured on this machine, unprivileged, against six protected processes
    (``System``, ``smss``, ``csrss``, ``wininit``, ``services``, ``lsass``): a
    raw ``OpenProcess`` handle check calls all six dead. This function calls all
    six alive, and still calls PID 999999 dead, so the answer is not simply
    "always alive".
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        winapi = cast(Any, importlib.import_module("_winapi"))
        process_query_limited_information = 0x1000
        try:
            handle = winapi.OpenProcess(
                process_query_limited_information,
                False,
                pid,
            )
        except OSError as exc:
            # OpenProcess reports ERROR_INVALID_PARAMETER for a PID that no
            # longer exists. Other failures (notably access denied) cannot
            # prove that the process is dead, so preserve its lock.
            return getattr(exc, "winerror", None) != 87
        try:
            try:
                return bool(winapi.GetExitCodeProcess(handle) == winapi.STILL_ACTIVE)
            except OSError:
                # A failed status query is likewise not evidence of death.
                return True
        finally:
            winapi.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists; we simply may not signal it.
        return True
    except OSError:
        return False
    return True


def validate_thread_id_format(thread_id: str) -> bool:
    """Validate that a thread_id is safe for filenames and indexing.

    Thread IDs are used as human-facing keys and may also be used in filesystem
    paths for thread digests. For safety and portability, enforce:
    - ASCII alphanumerics plus '.', '_', '-'
    - Must start with an alphanumeric character
    - Max length 128
    """
    candidate = (thread_id or "").strip()
    if not candidate:
        return False
    return _THREAD_ID_RE.fullmatch(candidate) is not None
