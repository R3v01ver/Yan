"""模型接入层。

零 HTTP 依赖，只用标准库 urllib —— 少一个依赖，开源项目就少一半安装失败。

支持：
  openai   OpenAI 兼容端点（OpenAI / DeepSeek / Qwen / Moonshot / Grok / Ollama / vLLM）
  anthropic Anthropic Messages API
  mock     离线演示与测试，不发任何网络请求
"""

import json
import os
import re
import time
import urllib.error
import urllib.request

TIMEOUT = 120
# 探测类请求（连通测试、拉模型列表）等不起 120 秒 —— 用户会以为程序死了。
PROBE_TIMEOUT = 20
# 429 限流自动重试：免费档账号 RPM 常常只有个位数，一场会串行发请求
# 撞限流是常态，不该把一屏 JSON 怼给用户。等它让等的秒数再重发。
MAX_429_RETRIES = 3


class ProviderError(RuntimeError):
    pass


def retry_after(err_text):
    """从 429 报错里解析服务端让等几秒（"try again after 1 seconds"）。"""
    m = re.search(r"after\s+([\d.]+)\s*second", err_text, re.I)
    return float(m.group(1)) if m else None


def fixed_temperature(err_text):
    """从「temperature: only 1 is allowed for this model」这类报错里取允许值。

    Kimi K2 这类推理模型把 temperature 锁死成 1，我们默认发的 0/0.7 会被
    服务端整单拒绝。与其让用户看一屏 JSON，不如自动按它要的值重发。
    取不到（报错格式变了）返回 None，走正常的报错路径。
    """
    m = re.search(r"temperature[^.\n{}]*?only\s+([\d.]+)\s+is allowed",
                  err_text, re.I)
    return float(m.group(1)) if m else None


def temp_limit(err_text):
    """从报错里解析模型允许的 temperature 上限。

    各家句式不一样，实测两种：
      Kimi:  "temperature: only 1 is allowed for this model"
      智谱:  "temperature参数非法：限制数值范围[0,1]"
    解析不出返回 None。
    """
    fixed = fixed_temperature(err_text)
    if fixed is not None:
        return fixed
    m = re.search(r"范围\s*\[\s*([\d.]+)\s*,\s*([\d.]+)\s*\]", err_text)
    if not m:
        m = re.search(r"range\s*\[\s*([\d.]+)\s*,\s*([\d.]+)\s*\]",
                      err_text, re.I)
    return float(m.group(2)) if m else None


def _post(url, headers, payload, timeout=TIMEOUT):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    attempts = MAX_429_RETRIES
    while True:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            if e.code == 429 and attempts > 0:
                attempts -= 1
                # 服务端说等几秒就等几秒；没说就指数退避 2s/4s/8s
                wait = retry_after(body) or 2.0 * (MAX_429_RETRIES - attempts)
                time.sleep(min(wait, 15.0))
                continue
            raise ProviderError("HTTP %s %s: %s" % (e.code, e.reason, body[:300]))
        except Exception as e:
            raise ProviderError(str(e))


# ── 输出退化检测 ──────────────────────────────────────────────
# 模型（实测智谱免费档最常见）偶尔会陷进复读机模式：同一片段无限重复，
# 比如 "A.C.A.C.A.C…" 刷满一整屏。这种输出没有任何信息量，原样收下
# 会污染会议（别的代表还要"参考"它），必须拦下重发。
_REPEAT = re.compile(r"(.{1,8}?)\1{11,}", re.S)


def is_degenerate(text):
    """输出退化：同一短片段连续重复 12 次以上。"""
    if not text or len(text) < 40:
        return False
    return bool(_REPEAT.search(text))


# ── 思考泄漏检测 ──────────────────────────────────────────────
# 思考模型（推理档）有时把「内心戏」直接写进正文：满屏都在讨论
# 「用户要求是啥、要先评还是先出方案、500 字够不够」—— 它在琢磨题，
# 不是在答题。这种内容没有答案，原样收下会污染整个会议。
# 分两档控制误伤：强特征命中一个就算；弱特征要凑够两个不同短语。
_META_STRONG = ("需要回复用户", "需要回答用户", "用户最后说", "原话「", "原话“",
                "看起来可能只要求", "需要兼顾？", "要如何？")
_META_WEAK = ("用户要求", "用户想要", "看起来可能", "但是前文", "前文要求",
              "最后又说", "需要遵守", "字以内", "需要评别人的")


def is_meta_leak(text):
    """模型在正文里讨论提示词该怎么执行（内心戏），而不是给出答案。"""
    s = (text or "").strip()
    if not s:
        return False
    if any(k in s for k in _META_STRONG):
        return True
    return sum(1 for k in _META_WEAK if k in s) >= 2


_THINK = re.compile(r"<think>.*?(?:</think>|$)", re.S | re.I)


def strip_think(text):
    """剥掉 <think>…</think> 思考块；没闭合的就剥到结尾。"""
    return _THINK.sub("", (text or "")).strip()


class Provider(object):
    name = "base"

    def complete(self, system, user, temperature=0.7, max_tokens=2000,
                 timeout=TIMEOUT):
        raise NotImplementedError


class OpenAICompat(Provider):
    name = "openai"

    def __init__(self, model, api_key=None, base_url="https://api.openai.com/v1"):
        self.model = model
        self.api_key = api_key or ""
        self.base_url = base_url.rstrip("/")

    def _call(self, system, user, temperature, max_tokens, timeout):
        out = _post(
            self.base_url + "/chat/completions",
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key,
            },
            {
                "model": self.model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout,
        )
        try:
            msg = out["choices"][0]["message"]
        except (KeyError, IndexError):
            raise ProviderError("响应格式异常: %s" % json.dumps(out)[:300])
        text = (msg.get("content") or "").strip()
        # 思考模型常把思考块（<think>…</think>）混在 content 里一起返回，
        # 剥掉只留正文；剥完空了再用思考字段顶上。
        text = strip_think(text)
        if not text:
            # 思考模型（GLM-4.5、Kimi k2.6 这类）有时把话全写在思考字段里，
            # content 是空的 —— 用户就会看到一个空气泡，以为程序死了。
            # 拿思考内容顶上，实在没有就明确报错，不许静默返回空串。
            text = (msg.get("reasoning_content") or "").strip()
        if not text:
            raise ProviderError(
                "这家模型这次没有返回内容（content 和思考字段都是空的）。"
                "常见于模型过载或被安全策略拦下，重发一次或换一条试试。")
        return text

    def complete(self, system, user, temperature=0.7, max_tokens=2000,
                 timeout=TIMEOUT):
        return self._robust(system, user, temperature, max_tokens, timeout)

    def _robust(self, system, user, temperature, max_tokens, timeout):
        """重试策略（三类已知故障自动扛，扛不住才报错）：
        - temperature 被锁死/超上限 → 按模型允许的范围重发
        - 输出退化（复读机）→ 提高温度重发，最多三次，再犯就明确报错
        - 思考泄漏（满屏内心戏、没有答案）→ 提高温度重发；重试通常能好，
          反复出现说明这是思考模型的老毛病，报错建议换普通对话档。
        """
        temp = temperature
        last = None                      # 最后一轮是哪种故障
        for _ in range(3):
            try:
                text = self._call(system, user, temp, max_tokens, timeout)
            except ProviderError as e:
                msg = str(e)
                if "temperature" not in msg.lower():
                    raise
                # 锁死型（Kimi："only 1 is allowed"）→ 直接用它要的值
                allowed = fixed_temperature(msg)
                if allowed is not None:
                    temp = allowed
                    continue
                # 范围型（智谱："限制数值范围[0,1]"）→ 把我们的值压回上限内。
                # 曾经在这里直接 raise —— 退化重试把温度抬到 1.2，智谱上限
                # 是 1，被拒后没人接，一屏 JSON 就糊用户脸上了。
                cap = temp_limit(msg)
                if cap is not None and temp > cap:
                    temp = cap
                    continue
                raise
            if not is_degenerate(text) and not is_meta_leak(text):
                return text
            if is_degenerate(text):
                last = "repeat"
                # 升温打散复读。1.0 是国产模型（智谱/豆包等）的普遍硬上限，
                # 超了会被整单拒绝，所以封顶 1.0 —— 到顶的模型重试时只能靠
                # 随机性，退化输出本身就是随机的，再掷一次也常就好了。
                temp = min(1.0, temp + 0.4)
            else:
                last = "think"
                temp = min(1.0, temp + 0.3)
        if last == "think":
            raise ProviderError(
                "这家模型连续把「思考过程」当成正文输出（满屏都在讨论该怎么答题，"
                "没有答案）。重发一次通常能好；反复出现的话，换普通对话档模型试试"
                "（比如 deepseek-chat 而不是 deepseek-reasoner 这类推理档）。")
        raise ProviderError(
            "这家模型连续输出乱码（同一片段无限重复），这条发言已放弃。"
            "重发一次通常就好；若反复出现，换个模型或等一会儿再试。")


class Anthropic(Provider):
    name = "anthropic"

    def __init__(self, model, api_key=None, base_url="https://api.anthropic.com"):
        self.model = model
        self.api_key = api_key or ""
        self.base_url = base_url.rstrip("/")

    def _call(self, system, user, temperature, max_tokens, timeout):
        out = _post(
            self.base_url + "/v1/messages",
            {
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            {
                "model": self.model,
                "system": system,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "messages": [{"role": "user", "content": user}],
            },
            timeout,
        )
        try:
            text = "".join(
                b.get("text", "") for b in out["content"] if b.get("type") == "text"
            ).strip()
        except (KeyError, TypeError):
            raise ProviderError("响应格式异常: %s" % json.dumps(out)[:300])
        if not text:
            # 同 OpenAI 端点：空回复不许静默通过，用户得知道发生了什么
            raise ProviderError("这家模型这次没有返回内容，重发一次或换条议题试试。")
        return text

    def complete(self, system, user, temperature=0.7, max_tokens=2000,
                 timeout=TIMEOUT):
        return OpenAICompat._robust(
            self, system, user, temperature, max_tokens, timeout)


class Mock(Provider):
    """离线演示 / 测试用代表。

    不发网络请求、不需要 key，但五个阶段都真的跑。
    用途：先确认这套机制是不是你要的，再烧钱接真模型。
    """

    name = "mock"

    def __init__(self, model="mock-1", **_):
        self.model = model

    @staticmethod
    def _topic(user):
        skip = ("议题：", "以下是", "请", "各方", "批评与答辩", "表决结果", "你此前", "-")
        for line in user.splitlines():
            s = line.strip()
            if not s or s.startswith(skip):
                continue
            return s[:40]
        return "议题"

    def complete(self, system, user, temperature=0.7, max_tokens=2000,
                 timeout=TIMEOUT):
        # 阶段按 user 提示判断（system 只是角色简报，不含阶段信息）
        # 角色已平权：三方同一份简报，不再有"反对派必须投反对"这种人为分歧
        topic = self._topic(user)

        if "请对这套方案表态" in user:
            return "表态：认同\n采纳了你的：第 1 条判据\n理由：修订后已覆盖我提的隐患。"

        if "最优的一套方案" in user:
            return (
                "## 最优方案\n"
                "1. 先做判据（采纳立场 A 的成本估算 + 立场 B 的补漏）\n"
                "2. 再落地（采纳立场 A 的实施步骤）\n\n"
                "## 为什么是它\n"
                "比单独任何一家都多一道成本校验，落地顺序也拆得更细。\n\n"
                "## 保留意见\n"
                "- 有一家认为应等证据齐了再动，未并进来，原样保留。\n\n"
                "## 未决事项\n"
                "- 还缺一条可复现的判据才能定论。"
            )

        if "请给出修订版" in user:
            return (
                "【MOCK 修订】\n"
                "- 吸收了另一家的成本校验，改了第 2 步，写明了代价。\n"
                "- 不吸收：那家的灰度周期对我这套的场景太长，理由是上线窗口只有三天。\n"
                "- 修订后的做法：原方案 + 成本校验 + 可验证判据，三步走。"
            )

        if "需要你评审" in user:
            return (
                "【MOCK 评审】针对「%s」：\n"
                "1. 值得我并进来的：对方把落地顺序拆得更细，我这套缺这一步。\n"
                "2. 隐患：第 2 条依据只是断言，边界情况会失效；补法：改成可验证判据。" % topic
            )

        return (
            "【MOCK 方案】关于「%s」：\n"
            "结论：分三步走，先判据、再试点、后铺开。\n"
            "依据：1. …… 2. ……\n"
            "这套解决不了的：边界情况需要额外判据。" % topic
        )


def build(kind, model=None, api_key=None, base_url=None, api_key_env=None):
    """按配置造一个 provider。api_key 优先取显式值，否则取环境变量。"""
    key = api_key
    if not key and api_key_env:
        key = os.environ.get(api_key_env, "")
    kind = (kind or "openai").lower()
    if key and (base_url or "").lower().startswith("http://"):
        print("警告：provider[%s] 的 base_url 是 http://，API key 会明文过网。"
              "确认这是本机或内网端点。" % kind)
    if kind == "openai":
        return OpenAICompat(model or "gpt-4o-mini", key, base_url or "https://api.openai.com/v1")
    if kind == "anthropic":
        # base_url 必须传下去：否则用户配了代理地址也静默打向官方端点，
        # key 被送到他没打算送的地方且不报错。
        return Anthropic(model or "claude-sonnet-4-20250514", key,
                         base_url or "https://api.anthropic.com")
    if kind in ("mock", "test"):
        return Mock(model or "mock-1")
    raise ProviderError("未支持的 provider 类型: %s" % kind)
