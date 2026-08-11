from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
import binascii

def get_key_iv(t_str):
    """
    模拟 JS 的逻辑：生成 Key 和 IV
    Key 和 IV 都是 "iam" + t 的字节
    """
    key_str = "iam" + str(t_str)
    # 转换为 bytes
    key_bytes = key_str.encode('utf-8')
    # 注意：AES 要求 Key 长度必须为 16, 24 或 32 字节
    # 原生 JS 库通常会处理长度不足的情况，但在 Python 中需要确认
    # 如果 JS 端传入的 t 很短导致 key 长度不为 16/24/32，CryptoJS 会用特殊的 Key 策略处理
    # 但根据代码 Base64.parse 的写法，通常期望输入符合块大小或被截断。

    # 假设 t 是标准的使得 key 长度为 16 字节（常用情况）
    # 如果长度不对，Python 会报错。实际场景中可能需要补零或截断
    return key_bytes, key_bytes


def encrypt(text, t_param=1629428467008):
    key, iv = get_key_iv(t_param)

    # 处理 Key 长度问题：如果 key 不是 16/24/32，CryptoJS 可能会将其 hash 或填充
    # 但最常见的情况是参数 t 使得 "iam"+t 刚好是 16 位，或者使用了更复杂的派生
    # 下面假设 key 长度合法

    cipher = AES.new(key, AES.MODE_CBC, iv)
    # PKCS7 填充
    padded_text = pad(text.encode('utf-8'), AES.block_size)
    ciphertext = cipher.encrypt(padded_text)
    # JS 逻辑是 ciphertext.toString() -> Hex
    return binascii.hexlify(ciphertext).decode('utf-8')


def decrypt(hex_text, t_param=1629428467008):
    key, iv = get_key_iv(t_param)

    # Hex 转 bytes
    ciphertext = binascii.unhexlify(hex_text)

    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted = cipher.decrypt(ciphertext)
    # 去除填充
    try:
        unpadded = unpad(decrypted, AES.block_size)
        return unpadded.decode('utf-8')
    except ValueError:
        return "解密失败：填充错误或密钥错误"

# 示例使用
# 假设 t 是 "testkey"，"iamtestkey" 是 10 字节，AES 不支持 10 字节 key。
# 这说明传入的 t 参数在实际场景中必须能让 "iam"+t 的长度凑成 16/24/32 字节。
# 或者 CryptoJS 在做 parse 时进行了某种 hash 处理(虽然代码没明写)。
# 最稳妥的复现方式是确保 t 的长度，例如 t="1234567890" (len=10), "iam"+"1234567890" = 13，依然报错。
# 必须保证 key 长度合法。