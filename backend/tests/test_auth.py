from datetime import timedelta

from jose import jwt

from server.auth import create_access_token, get_password_hash, verify_password
from server.config import JWT_ALGORITHM, JWT_SECRET


def test_password_hash_roundtrip():
    hashed = get_password_hash("correct horse battery staple")
    assert hashed != "correct horse battery staple"
    assert verify_password("correct horse battery staple", hashed)
    assert not verify_password("wrong password", hashed)


def test_access_token_contains_subject_and_expiry():
    token = create_access_token({"sub": "alice"}, expires_delta=timedelta(minutes=5))
    payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    assert payload["sub"] == "alice"
    assert "exp" in payload
