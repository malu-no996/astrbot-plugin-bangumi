"""群订阅：群里发「订阅番剧」命令登记下来的「哪个群订阅了番剧推送」。

这份数据回答的是面板上那张「订阅数据」页的问题 ——
**哪些群订了、谁订的、还开着吗**。它是**群级**的，和 store 里的「规则」
（一条 = 一部番剧 + 一个推送目标）不是一个粒度：

    群订阅（本文件）  = 这个群要不要收番剧推送 + 谁订的        ← 一行一个群
    规则（store.py）  = 这个群要收哪几部番剧、各在周几几点推   ← 一行一个「群 × 番剧」

两者配合：命令「订阅番剧」= 登记一条群订阅 + 把本季全部番剧各建一条规则；
面板「订阅数据」页可以在群里既有记录的基础上，单加 / 单删某部番剧的规则。

落盘 `data/subscriptions.json`：

    {"subscriptions": [
      {"id": "sub-<platform_id>-<group_id>",
       "platform_id": "napcat", "platform_type": "aiocqhttp",
       "group_id": "123456", "group_name": "某某群",
       "subscriber_id": "10001", "subscriber_name": "张三",
       "enabled": true, "created_at": 0, "updated_at": 0}
    ]}

★ **闸门只对「有记录」的群生效**（`muted_pairs`）：
  没有记录的目标（例如直接在面板上配的规则、私聊）一律放行 ——
  否则升级之后，老用户面板上那些订阅会在没人察觉的情况下**静默全停**。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import paths

DATA_DIR = paths.DATA_DIR
SUBS_FILE = DATA_DIR / "subscriptions.json"

_lock = threading.RLock()


# ---------------- 通用 ----------------


def _atomic_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _text(value, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


def sub_id(platform_id: str, group_id: str) -> str:
    """记录的 id。★ 用「平台实例 + 群」拼出来（不用随机串）：

    同一个群在同一台机器人上只可能有一条群订阅，用确定性 id 就天然做到了
    「重复发命令 = 覆盖刷新」而不是「攒出两条记录」。
    """
    return f"sub-{_text(platform_id, 64)}-{_text(group_id, 64)}"


def norm_sub(raw: dict) -> dict:
    """把任意 dict 夹成一条合法记录（缺 id 用平台实例 + 群补出来）。"""
    src = raw if isinstance(raw, dict) else {}
    pid = _text(src.get("platform_id"), 64)
    gid = _text(src.get("group_id"), 64)
    out = {
        "id": _text(src.get("id"), 128) or sub_id(pid, gid),
        "platform_id": pid,
        "platform_type": _text(src.get("platform_type"), 40),
        "group_id": gid,
        "group_name": _text(src.get("group_name"), 120),
        "subscriber_id": _text(src.get("subscriber_id"), 64),
        "subscriber_name": _text(src.get("subscriber_name"), 120),
        "enabled": bool(src.get("enabled", True)),
        "created_at": int(src.get("created_at") or 0),
        "updated_at": int(src.get("updated_at") or 0),
    }
    if not out["created_at"]:
        out["created_at"] = int(time.time())
    return out


# ---------------- 读写 ----------------


def load_subs() -> list[dict]:
    """全部群订阅（**已归一化**）；坏文件当空。"""
    raw = _read_json(SUBS_FILE, {"subscriptions": []})
    items = raw.get("subscriptions") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for r in items:
        if not isinstance(r, dict):
            continue
        sub = norm_sub(r)
        if sub["group_id"]:          # 没有群 ID 的记录没法用（也没法归位到哪个群）
            out.append(sub)
    return out


def save_subs(subs: list[dict]) -> None:
    with _lock:
        _atomic_write(SUBS_FILE, {"subscriptions": [norm_sub(s) for s in subs if s.get("group_id")]})


def get_sub(platform_id: str, group_id: str) -> dict | None:
    pid, gid = _text(platform_id, 64), _text(group_id, 64)
    if not gid:
        return None
    for s in load_subs():
        if s["group_id"] == gid and (not pid or s["platform_id"] == pid):
            return s
    return None


def upsert(platform_id: str, group_id: str, *, group_name: str = "", platform_type: str = "",
           subscriber_id: str = "", subscriber_name: str = "") -> dict:
    """登记 / 刷新一条群订阅（重复订阅 = 刷新名字，并**顺手重新启用**）。

    ★ 重复发「订阅番剧」时把它重新置为启用是刻意的：群里再发一次命令
      是用户表达「我要收」的最直接方式，不该因为面板上被停用过就默默无效。
    """
    pid, gid = _text(platform_id, 64), _text(group_id, 64)
    if not gid:
        raise ValueError("group_id 不能为空")
    with _lock:
        subs = load_subs()
        now = int(time.time())
        for s in subs:
            if s["group_id"] == gid and s["platform_id"] == pid:
                s["enabled"] = True
                s["updated_at"] = now
                # 空值不覆盖已有内容：QQ 官方拿不到群名/昵称时，别把面板上手填的抹掉
                for key, value in (("group_name", group_name), ("platform_type", platform_type),
                                   ("subscriber_id", subscriber_id),
                                   ("subscriber_name", subscriber_name)):
                    if value:
                        s[key] = _text(value, 120)
                save_subs(subs)
                return s
        sub = norm_sub({
            "platform_id": pid, "platform_type": platform_type,
            "group_id": gid, "group_name": group_name,
            "subscriber_id": subscriber_id, "subscriber_name": subscriber_name,
            "enabled": True, "created_at": now, "updated_at": now,
        })
        subs.append(sub)
        save_subs(subs)
        return sub


def update_sub(platform_id: str, group_id: str, **fields) -> dict | None:
    """只改可编辑字段（群名 / 订阅者名 / 启用）。找不到返回 None。"""
    allowed = ("group_name", "subscriber_name", "enabled")
    pid, gid = _text(platform_id, 64), _text(group_id, 64)
    with _lock:
        subs = load_subs()
        for s in subs:
            if s["group_id"] == gid and s["platform_id"] == pid:
                for key, value in fields.items():
                    if key not in allowed:
                        continue
                    s[key] = bool(value) if key == "enabled" else _text(value, 120)
                s["updated_at"] = int(time.time())
                save_subs(subs)
                return s
    return None


def set_enabled(platform_id: str, group_id: str, enabled: bool) -> dict | None:
    return update_sub(platform_id, group_id, enabled=bool(enabled))


def delete_sub(platform_id: str, group_id: str) -> bool:
    pid, gid = _text(platform_id, 64), _text(group_id, 64)
    with _lock:
        subs = load_subs()
        left = [s for s in subs if not (s["group_id"] == gid and s["platform_id"] == pid)]
        if len(left) == len(subs):
            return False
        save_subs(left)
        return True


# ---------------- 推送闸门 ----------------


def muted_pairs() -> set[tuple[str, str]]:
    """被**停用**的 `(platform_id, group_id)` 集合。

    后台推送循环每轮取一次即可（别在每条规则里 load 一次文件）。
    ★ 只收「有记录且 enabled=False」的 —— 没有记录的群**不在**这个集合里，
      也就是照常推送（保持面板订阅的原行为）。
    """
    return {
        (s["platform_id"], s["group_id"])
        for s in load_subs()
        if not s.get("enabled", True)
    }


def allows(platform_id: str, group_id: str) -> bool:
    """这个群现在收不收番剧推送（没记录 = 收）。"""
    pid, gid = _text(platform_id, 64), _text(group_id, 64)
    if not gid:
        return True
    for s in load_subs():
        if s["group_id"] == gid and s["platform_id"] == pid:
            return bool(s.get("enabled", True))
    return True
