"""推送编排：定集数 → 渲染 → 发送 → 排期推进；后台循环；页面状态。

集数怎么定
----------
1. **这部番在 B站有引进**（rule.bili_id）→ 查 B站时间表当天那条，用它的 `sort`。
   最准：B站会标明停播/连播，推算不出来这种情况。
2. 否则查本源的剧集表（`airdate == 今天`）：bangumi-data 是按「首播日 + 周期」
   推出来的（能正确处理双周更 P14D），bgm.tv 是官方剧集表。
3. 都查不到 → 按首播日推算：`(今天 - 首播日) // interval_days + 1`（默认 7 天一集）。
4. 推出来的集数超过 `total_episodes` 视为已完结，跳过并记录原因。

失败与退避
----------
`due_rules()` 只看「next_run_at 到点没到点」。失败时若不重排这个字段，那条订阅就
永远「到点」，于是**每 TICK 秒原地重跑一遍**。所以：
- **可重试**的失败（平台没连上 / 发送失败 / 未知异常）→ `_defer`：退避后重排。
- **不可重试**（今天没有可推集数 / 已完结）→ `_advance`：记结果并把排期推到下一次。
"""
from __future__ import annotations

import asyncio
import time
from datetime import date, datetime

from loguru import logger

from . import api, group_subs, render, schedule, sender, store

TICK = 30                    # 轮询间隔（秒）：番剧推送粒度是分钟级，30 秒足够
FIRST_DELAY = 20             # 启动后先缓一缓，别跟其它模块的启动任务挤在一起

# 推送失败后的退避重试：1 分钟 → 2 → 4 → …最多 1 小时。
RETRY_BASE = 60
RETRY_MAX = 3600

EP_SRC_LABEL = {
    "bdata": "bangumi-data 排期",
    "bilibili": "B站时间表",
    "bgm": "bgm 剧集表",
}

_runtime = {
    "running": False,
    "last_check": 0,       # 上次检查时间
    "last_due": 0,         # 上次有几条到点
    "last_pushed": 0,      # 上次成功推了几条
    "last_error": "",
}
_task: asyncio.Task | None = None
_running = False


# ---------------- 集数 ----------------


async def resolve_episode(rule: dict, day: date) -> tuple[int, str]:
    """今天播出的是第几集 + 这个数字是哪来的（页面上能看到来源，好判断可不可信）。"""
    iso = day.isoformat()
    prov = str(rule.get("provider") or "").strip().lower() or api.provider()

    # 1) B站有引进 → B站时间表最准（能看出停播/连播）
    bili = str(rule.get("bili_id") or "").strip()
    if bili:
        ep = await api.bili_episode_of(bili, iso)
        if ep > 0:
            return ep, "B站时间表"

    # 2) 本源的剧集表
    sid = int(rule.get("subject_id") or 0)
    if sid:
        ep = await api.episode_of(sid, iso, prov)
        if ep > 0:
            return ep, EP_SRC_LABEL.get(prov, "剧集表")

    # 3) 按首播日 + 周期推算
    ep = schedule.week_episode(str(rule.get("air_date") or ""), day, int(rule.get("interval_days") or 7))
    return ep, "按首播日推算"


async def _payload(rule: dict, day: date | None = None) -> tuple[int, str, str]:
    """算出「今天该推第几集 + 正文」；返回 `(集数, 正文, 集数来源)`。"""
    day = day or schedule.today()
    ep, src = await resolve_episode(rule, day)
    if ep <= 0:
        return 0, "", src
    total = int(rule.get("total_episodes") or 0)
    if total and ep > total:
        return -1, "", src       # -1 = 已完结（超出总集数）
    return ep, render.render_message(rule, ep, day), src


# ---------------- 排期推进 ----------------


def _defer(rule: dict, msg: str, now: float) -> None:
    """**可重试**的失败（实例没连上 / 发送失败 / 未知异常）：退避后重排。"""
    n = int(rule.get("fail_count") or 0) + 1
    wait = min(RETRY_BASE * (2 ** (n - 1)), RETRY_MAX)
    store.set_rule_field(
        str(rule.get("id")),
        last_result=msg,
        last_run_at=int(now),
        fail_count=n,
        next_run_at=int(now) + wait,
    )
    logger.warning(f"番剧推送失败（{rule.get('title') or '？'}，第 {n} 次）："
                   f"{msg}；{wait // 60} 分钟后重试")


def _advance(rule: dict, msg: str, now: float) -> None:
    """**这次机会用掉了**：记结果，并把排期推到下一次（下一周同一时刻）。

    用于「今天没有可推送的集数」「已完结」—— 这类不是故障，重试也没用，
    推下去反而会一直被判成「到点」。与「错过了不补推」的取舍一致。
    """
    store.set_rule_field(
        str(rule.get("id")),
        last_result=msg,
        last_run_at=int(now),
        fail_count=0,
        next_run_at=schedule.compute_next(rule, now),
    )


# ---------------- 跑一条 ----------------

async def run_rule(rule_id: str, source: str = "auto") -> dict:
    """按一条订阅推一次。`source` 只用于展示（auto=定时 / web=页面按钮）。

    ★ 手动触发也走这里：它是「看效果」的按钮，**不影响**下一次排期
      （排期只在跑完之后重算，手动跑完同样会重排）。
    """
    rule = store.get_rule(rule_id)
    if rule is None:
        return {"ok": False, "message": "订阅不存在"}
    now = time.time()
    day = schedule.today(now)
    ep, text, src = await _payload(rule, day)
    if ep == 0:
        msg = f"《{rule.get('title') or '？'}》今天没有可推送的集数（还没到首播日，或查不到播出安排）"
        _advance(rule, msg, now)
        return {"ok": False, "message": msg}
    if ep < 0:
        msg = f"《{rule.get('title') or '？'}》已完结（推算集数超过总集数 {rule.get('total_episodes')}）"
        _advance(rule, msg, now)
        return {"ok": False, "message": msg}

    try:
        await sender.send(rule, text)
    except Exception as exc:
        msg = f"发送失败：{exc}"
        _defer(rule, msg, now)
        return {"ok": False, "message": msg}

    store.set_rule_field(
        rule_id,
        last_run_at=int(now),
        last_run_date=day.isoformat(),
        last_episode=int(ep),
        fail_count=0,
        last_result=f"{source} 推送成功：第 {ep} 集（{src}）",
    )
    fresh = store.get_rule(rule_id) or rule
    store.set_rule_field(rule_id, next_run_at=schedule.compute_next(fresh, now))
    return {"ok": True, "message": f"已推送第 {ep} 集", "text": render.page_text(text, rule), "episode": ep}


async def preview(rule_id: str) -> str:
    """预览一次会发什么（不真发、不改任何状态）。"""
    rule = store.get_rule(rule_id)
    if rule is None:
        return ""
    ep, text, _src = await _payload(rule)
    if ep == 0:
        return "（今天没有可推送的集数：还没到首播日，或查不到播出安排）"
    if ep < 0:
        return f"（已完结：推算集数超过总集数 {rule.get('total_episodes')}）"
    return render.page_text(text, rule)


# ---------------- 后台循环 ----------------


async def loop_forever() -> None:
    """后台循环：模块总开关关掉时什么都不做（不查数据、不发消息）。"""
    await asyncio.sleep(FIRST_DELAY)
    while True:
        try:
            if not store.load_settings().get("enabled", True):
                await asyncio.sleep(TICK)
                continue
            due = schedule.due_rules(store)
            _runtime["last_check"] = int(time.time())
            _runtime["last_due"] = len(due)
            pushed = 0
            for rule in due:
                try:
                    result = await run_rule(str(rule.get("id")), "auto")
                    if result.get("ok"):
                        pushed += 1
                except Exception as exc:
                    # ★ 用的是循环开头拿到的 `rule`（run_rule 里可能已经改过状态，
                    #   但 fail_count 是单调递增的，用旧值只会让退避少退一档）
                    _defer(rule, f"推送异常：{exc}", time.time())
            if pushed:
                _runtime["last_pushed"] = pushed
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("番剧推送循环异常")
            _runtime["last_error"] = str(exc)
        await asyncio.sleep(TICK)


def start() -> None:
    """启动后台推送循环（在插件 initialize() 里调用）。"""
    global _task, _running
    if _running:
        return
    _running = True
    try:
        _task = asyncio.create_task(loop_forever())
        logger.info("番剧：推送循环已启动")
    except Exception as exc:
        _running = False
        logger.warning(f"番剧：推送循环启动失败：{exc}")


def stop() -> None:
    """停止后台推送循环（在插件 terminate() 里调用）。"""
    global _task, _running
    _running = False
    if _task is not None:
        _task.cancel()
        _task = None


# ---------------- 页面状态 ----------------


def status_text() -> str:
    """给页面看的运行状态。"""
    rules = store.load_rules()
    muted = group_subs.muted_pairs()
    muted_rules = sum(
        1 for r in rules
        if str(r.get("target_type")) == "group"
        and (str(r.get("platform_id") or ""), str(r.get("target_id") or "")) in muted
    )
    if not rules:
        return "还没有订阅任何番剧"
    enabled = [r for r in rules if r.get("enabled")]
    head = f"共 {len(rules)} 条订阅，启用 {len(enabled)} 条"
    if muted_rules:
        head += f"，其中 {muted_rules} 条所属群已停用"
    if not store.load_settings().get("enabled", True):
        return f"{head} · 模块已禁用（后台推送空转）"
    ts, name = schedule.next_fire(store)
    if not ts:
        return head
    d = datetime.fromtimestamp(ts)
    when = f"{d.month}/{d.day} {store.weekday_label(d.isoweekday())} {d.strftime('%H:%M')}"
    return f"{head} · 下一次推送：《{name}》{when}"


def status() -> dict:
    """运行状态（含上次检查 / 上次错误），页面「运行状态」行用。"""
    return {"text": status_text(), **_runtime}
