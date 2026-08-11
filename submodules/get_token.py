import requests
import json
import re
from .config import HEADERS

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
SERVICE_URL="http://10.22.192.38:9092/"


def get_token(E_Username,E_Password):
    s = requests.session()
    s.get(PRELOGIN_URL, headers=HEADERS)
    s.get(M_PRELOGIN_URL, headers=HEADERS)

    s.post(M_LOGIN_URL, headers=HEADERS, json={
        "checkKey":"1629428467008",
        "password":E_Password,
        "username":E_Username,
        "captchaVerification":None,
        "appId":"1442771163964026882",
        "mode":"none"
    })
    sessionid=s.cookies.get("JSESSIONID")
    s.get(M_AfterLOGIN_URL, headers=HEADERS, params={"sessionId":sessionid})

    resp=s.get(PRELOGIN_URL2, headers=HEADERS)
    sessionid_regex = r'[&?]service=([A-F0-9]+)'
    sessionid_match = re.search(sessionid_regex, resp.url)
    sessionid = sessionid_match.group(1)

    s.post(LOGIN_URL, headers=HEADERS, json={
        "checkKey":"1629428467008",
        "password":E_Password,
        "username":E_Username,
        "captchaVerification":None,
        "appId":"1442771163964026882",
        "mode":"none"
    })

    resp2=s.get(PRELOGIN2_URL, headers=HEADERS, params={"sessionId": sessionid})
    ticket_regex = r'(?:[?&]|%3F|%26)ticket(?:=|%3D)(.*?)(?:%26|&|$)'
    ticket_match = re.search(ticket_regex, resp2.url)
    ticket = ticket_match.group(1) if ticket_match else None

    resp3=s.get(LOGIN2_URL, headers=HEADERS, params={
        "_t":s.cookies.get("vpn_timestamp"),
        "ticket":ticket,
        "service":SERVICE_URL,
        "enlink-vpn":None
    })

    token = resp3.json()["result"]["token"]
    return token,s

