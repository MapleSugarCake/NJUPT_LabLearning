import json

import requests


def make_response(
    payload: object | None = None,
    *,
    status: int = 200,
    text: str | None = None,
    url: str = "https://example.test/response",
) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = url
    response.encoding = "utf-8"
    if payload is not None:
        response._content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        response.headers["Content-Type"] = "application/json"
    else:
        response._content = (text or "").encode("utf-8")
        response.headers["Content-Type"] = "text/html"
    response.request = requests.Request("GET", url).prepare()
    return response
