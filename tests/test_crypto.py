"""用固定虚构输入验证统一认证加密格式及参数校验。

本文件定义：
    test_encrypt_matches_known_sso_vectors：验证两组固定测试输入得到预期的 AES-CBC 十六进制结果。
    test_encrypt_rejects_invalid_key_length：验证加密参数派生出的密钥长度无效时拒绝处理。
"""

import pytest

from njupt_auth.crypto import encrypt
from njupt_auth.errors import AuthProtocolError


# 作用：验证两组固定测试输入得到预期的 AES-CBC 十六进制结果。
# 参数：无。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：使用合成测试向量，仅校验本地加密格式，不请求统一认证服务。
def test_encrypt_matches_known_sso_vectors() -> None:
    assert encrypt("20250001") == "601c2c24b06603aafa2408490810122f"
    assert encrypt("test-password") == "1511152bc9167f1f49cbee68e2ae5271"


# 作用：验证加密参数派生出的密钥长度无效时拒绝处理。
# 参数：无。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：传入明显过短的测试参数，预期 AuthProtocolError。
def test_encrypt_rejects_invalid_key_length() -> None:
    with pytest.raises(AuthProtocolError):
        encrypt("value", "short")
