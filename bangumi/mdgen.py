"""官方 markdown 的图片行生成。

★ 官方这条通道的图片**必须带尺寸提示** `![#宽px #高px](url)`：不带尺寸时官方会
  把图转存但**渲染不出来**（只剩一个空行）。官方自己的适配器也是「先探尺寸、
  再拼 markdown」，探测失败兜底 512×512。番剧封面是竖版海报，兜底按 2:3 给。

★ 官方 markdown 的图只能是 **https 公网 URL**（本地图塞不进去）—— 本机处理过的
  马赛克图走不了这条路，改走富媒体（见 sender.py）。
"""
from __future__ import annotations

from . import cover

# True （当前）= 封面写进 markdown（**一条**消息搞定，与 OneBot 观感一致）
MD_EMBED_COVER = True

MD_IMG_WIDTH = 320              # 展示宽度上限（只缩不放、不变形）
MD_IMG_FALLBACK = (320, 480)    # 探测失败时的兜底尺寸（竖版海报比例）


def md_image(url: str, width: int, height: int) -> str:
    """markdown 图片语法 —— **带尺寸**：`![#320px #480px](url)`。别省尺寸。"""
    url = str(url or "").strip()
    if not url:
        return ""
    if not url.startswith("https://"):           # 官方这条通道只认 https
        url = url.replace("http://", "https://", 1)
    return f"![#{int(width)}px #{int(height)}px]({url})"


async def md_image_tag(url: str) -> str:
    """封面图 → 可直接放进官方 markdown 的图片行（`![#宽px #高px](https://…)`）。

    没有图 / URL 空返回 `""`（调用方就当没有封面，照发文字）。
    """
    url = str(url or "").strip()
    if not url:
        return ""
    if not url.startswith("https://"):
        url = url.replace("http://", "https://", 1)
    width, height = cover.fit_size(*await cover.probe_size(url), MD_IMG_WIDTH, MD_IMG_FALLBACK)
    return md_image(url, width, height)
