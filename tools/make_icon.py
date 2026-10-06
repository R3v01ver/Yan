"""生成「砚」的图标。

规则：图案由「砚」这个字长出来，不是另画一个东西再配个字。
三种做法：
  A 砚台藏字 —— 砚台是底，字是台面上凿出来的（阳刻）
  B 宣纸落字 —— 宣纸底，墨写的砚字，压一枚朱砂小印
  C 朱砂印章 —— 一枚印，反白的砚字（阴文）

输出 tools/icon_preview.png 给人挑，挑定后 make_ico() 写 aiun/assets/yan.ico
"""
import os

from PIL import Image, ImageDraw, ImageFont

INK = (32, 34, 38)          # 墨
INK_SOFT = (58, 62, 68)     # 淡墨（砚池里的墨）
PAPER = (242, 238, 230)     # 宣纸
PAPER_DEEP = (231, 224, 211)
CINNABAR = (143, 51, 42)    # 朱砂

FONTS = [
    r"C:\Windows\Fonts\STZHONGS.TTF",   # 华文中宋：有碑刻味
    r"C:\Windows\Fonts\simhei.ttf",     # 黑体：小尺寸最清楚
    r"C:\Windows\Fonts\STKAITI.TTF",    # 楷体：更像手写
    r"C:\Windows\Fonts\simsun.ttc",     # 宋体：兜底
]


def _font(size, idx=0):
    for p in FONTS[idx:] + FONTS[:idx]:
        if os.path.isfile(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _glyph(img, box, ch, fill, idx=0, ratio=0.82, dy=0):
    """把一个字画进 box 里，自动撑到 ratio 大小并居中。"""
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    f = _font(10, idx)
    bbox = d.textbbox((0, 0), ch, font=f)
    gw, gh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    size = int(min(w / max(gw, 1), h / max(gh, 1)) * 10 * ratio)
    f = _font(size, idx)
    bbox = d.textbbox((0, 0), ch, font=f)
    gw, gh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    cx, cy = x0 + (w - gw) / 2, y0 + (h - gh) / 2 + dy
    d.text((cx - bbox[0], cy - bbox[1]), ch, font=f, fill=fill)
    return img


# ── A 砚台藏字：砚台是底，字从台面上凿出来 ──────────────
def variant_a(S=256):
    img = Image.new("RGBA", (S, S), INK + (255,))
    d = ImageDraw.Draw(img)
    m = int(S * 0.09)
    # 砚身：宣纸白，直角（不圆角，跟界面其余部分一致）
    d.rectangle([m, m, S - m, S - m], fill=PAPER)
    # 砚池：右上角一块方形墨，外缘贴着砚身边缘（不压字）
    w, h = int(S * 0.34), int(S * 0.24)
    d.rectangle([S - m - w, m, S - m, m + h], fill=INK)
    d.rectangle([S - m - w + int(S * 0.025), m + int(S * 0.025),
                 S - m - int(S * 0.025), m + h - int(S * 0.025)],
                fill=INK_SOFT)
    # 字：凿在砚堂正中，往左下让开右上角的池
    _glyph(img, (int(S * 0.10), int(S * 0.26), int(S * 0.86), int(S * 0.94)),
           "砚", INK, idx=0, ratio=0.92)
    return img


# ── B 宣纸落字：一张纸，一个墨字，一枚朱砂小印 ───────────
def variant_b(S=256):
    img = Image.new("RGBA", (S, S), PAPER + (255,))
    d = ImageDraw.Draw(img)
    # 纸的四边压一道淡墨框，小尺寸时轮廓才立得住
    d.rectangle([0, 0, S - 1, S - 1], outline=PAPER_DEEP, width=max(2, S // 64))
    _glyph(img, (int(S * 0.05), int(S * 0.03), int(S * 0.95), int(S * 0.95)),
           "砚", INK, idx=0, ratio=1.0)
    # 右下角一枚实心朱砂印，小尺寸时是「墨字 + 一点红」
    s = int(S * 0.17)
    k = int(S * 0.065)
    d.rectangle([S - s - k, S - s - k, S - k, S - k], fill=CINNABAR)
    return img


# ── C 朱砂印章：一枚印，反白的砚字 ──────────────────────
def variant_c(S=256):
    img = Image.new("RGBA", (S, S), PAPER + (255,))
    d = ImageDraw.Draw(img)
    m = int(S * 0.08)
    d.rectangle([m, m, S - m, S - m], fill=CINNABAR)
    # 印边：朱砂块里留一圈纸色，像刻印时留的边
    k = int(S * 0.035)
    d.rectangle([m + k, m + k, S - m - k, S - m - k], outline=PAPER,
                width=max(2, S // 90))
    _glyph(img, (m + k * 2, m + k * 2, S - m - k * 2, S - m - k * 2),
           "砚", PAPER, idx=0, ratio=1.0)
    return img


VARIANTS = [("A 砚台藏字", variant_a), ("B 宣纸落字", variant_b),
            ("C 朱砂印章", variant_c)]


def preview(out="tools/icon_preview.png"):
    S = 256
    rows, cols = len(VARIANTS), 4
    cell = S + 40
    W = cell * cols
    H = cell * rows
    sheet = Image.new("RGB", (W, H), (250, 248, 244))
    d = ImageDraw.Draw(sheet)
    for r, (name, fn) in enumerate(VARIANTS):
        base = fn(S)
        for c, size in enumerate((S, 96, 48, 20)):
            thumb = base.resize((size, size), Image.LANCZOS)
            x = c * cell + (cell - size) // 2
            y = r * cell + 24
            sheet.paste(thumb, (x, y), thumb if thumb.mode == "RGBA" else None)
            if c == 0:
                d.text((c * cell + 14, r * cell + 6), name, fill=(60, 60, 60))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    sheet.save(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", out))
    return out


def make_ico(which="a", out=None):
    fn = dict(a=variant_a, b=variant_b, c=variant_c)[which]
    here = os.path.dirname(os.path.abspath(__file__))
    out = out or os.path.join(here, "..", "aiun", "assets", "yan.ico")
    img = fn(256)
    img.save(out, sizes=[(256, 256), (128, 128), (64, 64), (48, 48),
                         (32, 32), (24, 24), (16, 16)])
    return os.path.abspath(out)


if __name__ == "__main__":
    import sys
    preview()
    pick = sys.argv[1] if len(sys.argv) > 1 else "a"
    print("ico:", make_ico(pick))
