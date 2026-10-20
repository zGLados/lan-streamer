"""Admin login: one password (scrypt hash in the settings table), session cookies."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from lanstreamer.core.store import Store

PASSWORD_KEY = "admin_password"
SESSION_S = 7 * 24 * 3600
MIN_PASSWORD = 8
_N, _R, _P = 2**14, 8, 1  # scrypt cost


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        check = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(check.hex(), digest)


class Auth:
    def __init__(self, store: Store, clock=time.time):
        self.store = store
        self.clock = clock
        self.sessions: dict[str, float] = {}  # token → expiry (in memory: a restart logs out)

    @property
    def has_password(self) -> bool:
        return self.store.get_setting(PASSWORD_KEY) is not None

    def set_password(self, password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"the password needs at least {MIN_PASSWORD} characters")
        self.store.set_setting(PASSWORD_KEY, hash_password(password))
        self.sessions.clear()  # log everybody out

    def login(self, password: str) -> str | None:
        stored = self.store.get_setting(PASSWORD_KEY)
        if stored is None or not verify_password(password, stored):
            return None
        token = secrets.token_urlsafe(32)
        self.sessions[token] = self.clock() + SESSION_S
        return token

    def valid(self, token: str | None) -> bool:
        expiry = self.sessions.get(token or "")
        if expiry is None:
            return False
        if expiry < self.clock():
            self.sessions.pop(token or "", None)
            return False
        return True

    def logout(self, token: str | None) -> None:
        self.sessions.pop(token or "", None)
