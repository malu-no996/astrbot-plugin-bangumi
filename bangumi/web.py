"""Web API 响应工具。

AstrBot 插件 API 由 Dashboard 统一鉴权（面板 bridge 自动带身份），所以原插件那套
「仅本机 / token」的 `_allowed/_denied` 不再需要 —— 这里只留三个帮手。

⚠️ 响应形状约定：所有成功响应顶层**不要带 `data` 字段**（统一用 `{"ok": True, ...}`）。
原因：面板 bridge 的父页面转发响应时固定做 `r.data.data ?? r.data` —— 只要顶层有
`data` 就**只转发里层**，前端会拿不到 `ok` 而假报失败。本项目一律发扁平对象。
"""
from __future__ import annotations

from astrbot.api.web import error_response, json_response as _json_response, request  # noqa: F401


def ok(**kwargs):
    """成功响应：扁平的 `{"ok": True, ...}`。"""
    return _json_response({"ok": True, **kwargs})


def fail(message: str, status: int = 400):
    """业务失败 → 4xx/5xx JSON（前端 bridge 会 reject，message 直接可显示）。"""
    return error_response(message, status_code=status)


def soft_fail(message: str, **kwargs):
    """上游 / 环境问题导致的失败：回 200 + ok:false。

    ★ 为什么不用 400：上游连不上**不是**客户端的错，400 会让浏览器控制台飘红、
    看起来像插件坏了 —— 真正的处理是页面上把原因写清楚。
    """
    return _json_response({"ok": False, "message": message, **kwargs}, status_code=200)


def result(payload: dict):
    """已经自带完整 `ok` / `message` 的载荷原样下发（别再用 ok(**payload) 叠一层）。"""
    return _json_response(dict(payload), status_code=200)


async def body() -> dict:
    """读 JSON body，坏请求当空 dict。"""
    try:
        data = await request.json(default={})
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def query(name: str, default: str = "") -> str:
    """读 query 参数（字符串）。"""
    try:
        value = request.query.get(name, default)
    except Exception:
        return default
    return str(value if value is not None else default)
