# astrbot-plugin-bangumi（番剧推送）

番剧订阅推送插件（AstrBot）。由 malu_qq_bot 的 bangumi 插件移植而来，功能保持一致。

## 功能

- **每日放送**：本周每天播什么，按周一~周日分组展示（封面 / 首播日 / 周几时刻 / 评分 / 当前话数）
- **订阅**：看中哪部点「订阅」→ 选平台实例、选群、填播出周几/几点/延迟 → 存成一条订阅；
  也支持**手动添加**与**批量订阅**（把整季新番一次性订到同一个群）
- **推送**：到「每周几的几点（+ 延迟分钟）」就往目标群发一条「《XX》第 N 集更新了」
  - **集数怎么定**：这部番在 B站有引进 → 查 B站时间表当天那集（最准，能看出停播/连播）；
    否则查本源剧集表（`airdate == 今天`）；都查不到才按首播日 + 周期推算（支持双周更 P14D）
  - 推算集数超过**总集数**视为已完结，自动跳过
  - **错过了不补推**：本周那个时刻已经过去就排到下一周
- **封面**：OneBot 走「文字 + 图片」一条消息；QQ 官方走 markdown（图带尺寸写进正文），
  封面被判「内容违规」时逐级马赛克、改用富媒体（本地马赛克图）重发 —— 不需要公网图床
- **数据源三选一**：bangumi-data（默认，经 jsDelivr）/ B站番剧时间表 / Bangumi.tv；
  bangumi-data 不带图，另用 AniList 补封面与总集数

## 安装

把本目录放到 AstrBot 的 `plugins/` 下（或通过插件市场安装），在 WebUI「插件管理」里
安装依赖后重启：`httpx`、`Pillow`（Pillow 只在「封面尺寸探测 / 马赛克降级」时用到，
缺了不影响文字推送）。

重启后在插件详情页打开 Page「番剧面板」。

## 代码结构（按功能拆分，一个文件一件事）

```
main.py                 Star 类：注册全部 Web API + 启动后台推送循环
metadata.yaml           插件元信息（name / display_name / version）
bangumi/
  paths.py              数据目录（运行数据都落插件目录下 data/，不进 git / 不进更新包）
  store.py              持久化：订阅 / 放送表缓存 / 剧集表缓存 / 模块设置
  api.py                上游数据源：bangumi-data / B站 / bgm.tv + AniList 补图
  cover.py              封面下载 + 本地缓存 + 块状马赛克降级 + 尺寸探测
  schedule.py           排期（下一次触发时刻 / 到点规则 / 按首播日推算集数）
  render.py             正文渲染（默认排版 / 自定义模板 / 封面占位行 / markdown 转换）
  mdgen.py              官方 markdown 图片行（![#宽px #高px](url)）
  sender.py             发送通道（按平台分发，走 context.send_message）
  targets.py            推送目标（平台实例列表 + OneBot 群列表）
  push.py               推送编排 + 后台循环 + 页面状态
  web.py                Web API 响应工具
  routes.py             全部路由表（main.py 统一注册）
pages/panel/            前端面板（index.html + style.css + js/core|calendar|subs|main.js）
```

## 数据落盘

全部在插件目录下的 `data/`（已 gitignore，更新插件不会被清）：

| 文件 | 内容 |
| --- | --- |
| `rules.json` | 全部订阅 |
| `calendar.json` | 每日放送缓存（默认 24 小时） |
| `eps/<key>.json` | 剧集表缓存（24 小时，文件名带源前缀） |
| `settings.json` | 数据源 + 模块总开关 |
| `covers/` | 封面缓存（原图 + 马赛克版） |

## 推送目标说明

推送目标 = **平台实例 ID + 群 ID**（统一会话 origin：`平台ID:GroupMessage:群ID`）。

- 平台实例 ID 见 WebUI「消息平台」页；
- 群 ID 是该平台群消息事件里的 session_id（aiocqhttp 的 QQ 群号可在下拉里直接选，
  其它平台手填）；
- **私聊**只有 OneBot 支持（QQ 官方机器人拿不到用户 openid）。
