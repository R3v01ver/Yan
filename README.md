# Yan (砚) — A Council of AIs That Argue, Not Agree

**English** | [中文](#砚--ai-圆桌会议)

Most multi-agent frameworks split up *work*. Yan is about **judgment quality**: give the same question to independent delegates, force at least one of them to falsify, let authors rebut point by point, then vote — and **preserve the dissent, never average it away**.

**The catch:** this only works if your delegates are actually different. Three instances of the same model will make the same mistake and then congratulate each other. Yan detects that and warns you the session is pointless.

### Quick start (zero API keys, zero network)

```bash
pip install -e .
aiun demo        # runs the full five-phase deliberation with mock delegates
```

### How a session runs

```
① Position      delegates state stances independently (no anchoring)
② Cross-exam    anonymous critique; the adversary must falsify, never agree
③ Rebuttal      authors accept or refute point by point (no blanket moves)
④ Vote          APPROVE / REJECT / ABSTAIN
⑤ Resolution    chair synthesizes: Resolution / Preserved Dissent / Open Questions
```

Convergence detection: if a round produces no new arguments, the session ends early.

Connects to any OpenAI-compatible endpoint (DeepSeek / Qwen / Moonshot / Ollama / vLLM), Anthropic, or the built-in mock. Zero HTTP library dependencies — pure standard library.

> Full design rationale, honest limitations, and configuration examples below (in Chinese).

---

# 砚 — AI 圆桌会议

> 让多个平权的模型各出一套方案、互相评审、取长补短，最后收敛成当前最优的一套。

一个模型的最大问题不是不够聪明，是**它会顺着自己走**：先有结论，再找支持证据，最后用流畅的语气让你觉得它很确定。

砚 不做分工执行（那是 AutoGen / CrewAI 的事），它做**判断质量**：把同一个议题丢给若干互相独立的代表，强制至少一个代表专门负责证伪，匿名互评，答辩，表决，最后由主持人形成决议——**并且原样保留异议，不和稀泥**。

---

## 先说清楚：什么时候不该用它

诚实边界比功能列表重要。

| 情况 | 建议 |
|---|---|
| 简单事实查询、格式转换、摘要 | 别用。单模型更快更便宜，开会纯浪费 |
| 代表全是同一个模型 | 收益接近 0。同质模型会犯同一个错，还互相加冕，产生虚假自信 |
| 预算敏感 / 要跑几千次 | 成本是单模型的 N 倍（N = 代表数 × 阶段数） |
| 需要秒级响应 | 别用，一轮会议是几十次串行调用 |

它值得开的会：**复杂判断、方案评审、风险论证、需要有人唱反调的决策**。也就是"错了代价很高"的场合。

---

## 下载现成的（不想装 Python）

到 [Releases](../../releases) 下载对应平台的单文件程序，双击即用，不需要装任何东西：

| 平台 | 文件名 | 怎么跑 |
|---|---|---|
| Windows | `砚-Windows-x86_64.exe` | 双击运行 |
| Linux | `砚-Linux-x86_64` | `chmod +x 砚-Linux-x86_64 && ./砚-Linux-x86_64` |

两个包都由 GitHub Actions 从同一个源码自动编出（见 `.github/workflows/build.yml`），
出包前会先跑密钥扫描和单元测试。

## 图形界面（不想碰命令行就用这个）

```bash
aiun-gui
```

或从上面 Releases 下载对应平台的单文件程序。填议题、选「演示」或「正式」、点开始，过程和结果都在窗口里显示，记录目录一键打开。

自己打包（Windows 和 Linux 用同一个 spec）：

```bash
pip install pyinstaller pyyaml pillow
pyinstaller --noconfirm --clean 砚.spec     # Windows 出 dist/砚.exe，Linux 出 dist/砚
```

> 打包环境必须带 `tkinter`（Linux 上常见缺失：`sudo apt install python3-tk`）。
> 少了它会打出**能编译、启动即崩**的程序 —— spec 里 `pathex=['.']` 那一行就是防这个的
> （曾经漏掉，结果整个 `aiun` 包没打进去，一进界面就 `No module named 'aiun.gui'`）。

---

## 零成本先跑一遍（不需要任何 API Key）

```bash
pip install -e .
aiun demo
```

`demo` 用内置的 Mock 代表跑完整流程——不发任何网络请求，但出方案、交叉评审、修订、收敛、表态五个阶段都是真的。先确认这套机制是你要的，再接真模型。

---

## 接真模型

```bash
aiun init              # 生成 council.yaml
$EDITOR council.yaml   # 填代表和凭证
aiun deliberate "是否应该把服务迁移到多 region 架构" --config council.yaml
```

配置示例：

```yaml
max_rounds: 2          # 评审-修订轮数，收敛会自动提前结束
anonymize: true        # 匿名互评，避免"权威偏见"

chair:                 # 主持人：把三家方案收敛成一套，建议选最强的模型
  provider: openai
  model: gpt-4o-mini

delegates:
  - name: 代表A
    role: peer          # 三方平权，角色一致（旧写的 proposer/adversary/reviewer 会自动归一）
    provider: openai
    model: gpt-4o-mini
  - name: 代表B
    role: peer
    provider: anthropic
    model: claude-sonnet-4-20250514
  - name: 代表C
    role: peer
    provider: ollama    # 本地 Ollama，零成本
    model: qwen2.5:14b

providers:
  openai:    {type: openai,   base_url: https://api.openai.com/v1, api_key_env: OPENAI_API_KEY}
  anthropic: {type: anthropic, api_key_env: ANTHROPIC_API_KEY}
  ollama:    {type: openai,   base_url: http://localhost:11434/v1}
```

**关键配置原则**：代表尽量跨模型家族。CLI 检测到所有代表同源会直接警告你"这场会大概率白开"。

**Key 的存放**：图形界面保存的 Key 用 Windows DPAPI 加密后存在你自己的用户目录（`%APPDATA%\Yan\keys.dat`，绑定当前 Windows 账户），**不会写进 council.yaml**——把整个项目目录传上 git 也不会泄露 Key。命令行用户推荐用 `api_key_env` 走环境变量。

---

## 会议流程

```
① 各自出方案   三方独立各出一套（互不看见，避免锚定）
② 交叉评审     每套方案都由另外两家来评：哪一点更好、哪一步会出事、怎么补
③ 修订补短     各家吸收针对自己的评审意见，改出修订版；不吸收的要说明理由
④ 定最优方案   主持人把三家所长收敛成一套，每一步注明来自哪家的哪一条
⑤ 表态         认同 / 有保留 / 不认同，保留意见原样留下
               ↑ 后两栏是重点：不允许把分歧平均掉
```

三方**平权**：没有"提案方 / 反对派 / 审查员"之分。实测把三家分成不同角色会让
弱的那家被分去审最强的（智谱免费档当审查员只会跑题，可它出的方案未必差）。
平权之后每家都出方案、每家都评别人，任何一家弱都会被另外两家的评审盖过去。

主持人只做收敛，不新增自己的主张 —— 但**主持人的模型质量决定最终方案的质量**，
所以它可以在界面上单独选，建议选最强的那家。

收敛检测：某一轮评审没有产生任何新东西，会议提前结束。不会为了"显得认真"空转。

---

## 为什么这么设计

每一条都对应一个已知的多智能体失效模式：

- **独立出方案**——对抗锚定效应。先看到别人的答案，你就只是在评论它。
- **三方平权**——对抗"让最弱的一家去审最强的"。角色不平等时，弱模型被分去
  挑强模型的毛病，测出来的不是分歧而是它的能力上限。
- **交叉评审**——对抗"自己评自己"与"只被一人评"的抽样偏差。每套方案都由
  另外两家独立评，任何一家跑偏都会被另外两家的评审盖过去。
- **匿名互评**——对抗权威偏见。暴露模型来源会让某些意见被无理由加权。
- **修订环节**——对抗"批评没人回应"。评审必须被吸收或说明理由，不能悬空。
- **保留异议**——对抗共识幻觉。被平均掉的少数意见常常是对的。
- **未决事项**——对抗过度确定。明确写出"还需要什么证据才能定论"。
- **收敛检测**——对抗仪式化。讨论本身不是目的。

---

## 作者与授权

作者 **R41N**（GitHub：[@R3v01ver](https://github.com/R3v01ver)）。

代码以 **MIT** 授权（[LICENSE](LICENSE)）：可自由使用、修改、商用，唯一条件是保留署名。
LICENSE 文件末尾附有一份与 MIT 一并生效的边界声明（输出非专业建议 / 费用自负 / 密钥自管）。

## 免责声明

**砚按「原样」提供，输出是模型生成的文本，不构成任何专业建议，作者不对使用后果负责。**
接真模型后一场会是几十次付费调用，成本由你掌控。完整条款见 [DISCLAIMER.md](DISCLAIMER.md)。

## 密钥是怎么保管的（发版前必读）

- **Key 不进仓库、不进配置文件**：GUI 填的 Key 存进系统保险库（Windows `%APPDATA%\Yan\keys.dat`，
  用系统 API 加密），yaml 里只留一个引用。命令行可以用环境变量（`api_key_env`）。
- **Key 发往哪由你配的 `base_url` 决定**；加载别人给的 `council.yaml`，等于把 Key 交给那个地址的持有者。
- 发版/提交前跑一遍体检脚本，它会扫出仓库里形似密钥的字符串：

```bash
python tools/secret_scan.py .     # 干净退出 0；发现疑似退出 1
```

CI 里这一步跑在打包之前（`.github/workflows/build.yml`），带着 key 发 release 只能吊销，删文件没用。

## 会规（rules.md）

接口模型没有记忆，规则每条请求都得随行带上——这是 API 机制，绕不开。
但规则不该写死在代码里：砚启动一场会时会读程序目录下的 `rules.md`，
把内容追加到每位代表（含主持人）的简报后面。想改规矩，改这一个文件就行。

另外，思考模型（推理档）偶尔会把"内心戏"写进正文——满屏都在讨论
"题目要求是什么、我该先写哪部分"，就是不给答案。砚会认出这种输出并
自动升温重发；`rules.md` 里写一条"不许在正文里讨论题目要求"能明显减少它的发生。

## 会议记录

每次会议落盘两份，可审计、可回放：

```
transcripts/2026-10-06T16-07-15.json   结构化，供程序消费
transcripts/2026-10-06T16-07-15.md     人读版
```

结构化记录带 `phase / speaker / role / content`，可以直接喂给复盘流程——**积累的不是决议库，是"我们在哪类议题上最容易集体犯错"的模式库**。

---

## 目录

```
aiun/
├── council.py     会议编排（五个阶段 + 收敛检测）
├── delegate.py    代表与角色简报
├── providers.py   模型接入（OpenAI 兼容 / Anthropic / Mock，零 HTTP 依赖）
├── transcript.py  会议记录落盘
└── cli.py         命令行
```

## 路线

- [x] 五阶段会议 + 收敛检测 + 异议保留
- [x] OpenAI 兼容 / Anthropic / 本地 Ollama / Mock
- [ ] 成本上限硬闸门：轮数上限已加（MAX_ROUNDS=5），`--max-calls` / `--max-tokens` 待做
- [ ] 从会议记录复盘归纳"集体误判模式"
- [ ] 并行出方案（当前串行）
- [ ] Web 视图回放

## License

MIT
