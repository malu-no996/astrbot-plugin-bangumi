"""群内命令：「订阅番剧」/「取消订阅番剧」。

一条命令干两件事（就是用户要的「默认本季全部番剧」）：

1. **登记群订阅**（group_subs.py）—— 这个群订阅了番剧推送，记下群 + 订阅者；
2. **把本季正在播的全部番剧订到本群**（bulk.subscribe_season）——
   各番按它自己的「周几 + 播出时刻」到点推，不是统一一个时间。

之后**单加 / 单删某部番**不用命令，去面板「订阅数据」页改（用户明确这么要求）。

权限
----
- 默认：**群主 + 群管理员**，外加 AstrBot 管理员（机器人主人）；
- 面板「订阅数据」页可切成**仅群主**（`settings.owner_only`）。

★ AstrBot 管理员在任何档位下都放行，是刻意的后门：群主退群 / 角色拿不到时
  还得有人能把群订阅改回来，否则只能去翻 data/subscriptions.json。

角色从哪来
----------
**消息原始 payload 里就带着**，不用额外调接口，也就没有「平台不支持」这回事：
- QQ 官方（botpy）：`message.raw_data["author"]["member_role"]` —— owner/admin/member。
  botpy 1.2.1 自己没解析 member_role，是 AstrBot 给适配器打了补丁把整份原始
  payload 存进 raw_data（见 astrbot-plugin-miyoho 的 subscribe.py，同一套挖法）。
- OneBot v11：`raw_message["sender"]["role"]`，同样是 owner/admin/member。

读不到才退回 OneBot 的 `get_group_member_info` 现查（双保险）。

唤醒前缀的坑
------------
AstrBot 命中 wake_prefix 时会**把前缀从 `event.message_str` 里剥掉**
（`core/pipeline/waking_check/stage.py`），所以「/订阅番剧」「绝区零订阅番剧」
到 message_str 时都已经是「订阅番剧」了 —— 但反过来说，前缀一旦恰好是命令的
前缀就会失配。这里两个候选都试：message_str（剥过的）+ 消息链 Plain 段拼的
原文（没剥过的），哪个匹配算哪个。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from . import bulk, group_subs, store

# 命令触发词（固定，不提供页面配置 —— 用户没要求，少一层状态）
SUBSCRIBE_TRIGGERS = ("订阅番剧", "订阅番剧推送", "番剧订阅")
UNSUBSCRIBE_TRIGGERS = ("取消订阅番剧", "退订番剧", "取消番剧订阅", "取消订阅番剧推送")

NO_GROUP_HINT = "「订阅番剧」要在群里发（私聊没法订阅群）"


# ---------------- 取文本 ----------------


def _strip_head(text: str) -> str:
    """剥掉开头的 `/`（循环剥，兼容 `//订阅番剧`）与 `@某人 `。

    `/` 是本机 wake_prefix 之一，AstrBot 一般已经剥过；但用户也可能把 `/` 当
    「命令符」写在**命令后面**的场合（`@机器人 /订阅番剧`）→ 再兜一层。
    """
    t = str(text or "").strip()
    while True:
        if t[:1] == "/":
            t = t[1:].lstrip()
            continue
        if t[:1] == "@":
            head, _, tail = t.partition(" ")
            if tail:                     # 只有「@xxx 后面还有内容」才当提及剥掉
                t = tail.lstrip()
                continue
        break
    return t.strip()


def text_candidates(event) -> list[str]:
    """命令匹配用的候选文本（**去重、保序**）。

    1. `event.message_str`：AstrBot 剥过 wake_prefix 的版本；
    2. 消息链里的 Plain 段拼起来：**没剥过**的原文（拿不到 At 段文本，正好）。
    两个都试，是为了同时兼容「带唤醒前缀发」和「唤醒前缀恰好是命令前缀」。
    """
    out: list[str] = []
    seen: set[str] = set()
    raw_msg = ""
    try:
        raw_msg = str(getattr(event, "message_str", "") or "")
    except Exception:  # noqa: BLE001
        raw_msg = ""
    plain = ""
    try:
        parts: list[str] = []
        for seg in event.get_messages() or []:
            name = type(seg).__name__
            if name == "Plain":
                parts.append(str(getattr(seg, "text", "") or ""))
            elif isinstance(seg, str):
                parts.append(seg)
        plain = "".join(parts)
    except Exception:  # noqa: BLE001
        plain = ""
    for cand in (raw_msg, plain):
        t = _strip_head(cand)
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def match(event) -> str | None:
    """命中返回 `"sub"` / `"unsub"`，否则 None（**精确匹配**，避免误吞别人的消息）。"""
    for text in text_candidates(event):
        if text in UNSUBSCRIBE_TRIGGERS:
            return "unsub"
        if text in SUBSCRIBE_TRIGGERS:
            return "sub"
    return None


# ---------------- 群内角色 ----------------


def _pick_role(obj: Any) -> str:
    if obj is None:
        return ""
    if isinstance(obj, dict):
        for key in ("member_role", "role"):
            value = obj.get(key)
            if value:
                return str(value)
        return ""
    for key in ("member_role", "role"):
        value = getattr(obj, key, None)
        if value:
            return str(value)
    return ""


def _dig_role(obj: Any) -> str:
    """在原始消息里挖角色：先看 author / sender，再看自己（兼容各适配器形态）。"""
    if isinstance(obj, dict):
        for key in ("author", "sender"):
            role = _pick_role(obj.get(key))
            if role:
                return role
        return _pick_role(obj)
    for attr in ("author", "sender"):
        role = _pick_role(getattr(obj, attr, None))
        if role:
            return role
    return _pick_role(obj)


def raw_role(event) -> str:
    """消息自带的群内角色（owner / admin / member）；读不到返回空串。"""
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if raw is None:
        return ""
    return _dig_role(getattr(raw, "raw_data", None)) or _dig_role(raw)


async def allowed(event, owner_only: bool) -> bool:
    """权限判定：默认群主 + 群管理员，`owner_only=True` 时仅群主。

    AstrBot 管理员（机器人主人）在两种档位下都放行 —— 见模块开头的说明。
    """
    try:
        if event.is_admin():
            return True
    except Exception:  # noqa: BLE001
        pass
    role = raw_role(event)
    if role:
        return role == "owner" if owner_only else role in ("owner", "admin")
    # 角色读不到（少见）→ 退回 OneBot 现查
    gid = str(getattr(event, "get_group_id", lambda: "")() or "")
    uid = str(getattr(event, "get_sender_id", lambda: "")() or "")
    bot = getattr(event, "bot", None)     # 只有 AiocqhttpMessageEvent 有 .bot
    if bot is None or not gid.isdigit() or not uid.isdigit():
        return False
    try:
        info = await bot.call_action(
            "get_group_member_info", group_id=int(gid), user_id=int(uid), no_cache=True,
        )
        role = str((info or {}).get("role") or "")
        return role == "owner" if owner_only else role in ("owner", "admin")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"番剧：查询群成员角色失败（群 {gid} / {uid}）：{exc}")
        return False


# ---------------- 取群名 / 订阅者 ----------------


async def _group_name(event, group_id: str) -> str:
    """群名：事件自带的 → OneBot 现查 → 空（面板上能补填）。"""
    group = getattr(getattr(event, "message_obj", None), "group", None)
    name = str(getattr(group, "group_name", "") or "").strip() if group is not None else ""
    if name:
        return name
    bot = getattr(event, "bot", None)
    if bot is None or not str(group_id).isdigit():
        return ""
    try:
        info = await bot.call_action("get_group_info", group_id=int(group_id))
        return str((info or {}).get("group_name") or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"番剧：获取群名失败（群 {group_id}）：{exc}")
        return ""


def _sender_of(event) -> tuple[str, str]:
    """(订阅者 ID, 订阅者名)。名字取不到就算了，不影响订阅。"""
    sid = ""
    try:
        sid = str(event.get_sender_id() or "")
    except Exception:  # noqa: BLE001
        sid = ""
    name = ""
    try:
        name = str(event.get_sender_name() or "").strip()
    except Exception:  # noqa: BLE001
        name = ""
    return sid, name


# ---------------- 命令实现 ----------------


async def _do_subscribe(event) -> str:
    gid = str(getattr(event, "get_group_id", lambda: "")() or "")
    pid = str(getattr(event, "get_platform_id", lambda: "")() or "")
    if not gid:
        return NO_GROUP_HINT
    if not pid:
        return "订阅失败：拿不到机器人实例信息，稍后再试"

    settings = store.load_settings()
    owner_only = bool(settings.get("owner_only"))
    if not await allowed(event, owner_only):
        return ("无权限：「订阅番剧」已设置为**仅群主**可用"
                if owner_only else
                "无权限：「订阅番剧」仅限群主、群管理员或 AstrBot 管理员使用")

    group_name = await _group_name(event, gid)
    subscriber_id, subscriber_name = _sender_of(event)
    platform_type = ""
    try:
        platform_type = str(event.get_platform_name() or "")
    except Exception:  # noqa: BLE001
        platform_type = ""

    # 1) 先登记群订阅（**先登记再订番剧**：即使放送表拉不动，群订阅也算订上了，
    #    用户到面板点一次「刷新放送表」再点「一键订阅本季全部」就能补上）
    sub = group_subs.upsert(
        pid, gid,
        group_name=group_name, platform_type=platform_type,
        subscriber_id=subscriber_id, subscriber_name=subscriber_name,
    )
    label = sub.get("group_name") or gid

    # 2) 本季正在播的全部番剧 → 各建一条规则（各按各自的播出时刻推）
    res = await bulk.subscribe_season(
        pid, gid,
        platform_name=pid, target_name=label,
        allow_fetch=True,
    )
    if not res.get("ok"):
        logger.info(f"番剧命令：群 {gid} 登记成功但批量订阅未完成：{res.get('message')}")
        return (f"✅ 已登记本群订阅：{label}\n"
                f"⚠️ 但这次没能订上番剧：{res.get('message')}\n"
                f"补上之后回到本群再发一次「订阅番剧」即可。")

    logger.info(f"番剧命令：群 {gid}（{label}）订阅成功，新增 {res.get('added')} 部")
    lines = [f"✅ 已为本群订阅番剧推送：{label}",
             f"· 本次订上 {res.get('added')} 部番剧"
             + (f"（跳过 {res.get('skipped')} 部已订过的）" if res.get("skipped") else "")]
    if not res.get("added"):
        lines.append("· 本季番剧之前都已经订过了，不用重复订")
    lines.append("· 到点会自动往本群发「《番剧名》第 N 集更新了」")
    lines.append("· 只想留几部 / 停用本群：面板「番剧推送 → 订阅数据」页里改")
    if not settings.get("enabled", True):
        # 模块总开关关着时不做静默拦截，但**必须**说清楚，否则用户会以为命令没生效
        lines.append("⚠️ 注意：插件总开关当前是关闭的，订阅已记下但还不会推送"
                     "（到面板把右上角的开关打开即可）")
    return "\n".join(lines)


async def _do_unsubscribe(event) -> str:
    gid = str(getattr(event, "get_group_id", lambda: "")() or "")
    pid = str(getattr(event, "get_platform_id", lambda: "")() or "")
    if not gid:
        return NO_GROUP_HINT
    if not pid:
        return "取消失败：拿不到机器人实例信息，稍后再试"

    settings = store.load_settings()
    owner_only = bool(settings.get("owner_only"))
    if not await allowed(event, owner_only):
        return "无权限：取消订阅仅限群主、群管理员或 AstrBot 管理员使用"

    removed = store.delete_rules([
        str(r.get("id")) for r in store.load_rules()
        if str(r.get("platform_id") or "") == pid and str(r.get("target_id") or "") == gid
    ])
    group_subs.delete_sub(pid, gid)
    logger.info(f"番剧命令：群 {gid} 取消订阅，删除 {removed} 条番剧规则")
    if not removed:
        return "已取消本群的番剧推送订阅（原本也没有订上番剧）"
    return (f"✅ 已取消本群的番剧推送：删除 {removed} 条番剧订阅\n"
            f"想重新订阅，在本群再发一次「订阅番剧」就行。")


async def handle(event) -> str | None:
    """处理一条群消息。

    - 返回 `None` = **不是本插件的命令** → 调用方放行（别抢别人的消息 / 别拦聊天）；
    - 返回文本 = 该回的话（命中命令就必须回，哪怕只是「无权限」）。
    """
    action = match(event)
    if action is None:
        return None
    try:
        if action == "sub":
            return await _do_subscribe(event)
        return await _do_unsubscribe(event)
    except Exception as exc:  # noqa: BLE001 —— 分发器自己的异常绝不能打断事件流
        logger.exception("番剧命令：处理异常")
        return f"命令执行出错：{type(exc).__name__}: {exc}"
