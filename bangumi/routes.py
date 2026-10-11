"""番剧插件的全部 Web API 路由。

注册方式（与原插件不同）：原插件把接口挂在 NoneBot 的 FastAPI app 上、自己写
`@app.get(PREFIX + "/x")`；AstrBot 这边由 main.py 读下面的 `ROUTES` 统一
`context.register_web_api(f"{PREFIX}{suffix}", handler, methods, desc)`。

`ROUTES` 项：`(suffix, handler, methods, desc)`。suffix 不带插件名前缀。

约定：成功响应一律 `web.ok(...)`（顶层不带 `data`），失败一律 `web.fail(msg)`。
"""
from __future__ import annotations

import time

from . import api, bulk, group_subs, push, schedule, store, targets, web

_context = None


def bind(context) -> None:
    """插件启动时绑定 Context（/targets 要用它列平台实例）。"""
    global _context
    _context = context


# ---------------- 概览 / 设置 ----------------


async def api_state():
    """模块概览：星期表 / 数据源 / 订阅数 / 推送状态 / 总开关。"""
    rules = store.load_rules()
    subs = group_subs.load_subs()
    return web.ok(
        weekdays=[{"key": k, "label": v} for k, v in store.WEEKDAYS],
        providers=[{"key": k, "label": v} for k, v in store.PROVIDER_LABELS],
        settings=store.load_settings(),
        push_status=push.status_text(),
        push=push.status(),
        counts={
            "rules": len(rules),
            "enabled": sum(1 for r in rules if r.get("enabled")),
            "groups": len(subs),
            "groups_off": sum(1 for s in subs if not s.get("enabled", True)),
        },
        calendar_fetched_at=int(store.calendar_cache().get("fetched_at") or 0),
        calendar_ttl_hours=store.CALENDAR_TTL // 3600,
        min_refresh_hours=store.MIN_REFRESH_INTERVAL // 3600,
        source=api.source_name(),
        now=int(time.time()),
    )


async def api_settings():
    """模块设置：数据源 + 总开关 + 命令权限档位。"""
    return web.ok(settings=store.load_settings(), source=api.source_name())


async def api_settings_save():
    """模块设置：数据源 / 总开关 / 「订阅番剧」命令权限档位。

    ★ 只写**明确传进来的**字段：缺 key 时不写，免得把没带的设置项默默改掉
      （曾经把 `enabled` 写成「缺 key = True」的默认，页面一保存就被打开）。
    """
    body = await web.body()
    fields: dict = {}
    if "provider" in body:
        fields["provider"] = str(body.get("provider") or "")
    if "enabled" in body:
        fields["enabled"] = bool(body.get("enabled"))
    if "owner_only" in body:
        fields["owner_only"] = bool(body.get("owner_only"))
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

    ★ 实现在 `bulk.subscribe_season`（群里那条「订阅番剧」命令走的是同一个函数），
      这里只负责把 HTTP body 翻译成它的参数 —— 两边逻辑不会漂移。

    ★ 去重口径：`同一条目 + 同一个目标` 已经存在才跳过（换一个群仍然会加）。
    ★ 只读缓存、不发起拉取：批量订阅这种批量写操作不该顺带触发一轮外部请求。
    """
    body = await web.body()
    if str(body.get("target_type") or "group") == "private":
        return web.fail("批量订阅只支持群：私聊得逐个填接收人，批量没有意义")

    time_mode = "fixed" if str(body.get("time_mode") or "") == "fixed" else "own"
    fixed_time = str(body.get("time") or "").strip()
    if time_mode == "fixed" and not fixed_time:
        return web.fail("选了「统一时刻」就得填一个 HH:MM")
    try:
        delay = max(0, min(int(str(body.get("delay_minutes") or 0)), 24 * 60))
    except (TypeError, ValueError):
        delay = 0

    res = await bulk.subscribe_season(
        str(body.get("platform_id") or "").strip(),
        str(body.get("target_id") or "").strip(),
        platform_name=str(body.get("platform_name") or ""),
        target_name=str(body.get("target_name") or ""),
        weekdays=body.get("weekdays") or [],
        time_mode=time_mode,
        time=fixed_time,
        delay_minutes=delay,
        allow_fetch=False,
    )
    if not res.get("ok"):
        return web.fail(res.get("message") or "批量订阅失败")
    return web.ok(
        added=res["added"],
        ids=res["ids"],          # 页面靠它做「撤销本次批量订阅」
        skipped=res["skipped"],
        message=res["message"],
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


# ---------------- 群订阅（「订阅数据」页） ----------------
#
# 一行 = 一个群（「订阅番剧」命令登记下来的，面板也能手工补）。
# 「这个群订了哪几部番」是 store 里的规则，按 platform_id + target_id 归到一起 ——
# 逐部番的新增 / 删除直接复用 /rule/save 与 /rule/delete，不另开接口。


def _with_anime_counts(subs: list[dict], rules: list[dict]) -> list[dict]:
    """给每条群订阅补上「订了几部番 / 几部启用」（一次 O(n²) 的比对，群数量级很小）。"""
    out: list[dict] = []
    for s in subs:
        pid, gid = s["platform_id"], s["group_id"]
        mine = [
            r for r in rules
            if str(r.get("platform_id") or "") == pid and str(r.get("target_id") or "") == gid
        ]
        out.append({
            **s,
            "anime_total": len(mine),
            "anime_enabled": sum(1 for r in mine if r.get("enabled")),
        })
    return out


async def api_subs():
    """「订阅数据」页：群订阅列表 + 权限档位 + 概览计数。"""
    rules = store.load_rules()
    subs = _with_anime_counts(group_subs.load_subs(), rules)
    subs.sort(key=lambda s: (not s.get("enabled", True), s.get("group_name") or s["group_id"]))
    return web.ok(
        subs=subs,
        settings=store.load_settings(),
        counts={
            "groups": len(subs),
            "online": sum(1 for s in subs if s.get("enabled", True)),
            "offline": sum(1 for s in subs if not s.get("enabled", True)),
            "anime": sum(s["anime_total"] for s in subs),
        },
    )


async def api_subs_toggle():
    """启用 / 停用某个群的番剧推送。

    ★ 启用时**要把该群的规则重排一次**：停用期间规则不会被跑，`next_run_at`
      全积压在过去 —— 不重排的话一解禁就一次性炸出几十条消息。
    """
    body = await web.body()
    pid = str(body.get("platform_id") or "").strip()
    gid = str(body.get("group_id") or "").strip()
    if not gid:
        return web.fail("缺少群 ID")
    enabled = bool(body.get("enabled"))
    sub = group_subs.set_enabled(pid, gid, enabled)
    if sub is None:
        return web.fail("这条群订阅不存在（可能已被删除）")
    fresh = schedule.reschedule_group(store, pid, gid) if enabled else 0
    message = "已启用：该群会继续收到番剧更新" if enabled else "已停用：该群不再收到任何番剧更新"
    if enabled and fresh:
        message += f"（{fresh} 条订阅已重排到下一次播出）"
    return web.ok(sub=sub, rescheduled=fresh, message=message)


async def api_subs_delete():
    """删除一条群订阅。

    ★ 口径（用户明确要求）：**连同该群的全部番剧订阅一起删** ——
      只删「群记录」而把几十条规则留着，页面上一片干净、后台却还在推，最容易误判。
    """
    body = await web.body()
    pid = str(body.get("platform_id") or "").strip()
    gid = str(body.get("group_id") or "").strip()
    if not gid:
        return web.fail("缺少群 ID")
    ids = [
        str(r.get("id")) for r in store.load_rules()
        if str(r.get("platform_id") or "") == pid and str(r.get("target_id") or "") == gid
    ]
    removed = store.delete_rules(ids)
    existed = group_subs.delete_sub(pid, gid)
    if not existed and not removed:
        return web.fail("这条群订阅不存在（可能已被删除）")
    return web.ok(removed=removed, message=f"已删除该群订阅，并清掉它的 {removed} 条番剧订阅")


async def api_subs_save():
    """面板上手工新增 / 编辑一条群订阅（群名、订阅者名可以补填或改）。

    ★ 为什么需要它：QQ 官方机器人拿不到**群名**，命令登记下来的记录群名是空的；
      另外完全不用命令、只想在面板上加一个群时也得有入口。
    """
    body = await web.body()
    pid = str(body.get("platform_id") or "").strip()
    gid = str(body.get("group_id") or "").strip()
    if not pid:
        return web.fail("请选择平台实例")
    if not gid:
        return web.fail("请填写群 ID（群消息事件里的 session_id，OneBot 就是 QQ 群号）")

    group_name = str(body.get("group_name") or "").strip()
    subscriber_name = str(body.get("subscriber_name") or "").strip()
    if group_subs.get_sub(pid, gid) is not None:
        sub = group_subs.update_sub(pid, gid, group_name=group_name, subscriber_name=subscriber_name)
        return web.ok(sub=sub, message="已保存群订阅")
    sub = group_subs.upsert(
        pid, gid,
        group_name=group_name,
        subscriber_id=str(body.get("subscriber_id") or "").strip(),
        subscriber_name=subscriber_name,
    )
    return web.ok(sub=sub, message="已新增群订阅（还没订番剧，可点「番剧」里的「一键订本季全部」）")


async def api_subs_anime_all():
    """把放送表里本季的番剧**全部**订给这个群（「订阅数据」页里的补订按钮）。

    命令已经自动做过这一步；这里是给「当时放送表还没拉过 / 想在面板上补」的场合。
    ★ 只读缓存（同批量订阅的口径），不发起外部请求。
    """
    body = await web.body()
    pid = str(body.get("platform_id") or "").strip()
    gid = str(body.get("target_id") or body.get("group_id") or "").strip()
    sub = group_subs.get_sub(pid, gid)
    if sub is None:
        return web.fail("这条群订阅不存在（可能已被删除）")
    res = await bulk.subscribe_season(
        pid, gid,
        platform_name=pid,
        target_name=sub.get("group_name") or gid,
        allow_fetch=False,
    )
    if not res.get("ok"):
        return web.soft_fail(res.get("message") or "订阅失败")
    return web.ok(added=res["added"], skipped=res["skipped"], ids=res["ids"], message=res["message"])


# ---------------- 工具 ----------------


def _next_at(rule: dict) -> int:
    return schedule.compute_next(rule)


ROUTES = [
    ("/state", api_state, ["GET"], "模块概览（星期表 / 数据源 / 订阅数 / 推送状态）"),
    ("/settings", api_settings, ["GET"], "读取模块设置（数据源 + 总开关 + 命令权限）"),
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
    # 「订阅数据」页（群订阅：群 ID / 群名 / 订阅者 / 启停 / 删除）
    ("/subs", api_subs, ["GET"], "群订阅列表（订阅数据页）"),
    ("/subs/save", api_subs_save, ["POST"], "新增 / 编辑群订阅"),
    ("/subs/toggle", api_subs_toggle, ["POST"], "启用 / 停用某个群的番剧推送"),
    ("/subs/delete", api_subs_delete, ["POST"], "删除群订阅（连它的番剧订阅一起删）"),
    ("/subs/anime/all", api_subs_anime_all, ["POST"], "把本季放送表里的番剧全部订给这个群"),
]
