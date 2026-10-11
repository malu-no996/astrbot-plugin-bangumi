"""发送通道：把推送内容经 AstrBot `context.send_message()` 发到指定会话。

统一会话 origin：`{platform_id}:{GroupMessage|FriendMessage}:{group_id|user_id}`
（platform_id 是 WebUI「消息平台」里的实例 ID；group_id 是该平台群消息事件里的
session_id，如 aiocqhttp 的 QQ 群号）。

**按平台类型分发**（对齐原插件「OneBot / QQ 官方」双通道的行为）：
- `qq_official`（QQ 官方机器人）→ 发 **Markdown**（msg_type=2）：封面图以公网外链
  直接写进 markdown（官方会转存），**不发 Image 组件**（带 Image 会被适配器拆成
  msg_type=7 富媒体、markdown 被丢掉）。封面被判「内容违规」时逐级马赛克，改用
  富媒体（Plain 正文 + 本地马赛克图，仍是「正文 + 图片一条消息」）重发。
- 其他平台（aiocqhttp 等）→ 发「文字 + 图片」普通消息链，显式 `use_markdown_=False`。

main.py 在插件初始化时调 bind(context)；push 只 import 本模块。
"""
from __future__ import annotations

from loguru import logger

from . import cover, mdgen, render

_context = None


def bind(context) -> None:
    """插件启动时绑定 AstrBot Context。"""
    global _context
    _context = context


def session_of(platform_id: str, target_type: str, target_id: str) -> str:
    """订阅里存的 (平台实例 ID, 群/私聊, 目标 ID) → 统一会话 origin。"""
    kind = "FriendMessage" if str(target_type or "") == "private" else "GroupMessage"
    return f"{str(platform_id or '').strip()}:{kind}:{str(target_id or '').strip()}"


def _inst_id(p) -> str:
    """平台实例的唯一 ID。⚠️ Platform 实例没有 .id 属性（getattr 恒为空），
    真实 ID 在 meta().id（来自配置项 config["id"]）—— context.send_message
    就是用 meta().id 来匹配 origin 里的平台段的，这里必须保持一致。"""
    try:
        return str(p.meta().id or "") or str((getattr(p, "config", None) or {}).get("id") or "")
    except Exception:
        return str((getattr(p, "config", None) or {}).get("id") or "")


def platform_type(platform_id: str) -> str:
    """按实例 ID 查平台适配器类型（meta().name，如 qq_official / aiocqhttp）。

    每次现查（就是一次内存列表遍历），不做缓存 —— 平台可能随时增删。
    查不到返回空串。
    """
    if _context is None or not str(platform_id or "").strip():
        return ""
    try:
        for p in _context.platform_manager.get_insts():
            if _inst_id(p) == str(platform_id):
                try:
                    return str(p.meta().name or "").strip().lower()
                except Exception:
                    return ""
    except Exception:
        pass
    return ""


def _chain(comps, markdown: bool):
    from astrbot.api.event import MessageChain

    chain = MessageChain(chain=comps)
    chain.use_markdown_ = bool(markdown)
    return chain


async def _post(session: str, comps, markdown: bool) -> None:
    """发一条消息；找不到平台 / 发送失败抛异常，由调用方记入结果。"""
    if _context is None:
        raise RuntimeError("插件尚未初始化完成，无法发送消息")
    ok = await _context.send_message(session, _chain(comps, markdown))
    # AstrBot 的 send_message 返回 bool（False = 未找到平台或发送失败）
    if ok is False:
        raise RuntimeError(
            f"消息发送失败（session={session}）：请确认平台实例 ID 正确、平台已连接，"
            "且目标 ID 是该平台消息事件里的 session_id"
        )


async def send(rule: dict, text: str) -> None:
    """按订阅配置把一条推送发出去。失败抛异常（调用方退避重试）。"""
    from astrbot.api.message_components import Image, Plain

    platform_id = str(rule.get("platform_id") or "").strip()
    target_id = str(rule.get("target_id") or "").strip()
    target_type = str(rule.get("target_type") or "group")
    if not platform_id:
        raise RuntimeError("没有选择推送用的平台实例")
    if not target_id:
        raise RuntimeError("推送目标为空：请填写群 ID / 私聊用户 ID")
    session = session_of(platform_id, target_type, target_id)

    img = str(rule.get("image") or "").strip()
    ptype = platform_type(platform_id)
    if ptype.startswith("qq_official"):
        await _send_official(session, text, img)
        return

    # 其他平台：文字 + 可选的封面图（本地马赛克版优先）
    body = render.fill_image(text, "")
    comps = [Plain(body)]
    if img:
        local = cover.local_path(img)
        if local is not None:
            try:
                comps.append(Image.fromFileSystem(str(local)))
            except Exception as exc:   # 本地文件不可用就退回远程 URL
                logger.warning(f"番剧推送：马赛克图读取失败，改用远程封面：{exc}")
                comps.append(Image.fromURL(img))
        else:
            comps.append(Image.fromURL(img))
    await _post(session, comps, markdown=False)


async def _send_official(session: str, text: str, img: str) -> None:
    """QQ 官方机器人：**一条**消息里同时有文字和封面图。

    两条路，都只发一条：
    A. 常规 —— 封面写进 markdown（`![#宽px #高px](公网URL)`）。
    B. 降级 —— 封面被判「内容违规」时改用**本地马赛克图**：`Plain(正文)` + 本地图，
       AstrBot 官方适配器内部转成 `msg_type=7`（正文 + 富媒体一条消息），
       不需要公网、不需要图床。级别逐级加强、落盘缓存，下次直接用缓存版。

    ★ 拼图失败不抛：没图 / URL 空就照发纯文字。图绝不能把整条推送算失败，
      否则会触发退避重试、把文字重复发一遍。
    """
    from astrbot.api.message_components import Image, Plain

    body = render.fill_image(text, "")              # 不带图的正文（降级那条消息用它）
    level = cover.level_of(img) if img else 0        # >0 = 这张封面已经打过马赛克
    if level <= 0:
        tag = await mdgen.md_image_tag(img) if (img and mdgen.MD_EMBED_COVER) else ""
        content = render.to_markdown(render.fill_image(text, tag, pad=True))
        try:
            await _post(session, [Plain(content)], markdown=True)
            return
        except Exception as exc:
            if not img:
                logger.warning(f"番剧推送：官方 markdown 发送失败，退回纯文本：{exc}")
                await _post(session, [Plain(body)], markdown=False)
                return
            logger.warning(f"番剧推送：封面可能被判内容违规（{exc}），改用本地马赛克图重发")

    # 降级：本地马赛克图 + 正文（一条富媒体）。缓存命中先按原级别试，再逐级加强。
    ladder = ([level] if level > 0 else []) + [
        lv for lv in range(1, cover.MAX_LEVEL + 1) if lv > level
    ]
    for lv in ladder:
        path = await cover.sanitize(img, lv)
        if path is None:
            logger.warning(f"番剧推送：第 {lv} 级马赛克图准备失败，跳过这一级")
            continue
        try:
            await _post(session, [Plain(body), Image.fromFileSystem(str(path))], markdown=False)
            logger.info(f"番剧推送：用 {lv} 级马赛克封面发送成功（已缓存，下次直接用）")
            return
        except Exception as exc:
            logger.warning(f"番剧推送：{lv} 级马赛克发送失败（继续升级）：{exc}")
    logger.warning("番剧推送：马赛克加到顶仍失败 —— 本次只发文字")
    await _post(session, [Plain(body)], markdown=False)
