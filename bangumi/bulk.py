"""批量订阅：把「每日放送」里符合条件的番剧**一次性订到同一个群**。

一份实现，两个入口（都在这里收口，免得两边逻辑漂移）：

- 面板「我的订阅 → 批量订阅到同一个群」（routes.api_rules_bulk）
- 群里发的「订阅番剧」命令（commands.py，不筛周几、各番按各自的播出时刻）

★ 为什么要有它：「一部番 = 一条订阅」这个粒度是对的，但当需求是
  「本季新番全部往一个群里推」时，逐条点几十次「订阅」不现实。
  批量只让人挑**一次目标**（实例 + 群），其余一律由放送表自动带出：
  周几 / 几点 / 更新周期 / 首播日 / 封面 / 总集数。

★ 去重口径：`同一条目 + 同一个目标` 已经存在才跳过（换一个群仍然会加 ——
  不同群各推一份是正常需求）。

★ **手动刷新才联网**：`allow_fetch=False`（面板路径）只读缓存，批量订阅这种
  批量写操作不该顺带触发一轮外部请求；`allow_fetch=True`（命令路径）才在
  缓存为空时真去拉一次，因为群里发命令的人看不到「去面板点刷新」这句提示。
"""

from __future__ import annotations

import time

from loguru import logger

from . import api, schedule, store

# 播出后默认推迟几分钟再推（给字幕组 / 配信留时间）。
# 与面板批量订阅浮层的初始值一致，用户抱怨「推太早」时改这里和浮层即可。
DEFAULT_DELAY_MINUTES = 10


def _wd(value) -> int:
    """周几归一到 1..7（1=周一）。"""
    try:
        wd = int(str(value).strip())
    except (TypeError, ValueError):
        return 1
    return wd if 1 <= wd <= 7 else 1


# ---------------- 放送表 ----------------


async def season_days(allow_fetch: bool = False) -> list[dict]:
    """放送表（按周一~周日分好组的 days）。

    - 默认**只读缓存**（放进度表那一步是 24 小时才真拉一次，所以这里读到的
      通常就是最新的一份）；
    - `allow_fetch=True` 且缓存为空时才真去拉一次；拉失败退回缓存（不为空就给旧的）。
    """
    cached = store.calendar_cache().get("days") or []
    if cached or not allow_fetch:
        return cached
    try:
        data = await api.calendar(force=False)
    except Exception as exc:  # noqa: BLE001 —— 拉不动就用缓存（这里必定是空，调用方会提示）
        logger.warning(f"番剧批量订阅：拉取放送表失败：{exc}")
        return []
    return data.get("days") or []


# ---------------- 计划 → 规则 ----------------


def build_raws(days: list[dict], *, platform_id: str, target_id: str, platform_name: str = "",
               target_name: str = "", weekdays=None, time_mode: str = "own",
               fixed_time: str = "", delay_minutes: int = 0, provider: str = "") -> tuple[list[dict], int]:
    """放送表 → 待写入的规则 dict 列表；返回 `(raws, 跳过的条数)`。

    `weekdays` 空 = 不筛（整季都要）；`time_mode="fixed"` 时全部用 `fixed_time`，
    否则各番用自己的播出时刻（推荐）。
    """
    picked: list[int] = []
    for w in (weekdays or []):
        wd = _wd(w)
        if wd not in picked:
            picked.append(wd)

    existing = {
        (str(r.get("subject_id") or ""), str(r.get("title") or "").strip())
        for r in store.load_rules()
        if str(r.get("platform_id") or "") == str(platform_id)
        and str(r.get("target_id") or "") == str(target_id)
    }

    raws: list[dict] = []
    skipped = 0
    for day in days or []:
        if not isinstance(day, dict):
            continue
        day_wd = _wd(day.get("weekday"))
        if picked and day_wd not in picked:
            continue
        for item in day.get("items") or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("native") or "").strip()
            if not title:
                continue
            key = (str(item.get("subject_id") or ""), title)
            if key in existing:
                skipped += 1
                continue
            item_time = str(item.get("time") or "").strip()
            raws.append({
                "subject_id": item.get("subject_id") or 0,
                "provider": provider,
                "bili_id": str(item.get("bili_id") or ""),
                "title": title,
                "native": str(item.get("native") or ""),
                "image": str(item.get("image") or ""),
                "url": str(item.get("url") or ""),
                "air_date": str(item.get("air_date") or ""),
                "total_episodes": int(item.get("total") or 0),
                "weekday": _wd(item.get("weekday") or day_wd),
                "time": fixed_time if time_mode == "fixed" else (item_time or "23:00"),
                "interval_days": int(item.get("interval") or 7),
                "delay_minutes": delay_minutes,
                "platform_id": platform_id,
                "platform_name": platform_name,
                "target_type": "group",
                "target_id": target_id,
                "target_name": target_name or target_id,
                "template": "",
                "enabled": True,
            })
            existing.add(key)          # 同一批里重复的番也别加两遍
    return raws, skipped


def apply(raws: list[dict]) -> list[dict]:
    """写入规则并按各自的「周几 + 时刻」排好下一次触发时间，返回**排期之后的**规则。

    ★ `store.add_rules` 已经整批只写一次盘；这里逐条排期也要写盘，但排期是
      批量订阅**必须**做的（不排就永远「到点」，循环里每 30 秒白跑一遍），
      所以放在写入之后一次做完。
    ★ 最后**回读一次**：`set_rule_field` 改的是盘上那份，`add_rules` 返回的副本
      还停在排期前 —— 直接把它交出去，调用方拿到的就是一群 `next_run_at=0` 的
      僵尸对象（排期看起来「没生效」，最难查）。
    """
    if not raws:
        return []
    made = store.add_rules(raws)
    now = time.time()
    for r in made:
        rid = str(r.get("id"))
        if not int(r.get("next_run_at") or 0):
            store.set_rule_field(rid, next_run_at=schedule.compute_next(r, now))
    ids = {str(r.get("id")) for r in made}
    return [r for r in store.load_rules() if str(r.get("id")) in ids]


# ---------------- 对外：一次批量订阅 ----------------


async def subscribe_season(platform_id: str, target_id: str, *, platform_name: str = "",
                           target_name: str = "", weekdays=None, time_mode: str = "own",
                           time: str = "", delay_minutes: int | None = None,
                           allow_fetch: bool = False) -> dict:
    """完整流程：取放送表 → 建规则 → 排期。

    返回 `{ok, added, skipped, ids, message}`：`ok=False` 时 `message` 是**给用户看**
    的失败原因（放送表为空 / 参数不对），调用方直接拿去回话或当接口响应。
    """
    pid, tid = str(platform_id or "").strip(), str(target_id or "").strip()
    if not pid:
        return {"ok": False, "added": 0, "skipped": 0, "ids": [], "message": "请选择推送用的平台实例"}
    if not tid:
        return {"ok": False, "added": 0, "skipped": 0, "ids": [],
                "message": "请选择要推送到的群 / 填写群 ID"}

    days = await season_days(allow_fetch=allow_fetch)
    if not days:
        return {"ok": False, "added": 0, "skipped": 0, "ids": [],
                "message": "放送表还是空的：先到面板「每日放送」页点一次「刷新放送表」"}

    delay = DEFAULT_DELAY_MINUTES if delay_minutes is None else max(0, min(int(delay_minutes), 24 * 60))
    raws, skipped = build_raws(
        days, platform_id=pid, target_id=tid,
        platform_name=platform_name, target_name=target_name,
        weekdays=weekdays, time_mode=time_mode, fixed_time=time, delay_minutes=delay,
        provider=api.provider(),
    )
    label = target_name or tid
    if not raws:
        return {"ok": True, "added": 0, "skipped": skipped, "ids": [],
                "message": f"没有新增：这些番剧已经订阅过「{label}」了"}

    made = apply(raws)
    return {
        "ok": True,
        "added": len(made),
        "skipped": skipped,
        "ids": [str(r.get("id")) for r in made],
        "message": (
            f"已订阅 {len(made)} 部番剧到「{label}」"
            + (f"（跳过 {skipped} 部已订过的）" if skipped else "")
        ),
    }
