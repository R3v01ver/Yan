"""本地方案库：把每场会综合出来的优选方案攒下来。

它做的不是训练模型权重 —— 那需要算力和数据，一台机器上跑不动也没必要。
它做的是这个圆桌自己的**经验本**：一场会结束，主持人综合出来的那套方案
连同议题一起落盘；下次遇到相近的议题，把最相关的几套翻出来，随议题一起
发给各位代表当参考。于是这个圆桌确实"记住了"自己过去给出的好答案，
而且全在本地、不联网、不花钱。

存储：
    <base>/experience/2026-10-06-议题摘要.md    人能直接读的方案全文
    <base>/experience/index.json                检索用的索引
"""

import json
import os
import re
import time

_DIRNAME = "experience"
_INDEX = "index.json"

# 中文没有空格分词，用二字组（bigram）当特征：覆盖率够用，且零依赖。
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _tokens(text):
    """把一句话拆成检索用的特征：中文取二字组，英文数字取词。"""
    text = (text or "").lower()
    grams = set()
    han = "".join(_CJK.findall(text))
    for i in range(len(han) - 1):
        grams.add(han[i:i + 2])
    for w in re.findall(r"[a-z0-9]{2,}", text):
        grams.add(w)
    return grams


def _slug(text, n=18):
    s = "".join(c for c in (text or "") if c.isalnum() or _CJK.match(c))
    return (s[:n] or "方案").strip()


def _dir(base):
    if os.path.basename(base or "") == _DIRNAME:
        return base
    return os.path.join(base or ".", _DIRNAME)


def _read_index(d):
    p = os.path.join(d, _INDEX)
    if not os.path.isfile(p):
        return []
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError):
        return []
    return data if isinstance(data, list) else []


# ── 提示词泄漏清洗 ────────────────────────────────────
# 有些模型（实测 GLM-4.5 会）不按格式答，先把「用户要求……严格输出三段」
# 这套指令复述一遍再作答。主持人有时还会把它当内容综合进方案。
# 这些话人读着莫名其妙，导出给别的 AI 更是直接污染它的上下文 —— 存之前刮掉。
# 只认这些强特征短语。像「用户要求低延迟」这种正常方案表述不能算 ——
# 裸的「用户要求」误伤率太高，必须带上下文（"用户要求作为"）才认。
_INSTR = ("严格输出", "硬性要求", "圆桌会议主持人", "作为圆桌", "我们需要回答用户",
          "禁止和稀泥", "未被采纳的意见原样保留", "只写被证据支持",
          "用户要求作为", "请你作为", "按你的工具挑")
# 自报家门：模型爱写"作为代表A，我认为…"。用户要的是内容，不是它的身份介绍。
_SELF = ("作为代表", "我是代表", "我们代表", "本人代表", "作为立场", "作为本代表",
         "我作为代表", "作为第", "本代表")
_MAX_BLOCK = 6          # 连带最多删几行，防止误吃掉大段正文


def scrub(text):
    """刮掉把指令当正文复述出来的那段。命中一行后连带删到空行为止。"""
    out, in_block, taken = [], False, 0
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s:                                  # 空行 = 这段复述结束
            in_block, taken = False, 0
            out.append(ln)
            continue
        if s.startswith(("#", "##", "-", "·", "*", ">")):
            in_block, taken = False, 0             # 正文结构出现，不再删
        if s.startswith("（") and s.endswith("）") and len(s) <= 60:
            continue
        if s.startswith(_SELF):        # "作为代表A，我认为…" —— 只要内容，不要身份介绍
            continue
        if in_block and taken < _MAX_BLOCK:
            taken += 1
            continue
        if any(k in s for k in _INSTR):
            in_block, taken = True, 1
            continue
        in_block, taken = False, 0
        out.append(ln)
    return "\n".join(out).strip()


def save(base, question, resolution, delegates=None, ts=None):
    """把一场会的综合方案存进本地方案库。返回 md 文件路径。"""
    resolution = scrub(resolution)                 # 别把提示词复述当成经验存下来
    d = _dir(base)
    os.makedirs(d, exist_ok=True)
    ts = ts or time.strftime("%Y-%m-%d")
    name = "%s-%s.md" % (ts, _slug(question))
    path = os.path.join(d, name)
    n = 2
    while os.path.isfile(path):                      # 同一天同一议题别互相覆盖
        path = os.path.join(d, "%s-%s-%d.md" % (ts, _slug(question), n))
        n += 1

    who = "、".join(delegates or []) or "（未记录）"
    with open(path, "w", encoding="utf-8") as f:
        f.write("# %s\n\n" % (question or "未命名议题"))
        f.write("时间：%s\n\n" % ts)
        f.write("参会：%s\n\n" % who)
        f.write("%s\n" % (resolution or "").strip())

    items = _read_index(d)
    items.append({
        "question": question or "",
        "path": os.path.abspath(path),
        "ts": ts,
        "tokens": sorted(_tokens(question)),
        "delegates": list(delegates or []),
    })
    with open(os.path.join(d, _INDEX), "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    return path


def all_items(base):
    return _read_index(_dir(base))


def count(base):
    return len(_read_index(_dir(base)))


def search(base, question, top_k=3, min_score=0.06):
    """按议题找最相关的历史方案。

    打分用 Dice 系数（2×交集 / 两边特征数之和）：比只看一边更平衡 ——
    既不会因为历史条目特征少就被"要不要"这种通用词顶上来，
    也不会因为新议题写得长就压过真正的相似项。
    """
    q = _tokens(question)
    if not q:
        return []
    scored = []
    for it in _read_index(_dir(base)):
        toks = set(it.get("tokens") or _tokens(it.get("question", "")))
        if not toks:
            continue
        score = 2.0 * len(q & toks) / (len(q) + len(toks))
        if score >= min_score:
            scored.append((score, it))
    scored.sort(key=lambda x: -x[0])
    return [it for _s, it in scored[:top_k]]


def _body_of(path):
    """读一份方案正文，去掉重复的元信息（标题、时间、参会）。"""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return "（记录文件已不在：%s）" % path
    out = []
    for ln in lines:
        s = ln.strip()
        if not out and (s.startswith("# ") or s.startswith("时间：")
                        or s.startswith("参会：")):
            continue
        out.append(ln)
    return scrub("\n".join(out))       # 老文件里也可能混着提示词复述


def export_markdown(base, path=None):
    """把整套方案库导出成一份能直接喂给别的 AI 助手的 Markdown。

    砚自己攒的经验，只放在本地的话只有砚自己看得到。导出成一份纯文本，
    Codex / Claude / Cursor 这类工具就能把它当项目背景读进去 ——
    这才是「知识为我所用」闭环的那一步。

    方案为空时返回 None（调用方应当提示用户先开一场正式会）。
    """
    items = _read_index(_dir(base))
    if not items:
        return None
    d = _dir(base)
    if not path:
        path = os.path.join(d, "砚-经验-%s.md" % time.strftime("%Y%m%d"))

    today = time.strftime("%Y-%m-%d")
    parts = [
        "# 砚的经验 · %d 套方案\n" % len(items),
        "> 由「砚」于 %s 导出。内容是各家模型在圆桌上取长补短、综合出来的方案，\n"
        "> 不是聊天记录。\n"
        ">\n"
        "> 用法（三选一，按你的工具挑）：\n"
        "> 1. 放进项目根目录，在 `AGENTS.md`（Codex）或 `CLAUDE.md` 里加一行：\n"
        ">    「做相关决策前先读《%s》里的方案。」\n"
        "> 2. 直接把内容贴给 AI 助手当背景资料。\n"
        "> 3. 放进 `~/.workbuddy/skills/<名字>/SKILL.md` 做成长期生效的技能。\n"
        % (today, os.path.basename(path)),
        "\n---\n",
    ]
    for i, it in enumerate(items, 1):
        parts.append("\n## %d. %s\n" % (i, it.get("question") or "未命名议题"))
        meta = []
        if it.get("ts"):
            meta.append("时间：%s" % it["ts"])
        if it.get("delegates"):
            meta.append("参会：%s" % "、".join(it["delegates"]))
        if meta:
            parts.append("%s\n" % " · ".join(meta))
        parts.append("\n%s\n" % _body_of(it.get("path", "")))
        parts.append("\n---\n")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    return path


def as_reference(base, question, top_k=3, chars=900, min_score=0.10):
    """给代表看的参考材料：过去相近议题沉淀下来的优选方案。没有就返回空串。"""
    hits = search(base, question, top_k=top_k)
    if not hits:
        return ""
    parts = []
    for i, it in enumerate(hits, 1):
        body = ""
        try:
            with open(it["path"], encoding="utf-8") as f:
                body = f.read()
        except OSError:
            body = "（记录文件已不在：%s）" % it["path"]
        body = body.strip()
        if len(body) > chars:
            body = body[:chars] + "…"
        parts.append("【过去的第 %d 套：%s（%s）】\n%s"
                     % (i, it["question"], it["ts"], scrub(body)))
    return "\n\n".join(parts)
