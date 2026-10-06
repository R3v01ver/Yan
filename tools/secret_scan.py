"""发布前体检：扫出仓库里可能夹带的密钥 / 个人数据。

用法：
    python tools/secret_scan.py [目录]        # 默认扫仓库根目录
    python tools/secret_scan.py --strict      # 连"疑似"也当失败（CI 用）

退出码：0 = 干净，1 = 发现疑似泄露。CI 里它跑在打包之前，
带着 key 发 release 是没法挽回的 —— 只能吊销。

为什么不用现成的 gitleaks：本项目零 HTTP 依赖是硬约束，
而且要能跟着仓库走（`python tools/secret_scan.py` 一条命令）。
"""

import os
import re
import sys

# 形似密钥的特征。宁可多报几个疑似，也不能漏掉真 key ——
# 漏一个就是"公开仓库 + 可计费的 API"。
PATTERNS = [
    ("OpenAI/DeepSeek 风格 sk-", re.compile(r"sk-[A-Za-z0-9_\-]{20,}")),
    ("Anthropic sk-ant-", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("智谱 id.secret", re.compile(r"\b[0-9a-f]{32}\.[A-Za-z0-9]{16}\b")),
    ("AWS AKIA", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("私钥文件", re.compile(r"-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----")),
    ("配置里的明文 key", re.compile(r"(?i)\b(api_key|apikey|secret|token)\b\s*[:=]\s*[\"'][^\"'\s]{12,}[\"']")),
]

# 测试里写死的假 key：它们长得像真的，但本来就该出现在仓库里。
ALLOW = {
    "sk-ant-api03-xxxx", "sk-proj-abcdef123456", "sk-abc1234567890xyz",
    "sk-1234567890abcdefwxyz", "sk-from-env", "sk-from-store",
    "sk-9f8e7d6c5b4a3210fedcba9876543210", "sk-SUPER-SECRET-VALUE",
    "abc123.def456",
}

# 这些目录不入库或不该被扫
SKIP_DIRS = {".git", "build", "dist", "__pycache__", ".venv", ".build-venv",
             "transcripts", "experience", "node_modules"}
SKIP_EXTS = {".exe", ".ico", ".png", ".jpg", ".pyc", ".dll", ".so", ".dat",
             ".zip", ".gz", ".pdf"}
MAX_FILE = 2 * 1024 * 1024     # 大文件不看，多半是二进制


def iter_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in SKIP_EXTS:
                continue
            yield os.path.join(dirpath, fn)


def is_text(path):
    try:
        if os.path.getsize(path) > MAX_FILE:
            return False
        with open(path, "rb") as f:
            return b"\x00" not in f.read(4096)
    except OSError:
        return False


def scan(root):
    hits = []
    for path in iter_files(root):
        if not is_text(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for i, ln in enumerate(lines, 1):
            for name, pat in PATTERNS:
                for m in pat.finditer(ln):
                    s = m.group(0)
                    # 配置里的明文 key：值是占位符（${...}、xxx）就不算
                    if name.startswith("配置里的明文") and (
                            "${" in s or "xxx" in s.lower() or "your" in s.lower()):
                        continue
                    if any(a in s for a in ALLOW):
                        continue
                    hits.append((os.path.relpath(path, root), i, name,
                                 s[:24] + ("…" if len(s) > 24 else "")))
    return hits


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    root = "."
    for a in argv:
        if not a.startswith("--"):
            root = a
    hits = scan(root)
    if hits:
        print("发现 %d 处疑似密钥，先处理再发版：\n" % len(hits))
        for path, line, name, s in hits:
            print("  %s:%d  [%s]  %s" % (path, line, name, s))
        print("\n处理办法：把 key 挪到环境变量或系统保险库（砚自带 keyring），")
        print("文件里的明文删掉；已经提交过的 key 直接去厂商后台吊销 ——")
        print("删文件没用，git 历史里还留着。")
        return 1
    print("干净：%s 下没发现密钥特征。" % os.path.abspath(root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
