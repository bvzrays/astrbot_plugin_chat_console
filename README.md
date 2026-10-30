<p align="center">
  <img src="logo.png" alt="聊天控制台 Logo" width="128" />
</p>

<h1 align="center">聊天控制台</h1>

<p align="center">
  <a href="https://github.com/bvzrays/astrbot_plugin_chat_console"><img src="https://img.shields.io/badge/版本-1.1.0-3978c5" alt="版本 1.1.0"></a>
  <img src="https://img.shields.io/badge/AstrBot-%E2%89%A54.24.2-20a67a" alt="AstrBot 4.24.2+">
  <img src="https://img.shields.io/badge/默认主题-深色-303846" alt="默认深色模式">
</p>

<p align="center">在 AstrBot WebUI 中集中查看群聊、处理未读与 @ 提醒，并向支持主动发送的适配器回复消息。</p>

## 效果示意

<p align="center">
  <img src="assets/chat-console-demo.png" alt="聊天控制台深色模式效果示意" width="100%">
</p>

## 功能

- **多适配器会话：** 以适配器自定义识别名区分实例，群聊列表和窗口状态分别保存。
- **未读与 @ 提醒：** 点进窗口立即标记已读；右上角提供“查看 N 条未读消息”和“有人@我”跳转按钮。首次进入窗口定位最新消息，不因未读或 @ 自动跳到旧位置。
- **会话管理：** 右键会话可置顶、修改备注、标为已读或删除会话。删除采用软隐藏，收到新消息后会话自动恢复。
- **会话隔离提醒：** 检测到 AstrBot 开启群聊会话隔离时显示提示，可在配置中开启同群会话合并。
- **消息收发：** 使用有界发送队列、按会话顺序锁和发送超时，避免慢适配器拖住其他会话；发送失败会提示具体适配器实例和连接检查方向。
- **OneBot 资料：** OneBot v11（包括 NapCat、SnowLuna）可读取群名、成员昵称/群名片、身份、等级、头衔等资料；支持时展示群头像和成员头像。不同实现返回的扩展字段可能不同，缺失字段会自动隐藏。
- **深色模式：** 默认深色，可在 WebUI 配置默认主题，也可在页面右上角切换。页面不依赖 localStorage，沙盒页面也能正常工作。
- **SQLite 存储：** 消息记录写入 SQLite，首次启动会自动导入旧版 `messages.json`；运行数据不会进入仓库。

## 安装

将插件目录放入 AstrBot 的 `data/plugins/`，或通过 AstrBot WebUI 的插件管理安装。启用插件后，在插件详情页打开“聊天控制台”。

- AstrBot：4.24.2 或更新版本
- 额外依赖：无

## 使用

选择左侧适配器和群聊即可查看消息。切换适配器或重新打开页面时，插件会恢复上次选择的适配器和会话窗口。右键会话可执行管理操作；右键 OneBot 群成员头像可查看适配器返回的资料。

发送前，插件会检查适配器是否声明支持主动发送。OneBot 反向 WebSocket 必须同时提供事件和 API 通道；如果当前实例未连接 API，页面会明确提示实例名称，请检查连接并确认没有选中另一个同群适配器实例。

## 配置

在 **WebUI → 插件管理 → 聊天控制台 → 配置** 中调整：

| 配置项 | 默认值 | 作用 |
| --- | ---: | --- |
| 默认使用深色模式 | `true` | 设置首次打开页面的主题。 |
| 合并隔离会话 | `false` | 按“适配器 + 群号”合并会话隔离产生的同群窗口。 |
| 读取 OneBot 资料 | `true` | 开启群资料和成员资料读取。 |
| 消息刷新间隔（秒） | `3` | 控制当前聊天窗口的增量刷新频率，范围 2–60。 |
| 会话列表刷新间隔（秒） | `10` | 控制群聊列表刷新频率，范围 5–120。 |
| 单条消息发送超时（秒） | `30` | 限制单条发送占用 worker 的时间，范围 5–120。 |
| 本地保留的聊天消息条数 | `5000` | SQLite 保留的最近消息数量，范围 500–50000。 |

主题按钮的选择仅作用于当前浏览器页面；配置项决定没有页面偏好时的默认主题。

## 数据与隐私

插件只记录启用后 AstrBot 实际收到的消息，不会下载平台历史聊天记录。聊天记录保存在 `data/plugin_data/astrbot_plugin_chat_console/chat_console.db`；旧版 `messages.json` 成功导入后会改名为 `messages.json.migrated`。数据库、迁移备份、群备注、置顶设置和窗口状态都属于运行数据，不应提交到代码仓库。

## TODO：适配情况

勾选表示已进行适配测试；未勾选表示尚未完成验证。目前已测试 QQ 官方机器人和 OneBot v11（NapCat、SnowLuna）。

- [ ] 钉钉 (DingTalk)
- [ ] Telegram
- [ ] Discord
- [x] QQ 官方机器人 (WebSocket，推荐)
- [ ] QQ 官方机器人 (Webhook)
- [x] OneBot v11 (NapCat / SnowLuna)
- [ ] 飞书 (Lark)
- [ ] 个人微信
- [ ] 企业微信 (智能机器人)
- [ ] 企业微信应用 & 微信客服
- [ ] 微信公众平台

## 更新日志

详见 [CHANGELOG.md](CHANGELOG.md)。

## 交流答疑

欢迎加入交流群：**870543663**。
