import pytest

from lanstreamer.core.auth import Auth, hash_password, verify_password
from lanstreamer.core.store import Store


def test_hash_and_verify():
    stored = hash_password("correct horse")
    assert stored.startswith("scrypt$") and "correct horse" not in stored
    assert verify_password("correct horse", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("x", "garbage")


def test_login_flow(tmp_path):
    now = [1000.0]
    auth = Auth(Store(tmp_path / "db"), clock=lambda: now[0])
    assert not auth.has_password and auth.login("anything") is None
    with pytest.raises(ValueError, match="at least 8"):
        auth.set_password("short")
    auth.set_password("long enough")
    assert auth.has_password
    assert auth.login("wrong") is None
    token = auth.login("long enough")
    assert auth.valid(token) and not auth.valid("other") and not auth.valid(None)
    now[0] += 8 * 24 * 3600
    assert not auth.valid(token)  # expired
    token = auth.login("long enough")
    auth.logout(token)
    assert not auth.valid(token)


def test_password_survives_restart_and_change_logs_out(tmp_path):
    auth = Auth(Store(tmp_path / "db"))
    auth.set_password("first password")
    token = auth.login("first password")
    auth2 = Auth(Store(tmp_path / "db"))
    assert auth2.login("first password") is not None
    auth.set_password("second password")
    assert not auth.valid(token)
