# 后端告警 Webhook 通知渠道设计

日期：2026-10-20

状态：已实现；staging 通知链路已验收

实现基于现有后端告警出口和数据库配置注册表。

在后端告警的发送处包一层很薄的渠道抽象，让同一条告警除了 Slack 还能发到企业微信群机器人。
**消息内容和现在的 Slack 消息一致**，告警规则、阈值、去重、冷却、静默、恢复补发全部不变。

## 1 范围

包含：

- 发送渠道接口，以及 Slack、企业微信两个实现。
- 企微 Webhook 配置项。
- DB 失败请求检测器的启用条件从“配置了 Slack”放宽为“配置了任一渠道”（Slack 与企微二选一）。
- 企微 Webhook 凭证的日志脱敏。

不包含：消息内容或格式改版、排查链接、`@` 负责人、按严重级别路由、同时向多个渠道发送、
跨副本去重、发送队列、`status-monitor` 告警控制平面、独立服务探测。

## 2 现状

所有后端告警最终经过 `observability/alerts.py`：

```text
alert_on_transition → alert_slack（去重／冷却／静默）→ _format_message → _post_to_slack
```

- `_format_message()` 生成 Slack mrkdwn 文本：标题、时间、环境、告警详情、服务器信息。
- `_post_to_slack()` 发送 `{"text": message}`，HTTP 2xx 视为成功，失败计入
  `alert_delivery_failures_total()`，日志通过 `secret_urls.posting_to()` 脱敏。
- Webhook 来自 `SLACK_ALERTS_WEBHOOK_URL`，为空时回退 `SLACK_WEBHOOK_URL`。
- `bootstrap.py` 仅在 `SLACK_WEBHOOK_URL` 非空时注册 DB 失败请求检测器。

## 3 设计

```text
alert_slack（不变：去重／冷却／静默／恢复顺序）
        ↓
  configured_sinks()  # 返回 0 或 1 个
        ├── SlackSink   → Slack Incoming Webhook
        └── WeComSink   → 企微群机器人 Webhook
```

### 3.1 渠道接口

```python
class AlertSink(Protocol):
    name: str

    def render(self, severity, title, context, status) -> str: ...
    async def post(self, message: str, *, dedupe_key: str) -> bool: ...
```

两个实现共用 `_format_message()` 的内容和行序，只替换标记语法和转义：

| | Slack | 企微 |
|---|---|---|
| 请求体 | `{"text": msg}` | `{"msgtype": "markdown", "markdown": {"content": msg}}` |
| 粗体／斜体 | `*x*`／`_x_` | `**x**`／不加斜体 |
| 动态文本转义 | 现有 `escape_slack_text` | 中和 `<`、`>`（防提及）和 `[`、`]`（防 Markdown 链接） |
| 成功判据 | HTTP 2xx | HTTP 2xx 且响应 JSON 中 `errcode == 0` |

实现上把 `_format_message()` 参数化为“粗体函数＋转义函数”，Slack 传入现有行为，
保证 Slack 输出逐字节不变。

企微 `markdown.content` 上限 4096 字节，超长时按 UTF-8 边界截断并追加“…”。
同一机器人每分钟最多 20 条，超限返回非零 `errcode`，按普通发送失败处理。

### 3.2 渠道选择

一个部署只选择一个渠道，配了哪个 Webhook 就发哪个，不新增渠道开关：

| 配置 | 发送到 |
|---|---|
| 只有 Slack URL（现有部署） | Slack，消息与告警逻辑不变；Webhook 配置修改需重启 |
| 只有 `WECOM_ALERTS_WEBHOOK_URL` | 企微 |
| 两者都有 | 配置错误：`Settings.broken_rules()` 报错，不发送 |
| 都没有 | 不发，与现在一致 |

Slack 和企微同时有效视为配置错误：启动校验拒绝该配置，管理端写入也会被拒绝；
`configured_sinks()` 最多返回一个 sink，`alert_slack()` 只向选中的渠道发送。发送失败
沿用现有计数和日志，保留下一次评估时的失败重试，以及有次数和时限约束的恢复补发。

### 3.3 改动点

| 文件 | 改动 |
|---|---|
| `observability/alerts.py` | 新增 `AlertSink`、`SlackSink`、`WeComSink`、`configured_sinks()`；`alert_slack()` 改为选择单个渠道发送。函数名和签名保留 |
| `config/settings.py`、`config/app_config_registry.py` | 新增 `WECOM_ALERTS_WEBHOOK_URL`，标记为 secret，与 Slack Webhook 互斥；`SLACK_ALERTS_WEBHOOK_URL`、`SLACK_WEBHOOK_URL`、`WECOM_ALERTS_WEBHOOK_URL` 统一标记 `restart_required` |
| `servers/bootstrap.py` | DB 检测器注册条件改为 `SLACK_WEBHOOK_URL` 或 `WECOM_ALERTS_WEBHOOK_URL` 非空。只配 `SLACK_ALERTS_WEBHOOK_URL` 的旧部署行为不变 |
| `admin/failed_request_alerter.py` | 去掉构造参数里对 Slack URL 的依赖（投递已经走 `alert_slack`）；`post_slack_alert` 兼容 helper 保持原样 |
| `utils/secret_urls.py` | `scrub()` 额外替换 URL 的查询串，企微的 `?key=` 不出现在日志中 |

Slack 的两个 Webhook 配置和企微 Webhook 统一要求重启后生效。DB 失败请求检测器只在
启动时注册；统一生效时机也保证渠道切换时不会因新旧配置分批生效而中断通知。

切换渠道时，在同一次管理端保存中清空旧渠道的全部 Webhook 配置（Slack 包括两个配置名），
并设置新渠道。发生变化的配置键进入 `pending_restart`；重启前，`config_value()` 和
`Settings` 继续使用启动时的配置，`configured_sinks()` 仍只返回旧渠道。重启后，新配置
整体生效，只返回新渠道。Slack → 企微和企微 → Slack 都遵循这一流程。

渠道数量少，第一版直接放在 `alerts.py`，不新建子包。后续渠道变多再拆。

### 3.4 管理台配置

本节由 [2026-10-21 管理台通知配置设计](2026-10-21-admin-alert-notifications-design.zh.md)
更新：`Admin → Configuration → Alerts` 使用单渠道下拉框，只展示所选渠道的 Webhook，
并提供测试通知按钮。前端在一次保存中清空旧渠道的全部配置并写入新渠道，复用现有
配置校验和重启机制。Webhook 仍为只写 secret，读取配置时不返回明文。

启用规则引擎仍需设置 `ALERTS_ENABLED`；验收环境应显式设置 `DEPLOYMENT_ENV`。
切换渠道保存空值，不删除数据库行，避免重新启用环境变量中的旧 Webhook。

## 4 测试

- 只配 Slack 时，消息内容与请求体和改动前一致（快照对比）。
- 企微请求体正确；`errcode=0` 成功，非零、非 JSON、非 2xx、超时均失败并计数。
- 企微消息超 4096 字节时截断；`<@...>` 不被渲染为提及，`[label](url)` 不被渲染为链接。
- Slack、企微同时配置：`Settings` 规则报错，管理端写入被拒绝，`configured_sinks()`
  不返回双渠道，`alert_slack()` 不发送。
- Slack 的两个 Webhook 配置和企微 Webhook 均标记 `restart_required`。
- 双向切换测试覆盖 Slack → 企微和企微 → Slack：一次保存清空旧渠道并设置新渠道后，
  新旧配置键均进入 `pending_restart`；重启前旧渠道仍唯一有效，重启后只有新渠道有效。
- DB 检测器：只配企微时注册；只配 `SLACK_ALERTS_WEBHOOK_URL` 时仍不注册。
- 日志和异常中不出现企微 `key`；Slack 脱敏不退化。
- 现有告警、去重、静默、恢复测试全部通过。

## 5 上线

1. 源码 MR：完成上述改动和测试。
2. 测试部署：在 Admin → Configuration → Alerts 中设置测试群的
   `WECOM_ALERTS_WEBHOOK_URL`、启用 `ALERTS_ENABLED`，并明确设置 `DEPLOYMENT_ENV`。
   清空两个 Slack Webhook 配置后保存并重启后端。若使用环境变量注入 Webhook，
   需确认没有数据库配置覆盖它。
3. staging 验证：确认能出网访问企微，触发一条测试告警，检查群消息、冷却和恢复。
   生产部署与机器人配置由各发行版独立审批和验收。

回退：清空 `WECOM_ALERTS_WEBHOOK_URL` 并重启即可停止企微通知；代码默认行为与现在一致。

## 参考

- [企微群机器人消息推送](https://developer.work.weixin.qq.com/document/path/91770)
- [企微接收告警限制](https://cloud.tencent.com/document/product/248/50413)
