"""推送目标：AstrBot 平台实例列表（可选带 OneBot 的群列表）。

推送目标 = 平台实例 ID + 群 ID。AstrBot 里就是一个统一会话 origin：
`{platform_id}:GroupMessage:{group_id}`。

- **OneBot（aiocqhttp）**：可以现拉群列表（`get_group_list`），也支持按 QQ 号私聊。
- **QQ 官方机器人**：**没有「所在群列表」接口**，群 ID 只能手填（群消息事件里的
  session_id）；**不支持私聊**（拿不到用户 openid）。
- 其他平台：群 ID 同样手填。

每次现查（就是一次内存列表遍历），不做缓存 —— 平台可能随时增删。
"""
from __future__ import annotations

from loguru import logger


def _inst_id(p) -> str:
    try:
        return str(p.meta().id or "") or str((getattr(p, "config", None) or {}).get("id") or "")
    except Exception:
        return str((getattr(p, "config", None) or {}).get("id") or "")


def _inst_type(p) -> str:
    try:
        return str(p.meta().name or "").strip().lower()
    except Exception:
        return ""


async def _onebot_groups(inst) -> list[dict]:
    """OneBot 实例现拉群列表（拿不到就空列表，不影响其它实例）。"""
    groups: list[dict] = []
    client = None
    try:
        getter = getattr(inst, "get_client", None)
        client = getter() if callable(getter) else getattr(inst, "bot", None)
    except Exception:
        client = None
    if client is None:
        return groups
    try:
        glist = await client.call_action("get_group_list")
    except Exception as exc:
        logger.debug(f"番剧：拉取群列表失败：{exc}")
        return groups
    for g in glist or []:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("group_id") or "").strip()
        if gid:
            groups.append({
                "group_id": gid,
                "group_name": str(g.get("group_name") or "").strip(),
            })
    return groups


async def instances(context) -> list[dict]:
    """列出可用于推送的平台实例。

    返回项：`{id, name, type, private, groups:[{group_id, group_name}]}`
    - `private` = 该平台能否按用户 ID 发私聊（OneBot 可以，QQ 官方不行）。
    """
    out: list[dict] = []
    try:
        insts = context.platform_manager.get_insts()
    except Exception:
        insts = []
    for p in insts or []:
        pid = _inst_id(p)
        if not pid:
            continue
        ptype = _inst_type(p)
        official = ptype.startswith("qq_official")
        onebot = ptype in ("aiocqhttp", "onebot", "onebot_v11")
        groups = await _onebot_groups(p) if onebot else []
        out.append({
            "id": pid,
            "name": pid,
            "type": ptype,
            "protocol": "qq_official" if official else ("onebot" if onebot else ptype or "other"),
            "private": bool(onebot),
            "groups": groups,
        })
    return out
