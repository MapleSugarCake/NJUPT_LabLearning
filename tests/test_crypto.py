import pytest

from labpass.crypto import encrypt
from labpass.exceptions import ConfigurationError


def test_encrypt_matches_known_sso_vectors() -> None:
    assert encrypt("20250001") == "601c2c24b06603aafa2408490810122f"
    assert encrypt("test-password") == "1511152bc9167f1f49cbee68e2ae5271"


def test_encrypt_rejects_invalid_key_length() -> None:
    with pytest.raises(ConfigurationError):
        encrypt("value", "short")
