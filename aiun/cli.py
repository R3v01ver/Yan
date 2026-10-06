"""命令行入口。

  aiun demo "议题"                  零成本跑一遍（不需要任何 key）
  aiun init                         生成 council.yaml
  aiun deliberate "议题" --config council.yaml
"""

import argparse
import json
import os
import sys

from . import keyring, providers
from .council import MAX_ROUNDS, Council
from .delegate import Delegate

EXAMPLE_CONFIG = """# 砚 会议配置
# 原则：代表尽量跨模型家族。所有代表同源 = 这场会大概率白开。

max_rounds: 2          # 评审-修订轮数；没有新东西会自动提前结束
anonymize: true        # 匿名互评，避免权威偏见

chair:                 # 主持人：把各家所长收敛成一套，建议选最强的模型
  provider: openai
  model: gpt-4o-mini

delegates:
  - name: 代表A
    role: peer         # 三方平权，角色一致
    provider: openai
    model: gpt-4o-mini
  - name: 代表B
    role: peer
    provider: anthropic
    model: claude-sonnet-4-20250514
  - name: 代表C
    role: peer
    provider: ollama
    model: qwen2.5:14b

providers:
  openai:    {type: openai,    base_url: https://api.openai.com/v1, api_key_env: OPENAI_API_KEY}
  anthropic: {type: anthropic, api_key_env: ANTHROPIC_API_KEY}
  ollama:    {type: openai,    base_url: http://localhost:11434/v1}
"""


def load_config(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if path.lower().endswith(".json"):
        return json.loads(text)
    try:
        import yaml
    except ImportError:
        raise SystemExit("读 YAML 配置需要 pyyaml：pip install pyyaml（或改用 .json 配置）")
    return yaml.safe_load(text)


def build_delegates(cfg):
    provs = cfg.get("providers") or {}
    out = []
    literal_key = False
    for i, d in enumerate(cfg.get("delegates") or []):
        pname = d.get("provider", "openai")
        pdef = provs.get(pname, {})
        kind = pdef.get("type", pname)
        akey = d.get("api_key") or pdef.get("api_key")
        if akey:
            literal_key = True
        else:
            # yaml 里不放 Key：回退到系统加密保险库（GUI 保存的 Key 都在那）
            akey = keyring.get_key(pdef.get("vendor") or pname,
                                   env_hint=pdef.get("api_key_env"))
        p = providers.build(
            kind,
            model=d.get("model") or pdef.get("model"),
            api_key=akey,
            base_url=d.get("base_url") or pdef.get("base_url"),
            api_key_env=pdef.get("api_key_env"),
        )
        out.append(
            Delegate(
                name=d.get("name") or ("代表%d" % (i + 1)),
                provider=p,
                role=d.get("role", "proposer"),
            )
        )
    if not out:
        raise SystemExit("配置里没有 delegates")

    c = cfg.get("chair") or {}
    pname = c.get("provider") or ""
    pdef = provs.get(pname) or {}
    if not pdef:
        # 没单独指定主持人（老配置）→ 退回用第一个代表的厂商，否则会造出一个
        # 空 provider，开会时以 401 炸在最后一步，前面的钱全白花。
        pname = (cfg.get("delegates") or [{}])[0].get("provider", "openai")
        pdef = provs.get(pname, {})
    ckey = c.get("api_key") or pdef.get("api_key")
    if ckey:
        literal_key = True
    else:
        ckey = keyring.get_key(pdef.get("vendor") or pname,
                               env_hint=pdef.get("api_key_env"))
    chair_p = providers.build(
        pdef.get("type", pname),
        model=c.get("model") or pdef.get("model"),
        api_key=ckey,
        base_url=c.get("base_url") or pdef.get("base_url"),
        api_key_env=pdef.get("api_key_env"),
    )
    if literal_key:
        print("警告：配置里写了明文 api_key。请确认这个文件不会被提交到 git —— "
              ".gitignore 只默认忽略 council.yaml 这一个文件名，"
              "换成别的名字（或放在子目录）就会泄露。推荐改用 api_key_env。")
    chair = Delegate(name=c.get("name") or "主持人", provider=chair_p, role="chair")
    return out, chair


def mock_council(outdir="transcripts", max_rounds=2, should_stop=None):
    # 名字和 GUI 侧栏名单保持一致（gui.DEMO_ROSTER）：既然是本地模拟，
    # 名字本身就该写清"演示"，别让用户以为这是他接入的模型。
    mk = lambda tag: providers.build("mock", model="mock-" + tag)
    # 三方平权后演示代表也不再分角色：叫「一方/二方/三方」，不叫提案方/审查员
    delegates = [
        Delegate("演示·一方", mk("a"), "peer"),
        Delegate("演示·二方", mk("b"), "peer"),
        Delegate("演示·三方", mk("c"), "peer"),
    ]
    chair = Delegate("主持人", mk("chair"), "chair")
    return Council(delegates, chair, max_rounds=max_rounds, outdir=outdir,
                   should_stop=should_stop)


def report(res):
    t = res.tally
    print("")
    print("=" * 60)
    print(res.resolution)
    print("=" * 60)
    print("")
    print("轮次: %d  收敛提前结束: %s" % (res.rounds_run, "是" if res.converged else "否"))
    print("表态: 认同 %d / 有保留 %d / 不认同 %d%s"
          % (t["AGREE"], t["RESERVE"], t["DISAGREE"],
             (" / 解析失败 %d" % t["UNPARSED"]) if t["UNPARSED"] else ""))
    print("记录: %s" % os.path.abspath(res.md_path))
    print("      %s" % os.path.abspath(res.json_path))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="yan", description="砚：多模型圆桌审议框架")
    sub = ap.add_subparsers(dest="cmd")

    p_init = sub.add_parser("init", help="生成 council.yaml 示例")
    p_init.add_argument("--out", default="council.yaml")

    p_demo = sub.add_parser("demo", help="用 Mock 代表跑完整流程，不需要 API key")
    p_demo.add_argument("question", nargs="?", default="是否应该把服务迁移到多 region 架构")
    p_demo.add_argument("--max-rounds", type=int, default=2)
    p_demo.add_argument("--outdir", default="transcripts")

    p_del = sub.add_parser("deliberate", help="开一场正式的会")
    p_del.add_argument("question")
    p_del.add_argument("--config", default="council.yaml")
    p_del.add_argument("--max-rounds", type=int, default=None)
    p_del.add_argument("--outdir", default="transcripts")

    args = ap.parse_args(argv)

    if args.cmd == "init":
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(EXAMPLE_CONFIG)
        print("已写入 %s ，填好代表和凭证后运行：" % args.out)
        print('  aiun deliberate "你的议题" --config %s' % args.out)
        return 0

    if args.cmd == "demo":
        print("[demo] 使用 Mock 代表，不发送任何网络请求。")
        c = mock_council(args.outdir, args.max_rounds)
        res = c.deliberate(args.question)
        report(res)
        return 0

    if args.cmd == "deliberate":
        if not os.path.isfile(args.config):
            raise SystemExit("配置文件不存在：%s（先跑 aiun init）" % args.config)
        cfg = load_config(args.config)
        # 用户会规：配置文件同目录下的 rules.md，存在就并进每份简报
        from . import delegate as _delegate
        rules = _delegate.load_rules(os.path.dirname(os.path.abspath(args.config)))
        if rules:
            print("已加载会规 rules.md（%d 字），将对每位代表生效。" % len(rules))
        delegates, chair = build_delegates(cfg)
        rounds = args.max_rounds or cfg.get("max_rounds") or 2
        c = Council(delegates, chair, max_rounds=rounds,
                    anonymize=bool(cfg.get("anonymize", True)), outdir=args.outdir)
        if c.diversity() < 2:
            print("警告：所有代表是同一个模型。同质模型会犯同一个错并互相加冕，"
                  "这场会的收益接近 0，只会更贵。建议至少换一个模型家族。")
        est = len(delegates) * (2 + 2 * c.max_rounds) + 1
        print("开会：%d 名代表，最多 %d 轮（轮数上限 %d）"
              % (len(delegates), c.max_rounds, MAX_ROUNDS))
        print("预计模型调用：约 %d 次。没有成本上限，跑之前先估一下账单。" % est)
        res = c.deliberate(args.question)
        report(res)
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
