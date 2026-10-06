# Security Policy

## Supported Versions

| Version | Supported |
|---|---|
| 0.1.x | ✅ |

## 报告漏洞

**请不要开 public issue。**

安全问题请通过 GitHub 的
[Private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
（仓库 → Security → Report a vulnerability）私下提交。

请包含：

- 影响的版本 / commit
- 复现步骤（可复现的最小配置即可，**不要附带真实 API key**）
- 实际影响（不是理论影响）
- 如果有的话，建议的修复方式

我会在 7 天内确认收到；确认后 30 天内给出处理结论（修复 / 拒绝并说明理由）。

## 范围说明

砚（Yan）是本地 CLI 工具：不监听端口、不接收远程输入、不执行 shell、不做反序列化。
因此**大多数传统 Web 漏洞类别不适用于本项目**。

真正算得上安全问题的，是下面这几类：

1. **凭证泄露**——API key 被写入日志、会议记录（`transcripts/`）、异常信息或被发往用户未配置的端点。
2. **配置导致的凭证外流**——`council.yaml` 里的 `base_url` 决定 key 发往哪里。
   **加载第三方提供的配置文件，等同于把你的 API key 交给那个 `base_url` 的持有者。**
3. **表决/决议结果可被操纵**——例如某代表的输出能在计票环节被解析成非其本意的票型。
4. **成本失控**——配置导致无限或不可预期的大量付费调用。

## 不在范围内

- 模型本身产生的内容（幻觉、偏见、有害输出）——那是模型厂商的责任。
- 用户自己把明文 key 提交到了公开仓库——请立刻吊销该 key。
- "AI 给出的结论是错的"——这是设计上承认的局限，不是漏洞；
  本项目的目标是让分歧可见，不是保证结论正确。
