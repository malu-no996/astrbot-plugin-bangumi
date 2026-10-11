"""排期：下一次触发时刻 / 到点规则 / 按首播日推算集数。

一条订阅 =「某部番剧 · 每周几的几点 · 推到哪」。所以排期很直接：
下一次触发 = **最近的那个「周几 + 时刻 + 延迟」**。

★ 两个刻意的取舍：
1. **错过了不补推**。创建规则时若本周那个时刻已经过去，就排到**下一周**，
   而不是立刻补一条 —— 否则「晚上 11 点建一条 8 点的订阅」会马上炸出一条消息。
2. **不带随机浮动**。外语背单词那种可以浮动，番剧不行：「《XX》第 5 集更新了」
   早半小时晚半小时都很怪，播出后几分钟推才对。
"""
from __future__ import annotations

import time
from datetime import date, datetime

from . import group_subs


def today(now: float | None = None) -> date:
    return datetime.fromtimestamp(now if now is not None else time.time()).date()


def _at(now: float, hh: int, mm: int, day_offset: int = 0) -> int:
    """某天某时刻的时间戳（按本时区）。"""
    base = datetime.fromtimestamp(now + day_offset * 86400)
    return int(base.replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp())


def compute_next(rule: dict, now: float | None = None) -> int:
    """算下一次触发时刻（算一次写进配置，之后不再变）。

    `last_run_date == 今天` 或「本周那个点已经过去了」→ 一律排到**下一周同一时刻**。
    """
    now = now if now is not None else time.time()
    weekday = int(rule.get("weekday") or 1)
    try:
        hh, mm = (int(x) for x in str(rule.get("time") or "00:00").split(":", 1))
    except ValueError:
        hh, mm = 0, 0
    delay = max(0, int(rule.get("delay_minutes") or 0)) * 60
    day = today(now)
    # 规则 weekday 1=周一；Python 的 weekday() 0=周一
    delta = ((weekday - 1) - day.weekday()) % 7
    if delta == 0:
        ts = _at(now, hh, mm) + delay
        ran_today = str(rule.get("last_run_date") or "") == day.isoformat()
        if ran_today or ts <= now:
            delta = 7            # 今天已推过 / 今天的点早过了 → 等下周
    ts = _at(now, hh, mm, day_offset=delta) + delay
    return int(max(ts, now + 5))


def week_episode(air_date: str, day: date, interval: int = 7) -> int:
    """按首播日推算第几集（首播当天 = 第 1 集）。

    `interval` 是几集之间隔几天（bangumi-data 的 broadcast 里带，如 P14D = 双周更）。
    """
    try:
        start = date.fromisoformat(str(air_date or "").strip())
    except ValueError:
        return 0
    delta = (day - start).days
    if delta < 0:
        return 0
    return delta // max(1, int(interval or 7)) + 1


def due_rules(store, now: float | None = None) -> list[dict]:
    """到点的规则（没排期的顺手补排）。

    ★ **被停用的群在这里就滤掉**（`group_subs.muted_pairs`）：不是等发送时再拦 ——
      不然那些规则会一直「到点」、每 30 秒原地重跑一遍，日志和退避计数全被污染。
      「没有群订阅记录」的目标不在停用集合里，照常推送（面板老订阅的行为不变）。
    """
    now = now if now is not None else time.time()
    muted = group_subs.muted_pairs()
    out: list[dict] = []
    for r in store.load_rules():
        if not r.get("enabled"):
            continue
        if str(r.get("target_type")) == "group" and (
            str(r.get("platform_id") or ""), str(r.get("target_id") or "")
        ) in muted:
            continue
        nxt = int(r.get("next_run_at") or 0)
        if not nxt:
            store.set_rule_field(str(r.get("id")), next_run_at=compute_next(r, now))
            continue
        if now >= nxt:
            out.append(r)
    return out


def reschedule_group(store, platform_id: str, target_id: str, now: float | None = None) -> int:
    """把某个群的全部规则重排一次（**只写一次盘**），返回重排的条数。

    ★ 用在「群订阅重新启用」上：停用期间规则不会被跑，`next_run_at` 全部积压在
      过去 —— 一解禁就会**一次性炸出几十条**消息（本季番剧数量级）。所以启用
      的时候统一按「错过的排到下一次」重算，和「创建时错过了不补推」同一口径。
    """
    now = now if now is not None else time.time()
    pid, tid = str(platform_id or ""), str(target_id or "")
    rules = store.load_rules()
    hit = 0
    for r in rules:
        if (str(r.get("platform_id") or "") == pid
                and str(r.get("target_id") or "") == tid):
            r["next_run_at"] = compute_next(r, now)
            hit += 1
    if hit:
        store.save_rules(rules)
    return hit


def next_fire(store) -> tuple[int, str]:
    """最近的**下一次**推送（时间戳, 番剧名）；没有就返回 `(0, "")`。

    ★ 后台循环是遍历**所有启用订阅**的（`due_rules`），哪一部的「周几 + 时刻 +
    延迟」到了就推那一部 —— 这里只是把最近的那一条找出来给页面看，让用户一眼确认
    「自动推送还活着、下一次是几点」。
    """
    best: tuple[int, str] = (0, "")
    for r in store.load_rules():
        if not r.get("enabled"):
            continue
        nxt = int(r.get("next_run_at") or 0)
        if not nxt:
            continue
        if not best[0] or nxt < best[0]:
            best = (nxt, str(r.get("title") or "？"))
    return best
