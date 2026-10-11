"""数据层：订阅规则、每日放送缓存、剧集表缓存、模块设置。

数据落插件目录下的 `data/`（见 paths.py）：

    data/rules.json            全部订阅（一条 = 一部番剧 + 一个推送目标）
    data/calendar.json         「每日放送」缓存（默认 24 小时，页面可强制刷新）
    data/settings.json         模块设置（数据源 provider / 总开关 enabled）
    data/eps/<eps_key>.json    某部番剧的剧集表缓存（24 小时；文件名带源前缀）
    data/covers/               封面缓存（原图 + 马赛克版，见 cover.py）

为什么要缓存：bangumi-data / B站 / bgm.tv 都是公共接口，有频率限制；「每日放送」
一天也变不了几次，剧集表更是首播前就定好了。缓存住既省请求，也让页面断网时还能看。

写盘一律 **临时文件 + os.replace**（原子替换），中途崩了不会留下半个 JSON。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from . import paths

DATA_DIR = paths.DATA_DIR
RULES_FILE = DATA_DIR / "rules.json"
CALENDAR_FILE = DATA_DIR / "calendar.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
EPS_DIR = DATA_DIR / "eps"

# 缓存有效期（秒）
# ★ CALENDAR_TTL = 一天：放送表一天之内不会有变化（bangumi-data 按月更新、B站时间表
#   也就每天刷一次），自动场景下**每天最多真拉一次**，不必 6 小时刷一遍。
CALENDAR_TTL = 24 * 3600
EPS_TTL = 24 * 3600

# 手动刷新（点「刷新放送表」）的**最小间隔**。
# ★ 一次「刷新」不是打一个请求 —— bangumi-data 源要下 2~3 个月的 JSON（每个近 100KB），
#   还要去 AniList 拉若干页整季列表补封面。限到 6 小时，一天的被动拉取次数就固定了。
# ★ 拉取失败**不更新** fetched_at，所以网络抖了可以立刻重试，不会被这个间隔卡住。
MIN_REFRESH_INTERVAL = 6 * 3600

# weekday：1=周一 … 7=周日（与 bgm.tv 日历的 weekday.cn、B站的 day_of_week 一致）
WEEKDAYS = ((1, "周一"), (2, "周二"), (3, "周三"), (4, "周四"), (5, "周五"), (6, "周六"), (7, "周日"))
WEEKDAY_LABELS = dict(WEEKDAYS)

# 数据源（详见 api.py 的 source_name）：
#   bdata    = bangumi-data 日本新番库，经 jsDelivr 拿（**默认**：全、只含日本番、带播出时刻）
#   bilibili = B站番剧时间表（只含 B站引进的，但集数最准，可用来补集数）
#   bgm      = Bangumi.tv（数据最全，但国内网络常常连不上）
PROVIDERS = ("bdata", "bilibili", "bgm")
DEFAULT_PROVIDER = "bdata"
PROVIDER_LABELS = (
    ("bdata", "bangumi-data 日本新番（带播出时刻）"),
    ("bilibili", "B站番剧时间表"),
    ("bgm", "Bangumi.tv"),
)

_RULE_DEFAULTS: dict = {
    "id": "",
    "subject_id": 0,          # 条目 id（含义随 provider：bangumi-data/bgm = bgm 条目 id，B站 = season_id）
    "provider": "",           # 这条订阅来自哪个源（空 = 跟随当前设置）
    "bili_id": "",            # 对应 B站 season_id（有就能用 B站时间表补准集数，没有就按周期推算）
    "interval_days": 7,       # 几集之间隔几天（bangumi-data 的 broadcast 里有，如 P7D → 7）
    "title": "",              # 展示名（优先中文名）
    "native": "",             # 原名（日文）
    "image": "",
    "url": "",
    "air_date": "",           # 首播日 YYYY-MM-DD
    "total_episodes": 0,      # 总集数（0 = 未知）
    "weekday": 1,             # 每周几播出（1..7）
    "time": "00:00",          # 播出时刻 HH:MM（本时区；bgm.tv 只给到「周几」，时刻要自己填）
    "delay_minutes": 0,       # 播出后推迟几分钟再推（给字幕组/配信留时间）
    "platform_id": "",        # 用哪个 AstrBot 平台实例发（实例 ID，如 aiocqhttp / 280-Eous）
    "platform_name": "",
    "target_type": "group",   # group / private
    "target_id": "",          # 群 ID（群消息事件里的 session_id）或私聊用户 ID
    "target_name": "",
    "template": "",           # 留空用默认排版
    "enabled": True,
    "next_run_at": 0,
    "last_run_date": "",
    "last_episode": 0,
    "last_run_at": 0,
    "last_result": "",
    "fail_count": 0,          # 连续失败次数（推送失败退避重试用，成功即清零）
    "created_at": 0,
}

_SETTINGS_DEFAULTS: dict = {
    "provider": DEFAULT_PROVIDER,   # 数据源
    "enabled": True,                # 模块总开关（关掉后后台推送循环空转，不查数据不发消息）
    # 「订阅番剧」命令的权限档位：False = 群主 + 群管理员（默认）；True = 仅群主
    "owner_only": False,
}

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


def _new_id() -> str:
    return "bg_" + uuid.uuid4().hex[:10]


def weekday_label(weekday) -> str:
    return WEEKDAY_LABELS.get(int(weekday or 0), str(weekday or ""))


def _norm_weekday(value) -> int:
    try:
        wd = int(str(value).strip())
    except (TypeError, ValueError):
        return 1
    return wd if 1 <= wd <= 7 else 1


def _norm_time(value) -> str:
    """`HH:MM` 规范化；不合法就回落 00:00（页面另有校验，这里是最后一道）。"""
    text = str(value or "").strip()
    try:
        hh, mm = text.split(":", 1)
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError):
        return "00:00"
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return "00:00"
    return f"{h:02d}:{m:02d}"


def _norm_int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip() or default)
    except (TypeError, ValueError):
        return default


def _norm_text(value, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


# ---------------- 订阅规则 ----------------


def norm_rule(raw: dict, old: dict | None = None) -> dict:
    """把页面/接口传来的任意 dict 夹成一条合法规则。

    `old` 存在时以它为基础覆盖（编辑场景：页面只回传部分字段也不丢东西）。
    ★ `subject_id` 是唯一键：同一部番剧可以订两次（推到不同的群），所以不按它去重。
    """
    base = dict(_RULE_DEFAULTS)
    if isinstance(old, dict):
        base.update({k: v for k, v in old.items() if k in _RULE_DEFAULTS})
    src = raw if isinstance(raw, dict) else {}
    out = dict(base)
    for key in _RULE_DEFAULTS:
        if key in src and src[key] is not None:
            out[key] = src[key]
    out["subject_id"] = _norm_int(out.get("subject_id"), 0)
    out["total_episodes"] = max(0, _norm_int(out.get("total_episodes"), 0))
    out["interval_days"] = min(365, max(1, _norm_int(out.get("interval_days"), 7)))
    out["delay_minutes"] = max(0, min(_norm_int(out.get("delay_minutes"), 0), 24 * 60))
    out["weekday"] = _norm_weekday(out.get("weekday"))
    out["time"] = _norm_time(out.get("time"))
    out["target_type"] = "private" if str(out.get("target_type")) == "private" else "group"
    for key in ("title", "native", "image", "url", "platform_id", "platform_name",
                "target_id", "target_name"):
        out[key] = _norm_text(out.get(key))
    out["bili_id"] = _norm_text(out.get("bili_id"), 40)
    prov = str(out.get("provider") or "").strip().lower()[:20]
    out["provider"] = prov if prov in PROVIDERS else ""
    out["template"] = str(out.get("template") or "")[:500]
    out["enabled"] = bool(out.get("enabled", True))
    out["air_date"] = _norm_text(out.get("air_date"), 10)
    if not out["id"]:
        out["id"] = _new_id()
    if not out["created_at"]:
        out["created_at"] = int(time.time())
    return out


def load_rules() -> list[dict]:
    raw = _read_json(RULES_FILE, {"rules": []})
    rules = raw.get("rules") if isinstance(raw, dict) else raw
    if not isinstance(rules, list):
        return []
    return [norm_rule(r) for r in rules if isinstance(r, dict)]


def save_rules(rules: list[dict]) -> None:
    with _lock:
        _atomic_write(RULES_FILE, {"rules": [norm_rule(r) for r in rules]})


def get_rule(rule_id: str) -> dict | None:
    for r in load_rules():
        if str(r.get("id")) == str(rule_id):
            return r
    return None


def add_rule(raw: dict) -> dict:
    rule = norm_rule(raw)
    with _lock:
        rules = load_rules()
        rules.append(rule)
        save_rules(rules)
    return rule


def add_rules(raws: list[dict]) -> list[dict]:
    """一次加多条（**只写一次盘**）。

    批量订阅时会一次建几十条：逐条 `add_rule` 等于把这个 JSON 反复重写几十遍，
    文件还会边写边涨 —— 这里整批 append 后统一落盘。
    """
    with _lock:
        rules = load_rules()
        made = [norm_rule(r) for r in raws if isinstance(r, dict)]
        rules.extend(made)
        save_rules(rules)
    return made


def update_rule(rule_id: str, raw: dict) -> dict | None:
    """更新一条；不存在返回 None（调用方决定要不要当新增处理）。"""
    with _lock:
        rules = load_rules()
        for i, r in enumerate(rules):
            if str(r.get("id")) == str(rule_id):
                merged = norm_rule(raw, old=r)
                merged["id"] = str(rule_id)
                rules[i] = merged
                save_rules(rules)
                return merged
    return None


def set_rule_field(rule_id: str, **fields) -> dict | None:
    """只改其中几个字段（排期 / 上次结果这类后台自己写的东西走这里）。"""
    with _lock:
        rules = load_rules()
        for i, r in enumerate(rules):
            if str(r.get("id")) == str(rule_id):
                r.update(fields)
                rules[i] = norm_rule(r)
                save_rules(rules)
                return rules[i]
    return None


def delete_rules(ids) -> int:
    """一次删多条（**只写一次盘**），返回实际删掉几条。"""
    wanted = {str(i) for i in (ids or [])}
    if not wanted:
        return 0
    with _lock:
        rules = load_rules()
        left = [r for r in rules if str(r.get("id")) not in wanted]
        removed = len(rules) - len(left)
        if removed:
            save_rules(left)
        return removed


def set_rules_enabled(ids, enabled: bool) -> int:
    """批量启停，返回改了几条。"""
    wanted = {str(i) for i in (ids or [])}
    if not wanted:
        return 0
    with _lock:
        rules = load_rules()
        hit = 0
        for r in rules:
            if str(r.get("id")) in wanted:
                r["enabled"] = bool(enabled)
                hit += 1
        if hit:
            save_rules(rules)
        return hit


def delete_rule(rule_id: str) -> bool:
    with _lock:
        rules = load_rules()
        left = [r for r in rules if str(r.get("id")) != str(rule_id)]
        if len(left) == len(rules):
            return False
        save_rules(left)
        return True


# ---------------- 模块设置 ----------------


def load_settings() -> dict:
    """模块设置：`provider`（用哪个数据源）+ `enabled`（总开关）。

    ★ 为什么默认不是 bgm.tv：bgm.tv 在不少网络下**根本连不上**（DNS 能解析、
    TCP 直接超时）—— 本机实测就是 `api.bgm.tv` 超时，而 gitee / baidu / jsDelivr
    都正常。所以默认走 **bangumi-data**（经 jsDelivr 拿，全量日本新番，还带播出
    时刻），B站时间表与 bgm.tv 留成可选项。
    """
    data = _read_json(SETTINGS_FILE, {})
    if not isinstance(data, dict):
        data = {}
    prov = str(data.get("provider") or "").strip().lower()[:20]
    enabled = data.get("enabled")
    owner_only = data.get("owner_only")
    return {
        "provider": prov if prov in PROVIDERS else DEFAULT_PROVIDER,
        "enabled": True if enabled is None else bool(enabled),
        "owner_only": False if owner_only is None else bool(owner_only),
    }


def save_settings(**fields) -> dict:
    """只写**明确传进来的**字段（没传的保持原值）。"""
    cur = load_settings()
    if "provider" in fields:
        prov = str(fields["provider"] or "").strip().lower()[:20]
        cur["provider"] = prov if prov in PROVIDERS else DEFAULT_PROVIDER
    if "enabled" in fields:
        cur["enabled"] = bool(fields["enabled"])
    if "owner_only" in fields:
        cur["owner_only"] = bool(fields["owner_only"])
    with _lock:
        _atomic_write(SETTINGS_FILE, cur)
    return cur


def digits(value) -> int:
    """从任意值里抽出**第一段数字**（`"ss12345"` → 12345，`"d593565"` → 593565）。

    抽不出数字返回 0。★ 这是给「id 类字段」兜底的：页面上的 bili_id 是手填的，
    用户填成 `ss12345` 这种带前缀的写法很正常，而这里**绝不能抛异常** ——
    它处在整条推送的必经之路上。
    """
    m = re.search(r"\d+", str(value or ""))
    return int(m.group(0)) if m else 0


def eps_key(provider: str, subject_id) -> str:
    """剧集表缓存的文件名。

    ★ 必须带源前缀：bangumi-data 用的是 bgm 条目 id，B站用的是 season_id，
    两个编号空间会撞车（同一个数字指的不是同一部番）。
    """
    mark = {"bdata": "d", "bilibili": "b", "bgm": "g"}.get(
        str(provider or "").strip().lower(), "x"
    )
    return f"{mark}{digits(subject_id)}"


# ---------------- 每日放送缓存 ----------------


def calendar_cache() -> dict:
    """`{"fetched_at": int, "days": [...]}`；没有缓存时 days 为空。"""
    data = _read_json(CALENDAR_FILE, {})
    if not isinstance(data, dict) or not isinstance(data.get("days"), list):
        return {"fetched_at": 0, "days": []}
    return data


def calendar_fresh(ttl: int = CALENDAR_TTL) -> bool:
    return int(calendar_cache().get("fetched_at") or 0) + ttl >= int(time.time())


def save_calendar(days: list[dict], fetched_at: int | None = None) -> dict:
    data = {"fetched_at": int(fetched_at or time.time()), "days": days}
    with _lock:
        _atomic_write(CALENDAR_FILE, data)
    return data


# ---------------- 剧集表缓存 ----------------


def _eps_file(key) -> Path:
    """key 形如 `d412008`（前缀见 `eps_key`），不同源的编号不会互相覆盖。"""
    return EPS_DIR / f"{str(key).strip() or 'x0'}.json"


def eps_cache(key: str, ttl: int = EPS_TTL) -> list[dict]:
    """某部番剧的剧集表（过期或没有 = 空列表，调用方去重新拉）。

    ★ 参数是 `eps_key()` 给的**字符串 key**（形如 `d412008`），不是条目 id。
    """
    data = _read_json(_eps_file(key), {})
    if not isinstance(data, dict):
        return []
    if int(data.get("fetched_at") or 0) + ttl < int(time.time()):
        return []
    eps = data.get("eps")
    return [e for e in eps if isinstance(e, dict)] if isinstance(eps, list) else []


def save_eps(key: str, eps: list[dict], fetched_at: int | None = None) -> None:
    """★ 这里的 `key` 同样是 `eps_key()` 的字符串 key。

    （历史教训：曾经写成 `int(subject_id)`，key 一带源前缀就抛 ValueError，
    于是所有 bangumi-data 源的推送全部写盘失败。key 一律按字符串处理。）
    """
    with _lock:
        _atomic_write(_eps_file(key), {
            "fetched_at": int(fetched_at or time.time()),
            "key": str(key),
            "eps": eps,
        })
