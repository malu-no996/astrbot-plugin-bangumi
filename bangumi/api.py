"""番剧数据源：bangumi-data（默认）/ B站时间表 / Bangumi.tv。

★ 为什么默认是 bangumi-data
--------------------------
本机实测：**api.bgm.tv / bgm.tv TCP 直连超时**（DNS 能解析到 IP，但连不上），
代理也 502；而 gitee / baidu / jsDelivr / api.bilibili.com 都正常。所以：

    bdata    bangumi-data 日本新番库，经 jsDelivr 取 GitHub 上的静态数据
             —— **默认**。全量日本 TV/WEB 新番（一个月 70+ 部），带精确首播
             时刻与周更周期（broadcast 里的 P7D/P14D），不需要任何 key。
    bilibili B站番剧时间表：只含 B站引进的（不全），但**集数最准**，
             且能看出停播/连播 —— 用来给订阅补集数正合适。
    bgm      Bangumi.tv：数据最全但国内常常连不上，留给网络好的环境。

★ 通用规矩
- 超时 15 秒、失败只记日志不抛（页面拿到 `ok:false` + 人话原因）；
- 结果一律交给 `store` 落盘缓存 —— 真正发请求的只有「刷新」按钮和推送时补集数；
- 字段一律「多取一个兜底」，别假设接口永远长一个样。

★ 编号空间：bangumi-data / bgm 用的是 **bgm 条目 id**，B站用的是 **season_id**，
两者会撞号 —— 所以剧集表缓存的文件名必须带源前缀（见 `store.eps_key`）。
"""
from __future__ import annotations

import os
import re
import time
import unicodedata
from datetime import datetime, timedelta

import httpx
from loguru import logger

from . import store

BGM_BASE = "https://api.bgm.tv"
BILI_BASE = "https://api.bilibili.com"
BDATA_BASE = "https://cdn.jsdelivr.net/gh/bangumi-data/bangumi-data@master/data/items"
ANILIST_URL = "https://graphql.anilist.co"     # 给 bangumi-data 补封面 / 总集数（它自己不带图）

UA = "astrbot-plugin-bangumi/1.0 (bangumi plugin)"
# B站不认这种自定义 UA，会给 -352/412；jsDelivr 无所谓，统一用浏览器 UA 最省事
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

TIMEOUT = 15.0
PAGE_SIZE = 100          # bgm.tv 剧集表每页条数（接口上限）
MAX_PAGES = 4            # 最多翻几页（400 集足够任何一部番，防接口异常时死循环）
BDATA_MONTHS = 6         # bangumi-data 往前拉几个月（覆盖半年番）
BDATA_MAX_EPS = 60       # 推算剧集表最多生成多少集
AHEAD_DAYS = 45          # 「还没开播」的番最多提前多少天显示（方便先订阅）


# ---------------- 数据源与客户端 ----------------


def provider() -> str:
    """当前数据源（页面可切换）。"""
    return str(store.load_settings().get("provider") or store.DEFAULT_PROVIDER)


def _headers(prov: str) -> dict:
    if prov == "bilibili":
        return {
            "User-Agent": BROWSER_UA,
            "Referer": "https://www.bilibili.com/anime/",
            "Accept": "application/json",
        }
    return {"User-Agent": UA, "Accept": "application/json"}


def _client_kwargs(prov: str = "") -> dict:
    """建 httpx 客户端的参数。

    代理只看环境变量：`BANGUMI_PROXY` > `HTTPS_PROXY` > `HTTP_PROXY`。
    httpx 默认 `trust_env=True` 本来就会读环境变量，要单独给这个插件配代理设
    `BANGUMI_PROXY` 即可。
    """
    prov = prov or provider()
    proxy = (
        os.environ.get("BANGUMI_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("http_proxy")
        or os.environ.get("HTTP_PROXY")
        or ""
    ).strip()
    kwargs: dict = {"timeout": TIMEOUT, "headers": _headers(prov), "follow_redirects": True}
    if proxy:
        kwargs["proxy"] = proxy
    return kwargs


# ---------------- 小工具 ----------------


def _ts(text) -> int:
    """`"2026-10-07T15:00:00.000Z"` → 时间戳；解析不了返回 0。"""
    raw = str(text or "").strip()
    if not raw:
        return 0
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=None)
        return int(dt.timestamp())
    return int(dt.timestamp())


def _local_date(ts: int) -> str:
    """时间戳 → 本机时区的 `YYYY-MM-DD`（推送面向的是本机时区，别用 UTC）。"""
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d")


def _hhmm(text) -> str:
    raw = str(text or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})", raw)
    if not m:
        return "00:00"
    h, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mm <= 59):
        return "00:00"
    return f"{h:02d}:{mm:02d}"


def _interval_days(broadcast) -> int:
    """`"R/2026-10-07T15:00:00.000Z/P7D"` → 7（几集之间隔几天，默认按周更）。"""
    m = re.search(r"/P(\d+)D", str(broadcast or ""))
    return max(1, int(m.group(1))) if m else 7


def _bili_ep_index(text) -> int:
    """`"第9话"` / `"第1话~第4话"` → 9 / 1。"""
    m = re.search(r"(\d+)", str(text or ""))
    return int(m.group(1)) if m else 0


def _norm_weekday(raw) -> int:
    """日历项的 weekday：可能是 dict（带 cn）/ 数字 / 字符串，统一成 1..7。"""
    if isinstance(raw, dict):
        for key in ("cn", "id", "ja", "en"):
            value = raw.get(key)
            if isinstance(value, int) and 1 <= value <= 7:
                return value
        return 1
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return 1
    return value if 1 <= value <= 7 else 1


# ---------------- 归一化：各源 → 页面要的形状 ----------------


def _pick_image(raw: dict) -> str:
    images = raw.get("images")
    if not isinstance(images, dict):
        return ""
    for key in ("large", "common", "medium", "grid", "small"):
        value = images.get(key)
        if value:
            return str(value)
    return ""


def norm_subject(raw: dict) -> dict:
    """把 bgm.tv 的条目（日历项 / 搜索结果）夹成页面要的形状。"""
    sid = raw.get("id")
    title = str(raw.get("name_cn") or "").strip() or str(raw.get("name") or "").strip()
    native = str(raw.get("name") or "").strip()
    rating = raw.get("rating")
    score = 0.0
    if isinstance(rating, dict):
        try:
            score = float(rating.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
    try:
        sid_int = int(sid or 0)
    except (TypeError, ValueError):
        sid_int = 0
    return {
        "subject_id": sid_int,
        "title": title,
        "native": native,
        "air_date": str(raw.get("air_date") or "").strip(),
        "image": _pick_image(raw),
        "url": f"https://bgm.tv/subject/{sid_int}" if sid_int else "",
        "score": score,
    }


def _bdata_title(raw: dict) -> str:
    """bangumi-data 的标题：优先简体中文译名，其次其它译名，最后日文原名。"""
    tr = raw.get("titleTranslate")
    if isinstance(tr, dict):
        for key in ("zh-Hans", "zh-Hant", "zh", "zh-cn"):
            value = tr.get(key)
            if isinstance(value, list) and value:
                return str(value[0]).strip()
            if isinstance(value, str) and value.strip():
                return value.strip()
    return str(raw.get("title") or "").strip()


def _bdata_ids(raw: dict) -> tuple[int, str, int]:
    """从 `sites` 里挑出 bgm 条目 id、B站 season_id、AniList id。

    - B站 id：有就能用 B站时间表补准集数；
    - AniList id：用来补**封面与总集数**（bangumi-data 自己不带图）。
    """
    sid = 0
    bili = ""
    anilist = 0
    for site in raw.get("sites") or []:
        if not isinstance(site, dict):
            continue
        name = str(site.get("site") or "").strip().lower()
        value = str(site.get("id") or "").strip()
        if name == "bangumi" and not sid:
            try:
                sid = int(value)
            except (TypeError, ValueError):
                sid = 0
        elif name == "bilibili" and not bili:
            bili = value
        elif name in ("anilist", "aniList") and not anilist:
            try:
                anilist = int(value)
            except (TypeError, ValueError):
                anilist = 0
    return sid, bili, anilist


def _bdata_subject(raw: dict, now: float | None = None) -> dict | None:
    """bangumi-data 的一条 → 页面形状；**不在播 / 非日本 TV 番** 返回 None。

    判断「在播」：`begin` 已过（或 45 天内开播）且 `end` 没到（空 = 未定完结日）。
    """
    now_ts = int(now if now is not None else time.time())
    if str(raw.get("type") or "").strip().lower() not in ("tv", "web"):
        return None
    if str(raw.get("lang") or "").strip().lower() != "ja":
        return None                      # 用户要的是日本新番
    begin = _ts(raw.get("begin"))
    if not begin:
        return None
    if begin > now_ts + AHEAD_DAYS * 86400:
        return None                      # 太久以后才播，先不占版面
    end = _ts(raw.get("end"))
    if end and end < now_ts - 86400:
        return None                      # 已完结

    start = datetime.fromtimestamp(begin).astimezone()
    interval = _interval_days(raw.get("broadcast"))
    sid, bili, anilist = _bdata_ids(raw)
    title = _bdata_title(raw)
    native = str(raw.get("title") or "").strip()
    if not title:
        return None
    # 今天该播第几集（纯推算，页面上写「预计」，真正推送时另有更准的办法）
    ep_now = 0
    if now_ts >= begin:
        ep_now = (now_ts - begin) // (interval * 86400) + 1
    return {
        "subject_id": sid,
        "title": title,
        "native": native if native != title else "",
        "air_date": start.strftime("%Y-%m-%d"),
        "image": "",                     # bangumi-data 不带图（卡片只显示文字，前端已兼容）
        "url": f"https://bgm.tv/subject/{sid}" if sid else str(raw.get("officialSite") or ""),
        "score": 0.0,
        "weekday": start.isoweekday(),
        "time": start.strftime("%H:%M"),
        "interval": interval,
        "bili_id": bili,
        "anilist_id": anilist,      # 用来补封面（见 _fill_anilist）
        "total": 0,                 # 总集数（bangumi-data 没有，从 AniList 补）
        "begin_ts": begin,          # 只用于「按标题搜到的图是不是同一部番」的校验
        "latest": f"第{ep_now}集" if ep_now else "",
    }


def _bili_subject(ep: dict, weekday: int, date_iso: str) -> dict | None:
    """B站时间表里的一条剧集 → 一部番剧的条目。"""
    sid = int(ep.get("season_id") or 0)
    title = str(ep.get("title") or "").strip()
    if not sid or not title:
        return None
    image = str(ep.get("square_cover") or ep.get("cover") or "").strip()
    if image.startswith("//"):
        image = "https:" + image
    return {
        "subject_id": sid,
        "title": title,
        "native": "",
        "air_date": date_iso,
        "image": image,
        "url": f"https://www.bilibili.com/bangumi/play/ss{sid}",
        "score": 0.0,
        "weekday": weekday,
        "time": _hhmm(ep.get("pub_time")),
        "interval": 7,
        "bili_id": str(sid),
        "latest": str(ep.get("pub_index") or ""),
    }


def _pack_days(buckets: dict[int, list]) -> list[dict]:
    """`{weekday: [item…]}` → 排好序的 `[{"weekday","label","items"}]`。"""
    return [
        {
            "weekday": wd,
            "label": store.weekday_label(wd),
            "items": sorted(buckets[wd], key=lambda x: str(x.get("time") or "")),
        }
        for wd in sorted(buckets)
    ]


# ---------------- 每日放送：各源 ----------------


async def _bdata_items() -> list[dict]:
    """拉 bangumi-data 最近几个月的条目（jsDelivr 上的静态 JSON，无需 key）。

    月份文件形如 `data/items/2026/10.json`；**未来月份没有文件**（404），
    所以逐月拉、拉不到就跳过（jsDelivr 404 返回的是文本，不是 JSON）。
    """
    now = datetime.fromtimestamp(time.time())
    urls: list[str] = []
    for back in range(BDATA_MONTHS):
        year, month = now.year, now.month - back
        while month <= 0:
            month += 12
            year -= 1
        urls.append(f"{BDATA_BASE}/{year}/{month:02d}.json")

    out: list[dict] = []
    async with httpx.AsyncClient(**_client_kwargs("bdata")) as client:
        for url in urls:
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    continue
                payload = resp.json()
            except Exception as exc:      # 单个月份失败不影响其它月份
                logger.debug(f"番剧：bangumi-data 取不到 {url}：{exc}")
                continue
            items = payload if isinstance(payload, list) else (payload.get("items") or [])
            out.extend([i for i in items if isinstance(i, dict)])
    return out


# ---------------- AniList：给 bangumi-data 补封面与总集数 ----------------
#
# bangumi-data 是纯文本数据库（**没有封面**），但它 2/3 的条目带 aniList id，
# 用 id 去 AniList 精确取封面最稳；没有 id 的按标题搜，用**开播日校验**
# （同名不同季很常见，不校验就会把第一季的图配到第二季上）。

_Q_MEDIA = (
    "query($ids:[Int]){Page(perPage:50){media(id_in:$ids,type:ANIME)"
    "{id coverImage{large medium} episodes startDate{year month day}}}}"
)
_Q_SEASON = (
    "query($y:Int,$s:MediaSeason,$p:Int){Page(page:$p,perPage:50)"
    "{media(seasonYear:$y,season:$s,type:ANIME,sort:POPULARITY)"
    "{id title{native romaji} coverImage{large medium} episodes startDate{year month day}}}}"
)
ANILIST_SEASON_PAGES = 3      # 每季最多翻几页（50/页，3 页 = 150 部，够一季）


async def _anilist_post(query: str, variables: dict) -> dict:
    async with httpx.AsyncClient(**_client_kwargs("bdata")) as client:
        resp = await client.post(ANILIST_URL, json={"query": query, "variables": variables})
        resp.raise_for_status()
        payload = resp.json()
    errors = payload.get("errors")
    if errors:
        first = errors[0] if isinstance(errors, list) and errors else {}
        raise RuntimeError(str(first.get("message") or "AniList 返回错误"))
    return payload.get("data") or {}


def _anilist_start_ts(sd) -> int:
    if not isinstance(sd, dict):
        return 0
    try:
        return int(datetime(int(sd.get("year") or 0), int(sd.get("month") or 0),
                            int(sd.get("day") or 0)).timestamp())
    except (TypeError, ValueError):
        return 0


def _anilist_info(media: dict) -> dict:
    cover = media.get("coverImage") or {}
    title = media.get("title") or {}
    try:
        total = int(media.get("episodes") or 0)
    except (TypeError, ValueError):
        total = 0
    return {
        "image": str(cover.get("large") or cover.get("medium") or "").strip(),
        "total": max(0, total),
        "start_ts": _anilist_start_ts(media.get("startDate")),
        "native": str(title.get("native") or "").strip(),
    }


async def anilist_media(ids) -> dict[int, dict]:
    """按 AniList id 批量取（一次最多 50 个）。"""
    ids = [int(i) for i in (ids or []) if str(i or "").strip().isdigit()][:50]
    if not ids:
        return {}
    data = await _anilist_post(_Q_MEDIA, {"ids": ids})
    out: dict[int, dict] = {}
    for media in ((data.get("Page") or {}).get("media") or []):
        if isinstance(media, dict) and int(media.get("id") or 0):
            out[int(media["id"])] = _anilist_info(media)
    return out


async def anilist_season(year: int, season: str, pages: int = ANILIST_SEASON_PAGES) -> list[dict]:
    """整季番剧（`WINTER/SPRING/SUMMER/FALL`），用来按日文原名给条目补图。"""
    out: list[dict] = []
    for page in range(1, max(1, int(pages)) + 1):
        data = await _anilist_post(_Q_SEASON, {"y": int(year), "s": season, "p": page})
        media = (data.get("Page") or {}).get("media") or []
        if not media:
            break
        out.extend([_anilist_info(m) for m in media if isinstance(m, dict)])
    return out


def _norm_name(text) -> str:
    """标题归一化（用来判断两个库的番剧是不是同一部）。

    直接比原名只能命中一半：两个库的同一部番差在**全角/半角、空格、标点、
    罗马数字**（「魔法少女育成計画 restart」vs「魔法少女育成计划 restart」、
    「Ⅱ」vs「II」）。NFKC 把全角压成半角、Ⅱ 变 II，再去掉空白与标点就够了。
    """
    s = unicodedata.normalize("NFKC", str(text or "").strip()).lower()
    s = re.sub(r"\s+", "", s)
    return re.sub(r"[^0-9a-z\u3040-\u30ff\u4e00-\u9fff]", "", s)


def _season_of(ts: int) -> tuple[int, str]:
    """时间戳 → `(年, 季)`（AniList 的 MediaSeason）。"""
    d = datetime.fromtimestamp(int(ts or 0)).astimezone()
    season = "WINTER" if d.month <= 3 else "SPRING" if d.month <= 6 else (
        "SUMMER" if d.month <= 9 else "FALL"
    )
    return d.year, season


def _apply_anilist(item: dict | None, info: dict, check_date: bool) -> bool:
    """把封面/总集数写进条目。`check_date=True` 时开播日差 7 天以上就当作配错、放弃。"""
    if not item or not isinstance(info, dict):
        return False
    if check_date:
        mine = int(item.get("begin_ts") or 0)
        theirs = int(info.get("start_ts") or 0)
        if mine and theirs and abs(mine - theirs) > 7 * 86400:
            return False
    if not item.get("image") and info.get("image"):
        item["image"] = info["image"]
    if not int(item.get("total") or 0) and int(info.get("total") or 0):
        item["total"] = int(info["total"])
    return True


async def _fill_anilist(items: list[dict]) -> None:
    """给一批 bangumi-data 条目补封面与总集数。

    ★ 补图失败不影响日历本身（图是锦上添花），所以这里**只记日志不抛**。
    """
    if not items:
        return
    by_id: dict[int, dict] = {}
    for it in items:
        aid = int(it.get("anilist_id") or 0)
        if aid:
            by_id[aid] = it
    if by_id:
        ids = sorted(by_id)
        for start in range(0, len(ids), 50):
            try:
                got = await anilist_media(ids[start:start + 50])
            except Exception as exc:
                logger.warning(f"番剧：AniList 取封面失败：{exc}")
                continue
            for aid, info in got.items():
                _apply_anilist(by_id.get(aid), info, check_date=False)

    # 没带 AniList id 的（多是本季新番，数据集还没关联）：
    # 拉整季列表按 **日文原名** 匹配 —— 比逐个按标题搜准得多（AniList 的 search 认不了中文名）
    todo = [it for it in items if not it.get("image")]
    if not todo:
        return
    seasons: set = set()
    for it in todo:
        seasons.add(_season_of(int(it.get("begin_ts") or 0)))
    for year, season in sorted(seasons):
        try:
            media_list = await anilist_season(year, season)
        except Exception as exc:
            logger.warning(f"番剧：AniList 取 {year} {season} 列表失败：{exc}")
            continue
        index = {_norm_name(m.get("native")): m for m in media_list if m.get("native")}
        for it in todo:
            if it.get("image"):
                continue
            hit = index.get(_norm_name(it.get("native")))
            if hit:
                _apply_anilist(it, hit, check_date=True)


async def _fetch_bdata_calendar() -> list[dict]:
    buckets: dict[int, list] = {}
    now = time.time()
    items: list[dict] = []
    for raw in await _bdata_items():
        item = _bdata_subject(raw, now)
        if item:
            items.append(item)
    try:
        await _fill_anilist(items)
    except Exception as exc:
        logger.warning(f"番剧：AniList 补图整批失败（不影响放送表）：{exc}")
    for item in items:
        buckets.setdefault(int(item["weekday"]), []).append(item)
    if not buckets:
        raise RuntimeError(
            "bangumi-data 里没有正在播的日本番剧（可能本季数据还没更新，或 jsDelivr 取不到）"
        )
    return _pack_days(buckets)


async def bili_timeline() -> list[dict]:
    """B站番剧时间表（前后 6 天，共 13 天），每天带 `day_of_week` 与 `episodes`。"""
    async with httpx.AsyncClient(**_client_kwargs("bilibili")) as client:
        resp = await client.get(
            f"{BILI_BASE}/pgc/web/timeline", params={"types": 1, "before": 6, "after": 6}
        )
        resp.raise_for_status()
        payload = resp.json()
    if int(payload.get("code") or 0) != 0:
        raise RuntimeError(f"B站返回 {payload.get('code')}：{payload.get('message')}")
    return [d for d in (payload.get("result") or []) if isinstance(d, dict)]


async def _fetch_bili_calendar() -> list[dict]:
    """B站时间表 → 按周几分组。

    同一部番在 13 天里可能出现 2 次；以**日期靠后**那次为准（timeline 按日期升序，
    直接覆盖即可），首播日取最早出现的那天。
    """
    agg: dict[int, dict] = {}
    for day in await bili_timeline():
        weekday = _norm_weekday(day.get("day_of_week"))
        date_iso = _local_date(int(day.get("date_ts") or 0))
        for ep in day.get("episodes") or []:
            if not isinstance(ep, dict):
                continue
            item = _bili_subject(ep, weekday, date_iso)
            if not item:
                continue
            sid = int(item["subject_id"])
            old = agg.get(sid)
            if old is None:
                agg[sid] = item
                continue
            if date_iso and (not old["air_date"] or date_iso < old["air_date"]):
                old["air_date"] = date_iso
            old["weekday"] = weekday
            old["time"] = item["time"]
            old["latest"] = item["latest"] or old["latest"]
    buckets: dict[int, list] = {}
    for item in agg.values():
        buckets.setdefault(int(item["weekday"]), []).append(item)
    return _pack_days(buckets)


async def _fetch_bgm_calendar() -> list[dict]:
    async with httpx.AsyncClient(**_client_kwargs("bgm")) as client:
        resp = await client.get(f"{BGM_BASE}/calendar")
        resp.raise_for_status()
        payload = resp.json()
    raw_days = payload if isinstance(payload, list) else (payload.get("data") or [])
    days: list[dict] = []
    for index, day in enumerate(raw_days):
        if not isinstance(day, dict):
            continue
        weekday = _norm_weekday(day.get("weekday") if "weekday" in day else index + 1)
        items: list[dict] = []
        for raw in day.get("items") or []:
            if not isinstance(raw, dict):
                continue
            item = norm_subject(raw)
            if item["subject_id"] and item["title"]:
                items.append(item)
        days.append({"weekday": weekday, "label": store.weekday_label(weekday), "items": items})
    days.sort(key=lambda d: d["weekday"])
    return days


async def fetch_calendar() -> list[dict]:
    """拉「每日放送」并按周一~周日分组。失败抛异常由调用方转成页面提示。"""
    prov = provider()
    if prov == "bdata":
        return await _fetch_bdata_calendar()
    if prov == "bilibili":
        return await _fetch_bili_calendar()
    return await _fetch_bgm_calendar()


def source_name() -> str:
    """当前用的数据源（页面上显示，一眼看出数据是从哪来的）。"""
    return {
        "bdata": "bangumi-data 日本新番库（jsDelivr）",
        "bilibili": "哔哩哔哩番剧时间表",
        "bgm": "Bangumi.tv（api.bgm.tv）",
    }.get(provider(), provider())


async def calendar(force: bool = False) -> dict:
    """取每日放送：缓存新鲜就直接用，`force=True` 才真发请求。

    ★ 一天只弄一次的规矩：
      · 自动调用（force=False）—— 缓存 24 小时内有效，过期才拉。
      · 手动刷新（force=True）—— 也要过 `MIN_REFRESH_INTERVAL`（默认 6 小时）。
        被限住时**不报错**，返回 `throttled=True` + 现有缓存 + 还剩多久，
        页面把话说清楚就行，别让用户以为刷新坏了。
    """
    cached = store.calendar_cache()
    fetched_at = int(cached.get("fetched_at") or 0)

    if not force and cached.get("days") and store.calendar_fresh():
        return {
            "days": cached["days"],
            "fetched_at": fetched_at,
            "cached": True,
            "source": source_name(),
        }

    # 手动刷新：先看够不够时间
    if force and fetched_at:
        left = int(fetched_at + store.MIN_REFRESH_INTERVAL - time.time())
        if left > 0 and cached.get("days"):
            return {
                "days": cached["days"],
                "fetched_at": fetched_at,
                "cached": True,
                "throttled": True,
                "wait_minutes": max(1, (left + 59) // 60),
                "source": source_name(),
            }

    days = await fetch_calendar()
    saved = store.save_calendar(days)
    return {"days": days, "fetched_at": int(saved["fetched_at"]), "cached": False, "source": source_name()}


# ---------------- 搜索 ----------------


def _search_in_days(days: list[dict], keyword: str, limit: int) -> list[dict]:
    """在没有在线搜索的源里，直接从已拉的放送表里按名字过滤（够用了）。"""
    key = str(keyword or "").strip().lower()
    out: list[dict] = []
    seen: set = set()
    for day in days or []:
        for item in day.get("items") or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "")
            native = str(item.get("native") or "")
            if key not in title.lower() and key not in native.lower():
                continue
            sid = str(item.get("subject_id") or "")
            if sid in seen:
                continue
            seen.add(sid)
            out.append(item)
            if len(out) >= limit:
                return out
    return out


async def search(keyword: str, limit: int = 20) -> list[dict]:
    """按名字搜条目（找不到就返回空列表，页面显示「没搜到」）。"""
    keyword = str(keyword or "").strip()
    if not keyword:
        return []
    limit = max(1, min(int(limit), 50))
    prov = provider()
    if prov in ("bdata", "bilibili"):
        # 这两个源没有稳定的在线搜索接口，直接在放送表里找（没缓存就先拉一次）
        days = (store.calendar_cache().get("days") or [])
        if not days:
            days = await fetch_calendar()
        return _search_in_days(days, keyword, limit)

    params = {"keyword": keyword, "limit": limit, "type": 2}
    async with httpx.AsyncClient(**_client_kwargs(prov)) as client:
        resp = await client.get(f"{BGM_BASE}/v0/search/subjects", params=params)
        resp.raise_for_status()
        payload = resp.json()
    raw = payload.get("data") if isinstance(payload, dict) else payload
    out: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        norm = norm_subject(item)
        if norm["subject_id"] and norm["title"]:
            out.append(norm)
    return out


# ---------------- 剧集表（用来定「今天第几集」） ----------------


def _cache_item(subject_id: int) -> dict | None:
    """放送表缓存里找这个条目（bangumi-data 靠它拿首播日与更新周期）。"""
    for day in store.calendar_cache().get("days") or []:
        for item in day.get("items") or []:
            if isinstance(item, dict) and str(item.get("subject_id") or "") == str(subject_id):
                return item
    return None


async def _bdata_episodes(subject_id: int) -> list[dict]:
    """bangumi-data 没有剧集表，用「首播日 + 周期」推一张出来。

    好处是能正确处理 **双周更（P14D）** 这类非常规排期；坏处是停播一周会算错一集。
    """
    item = _cache_item(subject_id)
    if not item:
        return []
    try:
        start = datetime.strptime(str(item.get("air_date") or ""), "%Y-%m-%d").date()
    except ValueError:
        return []
    interval = max(1, int(item.get("interval") or 7))
    return [
        {
            "sort": i + 1,
            "airdate": (start + timedelta(days=i * interval)).isoformat(),
            "name": f"第{i + 1}集",
        }
        for i in range(BDATA_MAX_EPS)
    ]


async def _bili_episodes(season_id: int) -> list[dict]:
    """B站时间表里这部番出现过的每一集（最准：能看出停播与连播）。"""
    sid = store.digits(season_id)      # bili_id 是页面上手填的，容忍 `ss12345` 这种写法
    if not sid:
        return []
    out: list[dict] = []
    for day in await bili_timeline():
        date_iso = _local_date(int(day.get("date_ts") or 0))
        for ep in day.get("episodes") or []:
            if not isinstance(ep, dict) or int(ep.get("season_id") or 0) != sid:
                continue
            index = _bili_ep_index(ep.get("pub_index"))
            if index:
                out.append({"sort": index, "airdate": date_iso, "name": str(ep.get("pub_index") or "")})
    out.sort(key=lambda e: e["sort"])
    return out


async def _bgm_episodes(subject_id: int) -> list[dict]:
    """拉 bgm.tv 的剧集表（翻页直到拿完或到 MAX_PAGES）。"""
    sid = store.digits(subject_id)
    if not sid:
        return []
    out: list[dict] = []
    offset = 0
    async with httpx.AsyncClient(**_client_kwargs("bgm")) as client:
        for _ in range(MAX_PAGES):
            resp = await client.get(
                f"{BGM_BASE}/v0/episodes",
                params={"subject_id": sid, "type": 0, "limit": PAGE_SIZE, "offset": offset},
            )
            resp.raise_for_status()
            payload = resp.json()
            data = payload.get("data") if isinstance(payload, dict) else payload
            if not isinstance(data, list) or not data:
                break
            for raw in data:
                if not isinstance(raw, dict):
                    continue
                try:
                    sort = int(raw.get("sort") or 0)
                except (TypeError, ValueError):
                    continue
                out.append({
                    "sort": sort,
                    "airdate": str(raw.get("airdate") or "").strip(),
                    "name": str(raw.get("name_cn") or raw.get("name") or "").strip(),
                })
            total = int((payload.get("total") if isinstance(payload, dict) else 0) or 0)
            offset += PAGE_SIZE
            if total and offset >= total:
                break
    out.sort(key=lambda e: e["sort"])
    return out


async def fetch_episodes(subject_id: int, prov: str = "") -> list[dict]:
    prov = prov or provider()
    if prov == "bdata":
        return await _bdata_episodes(subject_id)
    if prov == "bilibili":
        return await _bili_episodes(subject_id)
    return await _bgm_episodes(subject_id)


async def episodes(subject_id: int, prov: str = "", force: bool = False) -> list[dict]:
    """带缓存的剧集表：缓存新鲜就用缓存，否则拉一次（失败返回旧缓存，不抛）。"""
    prov = prov or provider()
    key = store.eps_key(prov, subject_id)
    cached = store.eps_cache(key)
    if cached and not force:
        return cached
    try:
        eps = await fetch_episodes(subject_id, prov)
    except Exception as exc:
        logger.warning(f"番剧：剧集表拉取失败（{prov}:{subject_id}）：{exc}")
        return cached      # 拉不动就先用旧的（哪怕过期），总比算不出集数强
    if eps:
        store.save_eps(key, eps)
    return eps or cached


async def episode_of(subject_id: int, day: str, prov: str = "") -> int:
    """某部番剧在 `day`（YYYY-MM-DD）播出的是第几集；查不到返回 0。"""
    for ep in await episodes(subject_id, prov):
        if str(ep.get("airdate") or "") == str(day):
            return int(ep.get("sort") or 0)
    return 0


async def bili_episode_of(season_id, day: str) -> int:
    """用 B站时间表查某天更新到第几话（**只有当这部番在 B站有引进时才查得到**）。

    这是最准的一条路：B站会标明停播/连播，比按周期推算可靠。查不到返回 0，
    调用方再回落到推算。
    """
    sid = str(season_id or "").strip()
    if not sid:
        return 0
    try:
        return await episode_of(sid, day, "bilibili")
    except Exception as exc:
        logger.debug(f"番剧：B站时间表查集数失败（ss{sid}）：{exc}")
        return 0


def now_ts() -> int:
    return int(time.time())
