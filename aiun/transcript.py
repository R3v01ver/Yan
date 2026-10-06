"""会议记录落盘。

每次会议写两份：结构化 JSON（供程序消费）+ Markdown（人读）。
结构化记录带 phase / role，可以直接喂复盘流程 ——
真正积累的不是决议库，是"我们在哪类议题上最容易集体犯错"的模式库。
"""

import json
import os
import re
from datetime import datetime

SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(text, n=24):
    s = SAFE.sub("-", text or "").strip("-")
    return (s[:n] or "session")


class Transcript(object):
    def __init__(self, question, outdir="transcripts"):
        self.question = question
        self.outdir = outdir
        self.started = datetime.now()
        self.entries = []
        self.meta = {}

    def add(self, phase, speaker, role, content):
        self.entries.append(
            {
                "phase": phase,
                "speaker": speaker,
                "role": role,
                "content": content,
                "at": datetime.now().isoformat(timespec="seconds"),
            }
        )

    def _base(self):
        stamp = self.started.strftime("%Y-%m-%dT%H-%M-%S")
        name = "%s-%s" % (stamp, _slug(self.question))
        return os.path.join(self.outdir, name)

    def save(self):
        os.makedirs(self.outdir, exist_ok=True)
        base = self._base()

        payload = {
            "question": self.question,
            "started_at": self.started.isoformat(timespec="seconds"),
            "meta": self.meta,
            "entries": self.entries,
        }
        jpath = base + ".json"
        with open(jpath, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        mpath = base + ".md"
        lines = [
            "# 砚 会议记录",
            "",
            "**议题**：%s" % self.question,
            "",
            "**开始**：%s" % payload["started_at"],
            "",
        ]
        cur = None
        for e in self.entries:
            if e["phase"] != cur:
                cur = e["phase"]
                lines += ["", "## %s" % cur, ""]
            lines += ["### %s（%s）" % (e["speaker"], e["role"]), "", e["content"], ""]
        with open(mpath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        return jpath, mpath
