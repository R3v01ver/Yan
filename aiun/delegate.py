"""代表与角色简报。

角色简报是整个系统里最值钱的一段文字。
没有指定角色时，所有智能体都会自发趋向附和 —— 这是多智能体讨论最常见的失效方式。

但反过来，把三家分成「提案方 / 反对派 / 审查员」也是错的：
三家权限不等，弱的那家分到"审查"就等于让最弱的去挑最强的毛病 —— 实测
智谱免费档当审查员时只会复述和跑题，而它出的方案未必差。三家平权之后，
每家都出方案、每家都评别人，任何一家弱都会被另外两家的评审盖过去。

会规文件：接口模型没有记忆，规则每次请求都得随行带上（这是 API 机制，
绕不开）。但规则不该写死在代码里 —— 砚启动时会读 <程序目录>/rules.md，
把里面的内容追加到每份简报后面。想改规矩，改这个文件就行，不用动代码。
"""

import os

# 每个角色都通用的三条禁令，写在这里免得各阶段重复一遍（重复就是烧 token）。
_COMMON = "禁止：开场白、自报家门（不许写“作为代表X”“我是XX”）、客套、复述要求。"

# 三方平权的同一份简报：出方案是本分，评别人也是本分，没有谁是专门挑刺的。
_PEER = (
    "你是本次圆桌的一方，和其他各方权限相同，没人有最终决定权。\n"
    "你要做两件事：先拿出自己的一套办法，再评别人的办法。\n"
    "评别人的时候：指出哪一步会在什么情况下出事，并给出怎么补；"
    "只挑刺不给补法等于使绊子。\n"
    + _COMMON
)

ROLE_BRIEFS = {
    # 老配置里写死的 proposer / adversary / reviewer 一律落到同一份平权简报，
    # 三个键都留着只是为了旧 council.yaml 还能跑，不代表还有角色之分。
    "peer": _PEER,
    "proposer": _PEER,
    "adversary": _PEER,
    "reviewer": _PEER,
    "chair": (
        "你是主持人，要把各家的办法综合成**当前最优的一套方案**：\n"
        "取各家之长、补各家之短，不是挑一个胜出者。每一步注明来自谁的哪一条。\n"
        "三方权限相同，你只负责收敛，不新增你自己的主张。\n"
        + _COMMON
    ),
}

# 输出上限：以前写死 2000，模型真用满就是白花钱。讨论要的是密度不是长度。
MAX_OUT = 1200

DEFAULT_ROLE = "peer"

# 用户会规（rules.md 的内容），启动时由 load_rules 装进来。
_EXTRA_RULES = ""


def load_rules(base):
    """读 <base>/rules.md 作为用户自定义会规，追加到每份简报后面。

    文件不存在或读不了就当没有 —— 这是可选项，不是必需品。
    返回装进去的规则文本（便于界面提示用户生效了没有）。
    """
    global _EXTRA_RULES
    path = os.path.join(base or ".", "rules.md")
    try:
        with open(path, encoding="utf-8") as f:
            _EXTRA_RULES = f.read().strip()
    except OSError:
        _EXTRA_RULES = ""
    return _EXTRA_RULES


class Delegate(object):
    def __init__(self, name, provider, role=DEFAULT_ROLE, model=None):
        self.name = name
        self.provider = provider
        self.role = role if role in ROLE_BRIEFS else DEFAULT_ROLE
        self.model = model or getattr(provider, "model", "?")

    @property
    def brief(self):
        b = ROLE_BRIEFS[self.role]
        return b + ("\n" + _EXTRA_RULES if _EXTRA_RULES else "")

    def say(self, user, temperature=0.7, max_tokens=MAX_OUT):
        return self.provider.complete(self.brief, user, temperature, max_tokens)

    def identity(self):
        """用于多样性检查：同 provider + 同 model 视为同源代表。"""
        return (getattr(self.provider, "name", "?"), self.model)
