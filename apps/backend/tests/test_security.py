import base64
import hashlib

from app.security import RateLimiter, hash_secret, issue_session, verify_secret, verify_session


def test_pbkdf2_round_trip_and_wrong_value() -> None:
    encoded = hash_secret("ABCD-1234", iterations=1_000, salt=b"fixed-test-salt")
    assert verify_secret("ABCD-1234", encoded)
    assert not verify_secret("WXYZ-9999", encoded)
    assert not verify_secret("ABCD-1234", "not-a-hash")


def test_pbkdf2_accepts_deploy_studio_format() -> None:
    value = "AIDP-2026"
    salt = "deploy-studio-test-salt"
    digest = hashlib.pbkdf2_hmac("sha256", value.encode(), salt.encode(), 1_000)
    encoded = f"pbkdf2_sha256$1000${salt}${base64.b64encode(digest).decode()}"
    assert verify_secret(value, encoded)
    assert not verify_secret("WRONG-0000", encoded)


def test_session_rejects_tampering_and_expiry() -> None:
    token = issue_session(b"k" * 32, "admin", now=100, ttl=10)
    assert verify_session(token, b"k" * 32, now=109) == "admin"
    assert verify_session(token + "x", b"k" * 32, now=109) is None
    assert verify_session(token, b"k" * 32, now=110) is None


def test_rate_limiter_is_windowed() -> None:
    limiter = RateLimiter(limit=2, window_seconds=10)
    assert limiter.allow("ip", now=0)
    assert limiter.allow("ip", now=1)
    assert not limiter.allow("ip", now=2)
    assert limiter.allow("ip", now=10)
