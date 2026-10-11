"""横屏卡片：左边封面海报，右边中文名 / 原名 / 放送时间 / 第 N 集更新了。

为什么要有它
------------
推送以前直接发封面原图（竖版海报），中文名、原名、放送时间全在正文文字里，
看图的人得在文字和海报之间来回对照。现在把它们拼成一张**横屏卡片**（1200×675）：

    ┌──────────────────────────────────────────────────────┐
    │  ┌────────┐   ▬▬▬                                   │
    │  │        │   《中文名》  ← 大号加粗，最多 3 行        │
    │  │  封面  │   原名：…     ← 次要色，最多 2 行          │
    │  │  2:3   │   放送：周三 23:00  ← 强调色               │
    │  └────────┘   第 N 集更新了   ← 强调色、加粗（图内强调） │
    └──────────────────────────────────────────────────────┘

「第 N 集更新了」正文里已经有一句，卡片上再强调一次 —— 用户要求图里也能一眼看到是第几集。
文字正文（render.py 默认排版）只留「【新番更新】作品名 / 第 N 集更新了」，原名、放送
只在卡片里出现，不在文字里重复。

背景 = 封面缩到极小再拉伸 + 高斯模糊 + 压暗 —— 与封面同色系的渐变底，再统一压暗到
固定程度，这样白字的对比度**不受封面明暗影响**（亮海报也不会把字吃掉）。

几个容易踩的点
--------------
- **字体回退**：主字体微软雅黑**没有**韩文 / 泰文 / 老挝文，番剧原名偶尔是韩文，
  直接画就是一排豆腐块方框。这里按 Unicode 区块逐字符回退到 Malgun Gothic / Tahoma。
  但**只在外语字符真的出现时才逐字符绘制**（`_mixed`）—— 逐字符是用统一 baseline
  重画的，纯中/日文没必要切，切了反而可能整行上下飘。
- **换行**：拉丁字母 / 数字攒成整词再换行（别把 `SPY×FAMILY` 切成 `SPY×FAMI` + `LY`），
  中日韩逐字换；单词本身就超宽时才硬切。
- **缓存**：`data/cards/<md5>.jpg`，键 = 封面地址 + 三行文字 + 马赛克级别。改了订阅里的
  名字 / 时间 → 键变了 → 自动重画，**不会发出旧卡片**；`_sweep` 只留最近 400 张。
- **马赛克级别**：`level=0` 用原图；`level>0` 先用 `cover.sanitize` 把封面打成第 N 级
  马赛克再拼。官方 QQ 判定「内容违规」时升级的是**拼进卡片之前的那张封面** ——
  卡片本身不做像素化，否则右边那几行字会跟着一起糊掉。
"""
from __future__ import annotations

import asyncio
import colorsys
import hashlib
import json
import os
from pathlib import Path

from loguru import logger

from . import cover, paths, store

CARD_DIR = paths.DATA_DIR / "cards"

SIZE = (1200, 675)            # 横屏 16:9
PAD = 36                      # 卡片内边距
GAP = 40                      # 封面与文字栏之间的间距
RADIUS = 16                   # 封面圆角
POSTER_RATIO = 2 / 3          # 封面按竖版海报比例占位

TITLE_SIZE = 52
TITLE_LINES = 3
META_SIZE = 30
META_LINES = 2
TITLE_LEAD = 74               # 标题行距
META_LEAD = 48                # 次要信息行距

BAR_W, BAR_H = 64, 6          # 标题上方的小色条
BAR_GAP = 28                  # 色条 → 标题
BLOCK_GAP = 26                # 标题 → 原名
LINE_GAP = 12                 # 原名 → 放送

TITLE_COLOR = (246, 248, 252)
NATIVE_COLOR = (176, 184, 203)
ACCENT_FALLBACK = (255, 138, 170)     # 封面取不出明显颜色时用的强调色（番剧粉）
BG_BASE = (13, 14, 22)
BG_DARKEN = 0.80              # 背景压暗程度（越大越暗，白字越稳）

EP_SIZE = 34                  # 「第 N 集更新了」字号（比标题小、比次要信息大）
EP_LEAD = 52                  # 强调行行距
EP_GAP = 22                   # 放送 → 第 N 集更新了 的间距

JPEG_QUALITY = 90
KEEP_FILES = 400              # 卡片缓存上限，超了按修改时间删最旧的

_WIN = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or "C:/Windows"
_FONT_DIR = Path(_WIN) / "Fonts"

# 每种的候选链：`(常规体, 粗体)`，按顺序试，全打不开才退回 Pillow 内置位图字体。
# （微软雅黑缺韩 / 泰 / 老挝；见模块开头「字体回退」。）
_FONTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "cjk": (("msyh.ttc", "simhei.ttf", "Deng.ttf"),
            ("msyhbd.ttc", "simhei.ttf", "Dengb.ttf")),
    "kr": (("malgun.ttf",),
           ("malgunbd.ttf", "malgun.ttf")),
    "th": (("LeelawUI.ttf", "tahoma.ttf"),
           ("LeelaUIb.ttf", "tahomabd.ttf", "tahoma.ttf")),
}

_CACHE: dict[tuple[str, int, bool], object] = {}


# ---------------- 字体 ----------------


def _pick(kind: str, size: int, bold: bool = False):
    """取字体对象。按 `(种类, 字号, 粗细)` 缓存 —— 逐字符绘制一调用就是几百次，
    每次都 `ImageFont.truetype` 会明显卡。"""
    size = max(8, int(size))
    key = (kind, size, bool(bold))
    font = _CACHE.get(key)
    if font is not None:
        return font
    from PIL import ImageFont

    regular, heavy = _FONTS.get(kind) or _FONTS["cjk"]
    for name in (heavy if bold else regular):
        try:
            font = ImageFont.truetype(str(_FONT_DIR / name), size)
            break
        except Exception:          # 这个字体没有 / 打不开 → 试下一个
            continue
    if font is None:
        font = ImageFont.load_default()
    _CACHE[key] = font
    return font


def _kind(ch: str) -> str:
    """字符该用哪种字体：韩文音节 / 字母 → kr，泰文 + 老挝文整块 → th，其余 → cjk。"""
    o = ord(ch)
    if 0xAC00 <= o <= 0xD7A3 or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F:
        return "kr"
    if 0x0E00 <= o <= 0x0EFF:
        return "th"
    return "cjk"


def _mixed(text: str) -> bool:
    """这串里有没有主字体画不出来的字符（只比区间，不做字形探测 —— 那个慢）。"""
    return any(_kind(ch) != "cjk" for ch in str(text))


def _tw(text: str, size: int, bold: bool = False) -> float:
    """显示宽度（逐字符回退版的测量，别用单字体的 `getlength`，否则混排会错位）。"""
    text = str(text or "")
    if not _mixed(text):
        return _pick("cjk", size, bold).getlength(text)
    return sum(_pick(_kind(ch), size, bold).getlength(ch) for ch in text)


def _draw(draw, x: int, top: int, text: str, size: int, bold: bool, fill) -> None:
    """在 `(x, top)` 画一行；`top` = 字行顶部（与 Pillow 的 `anchor="la"` 同一基准）。"""
    text = str(text or "")
    base = _pick("cjk", size, bold)
    if not _mixed(text):
        draw.text((x, top), text, font=base, fill=fill, anchor="la")
        return
    # anchor="la" 把 ascender 顶放在 top，等价于基线在 top + ascent（见 skill 实测表）
    ascent = base.getmetrics()[0]
    cx = x
    for ch in text:
        font = _pick(_kind(ch), size, bold)
        draw.text((cx, top + ascent), ch, font=font, fill=fill, anchor="ls")
        cx += font.getlength(ch)


# ---------------- 折行 ----------------


def _wordish(ch: str) -> bool:
    """要不要跟前后字符攒成一个「词」：拉丁 / 西里尔 / 数字 / 常见标点算，CJK 不算。"""
    if ch.isdigit():
        return True
    if ord(ch) >= 0x2E80:          # CJK / 假名 / 韩文 → 逐字换行
        return False
    return ch.isalnum() or ch in "-_/.&×·'’!?+:~"


def _tokens(text: str) -> list[tuple[str, bool]]:
    """切成不可再分的显示单元，返回 `(词, 前面是否带空格)`。"""
    out: list[tuple[str, bool]] = []
    buf = ""
    space = False
    for ch in str(text):
        if ch.isspace():
            if buf:
                out.append((buf, space))
                buf = ""
                space = False
            space = True
            continue
        if _wordish(ch):
            buf += ch
            continue
        if buf:
            out.append((buf, space))
            buf = ""
            space = False
        out.append((ch, space))
        space = False
    if buf:
        out.append((buf, space))
    return out


def _wrap(text: str, size: int, bold: bool, max_w: int, max_lines: int) -> list[str]:
    """按显示宽度折行，超过 `max_lines` 行就截断加省略号。"""
    text = str(text or "").strip()
    if not text:
        return []
    lines: list[str] = []
    cur = ""
    for word, space in _tokens(text):
        piece = word if (not cur or not space) else " " + word
        if not cur or _tw(cur + piece, size, bold) <= max_w:
            cur += piece
            continue
        lines.append(cur)
        cur = word
    if cur:
        lines.append(cur)

    out: list[str] = []
    for ln in lines:                       # 单个词本身就超宽 → 只能硬切
        if _tw(ln, size, bold) <= max_w:
            out.append(ln)
            continue
        buf = ""
        for ch in ln:
            if buf and _tw(buf + ch, size, bold) > max_w:
                out.append(buf)
                buf = ch
            else:
                buf += ch
        if buf:
            out.append(buf)

    if len(out) > max_lines:
        out = out[:max_lines]
        last = out[-1].rstrip()
        while last and _tw(last + "…", size, bold) > max_w:
            last = last[:-1]
        out[-1] = last + "…"
    return out


# ---------------- 画画 ----------------


def _cover_fill(im, width: int, height: int):
    """等比放大到「填满」再居中裁切（不变形）。裁的时候偏上一点 —— 海报的人脸 /
    标题通常在偏上的位置。"""
    from PIL import Image

    ratio = max(width / max(1, im.width), height / max(1, im.height))
    nw = max(width, round(im.width * ratio))
    nh = max(height, round(im.height * ratio))
    im = im.resize((nw, nh), Image.LANCZOS)
    left = (nw - width) // 2
    top = (nh - height) // 3
    return im.crop((left, top, left + width, top + height))


def _accent(art) -> tuple[int, int, int]:
    """从封面取一个强调色（放送时间那行用它）。取平均色再提饱和 / 提亮；
    封面本身太灰就退回固定的番剧粉。"""
    from PIL import Image

    px = art.resize((1, 1), Image.BOX).getpixel((0, 0))
    hue, sat, val = colorsys.rgb_to_hsv(px[0] / 255, px[1] / 255, px[2] / 255)
    if sat < 0.18:
        return ACCENT_FALLBACK
    r, g, b = colorsys.hsv_to_rgb(hue, min(0.95, max(sat, 0.5)), min(1.0, max(val, 0.92)))
    return (round(r * 255), round(g * 255), round(b * 255))


def _render(src: Path, title: str, native: str, when: str, ep=None) -> bytes | None:
    """拼一张卡片，返回 JPEG 字节；任何一步不行都返回 None（调用方退回原封面）。

    Pillow 是同步的，调用方用 `asyncio.to_thread` 跑，别卡住事件循环。
    """
    try:
        from io import BytesIO

        from PIL import Image, ImageDraw, ImageFilter
    except Exception as exc:               # noqa: BLE001 —— 没装 Pillow 就整条通道不用
        logger.warning(f"番剧卡片：Pillow 不可用（{exc}），退回封面原图")
        return None

    try:
        with Image.open(src) as raw:
            art = raw.convert("RGB")
    except Exception as exc:               # noqa: BLE001
        logger.warning(f"番剧卡片：封面读不出来（{exc}）")
        return None

    width, height = SIZE
    ph = height - 2 * PAD
    pw = max(1, round(ph * POSTER_RATIO))
    box = (PAD, PAD, PAD + pw, PAD + ph)
    accent = _accent(art)

    # 背景：封面 → 极小 → 拉伸 → 模糊 → 压暗
    bg = art.resize((28, 16), Image.BILINEAR).resize(SIZE, Image.BICUBIC)
    bg = bg.filter(ImageFilter.GaussianBlur(22))
    canvas = Image.blend(bg, Image.new("RGB", SIZE, BG_BASE), BG_DARKEN).convert("RGBA")

    # 封面投影（先用一张透明层画好再合成，别直接在 RGBA 上画半透明色）
    shadow = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (box[0] + 7, box[1] + 11, box[2] + 7, box[3] + 11),
        radius=RADIUS, fill=(0, 0, 0, 120),
    )
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(16)))

    poster = _cover_fill(art, pw, ph)
    mask = Image.new("L", (pw, ph), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, pw - 1, ph - 1), radius=RADIUS, fill=255)
    canvas.paste(poster, (box[0], box[1]), mask)

    # 文字画在独立透明层上：这样半透明的描边才会真正混合，而不是把 alpha 覆盖掉
    layer = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle(
        (box[0], box[1], box[2] - 1, box[3] - 1),
        radius=RADIUS, outline=(255, 255, 255, 46), width=1,
    )

    x = box[2] + GAP
    max_w = width - PAD - x
    title_lines = _wrap(title or "（未命名）", TITLE_SIZE, True, max_w, TITLE_LINES)
    native_lines = (
        [] if (not native or native == title)
        else _wrap(f"原名：{native}", META_SIZE, False, max_w, META_LINES)
    )
    when_lines = _wrap(f"放送：{when}", META_SIZE, False, max_w, 1)
    ep_lines = (
        [] if not ep
        else _wrap(f"第 {ep} 集更新了", EP_SIZE, True, max_w, 1)
    )

    block = BAR_H + BAR_GAP + len(title_lines) * TITLE_LEAD
    if native_lines:
        block += BLOCK_GAP + len(native_lines) * META_LEAD
    if when_lines:
        block += LINE_GAP + len(when_lines) * META_LEAD
    if ep_lines:
        block += EP_GAP + len(ep_lines) * EP_LEAD
    y = box[1] + max(0, (ph - block) // 2)          # 整块在封面高度里垂直居中

    draw.rounded_rectangle((x, y, x + BAR_W, y + BAR_H), radius=BAR_H // 2, fill=accent + (255,))
    y += BAR_H + BAR_GAP
    for line in title_lines:
        _draw(draw, x, y, line, TITLE_SIZE, True, TITLE_COLOR + (255,))
        y += TITLE_LEAD
    if native_lines:
        y += BLOCK_GAP
        for line in native_lines:
            _draw(draw, x, y, line, META_SIZE, False, NATIVE_COLOR + (255,))
            y += META_LEAD
    if when_lines:
        y += LINE_GAP
        _draw(draw, x, y, when_lines[0], META_SIZE, False, accent + (255,))
        y += META_LEAD          # ★ 以前「放送」是最后一行，画完不用挪 y；现在下面还有集数行
    if ep_lines:
        y += EP_GAP
        for line in ep_lines:
            _draw(draw, x, y, line, EP_SIZE, True, accent + (255,))
            y += EP_LEAD

    out = Image.alpha_composite(canvas, layer).convert("RGB")
    buf = BytesIO()
    out.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


# ---------------- 对外 ----------------


def fields_of(rule: dict) -> tuple[str, str, str]:
    """卡片右侧的三行：中文名 / 原名 / 放送时间（放送 = 周几 + 时刻）。"""
    title = str(rule.get("title") or "").strip()
    native = str(rule.get("native") or "").strip()
    try:
        weekday = int(rule.get("weekday") or 0)
    except (TypeError, ValueError):
        weekday = 0
    name = store.weekday_label(weekday) if 1 <= weekday <= 7 else ""
    when = f"{name} {str(rule.get('time') or '00:00').strip()}".strip()
    return title, native, when


def _key(url: str, title: str, native: str, when: str, level: int, ep) -> str:
    raw = json.dumps([url, title, native, when, int(level), ep], ensure_ascii=False)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _sweep() -> None:
    """卡片缓存别无限长：超过上限就按修改时间删最旧的。"""
    try:
        files = sorted(CARD_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
        for old in files[: max(0, len(files) - KEEP_FILES)]:
            old.unlink(missing_ok=True)
    except OSError:
        pass


async def build(rule: dict, *, level: int = 0, ep=None) -> Path | None:
    """生成（或取用缓存）这条订阅的横屏卡片，返回本地图片路径；生成不了返回 None。

    - `level=0` 用封面原图；
    - `level>0` 先用 `cover.sanitize` 把封面打成第 N 级马赛克再拼进去 ——
      官方 QQ 判定「内容违规」时，逐级升级的是**拼卡片之前的那张封面**。
    - `ep` = 今天更新的集数（整数）。非 None 时会在卡片右侧补一行「第 N 集更新了」；
      同一部番每集内容不同 → 缓存键含 ep，每集会重画（不影响其它缓存）。

    没有封面（`rule.image` 为空）直接返回 None：卡片的主体就是那张海报，没图不值得拼。
    """
    url = str(rule.get("image") or "").strip()
    if not url:
        return None
    title, native, when = fields_of(rule)
    level = max(0, min(int(level or 0), cover.MAX_LEVEL))
    dst = CARD_DIR / f"{_key(url, title, native, when, level, ep)}.jpg"
    if dst.exists():
        return dst                                  # 内容键相同 → 直接用缓存

    src = await (cover.ensure_src(url) if level == 0 else cover.sanitize(url, level))
    if src is None:
        return None
    raw = await asyncio.to_thread(_render, src, title, native, when, ep)
    if not raw:
        return None
    try:
        CARD_DIR.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, dst)
    except OSError as exc:
        logger.warning(f"番剧卡片：落盘失败：{exc}")
        return None
    _sweep()
    return dst
