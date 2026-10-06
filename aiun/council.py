"""会议编排：各自出方案 → 交叉评审 → 修订补短 → 主持人收敛 → 表态。

核心理念：用户抛出一个问题，三方**平权**——每家都出一套自己的办法，
每套办法都由另外两家来评（谁的更好、哪一步会出事、怎么补），各家再吸收
评审意见修订自己的方案，最后由主持人把三方所长收敛成当前最优的一套，
并让各方表态标明谁还有保留意见。

设计要点（每条对应一个已知的多智能体失效模式）：
  三方平权     对抗"让最弱的一家去审最强的"（实测智谱免费档当审查员只会跑题）
  交叉评审     对抗"自己评自己"与"只被一人评"的抽样偏差
  独立出方案   对抗锚定效应
  匿名互评     对抗权威偏见
  补短环节     对抗"指出问题却没人改"
  保留意见     对抗共识幻觉
  收敛检测     对抗仪式化（讨论本身不是目的）
"""

import hashlib
import re
import time

from .delegate import Delegate
from .notes import scrub
from .transcript import Transcript

LABELS = "ABCDEFGH"

# 成本闸门上限：一场会的调用次数 ≈ 代表数 × (2 + 2×轮数) + 1。
# 没有上限时，20 个代表 × 10 轮 = 440 次调用，没人拦得住。
MAX_ROUNDS = 5


def _label(i):
    """立场标签。超过 8 名代表时改用数字，不用取模 —— 取模会让两名代表撞同一个标签，
    匿名互评就分不清是谁了。"""
    return "立场" + (LABELS[i] if i < len(LABELS) else str(i + 1))

# 每段材料喂给模型前先掐到这个长度。
# 会议是串联的：前一轮的全文会被塞进后一轮的 prompt，代表数 × 轮数一涨，
# token 就是成倍翻。掐断省下的都是真金白银，而各方真正有用的通常就在前几行。
_CLIP = 700


def _drop_preamble(text):
    """主持人常常先写一大段分析草稿（"我们需要…""从材料看…"）才给方案。

    草稿都在方案标题之前，直接从标题截。实测 GLM-4.5 那次草稿比方案本身还长，
    方案等于被埋了 —— 而用户要的就是那一套方案，不是主持人的思考过程。
    """
    for head in ("## 最优方案", "## 方案", "##最优方案", "##方案"):
        i = (text or "").find(head)
        if i > 0:                       # 只有前面真的有东西才截
            return text[i:]
    return text


def _clip(text, limit=_CLIP):
    s = (text or "").strip()
    return s if len(s) <= limit else s[:limit] + "…（后略）"


POSITION_USER = """议题：{question}
{references}
拿出你自己的解决办法。500 字以内：
做法：<先做什么、后做什么、代价>
依据：<分条>
这套解决不了的：<分条>
禁止开场白、自报家门、客套、复述要求。
"""

CROSS_USER = """议题：{question}

需要你评审的另外两家的办法（{anon_note}，不含你自己的）：
{positions}

逐家评，共 400 字以内，每家说两件事：
1. 哪一点比我的好，值得我并进来
2. 哪一步会在什么情况下出事，怎么补
只挑刺不给补法等于使绊子。禁止开场白、自报家门、客套、复述要求。
"""

MERGE_USER = """你此前的办法：
{yours}

另外两家对你的评审意见：
{critiques}

请给出修订版，400 字以内：
- 吸收了哪几条，因此改了哪里（写明来自哪一家）
- 不吸收的，说明理由
- 修订后的完整做法
禁止开场白、自报家门、客套、复述要求。
"""

REVIEW_USER = """议题：{question}

主持人综合各家所长形成的这套方案：

{draft}

请对这套方案表态。严格按三行输出：
表态：认同 或 有保留 或 不认同
采纳了你的：<你的哪一条被并进来了，没有就写 无>
理由：<一句话>
"""

CHAIR_USER = """议题：{question}

三家修订后的办法（权限相同，没有谁的方案优先）：
{positions}

交叉评审里提出的可取之处与隐患：
{debate}

把三家所长收敛成当前最优的一套方案：取各家之长、补各家之短，不是挑一个胜出者。
每一步注明来自哪家的哪一条。

严格只输出四段，标题照抄，内容由你填：
## 最优方案
分步骤，能照着做；每步注明来自哪家的哪一条。
## 为什么是它
一句话：比任何一家单独的方案强在哪。
## 保留意见
没并进来的意见原样保留，不许和稀泥。
## 未决事项
还解决不了什么、还需要什么条件或证据。

禁止：
1. 禁止输出分析过程或草稿 —— 不要写"从上下文看""我们需要整合""用户提供的材料"。
2. 禁止把上面的格式说明抄进正文，禁止留空占位。
3. 禁止开场白和自报家门。
"""


def _hash(text):
    """仅用于评审文本去重，不涉及安全。用 sha256 只为避开 md5 的静态扫描告警。"""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


# 锚定「表态：」这一行。
# 旧实现在全文里找 APPROVE/REJECT/ABSTAIN 子串且 REJECT 优先 ——
# 模型只要写出"我不认为现在应该 REJECT 这个方案"，计票就被污染。
_REVIEW_LINE = re.compile(r"表态\s*[:：]\s*(不认同|有保留|保留|认同)")
_VERDICT_MAP = {"认同": "AGREE", "有保留": "RESERVE", "保留": "RESERVE",
                "不认同": "DISAGREE"}


def _verdict(text):
    m = _REVIEW_LINE.search(text or "")
    return _VERDICT_MAP[m.group(1)] if m else "UNPARSED"


class Result(object):
    def __init__(self, question, resolution, positions, critiques, defenses,
                 reviews, rounds_run, converged, paths):
        self.question = question
        self.resolution = resolution
        self.positions = positions
        self.critiques = critiques
        self.defenses = defenses
        self.reviews = reviews      # 各方对综合方案的表态（含保留意见）
        self.votes = reviews        # 旧名字，CLI 还在用
        self.rounds_run = rounds_run
        self.converged = converged
        self.json_path, self.md_path = paths

    @property
    def tally(self):
        t = {"AGREE": 0, "RESERVE": 0, "DISAGREE": 0, "UNPARSED": 0}
        for v in self.reviews:
            t[v["verdict"]] += 1
        return t


class CouncilStopped(RuntimeError):
    """用户点了「停止」。已产生的内容都在记录里，不是报错。"""


class Council(object):
    def __init__(self, delegates, chair=None, max_rounds=2, anonymize=True,
                 outdir="transcripts", should_stop=None):
        if not delegates:
            raise ValueError("至少要一名代表")
        self.delegates = delegates
        self.chair = chair or delegates[0]
        self.max_rounds = min(max(1, int(max_rounds)), MAX_ROUNDS)
        self.anonymize = anonymize
        self.outdir = outdir
        # 每次开口前问一次：用户喊停了吗。图形界面靠这个实现「停止」按钮。
        self.should_stop = should_stop

    def _gate(self):
        if self.should_stop and self.should_stop():
            raise CouncilStopped("用户停止了会议")

    def diversity(self):
        """不同 (provider, model) 的数量。为 1 说明这场会大概率白开。"""
        return len(set(d.identity() for d in self.delegates))

    def deliberate(self, question, emit=None, pace=0.0, references=""):
        """跑完五个阶段。

        emit:  可选的回调，每产生一条发言就调一次 emit(dict)。
               命令行不传，图形界面靠它把会议渲染成实时的群聊。
        pace:  每条发言之间暂停几秒。真实模型本身就慢，只有假代表需要靠它
               才有"在聊天"的节奏感，不影响任何逻辑。
        references: 本地方案库里翻出来的、和这个议题相近的历史优选方案。
               只在出方案阶段发给大家当参考，不参与后面的环节。
        """
        import time  # noqa: F401  pace 用；放这里只为不打扰纯逻辑读者

        def fire(**ev):
            if emit:
                emit(ev)
                # 停顿只放在"发言前"：让界面上的"正在输入"看得见，
                # 而不是把每条事件都拖慢。
                if pace > 0 and ev.get("type") == "typing":
                    time.sleep(pace)

        t = Transcript(question, self.outdir)
        t.meta = {
            "delegates": [
                {"name": d.name, "role": d.role, "provider": d.identity()[0], "model": d.model}
                for d in self.delegates
            ],
            "chair": {"name": self.chair.name, "model": self.chair.model},
            "max_rounds": self.max_rounds,
            "anonymize": self.anonymize,
            "diversity": self.diversity(),
        }

        anon_note = "已匿名，不显示出处，只针对内容评判" if self.anonymize else "附代表名"

        # ① 各自出方案：独立作答，互不看见
        ref_block = ""
        if references:
            ref_block = ("\n参考：本地方案库里，过去相近议题沉淀下来的优选方案"
                         "（只作参照，不许照抄，觉得过时或不适用的要说明）：\n%s\n"
                         % references)
        fire(type="stage", stage="① 各自出方案",
             note="各代表独立作答，互相看不见对方，避免锚定")
        positions = {}
        for i, d in enumerate(self.delegates):
            label = _label(i)
            self._gate()
            fire(type="typing", name=d.name)
            text = d.say(POSITION_USER.format(question=question,
                                              references=ref_block))
            positions[d.name] = {"label": label, "text": text}
            t.add("① 各自出方案", d.name, d.role, text)
            fire(type="msg", name=d.name, role=d.role, model=d.model, label=label,
                 stage="① 各自出方案", text=text)

        def block(exclude=None):
            """拼各家的方案。exclude 传一个代表，就把他自己的那份剔掉 ——
            交叉评审时每人只看另外两家的，不评自己。"""
            parts = []
            for d in self.delegates:
                if exclude is not None and d.name == exclude.name:
                    continue
                p = positions[d.name]
                who = p["label"] if self.anonymize else "%s（%s）" % (p["label"], d.name)
                parts.append("[%s]\n%s" % (who, _clip(p["text"])))
            return "\n\n".join(parts)

        # ②③ 交叉评审 + 补短修订，可多轮，没人再提出新东西即收敛
        seen = set()
        critiques, defenses = [], []
        rounds_run, converged = 0, False
        for r in range(self.max_rounds):
            rounds_run = r + 1
            fresh = False
            round_c = []
            cstage = "② 交叉评审·第%d轮" % rounds_run
            fire(type="stage", stage=cstage,
                 note="每套方案都由另外两家来评：哪一点更好、哪一步会出事、怎么补")
            for d in self.delegates:
                self._gate()
                fire(type="typing", name=d.name)
                c = d.say(CROSS_USER.format(
                    question=question, positions=block(exclude=d),
                    anon_note=anon_note))
                h = _hash(c)
                if h not in seen:
                    seen.add(h)
                    fresh = True
                round_c.append((d.name, c))
                t.add("② 交叉评审·第%d轮" % rounds_run, d.name, d.role, c)
                fire(type="msg", name=d.name, role=d.role, model=d.model,
                     stage=cstage, kind="critique", text=c)
            critiques.extend(round_c)

            # 修订时每家只看「别人对我的评审」，别把三家意见全塞给他 ——
            # 一是省 token，二是别的意见跟他无关，看了只会稀释重点。
            dstage = "③ 修订补短·第%d轮" % rounds_run
            fire(type="stage", stage=dstage,
                 note="吸收另外两家的意见，改出修订版；不吸收的要说明理由")
            for d in self.delegates:
                self._gate()
                fire(type="typing", name=d.name)
                mine = _clip("\n\n".join(
                    "%s 的评审：\n%s" % (n, c) for n, c in round_c if n != d.name),
                    600)
                df = d.say(MERGE_USER.format(
                    yours=positions[d.name]["text"], critiques=mine))
                # 修订版顶掉原方案：后面综合、表态看的都该是改过的版本
                positions[d.name]["text"] = df
                defenses.append((d.name, df))
                t.add("③ 修订补短·第%d轮" % rounds_run, d.name, d.role, df)
                fire(type="msg", name=d.name, role=d.role, model=d.model,
                     stage=dstage, kind="defense", text=df)

            if not fresh:
                converged = True
                t.meta["converged_at_round"] = rounds_run
                break

        # 只把"可取之处与隐患"交给主持人：各家修订后的做法已经在 positions 里了，
        # 再塞一遍就是同一份内容付两次钱（以前这里占了主持人输入的一大半）。
        debate = "\n\n".join(
            "%s 提出的可取之处与隐患：\n%s" % (n, _clip(c, 500)) for n, c in critiques
        )

        # ④ 定最优方案：主持人只做收敛，三家平权，没有谁的方案优先
        fire(type="stage", stage="④ 定最优方案",
             note="主持人把三家的长处收敛成一套，给出当前最优解")
        self._gate()
        fire(type="typing", name=self.chair.name)
        resolution = self.chair.say(CHAIR_USER.format(
            question=question, positions=block(), debate=debate),
            temperature=0.3, max_tokens=1600)
        # 有模型会把指令复述一遍再作答，主持人有时还把它综合进来 —— 刮掉，
        # 否则聊天卡片、会议记录、本地方案库三处都会被污染。
        resolution = scrub(resolution)
        resolution = _drop_preamble(resolution)   # 砍掉方案标题之前的分析草稿
        t.add("④ 最优方案", self.chair.name, "chair", resolution)
        fire(type="msg", name=self.chair.name, role="chair", model=self.chair.model,
             stage="④ 最优方案", kind="resolution", text=resolution)

        # ⑤ 对这套最优方案表态：不是投票否决它，是标明谁还有保留意见
        fire(type="stage", stage="⑤ 表态", note="认同 / 有保留 / 不认同，保留意见原样留下")
        reviews = []
        for d in self.delegates:
            self._gate()
            fire(type="typing", name=d.name)
            v = d.say(REVIEW_USER.format(question=question,
                                         draft=_clip(resolution, 1200)),
                      max_tokens=300)
            verdict = _verdict(v)
            reviews.append({"delegate": d.name, "raw": v, "verdict": verdict})
            t.add("⑤ 表态", d.name, d.role, v)
            fire(type="msg", name=d.name, role=d.role, model=d.model,
                 stage="⑤ 表态", kind="vote", verdict=verdict, text=v)

        paths = t.save()
        return Result(question, resolution, positions, critiques, defenses,
                      reviews, rounds_run, converged, paths)
