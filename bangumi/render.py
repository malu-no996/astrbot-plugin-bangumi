"""正文渲染：默认排版 / 自定义模板 / 封面占位行 / 官方 markdown 转换。

正文里 `IMAGE_MARK` 是「封面图该插在哪」的占位行：默认排版把它放在模板那几行之后 ——
即用户指定的顺序：

    第 N 集更新了
    封面图

「原名 / 放送」不写进正文（避免和卡片右侧重复），它们由卡片在图里展示。
发送端把占位行换成真图（OneBot 的 Image 组件 / 官方富媒体的本地卡片），没有封面就删掉。
用 NUL 包起来，用户在模板里打不出这个串。
"""
from __future__ import annotations

from loguru import logger

from . import store

DEFAULT_TEMPLATE = "【新番更新】{title}\n第 {ep} 集更新了"
TEMPLATE_PLACEHOLDERS = ("{title}", "{native}", "{ep}", "{total}", "{weekday}", "{time}", "{date}", "{url}")

IMAGE_MARK = "\x00image\x00"


def render_message(rule: dict, ep: int, day) -> str:
    """渲染一条推送正文；`rule.template` 非空时用它。

    默认排版里插一行 `IMAGE_MARK` 占位，封面图的位置就在那里（模板那几行之后、
    「原名 / 放送」之前）—— 真正的图片由发送端 `fill_image` 填进去。
    """
    title = str(rule.get("title") or rule.get("native") or "（未命名）").strip()
    native = str(rule.get("native") or "").strip()
    total = int(rule.get("total_episodes") or 0)
    ctx = {
        "title": title,
        "native": native,
        "ep": int(ep),
        "total": total,
        "weekday": store.weekday_label(rule.get("weekday")),
        "time": str(rule.get("time") or "00:00"),
        "date": day.isoformat(),
        "url": str(rule.get("url") or ""),
    }
    template = str(rule.get("template") or "").strip()
    if template:
        try:
            return template.format(**ctx)
        except (KeyError, IndexError, ValueError) as exc:
            logger.warning(f"番剧：自定义模板渲染失败（{exc}），回落默认排版")
    lines = DEFAULT_TEMPLATE.format(**ctx).split("\n")
    lines.append(IMAGE_MARK)          # 封面图就位（发送端替换；没封面就把这行删掉）
    # 原名 / 放送不再写进正文 —— 它们已经画在卡片右侧，正文只留「作品名 + 第 N 集更新了」
    return "\n".join(lines)


def fill_image(text: str, img_line: str, *, pad: bool = False) -> str:
    """把正文里的 `IMAGE_MARK` 占位行换成 `img_line`。

    - 有占位行：`img_line` 为空（没封面）→ 整行删掉；否则替换。`pad=True` 时图片行
      前后各留一个空行 —— 官方 markdown 要独立成块才稳定渲染。
    - 没有占位行（自定义模板）：`img_line` 非空就**追加到最后**（与旧行为一致）。
    """
    lines = str(text or "").split("\n")
    marked = any(ln.strip() == IMAGE_MARK for ln in lines)
    out: list[str] = []
    for ln in lines:
        if ln.strip() == IMAGE_MARK:
            if img_line:
                if pad and out and out[-1] != "":
                    out.append("")
                out.append(img_line)
                out.append("")
            continue
        out.append(ln)
    if img_line and not marked:
        out.append(img_line)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def page_text(text: str, rule: dict) -> str:
    """给页面看的正文：把 `IMAGE_MARK` 占位行换成一行提示（没封面就删掉），
    免得页面上露出一串控制字符。"""
    has_img = bool(str(rule.get("image") or "").strip())
    out: list[str] = []
    for ln in str(text or "").split("\n"):
        if ln.strip() == IMAGE_MARK:
            if has_img:
                out.append("（封面图）")
            continue
        out.append(ln)
    return "\n".join(out)


def to_markdown(text: str) -> str:
    """纯文本 → 官方 markdown：第一行当标题（`###`，官方会渲染得比较醒目），其余照抄。

    不做复杂转换：官方 markdown 语法很窄，标题 + 换行的效果已经足够好看。
    封面图不在这里拼 —— 进这里之前已经由 `fill_image` 按正文里的占位行把
    `mdgen.md_image_tag` 的结果放好了。
    """
    lines = str(text or "").splitlines()
    if not lines:
        return ""
    head = lines[0].strip()
    rest = lines[1:]
    return "\n\n".join([f"### {head}"] + ["\n".join(rest)] if rest else [f"### {head}"])
