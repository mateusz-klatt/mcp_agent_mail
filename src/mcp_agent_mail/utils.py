"""Utility helpers for the MCP Agent Mail service."""

from __future__ import annotations

import hashlib
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
    r"^(?:claude|codex|copilot|gemini|factory|cursor|cline|windsurf|opencode)"
    r"-(?:linux|wsl|win|mac|other)"
    r"-[A-Za-z0-9][A-Za-z0-9._-]{0,95}"
    r"-[1-9][0-9]*$",
)

# Legacy adjective+noun names remain recognizable for migrations, existing
# mailboxes, display aliases, and historical fixtures. New durable Agent rows
# use the client-os-host-slot contract below.
_VALID_AGENT_NAMES: frozenset[str] = frozenset(
    f"{adj}{noun}".lower() for adj in ADJECTIVES for noun in NOUNS
)

_BUILD_PATH_COMPONENT_MAX_BYTES = 80
_BUILD_PATH_COMPONENT_HASH_LENGTH = 32
_WINDOWS_RESERVED_COMPONENT_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


def _truncate_utf8_component(value: str, max_bytes: int) -> str:
    """Truncate on a character boundary to a portable component byte budget."""
    result: list[str] = []
    used = 0
    for char in value:
        encoded_size = len(char.encode("utf-8", errors="surrogatepass"))
        if used + encoded_size > max_bytes:
            break
        result.append(char)
        used += encoded_size
    return "".join(result)


def safe_build_path_component(value: str) -> str:
    """Return a readable, portable component without lossy-name collisions."""
    original = value
    unsafe_chars = frozenset('/\\:*?"<>|')
    sanitized = "".join(
        "_" if char in unsafe_chars or char.isspace() or ord(char) < 32 else char
        for char in original.strip()
    ).rstrip(" .")
    if sanitized in {"", ".", ".."}:
        sanitized = "unknown"

    reserved_on_windows = (
        sanitized.partition(".")[0].upper() in _WINDOWS_RESERVED_COMPONENT_STEMS
    )
    if reserved_on_windows:
        sanitized = f"safe-{sanitized}"

    encoded = sanitized.encode("utf-8", errors="surrogatepass")
    transformed = (
        sanitized != original
        or reserved_on_windows
        or len(encoded) > _BUILD_PATH_COMPONENT_MAX_BYTES
    )
    if not transformed:
        return sanitized

    digest = hashlib.sha256(
        original.encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    suffix = f"-{digest[:_BUILD_PATH_COMPONENT_HASH_LENGTH]}"
    prefix_budget = _BUILD_PATH_COMPONENT_MAX_BYTES - len(suffix)
    prefix = (
        _truncate_utf8_component(sanitized, prefix_budget).rstrip(" ._-") or "value"
    )
    return f"{prefix}{suffix}"


def slugify(value: str) -> str:
    """Normalize a human-readable value into a slug."""
    normalized = value.strip().lower()
    slug = _SLUG_RE.sub("-", normalized).strip("-")
    return slug or "project"


def generate_agent_name() -> str:
    """Return an adjective+noun alias.

    Production callers use this only for the non-addressable ``display_name``
    of a newly provisioned Agent; durable identities are never generated.
    """
    adjective = random.choice(tuple(ADJECTIVES))
    noun = random.choice(tuple(NOUNS))
    return f"{adjective}{noun}"


def validate_agent_name_format(name: str) -> bool:
    """Recognize a legacy adjective+noun Agent name.

    This case-insensitive predicate exists only for compatibility with
    persisted historical identities and non-durable display aliases. It must
    not be used to authorize creation of a new durable Agent; those identities
    require :func:`validate_client_platform_host_agent_id`.
    """
    if not name:
        return False

    # O(1) lookup using pre-built frozenset (vs O(n*m) iteration)
    return name.lower() in _VALID_AGENT_NAMES


_EXPLICIT_ID_SEPARATOR_RE = re.compile(r"[._-]")


def validate_explicit_agent_id(name: str) -> bool:
    """Validate the generic safe syntax used by explicit identity-like IDs.

    This is a lexical helper, not the durable Agent-name policy. Durable
    mailbox creation additionally requires
    :func:`validate_client_platform_host_agent_id`.
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
