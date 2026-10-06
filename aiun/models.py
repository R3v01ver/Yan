"""拉取厂商当前可用的模型列表。

用户不该被要求记住模型名 —— 那东西每个月都在变，写死在代码里必然过时。
正确做法：拿他填的 key 去问厂商「我能用哪些模型」，把返回的 id 列出来让他选。
拉不到（离线、代理、厂商不开放这个接口）就退回内置清单 + 手填。
"""

import json
import urllib.error
import urllib.request

from .providers import PROBE_TIMEOUT, ProviderError

MAX_SHOW = 60


def list_models(kind, base_url, api_key, timeout=PROBE_TIMEOUT):
    """返回该厂商可用的模型 id 列表（已排序去重）。失败抛 ProviderError。"""
    kind = (kind or "openai").lower()
    if kind in ("mock", "test"):
        return ["mock-a", "mock-b", "mock-c"]

    headers = {}
    if kind == "anthropic":
        url = (base_url or "https://api.anthropic.com").rstrip("/") + "/v1/models?limit=100"
        headers = {"x-api-key": api_key or "",
                   "anthropic-version": "2023-06-01"}
    else:
        url = (base_url or "https://api.openai.com/v1").rstrip("/") + "/models"
        headers = {"Authorization": "Bearer " + (api_key or "")}

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        raise ProviderError("HTTP %s %s：%s" % (e.code, e.reason, body[:200]))
    except Exception as e:
        raise ProviderError(str(e))

    ids = []
    for m in data.get("data") or []:
        mid = m.get("id") or m.get("name")
        if mid:
            ids.append(mid)
    return sorted(set(ids))[:MAX_SHOW]


def guess_vendor(api_key):
    """按 key 的外形猜厂商。猜不出就返回 None —— 不乱猜，猜错比不猜更坑。"""
    k = (api_key or "").strip()
    if not k:
        return None
    if k.startswith("sk-ant-"):
        return "anthropic"
    if "." in k and not k.startswith("sk-"):
        return "zhipu"          # 智谱是 id.secret 这种带点格式
    if k.startswith("sk-proj-") or k.startswith("sk-or-"):
        return "openai"
    if k.startswith("sk-") and len(k) >= 40 and k[3:].isalnum():
        # DeepSeek / Moonshot / 通义都是 sk- + 长串，外形分不出来，交给用户选
        return None
    return None
