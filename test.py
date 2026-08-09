import requests
import json
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
import binascii
import re


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


def encrypt(text, t_param):
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


def decrypt(hex_text, t_param):
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
E_Username=encrypt("B25030416",1629428467008)
E_Password=encrypt("Sy@20070625",1629428467008)


#第一步建立VPN的连接，获得cookie：ENSSESSIONID和GUESTSESSIONID（后续这两不变）【主会话】
PRELOGIN_URL="https://vpn.njupt.edu.cn:8443/http/webvpnc01f87dbae47c6e4069a3da910c73ebdc209d41128f21d35b57e760d9bad4569/students/students"

#第二步i.njupt.edu.cn临时JSESSIONID进行一次鉴权中间登录CAS，获得clientinfo【临时会话】
#PRE获取JSESSION，LOGIN获取tgc的cookie，After获取clientinfo（维持会话必需）
M_PRELOGIN_URL= "https://i.njupt.edu.cn/cas/login?service=https://vpn.njupt.edu.cn:8443/enlink/api/client/callback/cas"
M_LOGIN_URL= "https://i.njupt.edu.cn/ssoLogin/login"
M_AfterLOGIN_URL= "https://i.njupt.edu.cn/ssoLogin/index"

#第三步，回主会话正经登录
#PRE获取登录服务的主会话的sessionID，LOGIN二次验证鉴权（必需）确保主会话具备登录的权限
PRELOGIN_URL2="https://vpn.njupt.edu.cn:8443/http/webvpn85b2e3dcbef5577474e4a553381b9cce/cas/login?service=http%3A%2F%2F10.22.192.38%3A9092%2F"
LOGIN_URL = "https://vpn.njupt.edu.cn:8443/http/webvpn85b2e3dcbef5577474e4a553381b9cce/ssoLogin/login?enlink-vpn"

#第四步，validateLogin获取新的真正的业务内token
#PRE依靠第三步PRE的sessionID获取ST ticket和service，LOGIN2依靠ticket换新的服务内token
PRELOGIN2_URL="https://vpn.njupt.edu.cn:8443/http/webvpn85b2e3dcbef5577474e4a553381b9cce/ssoLogin/index"
LOGIN2_URL="https://vpn.njupt.edu.cn:8443/http/webvpnc01f87dbae47c6e4069a3da910c73ebdc0a307b03b8b6cbdba61b1f29c7dbb41/jeecg-boot/sys/cas/client/validateLogin"

#内网所需业务逻辑的URL
COURSE_URL=f"https://vpn.njupt.edu.cn:8443/http/webvpnc01f87dbae47c6e4069a3da910c73ebdc0a307b03b8b6cbdba61b1f29c7dbb41/jeecg-boot/jcedutec/courseSource/myCourseTypeList"
SERVICE_URL="http://10.22.192.38:9092/"

headers = {
    "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
}

s = requests.session()
s.get(PRELOGIN_URL, headers=headers)
s.get(M_PRELOGIN_URL, headers=headers)

resp0=s.post(M_LOGIN_URL, headers=headers, json={
    "checkKey":"1629428467008",
    "password":E_Password,
    "username":E_Username,
    "captchaVerification":None,
    "appId":"1442771163964026882",
    "mode":"none"
})

sessionid=s.cookies.get("JSESSIONID")
s.get(M_AfterLOGIN_URL, headers=headers, params={"sessionId":sessionid})

resp=s.get(PRELOGIN_URL2, headers=headers)
sessionid_regex = r'[&?]service=([A-F0-9]+)'
sessionid_match = re.search(sessionid_regex, resp.url)
sessionid = sessionid_match.group(1)
print(sessionid)

resp1=s.post(LOGIN_URL, headers=headers, json={
    "checkKey":"1629428467008",
    "password":E_Password,
    "username":E_Username,
    "captchaVerification":None,
    "appId":"1442771163964026882",
    "mode":"none"
})

resp2=s.get(PRELOGIN2_URL, headers=headers, params={"sessionId": sessionid})
ticket_regex = r'(?:[?&]|%3F|%26)ticket(?:=|%3D)(.*?)(?:%26|&|$)'
ticket_match = re.search(ticket_regex, resp2.url)
ticket = ticket_match.group(1) if ticket_match else None


resp3=s.get(LOGIN2_URL, headers=headers,params={
    "_t":s.cookies.get("vpn_timestamp"),
    "ticket":ticket,
    "service":SERVICE_URL,
    "enlink-vpn":None
})
print(resp3.status_code)

token = resp3.json()["result"]["token"]
headers["x-access-token"]=token

data=s.get(COURSE_URL, headers=headers,params={"_t":s.cookies.get("vpn_timestamp"),"enlink-vpn":None})
dict=json.loads(data.text)
course_list=[item['id'] for item in dict['result']]
print(course_list)
