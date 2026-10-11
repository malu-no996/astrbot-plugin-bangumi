"""astrbot-plugin-bangumi —— 番剧推送插件（AstrBot）。

由 malu_qq_bot 的 bangumi 插件移植，功能保持不变：

- **每日放送**：本周每天播什么（bangumi-data / B站时间表 / Bangumi.tv 三源可选，
  默认 bangumi-data 经 jsDelivr 取，另用 AniList 补封面与总集数），看中哪部点「订阅」
  → 选平台实例、选群、填播出周几/几点/延迟 → 存成一条订阅
- **我的订阅**：订阅列表（启停 / 改 / 删 / 立即推送 / 预览 / 批量启停删除）+
  批量订阅（把整季新番一次订到同一个群）+ 自定义消息模板
- **群内命令**：群里发「订阅番剧」= 登记本群订阅 + 把本季正在播的番剧全部订到本群
  （群主 / 群管理员可用，可在面板切成仅群主）；「取消订阅番剧」反过来清掉本群的全部订阅
- **订阅数据**：哪些群订了、订了几部番、谁订的，逐群启停 / 删除，
  以及单独给某个群加 / 减某部番剧
- **推送**：到「每周几的几点（+延迟分钟）」就往指定群发一条「《XX》第 N 集更新了」；
  **集数优先取剧集表里 airdate=今天 的那条**（B站有引进就查 B站时间表，最准），
  查不到才按首播日推算（7 天一集，支持双周更）；推算集数超过总集数视为已完结、自动跳过
- **封面**：OneBot 走「文字 + 图片」；QQ 官方走 markdown（图带尺寸写进正文），
  封面被判「内容违规」时逐级马赛克、改用富媒体重发（本地马赛克图，不需公网）

数据落插件目录下的 `data/`（订阅 / 放送表缓存 / 剧集表缓存 / 封面缓存 / 群订阅 / 设置）。

模块按功能拆分（见 bangumi/ 包）：store / api / cover / schedule / render / mdgen /
sender / targets / push / bulk / group_subs / commands / routes / web / paths，各自一件事。
"""
import asyncio

from loguru import logger

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from . import bangumi  # noqa: F401  —— 导入包即建好 data/ 目录与各模块
from .bangumi import commands, push, routes, sender

PLUGIN_NAME = "astrbot_plugin_bangumi"

# 页面 bridge 的 endpoint 不带插件名前缀；这里注册的路由必须带。
PREFIX = f"/{PLUGIN_NAME}"


class BangumiPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 发送通道与路由都要用 Context（平台实例列表 / 主动发送）
        sender.bind(context)
        routes.bind(context)
        self._tasks: list[asyncio.Task] = []
        self._register_apis()

    # ==================================================================
    # 生命周期
    # ==================================================================

    async def initialize(self):
        """启动后台推送循环（到点就往目标群推一条）。"""
        push.start()
        logger.info("番剧推送：插件已加载")

    async def terminate(self):
        push.stop()
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()

    # ==================================================================
    # 群内命令
    # ==================================================================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def bangumi_cmd_listener(self, event: AstrMessageEvent):
        """群里的「订阅番剧 / 取消订阅番剧」命令。

        几个刻意的取舍
        --------------
        - 用 `GROUP_MESSAGE` 而不是 `ALL`：命令只可能在群里发，注册 ALL 会让
          **私聊**消息也白过一遍（还带上会话自动命名之类的副作用）。
        - **没命中时什么都不做**（直接 return）：本插件的 filter 对每一条群消息都
          通过，一旦抢了不属于自己的消息，聊天和别的插件就再也收不到了。
        - 命中后 `event.stop_event()`：不然 AstrBot 还会把这条命令当成普通聊天
          交给 LLM（取决于平台配置，可能额外回一条）。
          ⚠️ 这里只 set `is_wake`、不 set `is_at_or_wake_command`（见
          `core/pipeline/waking_check/stage.py`），所以**不会**导致机器人乱接话。
        - 不做「模块总开关」拦截：关着总开关还发命令时，回执里会提醒一句
          （静默不响应最容易被当成「命令坏了」）。
        """
        try:
            text = await commands.handle(event)
        except Exception as exc:  # noqa: BLE001 —— 监听器自己的异常绝不能打断事件流
            logger.exception("番剧推送：命令处理异常")
            text = f"命令执行出错：{type(exc).__name__}: {exc}"
        if text is None:
            return                       # 不是本插件的命令 → 放行
        if text:
            # 原样发送：格式交给平台（官方机器人按 markdown 渲染，OneBot 走纯文本）
            yield event.plain_result(text)
        event.stop_event()

    # ==================================================================
    # Web API
    # ==================================================================

    def _register_apis(self) -> None:
        reg = self.context.register_web_api
        for suffix, handler, methods, desc in routes.ROUTES:
            reg(f"{PREFIX}{suffix}", handler, methods, desc)


# AstrBot 会自动实例化插件类（metadata.yaml 提供名称/版本等元信息）。
_ = bangumi
