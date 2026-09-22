"""提供与统一认证前端格式一致的凭据加密。

本文件定义：
    _key_and_iv：从统一认证参数派生字节密钥和初始向量。
    encrypt：按统一认证使用的 AES-CBC 格式加密文本凭据。
"""

import binascii

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from .config import CHECK_KEY
from .errors import AuthProtocolError


# 作用：从统一认证参数派生字节密钥和初始向量。
# 参数：
#     t_param：统一认证提供的加密参数，拼接固定前缀后作为密钥和初始向量。
# 返回：由同一字节串组成的二元组，分别作为密钥和初始向量。
# 说明：使用 AES 支持的密钥长度集合校验参数；长度不符时抛出 AuthProtocolError。
def _key_and_iv(t_param: str | int) -> tuple[bytes, bytes]:
    key = f"iam{t_param}".encode()
    if len(key) not in AES.key_size:
        raise AuthProtocolError("统一认证加密参数长度无效")
    return key, key


# 作用：按统一认证使用的 AES-CBC 格式加密文本凭据。
# 参数：
#     text：需加密的原始文本，按 UTF-8 编码并补齐到 AES 分组长度。
#     t_param：统一认证提供的加密参数，拼接固定前缀后作为密钥和初始向量。 默认值为 CHECK_KEY。
# 返回：加密结果的 ASCII 十六进制字符串。
# 说明：默认使用协议配置中的 CHECK_KEY；保留文本中的空白，不记录明文或密文。
def encrypt(text: str, t_param: str | int = CHECK_KEY) -> str:
    """按统一认证使用的 AES-CBC 格式加密文本凭据。"""

    key, iv = _key_and_iv(t_param)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    encrypted = cipher.encrypt(pad(text.encode("utf-8"), AES.block_size))
    return binascii.hexlify(encrypted).decode("ascii")
