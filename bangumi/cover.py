"""番剧封面：本地缓存 + 马赛克降级 + 尺寸探测。

为什么要有它
------------
QQ 官方机器人的 markdown 图片**必须是公网可访问的 URL**（官方会下载转存）。所以一旦
某张封面被判「消息内容违规」，就**没法**把处理过的图塞回同一条 markdown。此时改用
**富媒体通道**（`Plain(正文) + Image.fromFileSystem(本地马赛克图)`，适配器内部走
`msg_type=7`）—— 同样是「正文 + 图片一条消息」，且不需要公网、不需要图床。

马赛克怎么打
------------
代码不可能知道画面上哪一块「敏感」，所以**不猜** —— 整图做块状像素化，从最轻的一级
开始，被审核拒一次就加一级，**用审核结果当反馈信号**：

    1 级  块 = 图宽/40   几乎看不出被处理，只把纹理细节打碎
    2 级  块 = 图宽/24
    3 级  块 = 图宽/16
    4 级  块 = 图宽/10 + 去色（灰度）

被拒的消息不会出现在群里（只有过审那条会显示），所以逐级重试不会刷屏。

缓存布局（`data/covers/`，在 data/ 下，天然不进 git）
-----------------------------------------------------
    <md5>.src          原图（只下一次；升级马赛克时不必重新下载）
    <md5>.m<N>.jpg     第 N 级马赛克版（当前生效的那个，升级时替换并删掉旧的）
    index.json         { md5: {"url": 原图地址, "level": N, "at": 时间戳} }

`level` 缺失或为 0 = 没打过马赛克。发送端每次都先查它：命中就直接用本地马赛克版，
不再让原图去撞审核。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

import httpx
from loguru import logger

from . import api, paths

CACHE_DIR = paths.DATA_DIR / "covers"
INDEX_FILE = CACHE_DIR / "index.json"

# 每一级的马赛克：`blocks` = 图宽切成几块（**块数越小越糊**）；`gray=True` 时额外去色。
MOSAIC_LEVELS: tuple[dict, ...] = (
    {"blocks": 40},
    {"blocks": 24},
    {"blocks": 16},
    {"blocks": 10, "gray": True},
)
MAX_LEVEL = len(MOSAIC_LEVELS)

MAX_SIDE = 720                  # 发出去之前最长边缩到这个尺寸（推送用的小图，别把原图丢出去）
JPEG_QUALITY = 88
MAX_BYTES = 8 * 1024 * 1024     # 下载上限，超了就别往内存里塞
_PROBE_BYTES = 64 * 1024        # 探尺寸只取前 64KB（PNG IHDR / JPEG SOF 都很靠前）


# ---------------- 路径与索引 ----------------


def _md5(url: str) -> str:
    return hashlib.md5(str(url).strip().encode("utf-8")).hexdigest()


def _src_file(name: str) -> Path:
    return CACHE_DIR / f"{name}.src"


def _level_file(name: str, level: int) -> Path:
    return CACHE_DIR / f"{name}.m{int(level)}.jpg"


def _read_index() -> dict:
    try:
        data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:              # 不存在 / 坏了都当空索引，别影响推送
        return {}


def _write_index(data: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = INDEX_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, INDEX_FILE)
    except OSError as exc:
        logger.warning(f"番剧封面：缓存索引写盘失败：{exc}")


def level_of(url: str) -> int:
    """这张封面已经打到第几级马赛克（0 = 没打过）。"""
    url = str(url or "").strip()
    if not url:
        return 0
    entry = _read_index().get(_md5(url)) or {}
    try:
        return max(0, min(int(entry.get("level") or 0), MAX_LEVEL))
    except (TypeError, ValueError):
        return 0


def local_path(url: str) -> Path | None:
    """这张封面**当前生效的马赛克版**的本地文件；没打过马赛克返回 None。"""
    url = str(url or "").strip()
    if not url:
        return None
    level = level_of(url)
    if level <= 0:
        return None
    path = _level_file(_md5(url), level)
    return path if path.exists() else None


# ---------------- 下载与马赛克 ----------------


async def _download(url: str) -> bytes | None:
    try:
        kwargs = api._client_kwargs()
        kwargs["timeout"] = httpx.Timeout(20, connect=8)
        async with httpx.AsyncClient(**kwargs) as client:
            resp = await client.get(url)
    except Exception as exc:       # noqa: BLE001 —— 下不动就返回 None，由调用方决定怎么办
        logger.warning(f"番剧封面：下载失败 {url[:100]}：{type(exc).__name__}: {exc}")
        return None
    if resp.status_code != 200 or not resp.content:
        logger.warning(f"番剧封面：下载失败 {url[:100]}：HTTP {resp.status_code}")
        return None
    if len(resp.content) > MAX_BYTES:
        logger.warning(f"番剧封面：图片过大（{len(resp.content) // 1024} KB），跳过缓存")
        return None
    return resp.content


def _mosaic(data: bytes, level: int) -> bytes | None:
    """整图块状像素化（级别越高越糊，最后一级额外去色）。

    做法：先缩到发送尺寸（省流量），再把图缩到「宽/blocks」大小（BOX 面积平均，
    每个像素就是一块的平均色），最后 NEAREST 放大回去 —— 得到干净的块状马赛克。
    Pillow 是同步的，调用方用 `asyncio.to_thread` 跑，别卡住事件循环。
    """
    spec = MOSAIC_LEVELS[max(1, min(int(level), MAX_LEVEL)) - 1]
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(data)) as im:
            im = im.convert("RGB")
            if max(im.size) > MAX_SIDE:
                ratio = MAX_SIDE / max(im.size)
                im = im.resize(
                    (max(1, round(im.width * ratio)), max(1, round(im.height * ratio))),
                    Image.LANCZOS,
                )
            if spec.get("gray"):
                im = im.convert("L").convert("RGB")
            width, height = im.size
            block = max(2, round(width / int(spec["blocks"])))
            small = im.resize(
                (max(1, width // block), max(1, height // block)), Image.BOX
            )
            out = small.resize((width, height), Image.NEAREST)
            buf = BytesIO()
            out.save(buf, format="JPEG", quality=JPEG_QUALITY)
            return buf.getvalue()
    except Exception as exc:       # noqa: BLE001
        logger.warning(f"番剧封面：打马赛克失败（{level} 级）：{type(exc).__name__}: {exc}")
        return None


# ---------------- 对外：把某张封面缓存成「第 N 级马赛克版」 ----------------


async def ensure_src(url: str) -> Path | None:
    """确保封面**原图**已落到本地缓存（`<md5>.src`），返回它的路径（失败 None）。

    与 `sanitize` 的区别是**不打马赛克** —— 横屏卡片要在原图上裁切排版
    （见 card.py），只有被官方审核拦下时才需要马赛克版。原图只下一次。
    """
    url = str(url or "").strip()
    if not url:
        return None
    src = _src_file(_md5(url))
    if src.exists():
        return src
    data = await _download(url)
    if not data:
        return None
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = src.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, src)
    except OSError as exc:
        logger.warning(f"番剧封面：原图落盘失败：{exc}")
        return None
    return src


async def sanitize(url: str, level: int) -> Path | None:
    """确保「这张封面 + 这一级马赛克」的本地文件存在，返回它的路径（失败 None）。

    原图只下一次（`<md5>.src`），升级时从原图重新生成，不必再联网。
    生成好之后更新索引、删掉旧的级别文件 —— 也就是把本地缓存的图**覆盖**成新版。
    """
    url = str(url or "").strip()
    if not url:
        return None
    level = max(1, min(int(level), MAX_LEVEL))
    name = _md5(url)
    dst = _level_file(name, level)
    if dst.exists() and level_of(url) == level:
        return dst                                   # 已经就是这个级别，直接用

    src = await ensure_src(url)
    if src is None:
        return None

    try:
        raw = src.read_bytes()
    except OSError as exc:
        logger.warning(f"番剧封面：读缓存原图失败：{exc}")
        return None

    out = await asyncio.to_thread(_mosaic, raw, level)
    if not out:
        return None

    try:
        tmp = dst.with_suffix(".tmp")
        tmp.write_bytes(out)
        os.replace(tmp, dst)
    except OSError as exc:
        logger.warning(f"番剧封面：马赛克图落盘失败：{exc}")
        return None

    for other in CACHE_DIR.glob(f"{name}.m*.jpg"):   # 只留当前这一级
        if other != dst:
            try:
                other.unlink()
            except OSError:
                pass

    index = _read_index()
    index[name] = {"url": url, "level": level, "at": int(time.time())}
    _write_index(index)
    logger.info(f"番剧封面：已缓存 {level} 级马赛克图（{dst.name}）")
    return dst


# ---------------- 尺寸探测（给官方 markdown 图片行用） ----------------


def fit_size(width: int, height: int, max_w: int, fallback: tuple[int, int]) -> tuple[int, int]:
    """真实尺寸 → markdown 展示尺寸：按比例缩到 `max_w` 宽（只缩不放、不变形）。"""
    width, height = int(width or 0), int(height or 0)
    if width <= 0 or height <= 0:
        return fallback
    if width <= max_w:
        return width, height
    return max_w, max(1, round(height * max_w / width))


async def probe_size(url: str) -> tuple[int, int]:
    """探封面真实宽高，失败返回 `(0, 0)`。

    只发 `Range: bytes=0-65535` 取前 64KB —— PNG 的 IHDR、JPEG 的 SOF 都很靠前，
    够解出尺寸，不必把整张图拉下来（大图能省几百 KB）。解析用 Pillow。
    """
    url = str(url or "").strip()
    if not url:
        return 0, 0
    try:
        kwargs = api._client_kwargs()
        kwargs["timeout"] = httpx.Timeout(10, connect=6)
        async with httpx.AsyncClient(**kwargs) as client:
            resp = await client.get(url, headers={"Range": f"bytes=0-{_PROBE_BYTES - 1}"})
    except Exception as exc:  # noqa: BLE001 —— 探不到尺寸不该影响推送
        logger.debug(f"番剧封面：尺寸探测失败 {url[:100]}：{type(exc).__name__}: {exc}")
        return 0, 0
    if resp.status_code not in (200, 206):
        logger.debug(f"番剧封面：尺寸探测失败 {url[:100]}：HTTP {resp.status_code}")
        return 0, 0
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(resp.content)) as im:
            return int(im.width), int(im.height)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"番剧封面：尺寸解析失败 {url[:100]}：{type(exc).__name__}: {exc}")
        return 0, 0
