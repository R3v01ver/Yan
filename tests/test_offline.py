"""离线测试。不需要网络、不需要 API key、不需要 pytest。

跑法：python -m unittest discover -s tests
"""

import json
import os
import shutil
import tempfile
import unittest

from aiun import providers
from aiun.cli import mock_council


class TestCouncilOffline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.outdir = os.path.join(self.tmp, "transcripts")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_deliberation(self):
        c = mock_council(self.outdir, max_rounds=2)
        res = c.deliberate("测试议题：要不要开会")

        self.assertEqual(len(res.positions), 3, "应有 3 套独立方案")
        self.assertTrue(res.critiques, "互相取长不能为空")
        self.assertTrue(res.defenses, "补短修订不能为空")
        self.assertEqual(len(res.reviews), 3)

        tally = res.tally
        self.assertEqual(tally["UNPARSED"], 0, "表态必须能被解析")
        self.assertGreaterEqual(tally["AGREE"] + tally["RESERVE"], 2)

        self.assertIn("最优方案", res.resolution,
                      "开会的目的就是给出当前最优方案，不是把讨论记一遍")
        self.assertIn("保留意见", res.resolution, "最优方案必须原样保留反对意见，不许和稀泥")
        self.assertIn("未决事项", res.resolution)

        self.assertTrue(os.path.isfile(res.md_path))
        self.assertTrue(os.path.isfile(res.json_path))

        with open(res.json_path, encoding="utf-8") as f:
            data = json.load(f)
        phases = [e["phase"] for e in data["entries"]]
        self.assertTrue(any(p.startswith("①") for p in phases))
        self.assertTrue(any(p.startswith("④") for p in phases))
        self.assertTrue(any(p.startswith("⑤") for p in phases))
        self.assertEqual(data["meta"]["diversity"], 3)

    def test_convergence_stops_early(self):
        """Mock 代表第二轮不产生新论点，应触发收敛。"""
        c = mock_council(self.outdir, max_rounds=3)
        res = c.deliberate("收敛测试")
        self.assertTrue(res.converged, "无新论点时应判定收敛")
        self.assertLess(res.rounds_run, 3, "收敛后不应跑满轮数")

    def test_equal_peers_no_hierarchy(self):
        """演示会里三家角色必须一致（平权），不再是提案方/反对派/审查员。"""
        c = mock_council(self.outdir)
        roles = set(d.role for d in c.delegates)
        self.assertEqual(len(roles), 1, "三方角色必须相同，不许有上下级")
        self.assertNotEqual(c.chair.role, list(roles)[0], "主持人例外：只做收敛")

    def test_diversity_warning_trigger(self):
        """同源代表应被识别出来（diversity == 1）。"""
        p = providers.build("mock", model="mock-x")
        from aiun.council import Council
        from aiun.delegate import Delegate
        c = Council([Delegate("A", p, "proposer"), Delegate("B", p, "adversary")],
                    Delegate("chair", p, "chair"), outdir=self.outdir)
        self.assertEqual(c.diversity(), 1, "同源代表应被判为 diversity=1")

    def test_review_parsing_is_anchored(self):
        """表态必须锚定「表态：」这一行。

        全文扫子串会被正文里的"我不认同""这不是我的保留意见"这类句子污染 ——
        一句话就能翻转统计，那就不是表态了。
        """
        from aiun.council import _verdict
        self.assertEqual(_verdict("表态：认同\n理由：已吸收"), "AGREE")
        self.assertEqual(_verdict("表态： 有保留"), "RESERVE")
        self.assertEqual(_verdict("表态：不认同"), "DISAGREE")
        self.assertEqual(_verdict("我不认同这套做法，但整体可用"), "UNPARSED")
        self.assertEqual(_verdict("理由：这里提到 不认同 只是转述\n表态：认同"), "AGREE")

    def test_anthropic_base_url_is_honored(self):
        """base_url 必须传下去，否则 key 会静默发往官方端点而非用户配置的代理。"""
        p = providers.build("anthropic", api_key="k", base_url="https://proxy.local/v1")
        self.assertEqual(p.base_url, "https://proxy.local/v1")

    def test_max_rounds_is_clamped(self):
        """轮数必须有上限，否则成本不可控（代表数 × 轮数直接放大调用次数）。"""
        c = mock_council(self.outdir, max_rounds=99)
        self.assertLessEqual(c.max_rounds, 5)

    def test_many_delegates_do_not_crash(self):
        """超过 8 名代表时标签不得越界。"""
        from aiun.council import _label
        self.assertEqual(_label(7), "立场H")
        self.assertEqual(_label(8), "立场9")

    def test_degenerate_output_detected(self):
        """复读机输出必须被认出来——用户实测智谱免费档会输出 A.C.A.C… 刷满一屏。

        这种东西原样收下会污染会议（别的代表还要参考它），必须拦下重发。
        """
        from aiun.providers import is_degenerate
        self.assertTrue(is_degenerate("分析 6:0C5D!89." + "A.C." * 200))
        self.assertTrue(is_degenerate("结论是对的 " + "好的 " * 50))
        # 正常长文不该误伤
        self.assertFalse(is_degenerate(
            "这是一份正常的方案。" * 3 + "分三步：先判据，再试点，后铺开。"
            "每一步都要写明它来自谁的哪一条，没并进来的意见原样保留。"))
        # 短文本不判（重复少没有信息量损失）
        self.assertFalse(is_degenerate("A.C.A.C.A.C."))
        self.assertFalse(is_degenerate(""))

    def test_temp_limit_parses_vendor_ranges(self):
        """温度上限解析要覆盖各家句式。

        实测坑：退化重试把温度抬到 1.2，智谱上限 [0,1] 直接 400 拒单，
        旧代码只认 Kimi 的 "only 1 is allowed" 句式，没人接 → 一屏 JSON 糊脸。
        """
        from aiun.providers import temp_limit, fixed_temperature
        # Kimi 锁死型
        self.assertEqual(
            temp_limit("temperature: only 1 is allowed for this model"), 1.0)
        self.assertEqual(fixed_temperature(
            "temperature: only 1 is allowed for this model"), 1.0)
        # 智谱范围型（中文冒号、中文方括号都要吃得下）
        self.assertEqual(
            temp_limit('{"error":{"code":"1210","message":'
                       '"temperature参数非法：限制数值范围[0,1]"}}'), 1.0)
        # OpenAI 风格英文句式
        self.assertEqual(
            temp_limit("temperature range [0.0, 2.0] is allowed"), 2.0)
        # 解析不出 → None（走正常报错路径）
        self.assertIsNone(temp_limit("some other error"))

    def test_retry_after_parses_429_wait(self):
        """429 报错里服务端让等几秒要能取出来（智谱免费档 RPM=3 常撞）。"""
        from aiun.providers import retry_after
        self.assertEqual(retry_after('request reached organization max RPM: 3, '
                                     'please try again after 1 seconds'), 1.0)
        self.assertIsNone(retry_after("some other error"))

    def test_deliberate_cli_runs_on_config(self):
        """主命令 aiun deliberate 必须能从配置文件跑通。

        曾用 council.MAX_ROUNDS 但只导入了 Council，导致主命令必崩 —— 这条守住它。
        """
        from aiun.cli import main

        cfg = {
            "max_rounds": 2,
            "anonymize": True,
            "chair": {"provider": "mock", "model": "mock-chair"},
            "delegates": [
                {"name": "A", "role": "proposer", "provider": "mock", "model": "mock-a"},
                {"name": "B", "role": "adversary", "provider": "mock", "model": "mock-b"},
                {"name": "C", "role": "reviewer", "provider": "mock", "model": "mock-c"},
            ],
            "providers": {"mock": {"type": "mock"}},
        }
        path = os.path.join(self.tmp, "council.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

        rc = main(["deliberate", "CLI 冒烟议题", "--config", path, "--outdir", self.outdir])
        self.assertEqual(rc, 0, "deliberate 应正常返回 0")
        self.assertTrue(os.path.isdir(self.outdir), "应产出记录目录")

    def test_local_note_library_roundtrip(self):
        """本地方案库：存得进去、按议题检索得到、不相干的议题召不回来。"""
        from aiun import notes
        d = os.path.join(self.tmp, "exp")
        notes.save(d, "要不要把服务迁到多 region 架构？", "## 方案\n1. 双写\n2. 切流")
        notes.save(d, "要不要给空调设 26 度？", "## 方案\n1. 分区控制")

        self.assertEqual(notes.count(d), 2)
        hits = notes.search(d, "服务迁移到多 region 架构该不该做")
        self.assertTrue(hits, "相近议题必须能翻出历史方案")
        self.assertIn("region", hits[0]["question"])
        self.assertEqual(notes.search(d, "今晚吃什么"), [],
                         "不相干的议题不该被召回，否则等于给代表喂噪音")
        ref = notes.as_reference(d, "服务迁移到多 region 架构该不该做")
        self.assertIn("过去的第 1 套", ref)
        self.assertTrue(os.path.isfile(hits[0]["path"]), "存下来的方案要能直接打开读")

    def test_export_for_other_ai_assistants(self):
        """导出：把方案库变成一份别的 AI 助手能直接读的文件。

        攒在本地只有砚自己看得见 —— 导出这一步才把「知识为我所用」接上外面。
        """
        from aiun import notes
        d = os.path.join(self.tmp, "exp2")
        self.assertIsNone(notes.export_markdown(d), "空库不该产出空文件")

        notes.save(d, "要不要把服务迁到多 region 架构？", "## 方案\n1. 双写\n2. 切流",
                   delegates=["DeepSeek", "Kimi（月之暗面）"])
        out = notes.export_markdown(d)
        self.assertTrue(out and os.path.isfile(out), "必须真的写出文件")
        with open(out, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("要不要把服务迁到多 region 架构？", text)
        self.assertIn("1. 双写", text, "方案正文必须完整带出来，不然导出去没法用")
        self.assertIn("DeepSeek", text, "要写清这套方案是谁开会得出的")
        self.assertIn("AGENTS.md", text, "必须告诉用户怎么接进 Codex/Claude")
        self.assertNotIn("时间：", text.split("---")[0])   # 头部说明里不该混元信息

    def test_scrub_removes_prompt_regurgitation_without_eating_content(self):
        """模型把指令复述一遍再作答时，那段话必须刮掉；正常方案一个字不许动。

        实测 GLM-4.5 会这样。混进方案里，人读着莫名其妙，导出给别的 AI 用
        更是直接污染它的上下文。
        """
        from aiun.notes import scrub
        dirty = ("我们需要回答用户。用户要求作为圆桌会议主持人，把各家办法综合成一套。硬性要求：\n"
                 "1. 方案要具体到能照着做\n"
                 "2. 保留意见原样保留，禁止和稀泥\n"
                 "\n严格输出三段：\n\n"
                 "## 方案\n1. 先做资产梳理\n\n## 未决事项\n- 缺延迟数据")
        out = scrub(dirty)
        self.assertNotIn("严格输出", out)
        self.assertNotIn("硬性要求", out)
        self.assertNotIn("圆桌会议主持人", out)
        self.assertIn("## 方案", out, "真内容必须留下")
        self.assertIn("1. 先做资产梳理", out)
        self.assertIn("缺延迟数据", out)

        clean = ("## 方案\n1. 用户要求低延迟时走边缘节点\n2. 双写灰度\n\n"
                 "## 未决事项\n- 缺压测数据")
        self.assertEqual(scrub(clean), clean,
                         "「用户要求低延迟」是正常方案表述，不许当成指令复述删掉")

    def test_mock_provider_never_hits_network(self):
        """Mock 不发网络请求；阶段按 user 提示识别，评审必须带补救办法。"""
        p = providers.build("mock")
        brief = "你是本次圆桌的一方，和其他各方权限相同。"
        out = p.complete(brief, "议题：\nx\n\n需要你评审的另外两家的办法（已匿名）："
                                "\n[立场A]\n……\n\n逐家评，共 400 字以内。")
        self.assertIn("隐患", out, "评审必须指出哪一步会出事")
        self.assertIn("补法", out, "只挑刺不帮忙等于使绊子：隐患必须带补救办法")

    def test_three_parties_are_equals(self):
        """三方平权：不再有提案方/反对派/审查员之分。

        曾把三家分成不同角色，结果弱的那家被分去"审查"最强的 —— 实测智谱
        免费档当审查员只会跑题，而它出的方案未必差。现在三家同一份简报。
        """
        from aiun.delegate import Delegate, ROLE_BRIEFS
        mk = lambda: providers.build("mock")
        ds = [Delegate("甲", mk(), "proposer"), Delegate("乙", mk(), "adversary"),
              Delegate("丙", mk(), "reviewer")]
        briefs = set(d.brief for d in ds)
        self.assertEqual(len(briefs), 1, "三方必须拿到同一份简报，不许有角色差异")
        # 主持人例外：它只做收敛
        chair = Delegate("主持人", mk(), "chair")
        self.assertNotEqual(chair.brief, ds[0].brief)
        # 老配置里的旧角色名仍要能跑，不能因为改名就崩
        for r in ("proposer", "adversary", "reviewer", "peer"):
            self.assertIn(r, ROLE_BRIEFS, "旧配置的角色名 %s 必须还认得" % r)

    def test_meta_leak_detected(self):
        """思考模型把「内心戏」写进正文（满屏讨论题该怎么答）必须被认出来。

        实测原文特征：反复琢磨"用户要求是啥、要先评还是先出方案、500 字够不够"。
        正常方案提到一次"用户要求低延迟"不许误伤 —— 所以弱特征要凑够两个。
        """
        from aiun.providers import is_meta_leak
        leak = ("需要回复用户。用户要求：你是圆桌一方。看起来可能只要求自己的办法？"
                "但是前文要求评别人的办法。要如何？需要兼顾。矛盾。需要遵守最后明确。")
        self.assertTrue(is_meta_leak(leak))
        self.assertTrue(is_meta_leak("需要回复用户。先给方案再给依据。"))
        # 正常内容不许误伤
        self.assertFalse(is_meta_leak(
            "做法：先做资产梳理，再切流。依据：1. 成本低。用户要求低延迟时走边缘节点。"))
        self.assertFalse(is_meta_leak(""))

    def test_think_tags_are_stripped(self):
        """<think>…</think> 思考块必须剥掉；没闭合的剥到结尾。"""
        from aiun.providers import strip_think
        self.assertEqual(strip_think("<think>纠结一下</think>做法：先判据。"), "做法：先判据。")
        self.assertEqual(strip_think("<THINK>纠结</THINK>\n答案"), "答案")
        self.assertEqual(strip_think("<think>没闭合的思考…"), "")
        self.assertEqual(strip_think("正常内容"), "正常内容")
        self.assertEqual(strip_think(""), "")

    def test_rules_file_is_appended_to_briefs(self):
        """用户会规 rules.md 要并进简报；文件没了要退回空，不许残留上一场的。"""
        from aiun.delegate import Delegate, load_rules
        p = providers.build("mock")
        path = os.path.join(self.tmp, "rules.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("1. 不许说废话")
        self.assertIn("不许说废话", load_rules(self.tmp))
        self.assertIn("不许说废话", Delegate("甲", p, "peer").brief)
        self.assertIn("不许说废话", Delegate("主持", p, "chair").brief,
                      "会规对主持人也要生效")
        # 文件不存在 → 清空，不许残留
        self.assertEqual(load_rules(os.path.join(self.tmp, "nowhere")), "")
        self.assertNotIn("不许说废话", Delegate("乙", p, "peer").brief)

    def test_vendor_catalog_is_well_formed(self):
        """GUI 依赖厂商目录。字段缺失会让界面半死，先在这里守住。"""
        from aiun.gui import VENDORS, VENDOR_BY_KEY
        keys = set()
        for v in VENDORS:
            for field in ("key", "name", "tag", "color", "type", "base_url", "model"):
                self.assertIn(field, v, "厂商 %s 缺字段 %s" % (v.get("key"), field))
            self.assertIn(v["type"], ("openai", "anthropic", "mock"))
            keys.add(v["key"])
        self.assertEqual(keys, set(VENDOR_BY_KEY), "目录与索引必须一致")

    def test_guess_vendor_is_conservative(self):
        """Key 识别：能认出的要认出，认不出的必须返回 None（猜错比不猜坑）。"""
        from aiun.models import guess_vendor
        self.assertEqual(guess_vendor("sk-ant-api03-xxxx"), "anthropic")
        self.assertEqual(guess_vendor("abc123.def456"), "zhipu")
        self.assertEqual(guess_vendor("sk-proj-abcdef123456"), "openai")
        self.assertIsNone(guess_vendor("sk-9f8e7d6c5b4a3210fedcba9876543210"),
                          "DeepSeek/Kimi/通义外形相同，不许乱猜")
    def test_model_display_roundtrips_to_interface_name(self):
        """下拉显示「接口名（官网叫法）」，但发给接口的必须还是纯接口名。"""
        from aiun.gui import (MODEL_LABELS, VENDORS, model_display, model_id)

        self.assertIn("deepseek-chat", MODEL_LABELS,
                      "DeepSeek 的接口名和官网名不一样，必须标出来")
        for v in VENDORS:
            for m in v["models"] or []:
                self.assertEqual(model_id(model_display(m)), m,
                                 "%s 的显示串解析不回接口名" % m)
        # 手动输入的裸接口名不能被括号逻辑啃掉
        self.assertEqual(model_id("deepseek-chat"), "deepseek-chat")
        self.assertEqual(model_id(""), "")
        # 认不出的模型只显示接口名本身，不许编官网名
        self.assertEqual(model_display("my-custom-model"), "my-custom-model")

    def test_every_catalogued_model_has_a_vendor_name(self):
        """内置清单里的每个模型都要有官网叫法，否则用户不知道自己在用哪个。"""
        from aiun.gui import MODEL_LABELS, VENDORS
        for v in VENDORS:
            if v["type"] == "mock":     # 假代表没有官网，不需要说明
                continue
            for m in v["models"] or []:
                self.assertIn(m, MODEL_LABELS,
                              "内置模型 %s 缺官网叫法说明" % m)

    def test_fixed_temperature_retries_locked_models(self):
        """Kimi K2 这类模型锁死 temperature，报错里带的允许值要能解析出来。"""
        from aiun.providers import fixed_temperature
        err = ('HTTP 400 Bad Request: {"error":{"message":"temperature: only 1 '
               'is allowed for this model","type":"invalid_request_error"}}')
        self.assertEqual(fixed_temperature(err), 1.0)
        self.assertEqual(fixed_temperature("temperature Only 0.6 is allowed"), 0.6)
        # 报错格式变了就返回 None，走正常报错路径，不许瞎猜
        self.assertIsNone(fixed_temperature("HTTP 401 Unauthorized"))
        self.assertIsNone(fixed_temperature(""))

    def test_keyring_roundtrip_and_isolation(self):
        """保险库：存取回环、环境变量优先、坏密文静默当空。"""
        from aiun import keyring
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "keys.dat")
            keyring._store_path = lambda: path
            keyring.save("deepseek", "sk-abc1234567890xyz")
            self.assertEqual(keyring.load("deepseek"), "sk-abc1234567890xyz")
            self.assertTrue(keyring.has("deepseek"))
            keyring.save("deepseek", "")          # 空值 = 删除
            self.assertFalse(keyring.has("deepseek"))
            # 环境变量优先于保险库
            os.environ["_AIUN_TEST_KEY"] = "sk-from-env"
            try:
                keyring.save("openai", "sk-from-store")
                self.assertEqual(keyring.get_key("openai", "_AIUN_TEST_KEY"),
                                 "sk-from-env")
            finally:
                del os.environ["_AIUN_TEST_KEY"]
            self.assertEqual(keyring.get_key("openai"), "sk-from-store")
            # 掩码不泄露全文
            m = keyring.masked("sk-1234567890abcdefwxyz")
            self.assertNotIn("1234567890", m)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_saved_config_never_contains_plaintext_key(self):
        """硬性安全线：写盘的配置里永远不许出现明文 Key。"""
        from aiun.gui import VENDOR_BY_KEY

        class FakeWin:
            """只借 ConnectWindow._build_cfg 的逻辑，不起 Tk。"""
            from aiun import gui as _gui
            _build_cfg = _gui.ConnectWindow._build_cfg
            _vendor_name = _gui.ConnectWindow._vendor_name

        win = FakeWin()
        win.attend = {"deepseek": {"on": True, "role": "peer", "model": "deepseek-chat"}}
        win.providers = {"deepseek": {
            "type": "openai", "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-SUPER-SECRET-VALUE", "model": "deepseek-chat",
            "vendor": "deepseek"}}
        win.max_rounds = 2
        win.chair_name = "DeepSeek"
        cfg = win._build_cfg()
        blob = json.dumps(cfg, ensure_ascii=False)
        self.assertNotIn("sk-SUPER-SECRET-VALUE", blob,
                         "明文 Key 混进了写盘配置，安全线失守")
        self.assertEqual(cfg["providers"]["deepseek"].get("key_source"), "keyring")

    def test_vendors_with_keys_have_env_names(self):
        """要 Key 的厂商都必须给出环境变量名，CI 用户不落盘也能用。"""
        from aiun.gui import VENDORS
        from aiun.keyring import ENV_KEYS
        for v in VENDORS:
            if v["type"] in ("openai", "anthropic") and v["key"] not in ("compat", "ollama"):
                self.assertIn(v["key"], ENV_KEYS,
                              "厂商 %s 缺环境变量映射" % v["key"])


if __name__ == "__main__":
    unittest.main()
