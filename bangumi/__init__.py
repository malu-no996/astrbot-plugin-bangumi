"""番剧推送插件后端包。

按功能拆成独立模块（每个模块一件事）：

    paths      数据目录（运行数据都落插件目录下 data/，不进 git / 不进更新包）
    store      持久化：订阅规则 rules.json / 放送表缓存 / 剧集表缓存 / 模块设置
    api        上游数据源：bangumi-data（经 jsDelivr）/ B站番剧时间表 / Bangumi.tv，
               另用 AniList 给 bangumi-data 补封面与总集数
    cover      封面：下载 + 本地缓存 + 块状马赛克降级；尺寸探测
    card       横屏卡片：左封面 + 右「中文名 / 原名 / 放送时间」拼成一张图（推送发它）
    schedule   排期：下一次触发时刻 / 到点规则 / 按首播日推算集数 / 按群重排
    render     正文渲染：默认排版 / 自定义模板 / 封面占位行 / 官方 markdown 转换
    mdgen      官方 markdown 图片行（`![#宽px #高px](url)`）
    sender     发送通道：统一会话（platform_id + 群/私聊 + id）→ context.send_message，
               发横屏卡片，按平台类型分「QQ 官方富媒体」/「其它平台 正文+图片」
    targets    推送目标：AstrBot 平台实例列表（含 OneBot 的群列表）
    push       推送编排：定集数 → 渲染 → 发送 → 排期推进；后台循环；页面状态
    bulk       批量订阅：放送表 → 一批规则 → 落盘 → 排期（面板浮层与「订阅番剧」命令共用）
    group_subs 群订阅：哪个群订了番剧推送 / 谁订的 / 还开着吗（「订阅数据」页数据源 + 推送闸门）
    commands   群内命令：「订阅番剧」/「取消订阅番剧」（权限、群内角色、回执）
    web        Web API 响应工具（json_response / fail / body）
    routes     全部 Web API 路由表（main.py 统一注册）
"""
