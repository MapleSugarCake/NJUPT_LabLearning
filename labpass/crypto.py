"""Credential encryption compatible with the NJUPT SSO front end."""

import binascii

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from .config import CHECK_KEY
from .exceptions import ConfigurationError


def _key_and_iv(t_param: str | int) -> tuple[bytes, bytes]:
    key = f"iam{t_param}".encode()
    if len(key) not in AES.key_size:
        raise ConfigurationError("统一认证加密参数长度无效")
    return key, key


def encrypt(text: str, t_param: str | int = CHECK_KEY) -> str:
    """Encrypt a credential using the AES-CBC format expected by SSO."""

    key, iv = _key_and_iv(t_param)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    encrypted = cipher.encrypt(pad(text.encode("utf-8"), AES.block_size))
    return binascii.hexlify(encrypted).decode("ascii")
