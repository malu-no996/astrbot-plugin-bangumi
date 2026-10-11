"""番剧插件的全部 Web API 路由。

注册方式（与原插件不同）：原插件把接口挂在 NoneBot 的 FastAPI app 上、自己写
`@app.get(PREFIX + "/x")`；AstrBot 这边由 main.py 读下面的 `ROUTES` 统一
`context.register_web_api(f"{PREFIX}{suffix}", handler, methods, desc)`。

`ROUTES` 项：`(suffix, handler, methods, desc)`。suffix 不带插件名前缀。

约定：成功响应一律 `web.ok(...)`（顶层不带 `data`），失败一律 `web.fail(msg)`。
"""
from __future__ import annotations

import time

from . import api, push, schedule, store, targets, web

_context = None


def bind(context) -> None:
    """插件启动时绑定 Context（/targets 要用它列平台实例）。"""
    global _context
    _context = context


# ---------------- 概览 / 设置 ----------------


async def api_state():
    """模块概览：星期表 / 数据源 / 订阅数 / 推送状态 / 总开关。"""
    rules = store.load_rules()
    return web.ok(
        weekdays=[{"key": k, "label": v} for k, v in store.WEEKDAYS],
        providers=[{"key": k, "label": v} for k, v in store.PROVIDER_LABELS],
        settings=store.load_settings(),
        push_status=push.status_text(),
        push=push.status(),
        counts={"rules": len(rules), "enabled": sum(1 for r in rules if r.get("enabled"))},
        calendar_fetched_at=int(store.calendar_cache().get("fetched_at") or 0),
        calendar_ttl_hours=store.CALENDAR_TTL // 3600,
        min_refresh_hours=store.MIN_REFRESH_INTERVAL // 3600,
        source=api.source_name(),
        now=int(time.time()),
    )


async def api_settings():
    """模块设置：数据源 + 总开关。"""
    return web.ok(settings=store.load_settings(), source=api.source_name())


async def api_settings_save():
    body = await web.body()
    fields: dict = {}
    if "provider" in body:
        fields["provider"] = str(body.get("provider") or "")
    if "enabled" in body:
        fields["enabled"] = bool(body.get("enabled"))
    settings = store.save_settings(**fields)
    return web.ok(settings=settings, source=api.source_name(), message="设置已保存")


# ---------------- 每日放送 / 搜索 ----------------


async def api_calendar():
    """每日放送（周一~周日分组）。默认读缓存，`refresh=1` 才真去数据源拉。"""
    force = web.query("refresh") in ("1", "true", "yes")
    try:
        data = await api.calendar(force=force)
    except Exception as exc:
        # ★ 上游连不上**不是**客户端的错，所以这里回 200 + ok:false（而不是 400）：
        #   400 会让浏览器控制台飘红、看起来像插件坏了，真正的处理是页面上把原因写清楚。
        #   拉不动但手上还有旧缓存 → 给旧的 + 一句提示，别让用户面对一片空白。
        cached = store.calendar_cache()
        if cached.get("days"):
            return web.ok(
                days=cached["days"],
                fetched_at=int(cached.get("fetched_at") or 0),
                cached=True,
                source=api.source_name(),
                message=f"刷新失败，先用缓存：{exc}",
            )
        return web.soft_fail(
            f"拉取每日放送失败：{exc}（顶部数据源下拉里可以换一个源试试）",
            source=api.source_name(),
        )
    # 手动刷新被限流：不是出错，把「多久之内不再重复拉」说清楚
    if data.get("throttled"):
        data["message"] = (
            f"放送表一天之内不会有大变化，距离上次拉取不到 {store.MIN_REFRESH_INTERVAL // 3600} 小时，"
            f"先继续用缓存（{int(data.get('wait_minutes') or 0)} 分钟后可再刷新）"
        )
    return web.ok(**data)


async def api_search():
    """按名字搜番剧。"""
    q = web.query("q").strip()
    if not q:
        return web.fail("请输入要搜索的番剧名")
    try:
        items = await api.search(q)
    except Exception as exc:
        return web.fail(f"搜索失败：{exc}")
    return web.ok(items=items)


async def api_episodes():
    """某部番剧的剧集表（用来确认「第几集是哪天播」）。"""
    raw = web.query("subject_id").strip()
    try:
        sid = int(raw or 0)
    except ValueError:
        return web.fail("subject_id 不合法")
    if not sid:
        return web.fail("缺少 subject_id")
    prov = web.query("provider").strip().lower()[:20]
    eps = await api.episodes(sid, prov if prov in store.PROVIDERS else "")
    return web.ok(subject_id=sid, eps=eps)


# ---------------- 推送目标 ----------------


async def api_targets():
    """列出可用于推送的平台实例（OneBot 附群列表；群 ID 也可手填）。"""
    insts = await targets.instances(_context) if _context is not None else []
    if not insts:
        return web.soft_fail(
            "未检测到平台实例：请先在 AstrBot WebUI「消息平台」里配置并启用至少一个平台适配器",
            instances=[],
        )
    return web.ok(instances=insts)


# ---------------- 订阅增删改查 ----------------


async def api_rules_bulk():
    """把放送表里的一批番剧**一次性订到同一个群**。

    ★ 为什么要有它：「一部番 = 一条订阅」这个粒度是对的，但当需求是
      「本季新番全部往一个群里推」时，逐条点几十次「订阅」不现实。
      批量只让人挑**一次目标**（实例 + 群），其余一律由放送表自动带出：
      周几 / 几点 / 更新周期 / 首播日 / 封面 / 总集数。

    ★ 去重口径：`同一条目 + 同一个目标` 已经存在才跳过（换一个群仍然会加）。
    """
    body = await web.body()
    platform_id = str(body.get("platform_id") or "").strip()
    target_id = str(body.get("target_id") or "").strip()
    if not platform_id:
        return web.fail("请选择推送用的平台实例")
    if not target_id:
        return web.fail("请选择要推送到的群 / 填写群 ID")
    if str(body.get("target_type") or "group") == "private":
        return web.fail("批量订阅只支持群：私聊得逐个填接收人，批量没有意义")

    picked: list[int] = []
    for w in (body.get("weekdays") or []):
        try:
            wd = int(str(w).strip())
        except (TypeError, ValueError):
            continue
        if 1 <= wd <= 7 and wd not in picked:
            picked.append(wd)

    time_mode = "fixed" if str(body.get("time_mode") or "") == "fixed" else "own"
    fixed_time = str(body.get("time") or "").strip()
    if time_mode == "fixed" and not fixed_time:
        return web.fail("选了「统一时刻」就得填一个 HH:MM")
    try:
        delay = max(0, min(int(str(body.get("delay_minutes") or 0)), 24 * 60))
    except (TypeError, ValueError):
        delay = 0

    # ★ 只读缓存、不发起拉取：批量订阅这种批量写操作不该顺带触发一轮外部请求。
    days = store.calendar_cache().get("days") or []
    if not days:
        return web.fail("放送表还是空的：先回「每日放送」点一次「刷新放送表」")

    existing = {
        (str(r.get("subject_id") or ""), str(r.get("title") or "").strip())
        for r in store.load_rules()
        if str(r.get("platform_id") or "") == platform_id
        and str(r.get("target_id") or "") == target_id
    }

    prov = api.provider()
    raws: list[dict] = []
    skipped = 0
    for day in days:
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
                "provider": prov,
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
                "delay_minutes": delay,
                "platform_id": platform_id,
                "platform_name": str(body.get("platform_name") or ""),
                "target_type": "group",
                "target_id": target_id,
                "target_name": str(body.get("target_name") or target_id),
                "template": "",
                "enabled": True,
            })
            existing.add(key)          # 同一批里重复的番也别加两遍
    if not raws:
        return web.ok(added=0, skipped=skipped, message="没有新增：这些番剧已经订阅过这个群了")

    made = store.add_rules(raws)
    now = time.time()
    for r in made:
        rid = str(r.get("id"))
        if not int(r.get("next_run_at") or 0):
            store.set_rule_field(rid, next_run_at=schedule.compute_next(r, now))
    return web.ok(
        added=len(made),
        ids=[str(r.get("id")) for r in made],   # 页面靠它做「撤销本次批量订阅」
        skipped=skipped,
        message=(
            f"已订阅 {len(made)} 部番剧到「{str(body.get('target_name') or target_id)}」"
            f"（跳过 {skipped} 部重复）"
        ),
    )


async def api_rules():
    return web.ok(rules=store.load_rules(), now=int(time.time()))


async def api_rule_save():
    body = await web.body()
    if not str(body.get("title") or "").strip():
        return web.fail("番剧名为空：请从「每日放送」或搜索结果里选一部")
    if not str(body.get("platform_id") or "").strip():
        return web.fail("请选择推送用的平台实例")
    if not str(body.get("target_id") or "").strip():
        return web.fail("请选择群 / 填写私聊用户 ID")
    rid = str(body.get("id") or "").strip()
    # 订阅要记住自己是哪个源的（以后换了数据源，老订阅还能按原来的方式查集数）
    if not str(body.get("provider") or "").strip():
        body["provider"] = api.provider()
    rule = store.update_rule(rid, body) if rid else store.add_rule(body)
    if rule is None and rid:
        rule = store.add_rule(body)
    rid = str(rule.get("id"))
    fresh = store.get_rule(rid)
    if fresh and not int(fresh.get("next_run_at") or 0):
        store.set_rule_field(rid, next_run_at=_next_at(fresh))
    return web.ok(rule=store.get_rule(rid), message="已保存订阅")


async def api_rule_toggle():
    body = await web.body()
    rid = str(body.get("id") or "").strip()
    enabled = bool(body.get("enabled"))
    if not store.set_rule_field(rid, enabled=enabled):
        return web.fail("订阅不存在")
    return web.ok(message="已启用" if enabled else "已停用")


async def api_rule_delete():
    body = await web.body()
    rid = str(body.get("id") or "").strip()
    if not store.delete_rule(rid):
        return web.fail("订阅不存在")
    return web.ok(message="已删除订阅")


async def api_rule_batch():
    """批量启停 / 批量删除（`action` = enable / disable / delete）。

    ★ 删除是破坏性操作：**只接受显式传来的 id 列表**，不支持「全删」这种通配，
    免得前端一个空数组把整个订阅库清空。
    """
    body = await web.body()
    action = str(body.get("action") or "").strip().lower()
    ids = [str(i) for i in (body.get("ids") or []) if str(i).strip()]
    if action not in ("enable", "disable", "delete"):
        return web.fail("action 只能是 enable / disable / delete")
    if not ids:
        return web.fail("没有选中任何订阅")
    if action == "delete":
        n = store.delete_rules(ids)
        return web.ok(changed=n, message=f"已删除 {n} 条订阅")
    n = store.set_rules_enabled(ids, action == "enable")
    verb = "启用" if action == "enable" else "停用"
    return web.ok(changed=n, message=f"已{verb} {n} 条订阅")


async def api_rule_run():
    """立刻推一次（页面「立即推送」按钮），不改动启停状态。"""
    body = await web.body()
    rid = str(body.get("id") or "").strip()
    return web.result(await push.run_rule(rid, "web"))


async def api_rule_preview():
    """预览这条订阅现在推会发什么（不真发）。"""
    rid = web.query("id").strip()
    text = await push.preview(rid)
    return web.ok(text=text)


# ---------------- 工具 ----------------


def _wd(value) -> int:
    """周几归一到 1..7。"""
    try:
        wd = int(str(value).strip())
    except (TypeError, ValueError):
        return 1
    return wd if 1 <= wd <= 7 else 1


def _next_at(rule: dict) -> int:
    return schedule.compute_next(rule)


ROUTES = [
    ("/state", api_state, ["GET"], "模块概览（星期表 / 数据源 / 订阅数 / 推送状态）"),
    ("/settings", api_settings, ["GET"], "读取模块设置（数据源 + 总开关）"),
    ("/settings", api_settings_save, ["POST"], "保存模块设置"),
    ("/calendar", api_calendar, ["GET"], "每日放送（周一~周日分组）"),
    ("/search", api_search, ["GET"], "按名字搜番剧"),
    ("/episodes", api_episodes, ["GET"], "某部番剧的剧集表"),
    ("/targets", api_targets, ["GET"], "可用的平台实例列表"),
    ("/rules", api_rules, ["GET"], "全部订阅"),
    ("/rules/bulk", api_rules_bulk, ["POST"], "批量订阅到同一个群"),
    ("/rule/save", api_rule_save, ["POST"], "新增 / 编辑订阅"),
    ("/rule/toggle", api_rule_toggle, ["POST"], "启用 / 停用订阅"),
    ("/rule/delete", api_rule_delete, ["POST"], "删除订阅"),
    ("/rule/batch", api_rule_batch, ["POST"], "批量启停 / 批量删除"),
    ("/rule/run", api_rule_run, ["POST"], "立即推送一次"),
    ("/rule/preview", api_rule_preview, ["GET"], "预览推送内容"),
]
