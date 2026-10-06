"""砚（Yan）图形界面：把一场会渲染成多方圆桌群聊。

左边是会议室和各方代表，中间是聊天流 —— 代表一句一句地说话，
和真的会议一样分阶段推进：各自出方案 → 互相取长 → 补短修订 → 综合成一套 → 表态。

    python -m aiun.gui
"""

import os
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from . import keyring
from . import notes
from .cli import build_delegates, load_config, mock_council
from .council import CouncilStopped
from .delegate import ROLE_BRIEFS, load_rules
from .providers import PROBE_TIMEOUT

# ── 配色 ────────────────────────────────────────────────
C_SIDE = "#26282e"
C_SIDE_HI = "#34373f"
C_SIDE_TX = "#ece7db"
C_SIDE_DIM = "#97927f"
C_CHAT = "#f2eee6"
C_BUBBLE = "#fdfcf9"
C_BUBBLE_ME = "#8f332a"
C_RESOL = "#f6f0e2"
C_TEXT = "#26282e"
C_DIM = "#8f8a7c"
C_LINE = "#ddd6c6"
C_TOP = "#fdfcf9"
C_ACCENT = "#8f332a"

FONT = "Microsoft YaHei UI"

# 代表头像配色。按名字稳定取色，重启后颜色不变。
PALETTE = ["#46646f", "#8a5a3b", "#9c3a2f", "#476b5d", "#3a4d6e",
           "#a8842c", "#6d4a6e", "#5a5a52", "#a5484d", "#4a6a6a"]

ROLE_META = {
    # 三方平权：没有提案方/反对派/审查员之分，徽章一律显示"一方"
    "peer": ("一方", "#3a4d6e"),
    "proposer": ("一方", "#3a4d6e"),
    "adversary": ("一方", "#3a4d6e"),
    "reviewer": ("一方", "#3a4d6e"),
    "chair": ("主持人", "#8a5a3b"),
}
# 综合方案出来之后，各方对它表态：不是投票否决它，是标明谁还有保留意见
VERDICT_META = {
    "AGREE": ("认同", "#476b5d"),
    "RESERVE": ("有保留", "#a8842c"),
    "DISAGREE": ("不认同", "#9c3a2f"),
    "UNPARSED": ("未解析", "#8f8a7c"),
}

# 还没接入任何模型时的演示名单。名字必须自己说清"这是演示"——
# 写「代表A/B/C」这种占位符，用户会以为那就是他接入的 AI。
DEMO_ROSTER = [
    ("演示·一方", "peer", "mock-a"),
    ("演示·二方", "peer", "mock-b"),
    ("演示·三方", "peer", "mock-c"),
]


def base_dir():
    """配置与记录的基准目录：打包后取 exe 所在目录，源码运行取当前目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.getcwd()


def _icon_path():
    """窗口/任务栏图标。打包后 PyInstaller 会把 assets 解到 _MEIPASS 临时目录。"""
    if getattr(sys, "frozen", False):
        root = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(root, "aiun", "assets", "yan.ico")
    return p if os.path.isfile(p) else None


def _screen_avail(win):
    """屏幕真正能放窗口的区域（tk 像素，和 geometry 同一坐标系）。

    直接用 winfo_screenheight 是不够的：屏幕高度里含任务栏，而窗口外框还要
    比客户区多一圈标题栏和边框。这两样不扣掉，窗口底部就会伸到屏幕外面去
    ——表现就是最下面的输入框被任务栏吃掉。
    """
    try:
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    except Exception:
        sw, sh = 0, 0
    if not sw or not sh:
        sw, sh = 1440, 900
    # 任务栏 ~48，标题栏+边框 ~40，再留 ~16 的余量
    return max(760, sw - 48), max(520, sh - 104)


def _fit(win, w, h):
    """把想要的尺寸收进屏幕可用区，绝不超出。"""
    aw, ah = _screen_avail(win)
    return min(int(w), aw), min(int(h), ah)


def _centered(win, w, h):
    """让窗口开在屏幕中央，返回可直接给 geometry 的串。

    尺寸先过 _fit：无论屏幕多大、缩放到多少，窗口都保证完整落在屏幕里。
    """
    try:
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    except Exception:
        return "%dx%d" % (w, h)
    w, h = _fit(win, w, h)
    x = max(0, (sw - w) // 2)
    # 窗口外框比客户区多一圈标题栏和边框（约 38px），底部还压着任务栏。
    # 居中位置按「全屏高」算会把窗口往下推，外框就伸进任务栏里去了——
    # 所以这里取居中值和「贴着任务栏上沿也放得下」的更小者。
    frame = 38
    y = max(0, min((sh - h) // 2 - 12, sh - 44 - frame - h))
    return "%dx%d+%d+%d" % (w, h, x, y)


def mask(key):
    if not key:
        return ""
    k = str(key)
    return k[:3] + "***" + k[-4:] if len(k) > 10 else "***"


def initials(name):
    """头像上的字：有字母取字母（代表A → A），否则取第一个字（主持人 → 主）。"""
    s = (name or "?").strip()
    if not s:
        return "?"
    letters = [c for c in s if c.isascii() and c.isalpha()]
    if letters:
        return letters[0].upper()
    return s[0]


def color_of(name):
    return PALETTE[sum(ord(c) for c in str(name)) % len(PALETTE)]


def now():
    return datetime.now().strftime("%H:%M")


class Avatar(tk.Label):
    """直角头像：底色按名字取，中间放名字首字。

    用直角而不是圆：界面其余部分（气泡、按钮、输入框、卡片）全是 tk 原生的
    直角，头像要是圆的就成了唯一一处异形。

    用 Label 而不是 Canvas：Canvas 是 tk 里最重的控件，聊天流里一条消息一个，
    几十条消息滚起来就掉帧。底色 + 首字这件事 Label 完全做得了。
    """

    def __init__(self, master, name, size=36, bg=C_CHAT):
        super().__init__(master, text=initials(name), bg=color_of(name),
                         fg="#ffffff", bd=0, highlightthickness=0,
                         font=(FONT, 11 if size >= 34 else 9, "bold"),
                         width=3, height=2)


# ══════════════════════════════════════════════════════════
# 厂商目录：用户只需要选厂商 + 贴 Key
#
# 「类型 / 接口地址 / 模型名」这三项是程序该知道的事，不是用户该填的事。
# 模型名尤其不能写死在代码里 —— 它会过时，而厂商自己返回的列表不会。
# ══════════════════════════════════════════════════════════
VENDORS = [
    {
        "key": "deepseek", "name": "DeepSeek", "tag": "DS", "color": "#4d6bfe",
        "type": "openai", "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat", "models": ["deepseek-chat", "deepseek-reasoner"],
        "env": "DEEPSEEK_API_KEY",
        "hint": "去 platform.deepseek.com 的 API Keys 页面创建一个，sk- 开头",
        "url": "https://platform.deepseek.com/api_keys",
    },
    {
        "key": "kimi", "name": "Kimi（月之暗面）", "tag": "Ki", "color": "#16a5a5",
        "type": "openai", "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-32k",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "env": "MOONSHOT_API_KEY",
        "hint": "去 platform.moonshot.cn 的 API Key 管理里创建",
        "url": "https://platform.moonshot.cn/console/key",
    },
    {
        "key": "zhipu", "name": "智谱 GLM", "tag": "GL", "color": "#5b6bd6",
        "type": "openai", "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "models": ["glm-4-flash", "glm-4-air", "glm-4-plus"],
        "env": "ZHIPU_API_KEY",
        "hint": "去 open.bigmodel.cn 的控制台拿 Key，形如 xxxxx.yyyyy",
        "url": "https://open.bigmodel.cn/usercenter/apikeys",
    },
    {
        "key": "qwen", "name": "通义千问", "tag": "Qw", "color": "#615ced",
        "type": "openai", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus", "models": ["qwen-turbo", "qwen-plus", "qwen-max"],
        "env": "DASHSCOPE_API_KEY",
        "hint": "阿里云百炼控制台开通后创建 API Key",
        "url": "https://bailian.console.aliyun.com/",
    },
    {
        "key": "openai", "name": "OpenAI", "tag": "AI", "color": "#10a37f",
        "type": "openai", "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini", "models": ["gpt-4o-mini", "gpt-4o"],
        "env": "OPENAI_API_KEY",
        "hint": "platform.openai.com 创建；国内直连可能不通，需要自备代理",
        "url": "https://platform.openai.com/api-keys",
    },
    {
        "key": "anthropic", "name": "Claude（Anthropic）", "tag": "Cl", "color": "#d97757",
        "type": "anthropic", "base_url": "https://api.anthropic.com",
        "model": "claude-sonnet-4-20250514",
        "models": ["claude-sonnet-4-20250514", "claude-3-5-haiku-latest"],
        "env": "ANTHROPIC_API_KEY",
        "hint": "console.anthropic.com 创建，sk-ant- 开头",
        "url": "https://console.anthropic.com/settings/keys",
    },
    {
        "key": "ollama", "name": "本机 Ollama", "tag": "本", "color": "#4b5563",
        "type": "openai", "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5:14b", "models": [],
        "env": "",
        "hint": "先在本机跑 ollama serve。不用 Key，模型名点右侧「拉取」自动列出来",
        "url": "https://ollama.com/",
    },
    {
        "key": "compat", "name": "其他（兼容接口）", "tag": "＋", "color": "#8f8a7c",
        "type": "openai", "base_url": "",
        "model": "", "models": [],
        "env": "",
        "hint": "任何 OpenAI 兼容的中转或自建服务都填这里：只需要接口地址和 Key",
        "url": "",
    },
    {
        "key": "mock", "name": "演示代表（不联网）", "tag": "演", "color": "#8f8a7c",
        "type": "mock", "base_url": "",
        "model": "mock-a", "models": ["mock-a", "mock-b", "mock-c"],
        "env": "",
        "hint": "假代表，用来先看流程对不对，不发任何网络请求",
        "url": "",
    },
]
VENDOR_BY_KEY = dict((v["key"], v) for v in VENDORS)

# ── 模型名：接口名 ≠ 官网叫法 ──────────────────────────
# 这是最容易把人搞晕的地方：发给接口的那个名字，和厂商官网（控制台/App）上
# 显示的名字常常不是一个。DeepSeek 的接口名是 deepseek-chat，官网上叫 DeepSeek-V3；
# Claude 的接口名带一串日期后缀，官网上只写 Claude Sonnet 4。
# 下拉里两个都给，用户才知道自己到底在用哪个。
MODEL_LABELS = {
    "deepseek-chat": "官网：DeepSeek-V3 系列 · 通用对话",
    "deepseek-reasoner": "官网：DeepSeek-R1 系列 · 推理模型",
    "moonshot-v1-8k": "官网：Moonshot · 8 千字上下文",
    "moonshot-v1-32k": "官网：Moonshot · 3 万字上下文",
    "moonshot-v1-128k": "官网：Moonshot · 12 万字上下文",
    "glm-4-flash": "官网：GLM-4-Flash · 免费，快",
    "glm-4-air": "官网：GLM-4-Air · 均衡",
    "glm-4-plus": "官网：GLM-4-Plus · 更强，更贵",
    "qwen-turbo": "官网：通义千问-Turbo · 快，便宜",
    "qwen-plus": "官网：通义千问-Plus · 均衡",
    "qwen-max": "官网：通义千问-Max · 最强，最贵",
    "gpt-4o-mini": "官网：GPT-4o mini · 两边一致，便宜",
    "gpt-4o": "官网：GPT-4o · 两边一致，强但贵",
    "claude-sonnet-4-20250514": "官网：Claude Sonnet 4（日期只是接口的版本号）",
    "claude-3-5-haiku-latest": "官网：Claude 3.5 Haiku（-latest 表示总用最新的）",
}
MODEL_TIP = ("括号前那串是发给接口的名字，括号里是这家官网上的叫法 —— "
             "两边常常不一样（deepseek-chat 就是官网的 DeepSeek-V3）。")
MODEL_FALLBACK = "这是接口名，列表里没有想要的可以直接手动填。"


def model_display(mid):
    """下拉里怎么显示：接口名（官网叫法）。认不出的就只显示接口名。"""
    mid = (mid or "").strip()
    if not mid:
        return ""
    lab = MODEL_LABELS.get(mid)
    return "%s（%s）" % (mid, lab) if lab else mid


def pretty_model(mid):
    """名单里怎么叫这个模型：用官网叫法，别把接口名丢给用户。

    deepseek-chat → DeepSeek-V3，glm-4-flash → GLM-4-Flash。
    认不出的就原样显示接口名。
    """
    mid = (mid or "").strip()
    if not mid:
        return ""
    lab = MODEL_LABELS.get(mid)
    if not lab:
        return mid
    lab = lab.replace("官网：", "")
    for sep in ("（", "(", " · ", "，", ","):
        if sep in lab:
            lab = lab.split(sep)[0]
    return lab.strip() or mid


def model_id(text):
    """从下拉的显示串里取回真正要发给接口的模型名。"""
    t = (text or "").strip()
    for sep in ("（", "("):
        if sep in t:
            t = t.split(sep)[0]
    return t.strip()

# 三方平权后不再有角色可选，这个字段只为兼容旧的 council.yaml（会统一成 peer）
PEER_ROLE = "peer"
ROLE_LABEL = {PEER_ROLE: "一方"}

# 把网络层的报错翻译成人话 —— 看不懂的错误信息等于没有错误信息。
ERROR_HINTS = (
    (("401", "unauthorized"), "Key 不对或已失效，回厂商控制台重新生成一个。"),
    (("403", "forbidden"), "这个 Key 没权限用这个模型，或者额度用完了。"),
    (("404", "not found"), "接口地址或模型名不对。点「拉取可用模型」看这家到底有哪些模型。"),
    (("429", "rate"), "被限流了，或者额度用完。等一会儿再试。"),
    (("temperature",), "这个模型对采样参数有限制（Kimi K2 锁死 temperature=1，"
     "智谱部分模型上限 1）。程序会自动按它的范围重发，再看到这条说明重试也没过，换个模型试试。"),
    (("500", "502", "503", "504", "bad gateway"),
     "网关或代理拦了这次请求。国内直连国外厂商常见，换国内厂商或填一个兼容接口地址。"),
    (("timed out", "timeout", "超时"),
     "超时连不上。国内直连 OpenAI / Claude 通常不通，换国内厂商，或填一个兼容接口地址。"),
    (("getaddrinfo", "name or service"), "接口地址填错了，域名解析不出来。"),
    (("refused",), "连不上。用本机 Ollama 的话，先跑 ollama serve。"),
    (("ssl", "certificate"), "证书校验失败。公司代理或自建证书的话，检查一下代理设置。"),
)


def explain_error(err):
    s = str(err).lower()
    for keys, tip in ERROR_HINTS:
        for k in keys:
            if k in s:
                return tip
    return ""


def _err_text():
    """在 except 块里把当前异常转成字符串。

    给后台线程用：except ... as e 结束后 e 会被 Python 删掉，
    把它留进闭包等到主线程再取，会 NameError —— 回调一崩，界面就永远卡在"进行中"。
    """
    import re
    import traceback

    line = traceback.format_exc(limit=1).strip().splitlines()[-1]
    # 去掉 "aiun.providers.ProviderError: " 这类前缀，用户不需要看类名
    return re.sub(r"^[\w\.]*\w+Error:\s*", "", line)


def short_err(msg, limit=120):
    """错误原文压成一行短摘要。

    厂商返回的是几百字符的 JSON，原样贴出来：一占半屏，二把真正能看懂的
    人话提示挤出窗口。原文给一行就够，细节归 explain_error 的提示说。
    """
    s = " ".join(str(msg).split())
    if len(s) > limit:
        s = s[:limit].rstrip(" ,\"") + "…"
    return s


def panel_rect(canvas, x1, y1, x2, y2, **kw):
    """直角方块。

    界面必须是统一的直角：tkinter 的按钮、输入框、下拉框天生全是直角，
    只有手绘的这几块能自己决定形状。混着用（原生直角 + 手绘圆角）看着就是
    两套界面拼起来的，所以手绘的也走直角。
    """
    return canvas.create_rectangle(x1, y1, x2, y2, **kw)


class LogoBadge(tk.Canvas):
    """厂商标志：直角方块 + 缩写。"""

    def __init__(self, master, text, color, size=36, bg=C_TOP):
        super().__init__(master, width=size, height=size, highlightthickness=0, bg=bg)
        panel_rect(self, 1, 1, size - 1, size - 1, fill=color, outline="")
        self.create_text(size / 2, size / 2 + 1, text=text, fill="#ffffff",
                         font=(FONT, 12 if size >= 34 else 9, "bold"))


# ══════════════════════════════════════════════════════════
# 连接大模型
# ══════════════════════════════════════════════════════════
class ConnectWindow(tk.Toplevel):
    """选厂商 → 贴 Key → 程序自己去认模型。

    全程不出现「type / base_url」这类词：厂商决定前两个，
    模型名由「拉取」按钮拿 key 去问厂商要，用户只做选择。
    """

    def __init__(self, master, cfg, cfg_path, on_save=None):
        super().__init__(master)
        self.on_save = on_save
        self.cfg_path = cfg_path
        self.current = None
        self.providers = {}
        self.attend = {}
        self._cards = {}
        self._closed = False          # 窗口关掉后，后台线程的回调不能再碰控件
        self._op = {"fetch": False, "test": False}
        self._load(cfg or {})

        self.title("连接大模型")
        self.geometry(_centered(self, 1080, 800))
        self.minsize(*_fit(self, 940, 680))
        icon = _icon_path()
        if icon:
            try:
                self.iconbitmap(icon)
            except tk.TclError:
                pass
        self.transient(master)
        self.configure(bg=C_TOP)
        self.protocol("WM_DELETE_WINDOW", self._close)

        head = tk.Frame(self, bg=C_TOP)
        head.pack(fill="x", padx=24, pady=(18, 10))
        tk.Label(head, text="连接大模型", font=(FONT, 15, "bold"),
                 fg=C_TEXT, bg=C_TOP).pack(anchor="w")
        tk.Label(head, text="选一家，把它的 Key 贴进来，剩下的我们自己认。"
                           "模型名点「拉取」就会列出来，不用你记。",
                 font=(FONT, 9), fg=C_DIM, bg=C_TOP).pack(anchor="w", pady=(3, 0))

        body = tk.Frame(self, bg=C_TOP)
        body.pack(fill="both", expand=True, padx=24)
        self._build_vendor_list(body)
        self._build_detail(body)

        self._build_attend()

        foot = tk.Frame(self, bg=C_TOP)
        foot.pack(fill="x", padx=24, pady=(6, 14))
        tk.Label(foot, text="Key 用系统加密保存在你自己的用户目录里（不写进任何项目文件，"
                           "也不会随配置外传）。填一次就行，下次打开不用再填。",
                 font=(FONT, 8), fg="#8a6d00", bg=C_TOP).pack(side="left")
        tk.Button(foot, text="取消", font=(FONT, 9), relief="flat", padx=16, pady=6,
                  bg="#eee8db", fg=C_TEXT, activebackground="#e4ddcd",
                  command=self.destroy).pack(side="right", padx=(8, 0))
        self.save_btn = tk.Button(foot, text="保存并开始开会", font=(FONT, 9, "bold"),
                                  relief="flat", padx=18, pady=6, bg=C_ACCENT, fg="#ffffff",
                                  activebackground="#7a2c24", cursor="hand2",
                                  command=self._save_all)
        self.save_btn.pack(side="right")

        first = self.current or VENDORS[0]["key"]
        self._select(first)

    # ── 载入：把旧配置反推回「厂商」 ─────────────────────
    def _load(self, cfg):
        unknown = 0
        for name, d in (cfg.get("providers") or {}).items():
            vk = d.get("vendor") if d.get("vendor") in VENDOR_BY_KEY else None
            if vk is None:
                for k, v in VENDOR_BY_KEY.items():
                    if v["type"] == d.get("type") and (v["base_url"] or "") == (d.get("base_url") or ""):
                        vk = k
                        break
            if vk is None:
                unknown += 1
                if "compat" not in self.providers:
                    self.providers["compat"] = {
                        "type": d.get("type", "openai"), "base_url": d.get("base_url", ""),
                        "api_key": d.get("api_key") or keyring.load("compat"),
                        "model": d.get("model", ""),
                        "vendor": "compat", "_name": name}
                continue
            v = VENDOR_BY_KEY[vk]
            self.providers[vk] = {
                "type": v["type"], "base_url": d.get("base_url") or v["base_url"],
                "api_key": d.get("api_key") or keyring.load(vk),
                "model": d.get("model") or v["model"],
                "vendor": vk}

        for i, d in enumerate(cfg.get("delegates") or []):
            pname = d.get("provider", "")
            vk = None
            for k, pv in self.providers.items():
                if k == pname or pv.get("_name") == pname:
                    vk = k
                    break
            if vk is None:
                continue
            # 旧配置里的 proposer/adversary/reviewer 一律归一：现在三方平权
            self.attend[vk] = {"on": True, "role": PEER_ROLE,
                               "model": d.get("model") or self.providers[vk].get("model", "")}
        # 主持人：从旧配置里读回来，读不到就留给下拉自己选第一家
        ch = cfg.get("chair") or {}
        self.chair_name = ch.get("provider") or ""

        if not self.attend:
            # 没配过代表：已填 Key 的厂商默认参会。三家平权，不分配角色。
            for k, pv in self.providers.items():
                self.attend[k] = {
                    "on": bool(pv.get("api_key") or pv.get("type") in ("mock",)),
                    "role": PEER_ROLE,
                    "model": pv.get("model", "")}
        self.max_rounds = int(cfg.get("max_rounds") or 2)
        self.unknown = unknown

    # ── 左：厂商列表 ───────────────────────────────────
    def _build_vendor_list(self, parent):
        box = tk.Frame(parent, bg="#f5f1e8", highlightthickness=1,
                       highlightbackground=C_LINE, width=262)
        box.pack(side="left", fill="y")
        box.pack_propagate(False)
        tk.Label(box, text="大模型厂商", font=(FONT, 8, "bold"), fg=C_DIM,
                 bg="#f5f1e8").pack(anchor="w", padx=14, pady=(12, 6))
        self.vendor_box = tk.Frame(box, bg="#f5f1e8")
        self.vendor_box.pack(fill="both", expand=True, padx=8)

        for v in VENDORS:
            card = tk.Frame(self.vendor_box, bg="#f5f1e8")
            card.pack(fill="x", pady=1)
            badge = LogoBadge(card, v["tag"], v["color"], size=28, bg="#f5f1e8")
            badge.pack(side="left", padx=(8, 10), pady=4)
            txt = tk.Frame(card, bg="#f5f1e8")
            txt.pack(side="left", fill="x", expand=True)
            tk.Label(txt, text=v["name"], font=(FONT, 9, "bold"), fg=C_TEXT,
                     bg="#f5f1e8", anchor="w").pack(anchor="w")
            state = tk.Label(txt, text="", font=(FONT, 7), fg=C_DIM, bg="#f5f1e8",
                             anchor="w")
            state.pack(anchor="w")
            dot = tk.Label(card, text="", font=(FONT, 9), bg="#f5f1e8", width=2)
            dot.pack(side="right", padx=8)
            for w in (card, badge, txt) + tuple(txt.winfo_children()):
                w.bind("<Button-1>", lambda _e, k=v["key"]: self._select(k))
                w.bind("<Enter>", lambda _e, c=card: self._hover(c, "#eee8db"))
                w.bind("<Leave>", lambda _e, c=card, k=v["key"]: self._unhover(c, k))
            self._cards[v["key"]] = {"card": card, "state": state, "dot": dot,
                                     "sub": txt.winfo_children()[-1]}

    def _hover(self, card, color):
        self._paint(card, color)

    def _unhover(self, card, key):
        self._paint(card, "#eee8db" if key == self.current else "#f5f1e8")

    def _paint(self, card, color):
        def walk(w):
            for c in w.winfo_children():
                if isinstance(c, tk.Canvas):
                    try:
                        c.configure(bg=color)
                    except tk.TclError:
                        pass
                    continue
                try:
                    c.configure(bg=color)
                except tk.TclError:
                    pass
                walk(c)
        try:
            card.configure(bg=color)
        except tk.TclError:
            pass
        walk(card)

    def _vendor_state(self, key):
        """左栏每个厂商的当前状态：已连接 / 已填 Key / 未连接。"""
        pv = self.providers.get(key)
        v = VENDOR_BY_KEY[key]
        if not pv:
            return "未连接", "#c3c8d2"
        if v["type"] == "mock":
            return "可直接用", "#476b5d"
        if v["type"] == "ollama":
            return "本机（不用 Key）", "#476b5d"
        if pv.get("api_key"):
            return "已填 Key", "#476b5d"
        return "未填 Key", "#c3c8d2"

    def _refresh_vendor_list(self):
        for key, w in self._cards.items():
            txt, color = self._vendor_state(key)
            w["state"].configure(text=txt, fg=color)
            on = key in self.attend and self.attend[key]["on"]
            w["dot"].configure(text="✓" if on else "", fg=C_ACCENT)
            self._paint(w["card"], "#eee8db" if key == self.current else "#f5f1e8")

    # ── 右：选中厂商的详情 ─────────────────────────────
    def _build_detail(self, parent):
        self.detail = tk.Frame(parent, bg=C_TOP, highlightthickness=1,
                               highlightbackground=C_LINE)
        self.detail.pack(side="left", fill="both", expand=True, padx=(12, 0))
        self.d_top = tk.Frame(self.detail, bg=C_TOP)
        self.d_top.pack(fill="x", padx=20, pady=(16, 10))
        self.d_body = tk.Frame(self.detail, bg=C_TOP)
        self.d_body.pack(fill="both", expand=True, padx=20)
        # 文字换行宽度不能写死：窗口一缩窄，写死的 wraplength 比窗口还宽，
        # 字就冲出边缘被裁。跟住窗口实际宽度。
        self.bind("<Configure>", self._rewrap)

    def _rewrap(self, _e=None):
        w = self.d_body.winfo_width()
        w -= 36
        if w < 180:        # 窗口还没映射完/窄得离谱时不硬来，等下次缩放事件
            return
        stack = list(self.d_top.winfo_children()) + list(self.d_body.winfo_children())
        while stack:
            c = stack.pop()
            if isinstance(c, tk.Label):
                try:
                    if c.cget("wraplength") and int(str(c.cget("wraplength"))) > 0:
                        c.configure(wraplength=w)
                except (tk.TclError, ValueError):
                    pass
            stack.extend(c.winfo_children())

    def _select(self, key):
        # 切换厂商视为放弃上一次操作：否则上一个请求回来会把结果填到新厂商的控件上
        self._op["fetch"] = False
        self._op["test"] = False
        self.current = key
        v = VENDOR_BY_KEY[key]
        pv = self.providers.get(key) or {}

        for w in self.d_top.winfo_children():
            w.destroy()
        for w in self.d_body.winfo_children():
            w.destroy()

        head = tk.Frame(self.d_top, bg=C_TOP)
        head.pack(fill="x")
        LogoBadge(head, v["tag"], v["color"], size=44).pack(side="left")
        col = tk.Frame(head, bg=C_TOP)
        col.pack(side="left", padx=12)
        tk.Label(col, text=v["name"], font=(FONT, 13, "bold"), fg=C_TEXT,
                 bg=C_TOP, anchor="w").pack(anchor="w")
        tk.Label(col, text=v["hint"], font=(FONT, 8), fg=C_DIM, bg=C_TOP,
                 anchor="w", wraplength=430, justify="left").pack(anchor="w", pady=(2, 0))
        if v["url"]:
            link = tk.Label(head, text="去申请 ↗", font=(FONT, 8, "underline"),
                            fg=C_ACCENT, bg=C_TOP, cursor="hand2")
            link.pack(side="right", anchor="ne")
            link.bind("<Button-1>", lambda _e, u=v["url"]: self._open_url(u))

        # Key
        self._field_label("API Key" + ("" if v["type"] not in ("mock", "ollama")
                                       else "（这家不用 Key）"))
        krow = tk.Frame(self.d_body, bg=C_TOP)
        krow.pack(fill="x", pady=(4, 0))
        self.key_var = tk.StringVar(value=pv.get("api_key", ""))
        self.key_entry = tk.Entry(krow, textvariable=self.key_var, show="●",
                                  font=(FONT, 10), relief="solid", bd=1,
                                  highlightthickness=1, highlightbackground=C_LINE)
        self.key_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self.eye = tk.Label(krow, text="显示", font=(FONT, 8), fg=C_DIM, bg=C_TOP,
                            cursor="hand2", padx=8)
        self.eye.pack(side="left")
        self.eye.bind("<Button-1>", lambda _e: self._toggle_key())
        self.key_var.trace_add("write", lambda *_a: self._on_key_change())
        self.guess = tk.Label(self.d_body, text="", font=(FONT, 8), fg="#476b5d",
                              bg=C_TOP, anchor="w")
        self.guess.pack(anchor="w", pady=(3, 0))

        # 模型
        self._field_label("用哪个模型")
        mrow = tk.Frame(self.d_body, bg=C_TOP)
        mrow.pack(fill="x", pady=(4, 0))
        self.model_var = tk.StringVar(
            value=model_display(pv.get("model", v["model"])))
        self.model_box = ttk.Combobox(mrow, textvariable=self.model_var,
                                      values=[model_display(m) for m in v["models"]],
                                      width=34)
        self.model_box.pack(side="left")
        self.fetch_btn = tk.Button(mrow, text="拉取可用模型", font=(FONT, 8),
                                   relief="flat", bg="#eee8db", fg=C_TEXT, padx=10, pady=4,
                                   activebackground="#e4ddcd", cursor="hand2",
                                   command=self._fetch_models)
        self.fetch_btn.pack(side="left", padx=8)
        self.model_state = tk.Label(mrow, text="", font=(FONT, 8), fg=C_DIM, bg=C_TOP)
        self.model_state.pack(side="left")
        # 内置清单会过时，明说它的来源，别让用户以为这就是厂商现在有的
        self.model_state.configure(
            text="内置清单，可能过时" if v["models"] else "")
        self.model_note = tk.Label(self.d_body, text=self._note(v["model"]),
                                   font=(FONT, 8), fg=C_DIM, bg=C_TOP, anchor="w",
                                   wraplength=430, justify="left")
        self.model_note.pack(anchor="w", pady=(3, 0))
        tk.Label(self.d_body, text=MODEL_TIP, font=(FONT, 8), fg="#9aa2b4",
                 bg=C_TOP, anchor="w", wraplength=430, justify="left").pack(anchor="w")
        self.model_var.trace_add("write",
                                 lambda *_a: self.model_note.configure(
                                     text=self._note(self.model_var.get())))

        # 高级：接口地址（默认折叠，多数人一辈子不用动）
        self.adv_open = tk.BooleanVar(value=(v["type"] == "compat"))
        self.adv_btn = tk.Label(self.d_body, text="▸ 高级（接口地址）", font=(FONT, 8),
                                fg=C_DIM, bg=C_TOP, cursor="hand2", anchor="w")
        self.adv_btn.pack(anchor="w", pady=(14, 0))
        self.adv_btn.bind("<Button-1>", lambda _e: self._toggle_adv())
        self.adv_frame = tk.Frame(self.d_body, bg=C_TOP)
        self.url_var = tk.StringVar(value=pv.get("base_url", v["base_url"]))
        tk.Label(self.adv_frame, text="接口地址（换代理或自建才需要改）",
                 font=(FONT, 8), fg=C_DIM, bg=C_TOP).pack(anchor="w")
        tk.Entry(self.adv_frame, textvariable=self.url_var, font=(FONT, 9),
                 relief="solid", bd=1).pack(fill="x", ipady=4, pady=(3, 0))
        self._toggle_adv()

        # 操作
        bar = tk.Frame(self.d_body, bg=C_TOP)
        bar.pack(anchor="w", pady=(18, 0))
        self.test_btn = tk.Button(bar, text="测一下能不能连通", font=(FONT, 9),
                                  relief="flat", bg="#eee8db", fg=C_TEXT, padx=14, pady=6,
                                  activebackground="#e4ddcd", cursor="hand2",
                                  command=self._test)
        self.test_btn.pack(side="left")
        self.save_one = tk.Button(bar, text="保存这家", font=(FONT, 9, "bold"),
                                  relief="flat", bg=C_ACCENT, fg="#ffffff", padx=16, pady=6,
                                  activebackground="#7a2c24", cursor="hand2",
                                  command=self._save_current)
        self.save_one.pack(side="left", padx=8)
        if key in self.providers:
            rm = tk.Label(bar, text="移除", font=(FONT, 9), fg="#9c3a2f", bg=C_TOP,
                          cursor="hand2", padx=8)
            rm.pack(side="left")
            rm.bind("<Button-1>", lambda _e, k=key: self._remove(k))
        self.act_state = tk.Label(self.d_body, text="", font=(FONT, 8), fg=C_DIM,
                                  bg=C_TOP, anchor="w", wraplength=470, justify="left")
        self.act_state.pack(anchor="w", pady=(6, 0))

        self._refresh_vendor_list()
        # 面板刚建完布局数字还没定，等一拍再按真实宽度重排换行；
        # 只靠窗口缩放事件的话，首屏（没动过窗口）就还是写死的初值。
        self.after(60, self._rewrap)

    def _field_label(self, text):
        tk.Label(self.d_body, text=text, font=(FONT, 9, "bold"), fg=C_TEXT,
                 bg=C_TOP, anchor="w").pack(anchor="w", pady=(14, 0))

    def _note(self, model):
        return MODEL_LABELS.get(model_id(model), MODEL_FALLBACK)

    def _toggle_key(self):
        if self.key_entry.cget("show"):
            self.key_entry.configure(show="")
            self.eye.configure(text="隐藏")
        else:
            self.key_entry.configure(show="●")
            self.eye.configure(text="显示")

    def _toggle_adv(self):
        if self.adv_open.get():
            self.adv_frame.pack(fill="x", pady=(4, 0), after=self.adv_btn)
            self.adv_btn.configure(text="▾ 高级（接口地址）")
        else:
            self.adv_frame.pack_forget()
            self.adv_btn.configure(text="▸ 高级（接口地址）")

    def _open_url(self, url):
        import webbrowser
        webbrowser.open(url)

    def _on_key_change(self):
        """粘 Key 时按外形猜厂商：猜得出就自动切过去，猜不出不乱动。"""
        from .models import guess_vendor
        g = guess_vendor(self.key_var.get())
        if g and g != self.current:
            self.guess.configure(
                text="看着像 %s 的 Key，要不要切过去？[点这里]" % VENDOR_BY_KEY[g]["name"],
                fg=C_ACCENT, cursor="hand2")
            self.guess.bind("<Button-1>", lambda _e, k=g: self._select(k))
        else:
            self.guess.configure(text="", cursor="")

    # ── 动作 ───────────────────────────────────────────
    def _current_conf(self):
        v = VENDOR_BY_KEY[self.current]
        return {
            "type": v["type"],
            "base_url": self.url_var.get().strip() or v["base_url"],
            "api_key": self.key_var.get().strip(),
            "model": model_id(self.model_var.get()) or v["model"],
            "vendor": self.current,
        }

    def _save_current(self):
        conf = self._current_conf()
        v = VENDOR_BY_KEY[self.current]
        if v["type"] not in ("mock", "ollama") and not conf["api_key"]:
            self.act_state.configure(text="还没填 Key。%s" % v["hint"], fg="#a8842c")
            return
        if v["type"] == "compat" and not conf["base_url"]:
            self.act_state.configure(text="兼容接口需要填接口地址。", fg="#a8842c")
            return
        # Key 立刻进系统加密保险库（council.yaml 里永远不写明文）
        if v["type"] not in ("mock", "ollama"):
            keyring.save(self.current, conf["api_key"])
        self.providers[self.current] = conf
        self.attend.setdefault(self.current, {"on": True, "role": PEER_ROLE,
                                              "model": conf["model"]})
        self.attend[self.current]["model"] = conf["model"]
        self._refresh_vendor_list()
        self._build_attend()
        # 自动保存：这一步配置就已经落盘了，之后关窗/重开都在
        self._write_cfg(silent=True)
        self.act_state.configure(
            text="已保存（Key 已进系统加密保险库）。以后不用再填。到下面确认它参不参会。",
            fg="#476b5d")


    def _remove(self, key):
        self.providers.pop(key, None)
        self.attend.pop(key, None)
        self._refresh_vendor_list()
        self._build_attend()
        self._select(key)

    def _close(self):
        """关窗口时把标志位立起来：后台线程回来的结果不能再碰已经销毁的控件。"""
        self._closed = True
        self.destroy()

    def _gui(self, fn):
        def run():
            if self._closed:
                return
            try:
                fn()
            except tk.TclError:
                pass
        try:
            self.after(0, run)
        except tk.TclError:
            pass

    def _missing_key(self, conf):
        """这家需要 Key 却没填 —— 直接说话，别发一个注定失败的请求。"""
        v = VENDOR_BY_KEY[self.current]
        if v["type"] in ("mock", "ollama") or conf["api_key"]:
            return None
        return "还没填 Key。%s" % v["hint"]

    def _fetch_models(self):
        """拿 Key 去问厂商「我能用哪些模型」，列出来让用户选。"""
        if self._op["fetch"]:          # 正在拉：再点一次就是取消
            self._op["fetch"] = False
            self.fetch_btn.configure(text="拉取可用模型")
            self.model_state.configure(text="已取消", fg=C_DIM)
            return
        conf = self._current_conf()
        miss = self._missing_key(conf)
        if miss:
            self.model_state.configure(text="先填 Key", fg="#a8842c")
            self.act_state.configure(
                text=miss + "\n\n拉模型列表要拿你的 Key 去问厂商，没 Key 它不会告诉你。",
                fg="#a8842c")
            return
        self._op["fetch"] = True
        self.fetch_btn.configure(text="取消")
        self.model_state.configure(text="正在问 %s…（20 秒没回就放弃）"
                                   % VENDOR_BY_KEY[self.current]["name"], fg=C_DIM)

        def work():
            from .models import list_models
            try:
                ids = list_models(conf["type"], conf["base_url"], conf["api_key"])
                self._gui(lambda m=ids: self._on_models(m, None))
            except Exception:
                # 先把异常转成字符串：except 块结束时 e 会被删掉，
                # 留到 lambda 里再取会 NameError，回调直接死掉、按钮永远不恢复。
                msg = _err_text()
                self._gui(lambda m=msg: self._on_models(None, m))

        threading.Thread(target=work, daemon=True).start()

    def _on_models(self, ids, err):
        if not self._op["fetch"]:
            return
        self._op["fetch"] = False
        self.fetch_btn.configure(text="拉取可用模型")
        if err:
            self.model_state.configure(text="拉取失败（可手动填）", fg="#a8842c")
            self.act_state.configure(text="拉取失败：%s\n\n%s"
                                     % (short_err(err), explain_error(err) or
                                        "也可以手动填模型名，填这家控制台里显示的那个。"),
                                     fg="#a8842c")
            return
        if not ids:
            self.model_state.configure(text="这家没返回模型列表", fg="#a8842c")
            return
        shown = [model_display(i) for i in ids]
        self.model_box["values"] = shown
        cur = model_id(self.model_var.get())
        self.model_var.set(model_display(cur) if cur in ids else shown[0])
        self.model_state.configure(text="拿到 %d 个模型（厂商实时列表）" % len(ids),
                                   fg="#476b5d")
        self.act_state.configure(
            text="这是 %s 当前真实返回的 %d 个模型。列表里是接口名，"
                 "官网控制台上显示的名字可能不同，名字对不上是正常的。"
                 % (VENDOR_BY_KEY[self.current]["name"], len(ids)), fg="#476b5d")

    def _test(self):
        """发一个极小的请求确认 Key 能用。会花掉一点点 token。"""
        if self._op["test"]:           # 正在测：再点一次就是取消
            self._op["test"] = False
            self.test_btn.configure(text="测一下能不能连通")
            self.act_state.configure(text="已取消（刚才那条请求会在后台自己跑完）。",
                                     fg=C_DIM)
            return
        conf = self._current_conf()
        miss = self._missing_key(conf)
        if miss:
            self.act_state.configure(text=miss + "\n\n连通测试就是拿这个 Key 发一条最小请求，"
                                                 "没 Key 测不出东西。", fg="#a8842c")
            return
        self._op["test"] = True
        self.test_btn.configure(text="取消")
        self.act_state.configure(text="正在发一条最小请求（20 秒没回就是连不通，会消耗极少量 token）…",
                                 fg=C_DIM)

        def work():
            from . import providers
            try:
                p = providers.build(conf["type"], model=conf["model"],
                                    api_key=conf["api_key"], base_url=conf["base_url"])
                out = p.complete("只回复一个字：好", "好", temperature=0,
                                 max_tokens=8, timeout=PROBE_TIMEOUT)
                self._gui(lambda m=out[:60]: self._on_test(True, m))
            except Exception:
                msg = _err_text()      # 同上：异常必须先落地成字符串
                self._gui(lambda m=msg: self._on_test(False, m))

        threading.Thread(target=work, daemon=True).start()

    def _on_test(self, ok, msg):
        if not self._op["test"]:
            return
        self._op["test"] = False
        self.test_btn.configure(text="测一下能不能连通")
        if ok:
            self.act_state.configure(text="通了，模型回了：%s" % msg, fg="#476b5d")
        else:
            tip = explain_error(msg) or \
                "先确认 Key 填对、这家账号有余额；国内直连国外厂商通常不通，可换国内厂商。"
            self.act_state.configure(text="没通：%s\n\n%s" % (short_err(msg), tip), fg="#9c3a2f")

    # ── 参会设置 ───────────────────────────────────────
    def _build_attend(self):
        if hasattr(self, "attend_frame"):
            for w in self.attend_frame.winfo_children():
                w.destroy()
        else:
            self.attend_frame = tk.Frame(self, bg=C_TOP)
            self.attend_frame.pack(fill="x", padx=24, pady=(10, 0))

        f = self.attend_frame
        top = tk.Frame(f, bg=C_TOP)
        top.pack(fill="x")
        tk.Label(top, text="谁参会", font=(FONT, 11, "bold"), fg=C_TEXT,
                 bg=C_TOP).pack(side="left")
        tk.Label(top, text="三家平权：每家都出方案，也都会评别人的方案",
                 font=(FONT, 8), fg=C_DIM, bg=C_TOP).pack(side="left", padx=10)
        tk.Label(top, text="讨论轮数", font=(FONT, 9), fg=C_DIM, bg=C_TOP).pack(side="right")
        round_box = tk.Frame(top, bg=C_TOP)
        round_box.pack(side="right", padx=(0, 6))
        self.round_btns = {}
        for n in ("1", "2", "3"):
            b = tk.Label(round_box, text=n, font=(FONT, 9, "bold"), padx=10, pady=3,
                         cursor="hand2", bg="#eee8db", fg=C_DIM)
            b.pack(side="left", padx=(2, 0))
            b.bind("<Button-1>", lambda _e, v=n: self._set_rounds(v))
            self.round_btns[n] = b
        self._set_rounds(str(self.max_rounds))

        row = tk.Frame(f, bg=C_TOP)
        row.pack(fill="x", pady=(8, 0))
        if not self.providers:
            tk.Label(row, text="还没连接任何一家。左边选一家，贴上 Key，点「保存这家」。",
                     font=(FONT, 9), fg=C_DIM, bg=C_TOP).pack(anchor="w")
            return

        for key, pv in self.providers.items():
            v = VENDOR_BY_KEY.get(key, VENDOR_BY_KEY["compat"])
            name = v["name"] if key != "compat" else (pv.get("_name") or "兼容接口")
            a = self.attend.setdefault(key, {"on": False, "role": "reviewer",
                                             "model": pv.get("model", "")})
            card = tk.Frame(row, bg=C_TOP, highlightthickness=1,
                            highlightbackground=C_LINE)
            card.pack(side="left", padx=(0, 10), pady=4)
            inner = tk.Frame(card, bg=C_TOP)
            inner.pack(padx=10, pady=8)
            h = tk.Frame(inner, bg=C_TOP)
            h.pack(fill="x")
            LogoBadge(h, v["tag"], v["color"], size=26).pack(side="left")
            tk.Label(h, text=name, font=(FONT, 9, "bold"), fg=C_TEXT,
                     bg=C_TOP).pack(side="left", padx=6)

            sw = tk.Label(inner, text="参会中" if a["on"] else "不参会",
                          font=(FONT, 8, "bold"), padx=8, pady=3, cursor="hand2",
                          bg=C_ACCENT if a["on"] else "#eee8db",
                          fg="#ffffff" if a["on"] else C_DIM)
            sw.pack(anchor="w", pady=(6, 0))
            sw.bind("<Button-1>", lambda _e, k=key: self._toggle_attend(k))

            # 角色下拉已取消：三方平权，没有"反对派/审查员"之分了。
            # 这里只显示它当前用的模型，方便确认哪家参会。
            tk.Label(inner, text=(a.get("model") or pv.get("model") or ""),
                     font=(FONT, 7), fg=C_DIM, bg=C_TOP).pack(anchor="w", pady=(4, 0))

        self._build_chair_row()

    def _build_chair_row(self):
        """主持人用哪家模型。

        主持人是最后把三家方案收敛成一套的那个人，它弱，整套方案就弱。
        所以让它单独选，别跟着第一家走 —— 免费档（比如智谱 flash）当代表还行，
        当主持人就容易出草稿味和跑题。
        """
        f = self.attend_frame
        box = tk.Frame(f, bg=C_TOP)
        box.pack(fill="x", pady=(10, 0))
        tk.Label(box, text="主持人用哪家（最后收敛成最优方案的那一个）",
                 font=(FONT, 9, "bold"), fg=C_TEXT, bg=C_TOP).pack(side="left")
        names = [self._vendor_name(k) for k in self.providers]
        self.chair_cb = ttk.Combobox(box, values=names, state="readonly", width=18)
        cur = self.chair_name or (names[0] if names else "")
        if cur not in names:
            cur = names[0] if names else ""
        self.chair_name = cur
        self.chair_cb.set(cur)
        self.chair_cb.pack(side="left", padx=10)
        self.chair_cb.bind("<<ComboboxSelected>>", self._on_chair_change)
        tk.Label(box, text="建议选最强的那家 —— 它决定最终方案的质量",
                 font=(FONT, 8), fg=C_DIM, bg=C_TOP).pack(side="left")

    def _vendor_name(self, key):
        pv = self.providers.get(key) or {}
        v = VENDOR_BY_KEY.get(key, VENDOR_BY_KEY["compat"])
        return v["name"] if key != "compat" else (pv.get("_name") or "兼容接口")

    def _on_chair_change(self, _e):
        self.chair_name = self.chair_cb.get()
        self._write_cfg(silent=True)

    def _set_rounds(self, v):
        self.max_rounds = int(v)
        for k, b in self.round_btns.items():
            on = k == v
            b.configure(bg=C_ACCENT if on else "#eee8db", fg="#ffffff" if on else C_DIM)

    def _toggle_attend(self, key):
        a = self.attend.setdefault(key, {"on": False, "role": PEER_ROLE,
                                         "model": self.providers.get(key, {}).get("model", "")})
        a["on"] = not a["on"]
        self._build_attend()
        self._refresh_vendor_list()


    # ── 保存 ───────────────────────────────────────────
    def _build_cfg(self):
        """构造配置。注意：明文 Key 永远不进这份配置 —— Key 在系统加密保险库
        （aiun/keyring.py）里，开会时由 cli.build_delegates 从那里取。"""
        on = [(k, a) for k, a in self.attend.items() if a["on"] and k in self.providers]

        delegates = []
        for k, a in on:
            pv = self.providers[k]
            v = VENDOR_BY_KEY.get(k, VENDOR_BY_KEY["compat"])
            delegates.append({
                "name": v["name"] if k != "compat" else (pv.get("_name") or "兼容接口"),
                "role": a["role"], "provider": pv.get("_name") or k,
                "model": a.get("model") or pv.get("model") or "",
            })
        providers_out = {}
        for k, pv in self.providers.items():
            name = pv.get("_name") or k
            d = dict((kk, vv) for kk, vv in pv.items() if not kk.startswith("_"))
            d.pop("api_key", None)          # 保险库里有，yaml 里不落明文
            d["key_source"] = "keyring"
            providers_out[name] = d

        # 主持人单独选：只有在下拉里真选了这家、且这家已接入时才写进去
        chair_out = {"provider": "", "model": ""}
        for k, pv in self.providers.items():
            if self._vendor_name(k) == self.chair_name:
                chair_out = {"provider": pv.get("_name") or k,
                             "model": pv.get("model", "")}
                break
        return {
            "max_rounds": self.max_rounds,
            "anonymize": True,
            "chair": chair_out,
            "delegates": delegates,
            "providers": providers_out,
        }

    def _write_cfg(self, silent=False):
        """写盘。silent=True 供「保存这家」自动保存用：不弹窗、不关窗。"""
        if silent and not self.providers:
            return                       # 什么都没配过，别写空文件
        cfg = self._build_cfg()
        path = self.cfg_path or os.path.join(base_dir(), "council.yaml")
        if not os.path.isabs(path) or not os.path.isdir(os.path.dirname(path)):
            if silent:
                path = os.path.join(base_dir(), "council.yaml")
            else:
                path = filedialog.asksaveasfilename(
                    parent=self, title="保存配置到", initialfile="council.yaml",
                    defaultextension=".yaml",
                    filetypes=[("YAML", "*.yaml *.yml"), ("JSON", "*.json")])
                if not path:
                    return False
        try:
            with open(path, "w", encoding="utf-8") as f:
                if path.lower().endswith(".json"):
                    import json
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                else:
                    import yaml
                    yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        except Exception as e:
            if silent:
                return False
            return messagebox.showerror("砚", "保存失败：%s" % e, parent=self)

        self.cfg_path = path
        if self.on_save:
            self.on_save(cfg, path)
        if not silent:
            messagebox.showinfo(
                "砚", "已保存到 %s\n\n%d 位代表参会，回到主界面发议题就能开会。\n\n"
                        "Key 没有写进这个文件 —— 它存在系统加密保险库里，以后不用再填。"
                % (path, len(cfg["delegates"])), parent=self)
            self.destroy()
        return True

    def _save_all(self):
        on = [(k, a) for k, a in self.attend.items() if a["on"] and k in self.providers]
        if not on:
            messagebox.showwarning("砚", "还没让任何一家参会。\n\n"
                                           "先在左边选厂商、贴 Key、点「保存这家」，"
                                           "再在这里把它打开。", parent=self)
            return
        # 平权之后不再检查"有没有反对派"，但另一个风险还在：
        # 三家都是同一个模型 = 自己跟自己开会，讨论不出任何新东西。
        models = set((self.providers[k].get("model") or "") for k, _a in on)
        if len(models) < 2 and len(on) > 1:
            if not messagebox.askyesno(
                    "砚",
                    "参会的几家用的都是同一个模型（%s）。\n"
                    "这样等于一个模型自己跟自己开会，取长补短就失去意义了。\n\n"
                    "仍然按现在的配置保存？" % (models.pop() if models else "同一个模型"),
                    parent=self):
                return
        self._write_cfg()
# ══════════════════════════════════════════════════════════
# 主窗口
# ══════════════════════════════════════════════════════════
class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.busy = False
        self.cfg_path = os.path.join(base_dir(), "council.yaml")
        self.cfg = {"max_rounds": 2, "anonymize": True, "chair": {},
                    "delegates": [], "providers": {}}
        self.roster = {}
        self._wraps = []
        self._last_wrap = None      # 上次重排用的宽度，没变就不重排
        self._wrap_job = None       # 合并密集的尺寸事件
        self.last_dir = None
        self._stop_event = None       # 会议进行中可「停止」
        self._typing_since = None     # 当前这条发言从什么时候开始等
        self._typing_name = ""

        root.title("砚 · 多方圆桌")
        # 想要 1720x1030（对齐 WorkBuddy 的手感，实测其窗口 1721x1033），
        # 但尺寸要按屏幕可用区收一遍：小屏幕、高缩放、带任务栏的情况都不能
        # 让窗口底部（输入框那一条）跑出屏幕。最小尺寸同理，否则最小尺寸比
        # 屏幕还大时用户根本没法把窗口缩小到能看全。
        root.geometry(_centered(root, 1720, 1030))
        root.minsize(*_fit(root, 1100, 720))
        icon = _icon_path()
        if icon:
            try:
                root.iconbitmap(icon)
            except tk.TclError:
                pass
        root.configure(bg=C_CHAT)
        try:
            ttk.Style().theme_use("clam")
        except tk.TclError:
            pass

        self._build_sidebar()
        self._build_main()

        self._load_cfg(silent=True)
        # 接入过模型就直接进正式模式：名单和开会用的都是他自己那几家，
        # 没接入才留在演示（本地假代表）。
        self._set_mode("real" if (self.cfg.get("delegates") or []) else "demo")
        # 开场白放在模式定下来之后：接没接入模型，说的是两套话
        self._hello()
        root.after(80, self._drain)

        # 尺寸自适应：窗口变大变小，侧栏和输入框跟着让位，保证输入区永远完整
        self._last_wh = (0, 0)
        root.bind("<Configure>", self._on_win)
        root.after(400, self._ensure_visible)
        # 气泡宽度也跟窗口走：首屏布局数字出来后刷一次，不然永远停在写死的初值，
        # 宽窗口右边空一大块
        root.after(250, self._apply_wrap)

    # ── 气泡换行宽度 ───────────────────────────────────
    def _wrap_width(self):
        """气泡该多宽：跟聊天区当前实际宽度走，宽窗口不空右边，窄窗口不溢出。"""
        try:
            w = self.chat.winfo_width() - 150      # 头像 + 内边距 + 滚动条
        except Exception:
            return 560
        if w < 200:
            return 560
        return max(320, min(880, w))

    def _apply_wrap(self):
        w = self._wrap_width()
        if w == self._last_wrap:
            return                            # 宽度没变就别动，重排是有代价的
        self._last_wrap = w
        for lb in self._wraps:
            lb.configure(wraplength=w)

    def _on_win(self, event):
        """窗口尺寸变了：侧栏宽度、输入框高度跟着调整。

        目的是任何尺寸下都看得到完整输入框 —— 大屏多给两行，小屏收到两行，
        但不让它被挤出可视区。
        """
        if event.widget is not self.root:
            return
        w, h = event.width, event.height
        if (w, h) == self._last_wh or w < 2 or h < 2:
            return
        self._last_wh = (w, h)

        side_w = 248 if w >= 1500 else (220 if w >= 1180 else 190)
        if self.side.cget("width") != side_w:
            self.side.configure(width=side_w)

        rows = 4 if h >= 940 else (3 if h >= 760 else 2)
        try:
            if int(float(self.entry.cget("height"))) != rows:
                self.entry.configure(height=rows)
        except (tk.TclError, ValueError):
            pass

    def _ensure_visible(self):
        """兜底：窗口万一还是伸出了屏幕下沿（多屏、任务栏在侧边等），收一次。"""
        try:
            if self.root.state() == "zoomed":
                return
            self.root.update_idletasks()
            top = self.root.winfo_rooty()
            h = self.root.winfo_height()
            sh = self.root.winfo_screenheight()
        except Exception:
            return
        if top + h > sh - 40:                  # 底部已经越过屏幕下沿
            self.root.geometry(_centered(self.root, self.root.winfo_width(), h))

    # ── 左侧：会议室 + 代表名单 ─────────────────────────
    def _build_sidebar(self):
        side = tk.Frame(self.root, bg=C_SIDE, width=248)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        self.side = side

        brand = tk.Frame(side, bg=C_SIDE)
        brand.pack(fill="x", padx=16, pady=(18, 14))
        tk.Label(brand, text="砚", font=(FONT, 20, "bold"),
                 fg="#ffffff", bg=C_SIDE).pack(side="left")
        tk.Label(brand, text="多方圆桌", font=(FONT, 9),
                 fg=C_SIDE_DIM, bg=C_SIDE).pack(side="left", padx=(8, 0), pady=(6, 0))

        tk.Label(side, text="会议室", font=(FONT, 8, "bold"),
                 fg=C_SIDE_DIM, bg=C_SIDE).pack(anchor="w", padx=18, pady=(6, 6))

        room = tk.Frame(side, bg=C_SIDE_HI)
        room.pack(fill="x", padx=10)
        inner = tk.Frame(room, bg=C_SIDE_HI)
        inner.pack(fill="x", padx=10, pady=10)
        tk.Label(inner, text="圆桌会议室", font=(FONT, 10, "bold"),
                 fg=C_SIDE_TX, bg=C_SIDE_HI).pack(anchor="w")
        self.room_sub = tk.Label(inner, text="等待发起议题", font=(FONT, 8),
                                 fg=C_SIDE_DIM, bg=C_SIDE_HI, anchor="w")
        self.room_sub.pack(anchor="w", fill="x", pady=(2, 0))

        self.roster_head = tk.Label(side, text="与会代表", font=(FONT, 8, "bold"),
                                    fg=C_SIDE_DIM, bg=C_SIDE)
        self.roster_head.pack(anchor="w", padx=18, pady=(20, 6))
        self.roster_box = tk.Frame(side, bg=C_SIDE)
        self.roster_box.pack(fill="x", padx=10)

        foot = tk.Frame(side, bg=C_SIDE)
        foot.pack(side="bottom", fill="x", padx=10, pady=12)
        self._side_button(foot, "⚙  连接大模型 · 填 Key", self._open_settings).pack(fill="x", pady=2)
        self._side_button(foot, "📁  打开会议记录", self._open_dir).pack(fill="x", pady=2)
        # 本地方案库：每场会综合出来的优选方案都存在这里，下次相近议题自动翻出来
        self.note_btn = self._side_button(foot, "📚  本地方案库", self._open_notes)
        self.note_btn.pack(fill="x", pady=2)
        self._side_button(foot, "📤  导出给 AI 助手", self._export_notes).pack(fill="x", pady=2)
        self._refresh_note_count()

    def _refresh_note_count(self):
        """侧栏随时显示方案库攒了多少套 —— 看不见的积累等于没积累。"""
        try:
            n = notes.count(base_dir())
        except Exception:
            n = 0
        self.note_btn.configure(
            text="📚  本地方案库 · %d 套" % n if n else "📚  本地方案库（空）")

    def _open_notes(self):
        d = os.path.join(base_dir(), "experience")
        try:
            if not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            os.startfile(d)
        except Exception:
            messagebox.showinfo("本地方案库",
                                "位置：%s\n\n每场会综合出来的方案都存在这里。"
                                "下次遇到相近议题会自动发给代表参考。" % d,
                                parent=self.root)

    def _export_notes(self):
        """把攒下的方案导出成一份别的 AI 助手能直接读的文件。

        砚攒的经验放在本地，只有砚自己看得见。导出成纯文本之后，Codex / Claude /
        Cursor 这类工具就能拿它当项目背景 —— 这是「知识为我所用」闭环的那一步。
        """
        try:
            path = notes.export_markdown(base_dir())
        except Exception as e:
            messagebox.showerror("砚", "导出失败：%s" % e, parent=self.root)
            return
        if not path:
            messagebox.showinfo(
                "导出给 AI 助手",
                "本地方案库还是空的，没什么可导出。\n\n"
                "接上 Key 开一场正式会议，会议结束那套综合方案会自动存进方案库，\n"
                "到那时再导出就有东西了（演示模式不入库 —— 假代表凑的东西不算经验）。",
                parent=self.root)
            return
        try:
            n = notes.count(base_dir())
        except Exception:
            n = 0
        messagebox.showinfo(
            "已导出 %d 套方案" % n,
            "文件：%s\n\n"
            "怎么用（按你的工具挑一个）：\n"
            "1. 丢进项目根目录，在 AGENTS.md（Codex）或 CLAUDE.md 里加一行：\n"
            "     做相关决策前先读《%s》。\n"
            "2. 直接把内容贴给 AI 助手当背景资料。\n"
            "3. 放进 ~/.workbuddy/skills/<名字>/SKILL.md，做成长期生效的技能。\n\n"
            "文件夹已经替你打开了，方便直接拖走。"
            % (path, os.path.basename(path)),
            parent=self.root)
        try:
            os.startfile(os.path.dirname(path))
        except Exception:
            pass

    def _side_button(self, parent, text, cmd):
        btn = tk.Label(parent, text=text, font=(FONT, 9), fg=C_SIDE_TX, bg=C_SIDE,
                       anchor="w", padx=10, pady=7, cursor="hand2")
        btn.bind("<Button-1>", lambda _e: cmd())
        btn.bind("<Enter>", lambda e: e.widget.configure(bg=C_SIDE_HI))
        btn.bind("<Leave>", lambda e: e.widget.configure(bg=C_SIDE))
        return btn

    def _refresh_roster(self):
        for w in self.roster_box.winfo_children():
            w.destroy()
        self.roster = {}

        if self.mode_is_demo():
            # 演示：名单里也写明白是演示，不冒充用户自己的模型
            rows = [(n, r, m) for n, r, m in DEMO_ROSTER]
            rows.append(("主持人", "chair", "mock-chair"))
        else:
            # 正式：名单就是用户接入的那几家，一行一个 AI
            rows = []
            for d in self.cfg.get("delegates") or []:
                rows.append((d.get("name") or "代表", d.get("role", "peer"),
                             pretty_model(d.get("model") or "")))
            chair = self.cfg.get("chair") or {}
            chair_sub = " · ".join(
                x for x in (chair.get("provider"),
                            pretty_model(chair.get("model", ""))) if x)
            rows.append(("主持人", "chair", chair_sub))

        head = "与会代表 · %d" % len(rows)
        if self.mode_is_demo():
            head += "（演示，还没接入真模型）"
        self.roster_head.configure(text=head)
        for name, role, model in rows:
            row = tk.Frame(self.roster_box, bg=C_SIDE)
            row.pack(fill="x", pady=1)
            row.bind("<Double-Button-1>", lambda _e, r=role: self._show_brief(r))

            av = Avatar(row, name, size=30, bg=C_SIDE)
            av.pack(side="left", padx=(8, 8), pady=6)

            mid = tk.Frame(row, bg=C_SIDE)
            mid.pack(side="left", fill="x", expand=True)
            n_lbl = tk.Label(mid, text=name, font=(FONT, 9, "bold"),
                             fg=C_SIDE_TX, bg=C_SIDE, anchor="w")
            n_lbl.pack(anchor="w")
            sub = model or ROLE_META.get(role, ("代表", ""))[0]
            m_lbl = tk.Label(mid, text=sub, font=(FONT, 8), fg=C_SIDE_DIM,
                             bg=C_SIDE, anchor="w")
            m_lbl.pack(anchor="w")

            tag_txt, tag_color = ROLE_META.get(role, ("代表", C_SIDE_DIM))
            tag = tk.Label(row, text=tag_txt, font=(FONT, 7, "bold"), fg="#ffffff",
                           bg=tag_color, padx=5, pady=1)
            tag.pack(side="right", padx=(6, 8))
            tag._keep_bg = True   # 角色徽章有自己的颜色，hover 时不要被刷掉

            enter = lambda _e, r=row: self._paint_row(r, C_SIDE_HI)
            leave = lambda _e, r=row: self._paint_row(r, C_SIDE)
            for w in (row, av, mid, n_lbl, m_lbl):
                w.bind("<Enter>", enter)
                w.bind("<Leave>", leave)

            self.roster[name] = {"row": row, "tag": tag, "role": role}

    def _paint_row(self, row, color):
        """整行换底色：行内所有控件都要跟着换，否则会留一块补丁。"""
        def walk(w):
            for c in w.winfo_children():
                if getattr(c, "_keep_bg", False):
                    continue
                try:
                    c.configure(bg=color)
                except tk.TclError:
                    pass
                walk(c)
        try:
            row.configure(bg=color)
        except tk.TclError:
            pass
        walk(row)

    def _set_delegate_state(self, name, text=None, color=C_SIDE_DIM):
        """代表状态：发言中 / 已投赞成 / 在线。"""
        info = self.roster.get(name)
        if not info:
            return
        tag = info["tag"]
        if text is None:
            return
        tag.configure(text=text, bg=color)

    def _show_brief(self, role):
        messagebox.showinfo("行事准则", ROLE_BRIEFS.get(role, ROLE_BRIEFS["proposer"]),
                            parent=self.root)

    # ── 右侧：聊天区 ───────────────────────────────────
    def _build_main(self):
        main = tk.Frame(self.root, bg=C_CHAT)
        main.pack(side="left", fill="both", expand=True)

        top = tk.Frame(main, bg=C_TOP, height=58)
        top.pack(fill="x")
        top.pack_propagate(False)
        tk.Frame(main, bg=C_LINE, height=1).pack(fill="x")

        left = tk.Frame(top, bg=C_TOP)
        left.pack(side="left", padx=18)
        self.title_lbl = tk.Label(left, text="圆桌会议室", font=(FONT, 12, "bold"),
                                  fg=C_TEXT, bg=C_TOP)
        self.title_lbl.pack(anchor="w", pady=(8, 0))
        self.sub_lbl = tk.Label(left, text="输入议题，各方代表开始讨论",
                                font=(FONT, 8), fg=C_DIM, bg=C_TOP)
        self.sub_lbl.pack(anchor="w")

        right = tk.Frame(top, bg=C_TOP)
        right.pack(side="right", padx=16)
        self.mode = tk.StringVar(value="demo")
        self.mode_btns = {}
        box = tk.Frame(right, bg=C_TOP)
        box.pack(side="left", padx=(0, 14))
        for key, txt in (("demo", "演示"), ("real", "正式")):
            b = tk.Label(box, text=txt, font=(FONT, 8, "bold"), padx=10, pady=4,
                         cursor="hand2", bg="#eee8db", fg=C_DIM)
            b.pack(side="left", padx=(2, 0))
            b.bind("<Button-1>", lambda _e, k=key: self._set_mode(k))
            self.mode_btns[key] = b
        tk.Label(right, text="轮数", font=(FONT, 8), fg=C_DIM, bg=C_TOP).pack(side="left")
        self.rounds = tk.StringVar(value="2")
        tk.Spinbox(right, from_=1, to=5, width=3, font=(FONT, 9),
                   textvariable=self.rounds).pack(side="left", padx=(4, 0))
        self._set_mode("demo")

        # pack 顺序就是挤压顺序：tk 在空间不够时从最后 pack 的控件开始压。
        # 所以输入区先锁定（side=bottom），"正在发言"提示次之，聊天流排最后
        # ——窗口再矮，被压缩的也是聊天流，输入框永远完整可输入。
        comp = tk.Frame(main, bg=C_TOP)
        comp.pack(side="bottom", fill="x", padx=14, pady=(4, 14))
        self.typing = tk.Label(main, text="", font=(FONT, 8), fg=C_DIM, bg=C_CHAT,
                               anchor="w")
        self.typing.pack(side="bottom", fill="x", padx=20)

        chat_wrap = tk.Frame(main, bg=C_CHAT)
        chat_wrap.pack(fill="both", expand=True)
        self.chat = tk.Text(chat_wrap, wrap="word", bg=C_CHAT, bd=0,
                            highlightthickness=0, spacing1=2, spacing3=8,
                            padx=14, pady=10, cursor="arrow", font=(FONT, 10),
                            undo=False)
        self.chat.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(chat_wrap, command=self.chat.yview)
        sb.pack(side="right", fill="y")
        self.chat.configure(yscrollcommand=sb.set)
        self.chat.tag_configure("center", justify="center")
        self.chat.tag_configure("me", justify="right", rmargin=18)
        self.chat.bind("<Configure>", self._on_chat_resize)
        # 滚轮统一在 root 上收：鼠标停在气泡上时，事件会被气泡那个嵌入控件吃掉，
        # 传不到 Text，表现就是滚不动或者一格一格跳（"卡卡的"）。
        self.root.bind_all("<MouseWheel>", self._on_wheel)
        self.root.bind_all("<Button-4>", self._on_wheel)
        self.root.bind_all("<Button-5>", self._on_wheel)

        tk.Frame(comp, bg=C_LINE, height=1).pack(fill="x")
        body = tk.Frame(comp, bg=C_TOP)
        body.pack(fill="x", padx=10, pady=10)

        self.entry = tk.Text(body, height=3, wrap="word", bd=0, highlightthickness=0,
                             font=(FONT, 10), fg=C_TEXT, bg=C_TOP,
                             insertbackground=C_ACCENT)
        self.entry.pack(fill="x")
        # 不预填示例议题：预填的文字会被当成真内容发出去（用户一按 Enter
        # 就开了一场他没出题的会），还让每次进来都要先手动删干净。
        self.entry.bind("<Return>", self._on_enter)
        self.entry.bind("<Shift-Return>", lambda _e: None)

        row = tk.Frame(body, bg=C_TOP)
        row.pack(fill="x", pady=(8, 0))
        tk.Label(row, text="Enter 发起会议 · Shift+Enter 换行", font=(FONT, 8),
                 fg=C_DIM, bg=C_TOP).pack(side="left")
        self.send_btn = tk.Button(row, text="发起会议", font=(FONT, 9, "bold"),
                                  bg=C_ACCENT, fg="#ffffff", activebackground="#7a2c24",
                                  activeforeground="#ffffff", relief="flat", padx=20, pady=5,
                                  cursor="hand2", command=self._on_send)
        self.send_btn.pack(side="right")

    def _on_chat_resize(self, _event=None):
        """聊天区尺寸变了：重排气泡宽度，但合并成一次再做。

        尺寸事件非常密集（插入内容、滚动、拉窗口都会来），每来一次就把几十个
        气泡的 wraplength 重设一遍，等于强迫 Text 重新布局所有嵌入窗口 ——
        这是滚动掉帧的主因之一。合并掉，且宽度没真变就不动手。
        """
        job = getattr(self, "_wrap_job", None)
        if job:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        self._wrap_job = self.root.after(150, self._apply_wrap)

    def _in_chat(self, w):
        """这个控件是不是聊天区里的（气泡、阶段条都算）。"""
        while w is not None:
            if w is self.chat:
                return True
            w = getattr(w, "master", None)
        return False

    def _on_wheel(self, event):
        """滚轮：只接管落在聊天区上的那一次，其余交回各自的默认行为。

        tk 默认一格跳 3 行，颗粒太粗；这里按像素滚，细腻得多。
        """
        try:
            w = self.root.winfo_containing(event.x_root, event.y_root)
        except Exception:
            return None
        if w is None or not self._in_chat(w):
            return None                      # 输入框、侧栏各自滚自己的
        delta = getattr(event, "delta", 0)
        if delta:
            step = -int(delta / 3)           # 一格 120 → 40px
        elif getattr(event, "num", 0) == 4:
            step = -90
        elif getattr(event, "num", 0) == 5:
            step = 90
        else:
            return None
        if step:
            self.chat.yview_scroll(step, "pixels")
        return "break"

    def _on_enter(self, _e):
        if not self.busy:
            self._run()
        return "break"

    def mode_is_demo(self):
        return self.mode.get() == "demo"

    def _set_mode(self, key):
        self.mode.set(key)
        for k, b in self.mode_btns.items():
            on = (k == key)
            b.configure(bg=C_ACCENT if on else "#eee8db",
                        fg="#ffffff" if on else C_DIM)
        self._refresh_roster()

    # ── 消息渲染 ───────────────────────────────────────
    def _insert(self, widget, tag=None):
        self.chat.configure(state="normal")
        self.chat.insert("end", "\n", ())
        self.chat.window_create("end", window=widget)
        self.chat.insert("end", "\n", (tag,) if tag else ())
        self.chat.configure(state="disabled")
        if self.chat.yview()[1] > 0.97:
            self.chat.see("end")

    def _push_sys(self, text, center=True):
        f = tk.Frame(self.chat, bg=C_CHAT)
        tk.Label(f, text=text, font=(FONT, 8), fg=C_DIM, bg=C_CHAT,
                 wraplength=min(640, self._wrap_width()), justify="center").pack(padx=24, pady=2)
        self._insert(f, "center")

    def _push_stage(self, stage, note=""):
        f = tk.Frame(self.chat, bg=C_CHAT)
        line = tk.Frame(f, bg=C_CHAT)
        line.pack(padx=20, pady=(10, 2))
        tk.Label(line, text="—" * 6, fg=C_LINE, bg=C_CHAT, font=(FONT, 8)).pack(side="left", padx=(0, 8))
        tk.Label(line, text=stage, fg="#6b7383", bg=C_CHAT,
                 font=(FONT, 9, "bold")).pack(side="left")
        tk.Label(line, text="—" * 6, fg=C_LINE, bg=C_CHAT, font=(FONT, 8)).pack(side="left", padx=(8, 0))
        if note:
            tk.Label(f, text=note, fg=C_DIM, bg=C_CHAT, font=(FONT, 8)).pack(padx=20)
        self._insert(f, "center")

    def _push_msg(self, name, text, role="proposer", mine=False, kind=None,
                  verdict=None, model=None, copy_text=None):
        outer = tk.Frame(self.chat, bg=C_CHAT)
        row = tk.Frame(outer, bg=C_CHAT)
        row.pack(anchor="e" if mine else "w")

        av = Avatar(row, name, size=36, bg=C_CHAT)
        av.pack(side="right" if mine else "left", padx=(0, 10) if mine else (0, 10),
                anchor="ne")

        col = tk.Frame(row, bg=C_CHAT)
        col.pack(side="right" if mine else "left")

        head = tk.Frame(col, bg=C_CHAT)
        head.pack(fill="x" if not mine else "x")
        if mine:
            tk.Label(head, text=now(), font=(FONT, 7), fg=C_DIM, bg=C_CHAT).pack(side="right")
            tk.Label(head, text=name, font=(FONT, 9, "bold"), fg=C_TEXT,
                     bg=C_CHAT).pack(side="right", padx=(0, 6))
        else:
            tk.Label(head, text=name, font=(FONT, 9, "bold"), fg=C_TEXT,
                     bg=C_CHAT).pack(side="left")
            tag_txt, tag_color = ROLE_META.get(role, ("代表", C_DIM))
            tk.Label(head, text=tag_txt, font=(FONT, 7, "bold"), fg="#ffffff",
                     bg=tag_color, padx=5, pady=1).pack(side="left", padx=6)
            tk.Label(head, text=now(), font=(FONT, 7), fg=C_DIM, bg=C_CHAT).pack(side="left")

        bubble_bg = C_BUBBLE_ME if mine else (C_RESOL if kind == "resolution" else C_BUBBLE)
        fg = "#ffffff" if mine else C_TEXT
        bubble = tk.Frame(col, bg=bubble_bg, highlightthickness=1,
                          highlightbackground=C_LINE if not mine else C_BUBBLE_ME)
        bubble.pack(anchor="e" if mine else "w")

        if kind == "resolution":
            # 最优方案是 markdown 写的（## 最优方案 / - 条目），聊天里去掉记号更干净
            lines = []
            for ln in text.splitlines():
                ln = ln.replace("## ", "").strip()
                if ln == "-":
                    continue
                if ln.startswith("- "):
                    ln = "· " + ln[2:]
                lines.append(ln)
            if lines and lines[0] in ("最优方案", "方案"):
                lines = lines[1:]   # 卡片已有标题，正文里那句就不重复了
            text = "\n".join(lines)
            copy_text = text        # 复制按钮带走的是去掉记号的干净正文
            tk.Label(bubble, text="最 优 方 案", font=(FONT, 8, "bold"), fg="#a8842c",
                     bg=C_RESOL).pack(anchor="w", padx=12, pady=(10, 0))

        body = tk.Label(bubble, text=text, font=(FONT, 10), fg=fg, bg=bubble_bg,
                        justify="left", wraplength=self._wrap_width(), anchor="w")
        body.pack(padx=12, pady=9)
        self._wraps.append(body)

        if copy_text:
            cb = tk.Frame(col, bg=C_CHAT)
            cb.pack(anchor="w", pady=(4, 0))

            def _copy(t=copy_text):
                self.chat.clipboard_clear()
                self.chat.clipboard_append(t)
                self.typing.configure(text="最优方案已复制，可以直接去粘贴了")

            tk.Button(cb, text="复制方案", font=(FONT, 8), fg=C_DIM, bg="#eee8db",
                      activebackground="#e2dccd", activeforeground=C_TEXT,
                      relief="flat", cursor="hand2", command=_copy).pack()

        if verdict:
            v_txt, v_color = VERDICT_META.get(verdict, (verdict, C_DIM))
            vb = tk.Frame(col, bg=C_CHAT)
            vb.pack(anchor="e" if mine else "w", pady=(4, 0))
            tk.Label(vb, text=v_txt + "这套方案", font=(FONT, 7, "bold"), fg="#ffffff",
                     bg=v_color, padx=6, pady=2).pack()

        self._insert(outer, "me" if mine else None)

    def _push_result(self, tally, rounds, converged, md_path):
        card = tk.Frame(self.chat, bg=C_BUBBLE, highlightthickness=1,
                        highlightbackground=C_LINE)
        pad = tk.Frame(card, bg=C_BUBBLE)
        pad.pack(padx=16, pady=12)
        tk.Label(pad, text="会议结束", font=(FONT, 10, "bold"), fg=C_TEXT,
                 bg=C_BUBBLE).pack(anchor="w")
        tk.Label(pad, text="进行 %d 轮%s" % (rounds, "（无新论点，提前收敛）" if converged else ""),
                 font=(FONT, 8), fg=C_DIM, bg=C_BUBBLE).pack(anchor="w", pady=(2, 8))

        grid = tk.Frame(pad, bg=C_BUBBLE)
        grid.pack(anchor="w")
        keys = ["AGREE", "RESERVE", "DISAGREE"] + (
            ["UNPARSED"] if tally.get("UNPARSED") else [])
        for key in keys:
            txt, color = VERDICT_META[key]
            cell = tk.Frame(grid, bg=C_BUBBLE)
            cell.pack(side="left", padx=(0, 18))
            tk.Label(cell, text=str(tally[key]), font=(FONT, 16, "bold"), fg=color,
                     bg=C_BUBBLE).pack()
            tk.Label(cell, text=txt, font=(FONT, 8), fg=C_DIM, bg=C_BUBBLE).pack()

        tk.Label(pad, text="记录：%s" % md_path, font=(FONT, 8), fg=C_DIM,
                 bg=C_BUBBLE, wraplength=min(640, self._wrap_width()),
                 justify="left").pack(anchor="w", pady=(10, 0))
        self._insert(card, "center")

    def _hello(self):
        if self.mode_is_demo():
            self._push_sys("这里是砚的圆桌会议室。\n"
                           "把议题发进群里，各家先拿出自己的办法，再互相取长补短，"
                           "最后由主持人综合成一套方案。\n\n"
                           "现在还没接入模型，场上是三个本地假代表（不联网、不花钱），"
                           "只用来看流程。想让真正的大模型上桌：点左下角「连接大模型」，"
                           "选一家、贴 Key，名单就会换成你接入的那几家。")
        else:
            names = "、".join(d.get("name") or "代表"
                              for d in (self.cfg.get("delegates") or []))
            self._push_sys("已接入：%s\n\n写议题，发进群里就开会。" % (names or "（暂无代表）"))

    # ── 配置 ───────────────────────────────────────────
    def _load_cfg(self, silent=False):
        migrated = False
        if self.cfg_path and os.path.isfile(self.cfg_path):
            try:
                self.cfg = load_config(self.cfg_path) or {}
            except Exception as e:
                if not silent:
                    messagebox.showerror("砚", "读取失败：%s" % e)
                return
            # 老版本的 yaml 里可能有明文 Key：搬进加密保险库，并从文件里删掉
            for name, d in (self.cfg.get("providers") or {}).items():
                if d.get("api_key"):
                    keyring.save(d.get("vendor") or name, d["api_key"])
                    d.pop("api_key", None)
                    migrated = True
            if migrated:
                try:
                    import yaml
                    with open(self.cfg_path, "w", encoding="utf-8") as f:
                        yaml.safe_dump(self.cfg, f, allow_unicode=True, sort_keys=False)
                except Exception:
                    pass
        # 运行时从保险库取回 Key（yaml 里只有引用，没有明文）
        for name, d in (self.cfg.get("providers") or {}).items():
            if not d.get("api_key"):
                d["api_key"] = keyring.get_key(d.get("vendor") or name,
                                               env_hint=d.get("api_key_env"))
        self.cfg.setdefault("providers", {})
        self.cfg.setdefault("delegates", [])
        self.cfg.setdefault("chair", {})
        try:
            self.rounds.set(str(self.cfg.get("max_rounds", 2)))
        except tk.TclError:
            pass

    def _open_settings(self):
        ConnectWindow(self.root, self.cfg, self.cfg_path, on_save=self._on_cfg_saved)

    def _on_cfg_saved(self, cfg, path):
        self.cfg = cfg
        self.cfg_path = path
        # 接进来了就切正式：名单立刻变成他接入的那几家，不用他自己再点一次
        self._set_mode("real")

    def _open_dir(self):
        d = self.last_dir or os.path.join(base_dir(), "transcripts")
        if not os.path.isdir(d):
            return messagebox.showinfo("砚", "还没有会议记录：%s" % d)
        try:
            os.startfile(d)  # noqa: S606 Windows 专用
        except Exception:
            messagebox.showinfo("砚", d)

    # ── 开会 ───────────────────────────────────────────
    def _run(self):
        if self.busy:
            return
        q = self.entry.get("1.0", "end").strip()
        if not q:
            return messagebox.showwarning("砚", "先写一个议题")
        try:
            rounds = max(1, min(5, int(self.rounds.get())))
        except ValueError:
            rounds = 2

        mode = self.mode.get()
        if not self.mode_is_demo() and not os.path.isfile(self.cfg_path):
            if messagebox.askyesno(
                    "砚",
                    "正式模式需要一个配置文件（council.yaml），现在没有。\n\n"
                    "要打开设置填模型密钥和代表吗？"):
                self._open_settings()
            return

        self.busy = True
        self._stop_event = threading.Event()
        # 会议进行中发送按钮变「停止」：模型卡住、跑偏了，用户得能喊停，
        # 不能只能干看着「进行中…」
        self.send_btn.configure(state="normal", text="停 止", bg="#6b1410",
                                command=self._on_send)
        self.entry.configure(state="disabled")
        self.title_lbl.configure(text=q if len(q) < 40 else q[:38] + "…")
        self.sub_lbl.configure(text="会议进行中")
        self.room_sub.configure(text=q if len(q) < 24 else q[:22] + "…")
        self.typing.configure(text="")
        for name in self.roster:
            self._set_delegate_state(name, "在线", C_SIDE_DIM)

        self._push_msg("你", q, mine=True)
        # 发出去就清空输入框：上一场的议题留在里面，看起来像被"自动填充"，
        # 手一抖 Enter 就把旧题重发一遍。
        self.entry.delete("1.0", "end")
        threading.Thread(target=self._worker, daemon=True,
                         args=(q, rounds, mode, self.cfg_path)).start()

    def _on_send(self):
        """同一个按钮两个职责：闲时是「发起会议」，会中是「停止」。"""
        if self.busy:
            self._stop()
        else:
            self._run()

    def _stop(self):
        if self._stop_event:
            self._stop_event.set()
        self.send_btn.configure(state="disabled", text="停止中…")
        self.typing.configure(
            text="正在停止…（等当前这一条说完就停，最长再等两分钟）")

    def _worker(self, question, rounds, mode, cfg_path):
        stop = self._stop_event
        try:
            outdir = os.path.join(base_dir(), "transcripts")
            if mode == "demo":
                c = mock_council(outdir, rounds, should_stop=stop.is_set)
                pace = 0.35          # 假代表太快，加一点停顿才有聊天感
            else:
                cfg = load_config(cfg_path)
                # 用户会规 rules.md：启动一场会时装载，改规矩只改文件不动代码
                rules = load_rules(base_dir())
                if rules:
                    self.q.put({"type": "info",
                                "text": "已加载会规 rules.md（%d 字），对每位代表生效。"
                                        % len(rules)})
                delegates, chair = build_delegates(cfg)
                from .council import Council
                c = Council(delegates, chair, max_rounds=rounds,
                            anonymize=bool(cfg.get("anonymize", True)),
                            outdir=outdir, should_stop=stop.is_set)
                pace = 0.0
                if c.diversity() < 2:
                    self.q.put({"type": "info",
                                "text": "注意：所有代表是同一个模型，这场会的收益接近 0。"})
                est = len(delegates) * (2 + 2 * c.max_rounds) + 1
                self.q.put({"type": "info",
                            "text": "%d 名代表，最多 %d 轮，预计调用约 %d 次。"
                                    % (len(delegates), c.max_rounds, est)})

            refs = ""
            if mode != "demo":
                # 本地方案库：过去相近议题沉淀下来的优选方案，发给代表当参考。
                # 这是"让圆桌记住自己过去的答案"的实现方式，不联网、不花钱。
                try:
                    refs = notes.as_reference(base_dir(), question)
                except Exception:
                    refs = ""
                if refs:
                    self.q.put({"type": "info",
                                "text": "本地方案库里有相关的旧方案，已发给各位代表作参考"
                                        "（共 %d 套）。" % notes.count(base_dir())})

            res = c.deliberate(question, emit=self.q.put, pace=pace,
                               references=refs)
            # 综合出来的这套方案攒进本地方案库，下次遇到相近议题会被翻出来。
            # 演示模式不入库：那是假代表凑出来的，混进来会污染真经验。
            saved = ""
            if mode != "demo":
                try:
                    saved = notes.save(base_dir(), question, res.resolution,
                                       delegates=[d.name for d in c.delegates])
                except Exception:
                    saved = ""
            self.q.put({"type": "done",
                        "tally": res.tally, "rounds": res.rounds_run,
                        "converged": res.converged,
                        "resolution": res.resolution,
                        "chair": c.chair.name,
                        "md": os.path.abspath(res.md_path),
                        "note_path": saved})
        except CouncilStopped:
            self.q.put({"type": "stopped"})
        except SystemExit as e:
            self.q.put({"type": "err", "text": str(e)})
        except Exception as e:
            self.q.put({"type": "err", "text": "%s: %s" % (type(e).__name__, e)})

    def _drain(self):
        try:
            while True:
                ev = self.q.get_nowait()
                kind = ev.get("type")
                if kind == "stage":
                    self._push_stage(ev["stage"], ev.get("note", ""))
                elif kind == "typing":
                    name = ev["name"]
                    self._typing_since = time.monotonic()
                    self._typing_name = name
                    self._tick_typing()
                    self._set_delegate_state(name, "发言中", C_ACCENT)
                elif kind == "msg":
                    name = ev["name"]
                    self._typing_since = None
                    self.typing.configure(text="")
                    self._push_msg(name, ev.get("text", ""), role=ev.get("role", "proposer"),
                                   kind=ev.get("kind"), verdict=ev.get("verdict"))
                    if ev.get("verdict"):
                        v_txt, v_color = VERDICT_META.get(ev["verdict"], (ev["verdict"], C_DIM))
                        self._set_delegate_state(name, v_txt, v_color)
                    else:
                        self._set_delegate_state(name, "在线", C_SIDE_DIM)
                elif kind == "info":
                    self._push_sys(ev["text"])
                elif kind == "stopped":
                    self._meeting_over("已停止")
                    self._push_sys("已停止这场会。已经说过的话都在，「打开会议记录」能看全文。")
                elif kind == "done":
                    self._finish(ev)
                elif kind == "err":
                    self._meeting_over("出错了")
                    # 弹窗给人话 + 一行短摘要；几百字的原始 JSON 没人看得懂
                    tip = explain_error(ev["text"])
                    messagebox.showerror(
                        "砚", (tip + "\n\n" if tip else "")
                        + short_err(ev["text"]))
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    def _tick_typing(self):
        """等待计时：模型半天不回话时，用户得知道是在等、等了多久、等多久会断。"""
        if self._typing_since is None:
            return
        waited = int(time.monotonic() - self._typing_since)
        self.typing.configure(
            text="%s 正在发言… 已等 %d 秒（单条最多等 120 秒，等不到会报错，可点「停止」）"
                 % (self._typing_name, waited))
        self.root.after(1000, self._tick_typing)

    def _meeting_over(self, title):
        """无论正常结束、出错还是手动停止，都把界面恢复到能开下一场。"""
        self.busy = False
        self._typing_since = None
        self._stop_event = None
        self.typing.configure(text="")
        self.send_btn.configure(state="normal", text="发起会议", bg=C_ACCENT,
                                command=self._on_send)
        self.entry.configure(state="normal")
        self.sub_lbl.configure(text=title)

    def _finish(self, ev):
        self._meeting_over(
            "已结束 · 赞成 %d / 反对 %d / 弃权 %d"
            % (ev["tally"]["AGREE"], ev["tally"]["RESERVE"], ev["tally"]["DISAGREE"]))
        self.tally = ev["tally"]
        self.last_dir = os.path.dirname(ev["md"])
        # 结束时把最优方案再完整发一遍：开会过程刷屏后，它容易被埋在中间，
        # 用户不该翻半天才找得到"这场会到底得出了什么"。
        resolution = ev.get("resolution") or ""
        if resolution:
            self._push_stage("最终方案", "这场会收敛出来的当前最优解，点「复制方案」可以直接带走")
            self._push_msg(ev.get("chair") or "主持人", resolution, role="chair",
                           kind="resolution")
        self._push_result(ev["tally"], ev["rounds"], ev["converged"], ev["md"])
        if ev.get("note_path"):
            self._push_sys("这套最优方案已存进本地方案库（共 %d 套）。下次遇到相近的议题，"
                           "会自己翻出来给代表当参考 —— 相当于这个圆桌记住了自己过去的答案。"
                           % notes.count(base_dir()))
        self._push_sys("再发一个议题就可以开下一场会。")
        self._refresh_note_count()
        self.chat.see("end")


def _dpi():
    """Windows 高 DPI 下让界面不糊。失败就按系统默认走，不影响启动。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main():
    _dpi()
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
