"""App-level per-user authentication for the ``/mail`` viewer — stdlib only.

Why this exists
---------------
The ``/mail`` UI is ~34 server-rendered routes, several of which are destructive
(``/mail/api/delete-messages``, ``retire-agent``, ``archive-project``,
``/mail/{project}/overseer/send``). The server's only HTTP auth is a static
bearer token, and a browser cannot attach an ``Authorization`` header when you
type a URL — so on a public domain the UI is either unreachable (401) or, if the
proxy injects the bearer for everyone, anonymously destructive. Neither is
acceptable. This module supplies the third option: a login the browser can
actually perform.

The primitives are deliberately pure so they unit-test without a clock, a
database or an environment:

* :func:`hash_password` / :func:`verify_password` — ``hashlib.scrypt``
  (memory-hard) with a per-password random salt. Stored form is
  ``scrypt$n$r$p$salt_b64$hash_b64``. Verification is constant-time and never
  raises on a malformed stored value (returns ``False``).
* :func:`make_session` / :func:`verify_session` — an HMAC-SHA256 signed
  ``username|epoch|expiry`` token. Verification checks the signature
  (constant-time) *before* the expiry, so a forged token and an expired one are
  rejected identically.
* :func:`same_origin` — the CSRF defence, paired with a ``SameSite=Lax`` cookie.

``epoch`` is a per-user counter bumped whenever the password changes or the
account is disabled; it rides inside the signed payload and is compared against
the database on every request. That is what makes "change my password" and
"disable this account" terminate existing sessions immediately, without a
server-side session table to garbage-collect.

The signing ``secret`` is always passed in. The web layer reads it from
``MAIL_UI_SESSION_SECRET`` and fails closed when it is empty — an unset secret
disables the UI rather than silently signing with a default.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import Final

# scrypt cost. n=2**14 / r=8 / p=1 is ~16 MiB and tens of milliseconds per hash:
# negligible for an interactive login, expensive in bulk for an offline attacker.
# maxmem is set explicitly so the call cannot trip OpenSSL's default cap.
_SCRYPT_N: Final = 2**14
_SCRYPT_R: Final = 8
_SCRYPT_P: Final = 1
_SCRYPT_MAXMEM: Final = 64 * 1024 * 1024
_SALT_BYTES: Final = 16
_DKLEN: Final = 32

# A fixed dummy password whose hash is computed once at import, so authenticate()
# performs EXACTLY ONE scrypt whether or not the account exists. Without this, a
# missing user returns in microseconds and an existing one in ~50 ms, which is a
# clean username-enumeration oracle.
_DUMMY_PASSWORD: Final = "mcp-agent-mail-dummy-password"

SESSION_TTL: Final = 14 * 24 * 3600.0  # 14 days

# Roles, most privileged first. "admin" may perform destructive actions
# (delete/retire/archive/overseer-send); "viewer" is read-only.
ROLE_ADMIN: Final = "admin"
ROLE_VIEWER: Final = "viewer"
ROLES: Final = (ROLE_ADMIN, ROLE_VIEWER)
DEFAULT_NEW_ROLE: Final = ROLE_VIEWER


def _b64e(raw: bytes) -> str:
    # URL-safe so the token is a clean cookie value; '$'-free so the stored-hash split holds.
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def hash_password(password: str) -> str:
    """Hash ``password`` into the storable ``scrypt$n$r$p$salt$hash`` form."""
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
        dklen=_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64e(salt)}${_b64e(dk)}"


# Computed ONCE at import. authenticate() verifies an unknown user's password
# against this so the timing matches the real path. It is never a successful login.
_DUMMY_STORED: Final = hash_password(_DUMMY_PASSWORD)


def verify_password(password: str, stored: str) -> bool:
    """``True`` iff ``password`` matches the ``scrypt$…`` ``stored`` value.

    Constant-time, and never raises: a malformed, foreign or non-string
    ``stored`` value simply returns ``False``. The scrypt parameters are read
    back out of the stored string rather than assumed, so raising the cost
    factors later does not invalidate existing hashes.
    """
    if not isinstance(password, str) or not isinstance(stored, str):
        return False
    try:
        algo, n_s, r_s, p_s, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = _b64d(salt_b64)
        expected = _b64d(hash_b64)
        dk = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n_s),
            r=int(r_s),
            p=int(p_s),
            maxmem=_SCRYPT_MAXMEM,
            dklen=len(expected),
        )
    except (ValueError, TypeError, OverflowError, MemoryError):
        # wrong field count / bad base64 / bad ints / out-of-range or hostile params
        return False
    return hmac.compare_digest(dk, expected)


def authenticate(username: str, password: str, stored: "str | None") -> bool:
    """``True`` iff ``password`` matches ``stored``.

    ``stored`` is ``None`` when the account does not exist; a dummy scrypt still
    runs in that case so timing cannot distinguish "no such user" from "wrong
    password".
    """
    if not isinstance(username, str) or not isinstance(password, str):
        return False  # malformed input (raw form data) -> fail closed, never raise
    if not isinstance(stored, str):
        verify_password(password, _DUMMY_STORED)  # equalise timing; NEVER a successful login
        return False
    return verify_password(password, stored)


def make_session(username: str, *, epoch: int, now: float, secret: bytes, ttl: float = SESSION_TTL) -> str:
    """An HMAC-SHA256 signed ``username|epoch|expiry`` token (``payload.sig``)."""
    payload = f"{username}|{int(epoch)}|{int(now + ttl)}".encode("utf-8")
    sig = hmac.new(secret, payload, hashlib.sha256).digest()
    return f"{_b64e(payload)}.{_b64e(sig)}"


def verify_session(token: str, *, now: float, secret: bytes) -> "tuple[str, int] | None":
    """Return ``(username, epoch)`` for a valid, unexpired, correctly-signed token.

    Returns ``None`` otherwise. The signature is verified constant-time *before*
    the expiry is read, so a forged token cannot be distinguished from a stale one.
    The caller must still confirm ``epoch`` against the database — that is what
    makes a password change or an account disable revoke live sessions.
    """
    if not isinstance(token, str):
        return None
    try:
        payload_b64, sig_b64 = token.split(".")
        payload = _b64d(payload_b64)
        sig = _b64d(sig_b64)
    except (ValueError, TypeError):
        return None
    expected = hmac.new(secret, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        username, epoch_s, expiry_s = payload.decode("utf-8").rsplit("|", 2)
        epoch = int(epoch_s)
        expiry = int(expiry_s)
    except (ValueError, UnicodeDecodeError):
        return None
    if now >= expiry:
        return None
    return username, epoch


def same_origin(origin_header: str, referer_header: str, expected_host: str) -> bool:
    """Cross-origin check for state-changing requests (the CSRF defence).

    Deliberately not a per-form CSRF token: the viewer has ~34 server-rendered
    templates plus inline ``fetch`` calls, and threading a token through all of
    them is a large diff with many places to forget one. Instead the session
    cookie is issued ``SameSite=Lax`` (so a browser will not attach it to a
    cross-site POST at all) and this function is the server-side second line.

    Browsers send ``Origin`` on every POST, same-site or not, so an absent
    ``Origin`` on a state-changing request is treated as suspicious unless a
    ``Referer`` corroborates the host. Both missing -> rejected.
    """
    if not expected_host:
        return False

    def _host_of(url: str) -> str:
        # Cheap scheme://host[:port]/... split; avoids pulling in urlparse for a hot path.
        rest = url.split("://", 1)[-1]
        return rest.split("/", 1)[0].strip().lower()

    if origin_header:
        return _host_of(origin_header) == expected_host.strip().lower()
    if referer_header:
        return _host_of(referer_header) == expected_host.strip().lower()
    return False


def valid_username(username: str) -> bool:
    """Usernames must survive the ``|``-delimited signed payload unambiguously."""
    return (
        isinstance(username, str)
        and 1 <= len(username) <= 64
        and "|" not in username
        and "/" not in username
        and username.strip() == username
    )
