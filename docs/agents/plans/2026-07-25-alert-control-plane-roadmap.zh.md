# Unified Alert Control Plane — 收口与迁移 Roadmap

> 状态：**v3 — P0 修复已提 PR（#1042 + closeout PR），待 PM 确认后转为 GitHub Roadmap Issue**
> 撰写日期：2026-07-25（v3：2026-07-27 更新执行状态与产品分层）
> 评审基线：`dev@ebe6e813`（含 #1032 / #1033 / #1034）
> 关联设计文档：
> - [统一告警控制平面目标设计](../specs/2026-07-20-unified-alert-control-plane-target-design.zh.md)
> - [NotificationSink 抽象](../specs/2026-07-21-alert-control-plane-notification-sink-abstraction.zh.md)

---

## 0. 一句话现状

**新架构已完成并成功迁移了第一个真实告警类型；不是所有 Slack 告警都迁完。**

产品目标（终态体验）：**一次故障 = 一个 Slack thread** —— 帖子原地更新持续时长与次数、恢复挂在同一 thread、自带部署出处、渠道可插拔。

### 剩余工作按「用户看不看得见」分两类

**🟢 用户看得见的（这些才是产品体验）**

| 缺什么 | 现在的后果 |
|---|---|
| storm / cycle / 后端 11 个告警未迁移 | 同一个频道两种体验：小故障是 incident thread，大故障和后端告警还是裸文本 |
| 生产未上线 | 新体验只在 staging，线上全是老样子 |
| Codex 分析未移植 | thread 里没有根因回复 |
| ack / 静默 / 转派没做 | on-call 只能看不能操作（当前 13 个 PR 完全未覆盖，属新需求） |

**⚪ 用户看不见的（地基，不做会出事，做完用户也感觉不到）**

身份注册、回滚安全（P3-H）、可观测性、legacy 清理。

### 状态表

| 部分 | 状态 |
|---|---|
| Control Plane 状态机、去重、线程、重试 | ✅ 已完成 |
| SlackSink | ✅ 已完成 |
| staging 单模型上下线告警 | ✅ 已迁移（**仅当同时故障 ≤ `ALERT_STORM_THRESHOLD`=5**） |
| **P0 收口修复** | 🟡 已提 PR：#1042（3 项 P0 + Codex 两条）与 closeout PR（P1-B/P2-G/health/README），待合并 |
| status-monitor storm / cycle 告警 | ❌ 未迁移，**且契约里没有对应类型 —— 卡 D1 决策** |
| 后端全部 `alert_slack` 告警（11 个调用点） | ❌ 未迁移，但已有 dormant Python 契约 |
| 全局 snooze（管理员暂停告警） | ⚠️ 新链路无对应能力 —— 迁移即功能回归 |
| 旧 Codex 自动分析回复 | ❌ 新链路未实现（`dispatch_analysis` 返回 `analysis_not_enabled`） |
| production 上线 | ❌ 未开始（代码中**无任何可发 production 的路径**） |
| 删除旧 relay / webhook / secrets | ❌ 未完成 |
| 企业微信等其他 Sink | 抽象已就位，未实现 |
| 真实端到端验证记录 | ❌ 无 —— C3c 签收的硬前置 |

### 为什么不能一次性全切

这些告警来自不同进程，拥有各自的状态、重试与恢复逻辑。必须逐类切换并保证**同一告警始终只有一个 writer**，否则会重复发 Slack，或者恢复消息丢失。先选 `model_unavailable` 是为了用真实流量证明架构。

### 工作量的真实形状

按**调用点**算 14 个（status-monitor 3 + 后端 11），按**迁移工作量**远小于此：

- 后端 11 个调用点全部收敛于**单一函数** `alert_slack`（`apps/backend/serving/observability/alerts.py:310`），只需在这一处做适配
- 该函数已有 `status: firing|resolved` 与 `dedupe_key`，天然对应控制平面的 status 与 fingerprint
- `apps/backend/serving/observability/control_plane_contract.py`（257 行）已存在：规范契约的 Python 镜像，dormant、仅被自身测试引用

**真正需要新设计的只有 storm / cycle**（契约只放行 `model_unavailable` 与 `provider_circuit_open` 两种类型，没有"聚合故障"概念）。

---

## 1. 已完成的资产（不需要重做）

评审逐条推演，未发现设计缺陷：

- **incident 生命周期状态机** —— 开启 / 重复 / 恢复 / 恢复后再故障（新 generation）/ 配额抑制
- **投递保证** —— 结果不确定时回读 Slack 验证真实效果；claim / lease / fencing 成立；重放不重复发帖
- **单写者、单 parent、不可变 `DeliveryRef`**
- **平台无关性** —— 状态机不知道 Slack 存在；加企业微信只动渠道白名单、运行时配置、执行器装配三处
- **身份与出处** —— GitHub OIDC 全字段校验，告警必须来自 CI 注册过的确切部署版本
- **fail-closed 配置** —— Binding 缺失、配置错误、版本不匹配均真正拒绝

---

## 2. 执行顺序（8 步）

| # | 步骤 | 状态 / 前置 | 粗略工作量 |
|---|---|---|---|
| 1 | 暂停 production rollout | ✅ 已执行 | — |
| 2 | 🔴 收口 P0 + 补真实 staging 验证 | 🟡 代码已提 PR（见 §2.1）；**真实 staging 验证仍缺** | 1–2 人周 |
| 3 | staging 迁移 storm / cycle | 🔴 **卡 D1 决策** | 1–4 人周（取决于 D1） |
| 4 | staging 迁移后端 `alert_slack`（单点适配） | 前置：步骤 2 | 2–3 人周 |
| 5 | 补齐 snooze 等能力对齐 | 前置：步骤 4 | 0.5–1 人周 |
| 6 | 决定并实现新链路 Codex 分析 | 前置：D6 决策 | 2–4 人周 |
| 7 | 部署 production + 观察期 | 前置：步骤 3–6 | 2–3 人周 |
| 8 | 确认零调用方后删除 relay / webhook / secrets | 前置：步骤 7 观察期 | 0.5 人周 |

> 合计 8.5–18.5 人周 ≈ 单人全职 2–4.5 个月；区间宽窄主要取决于 D1 与 D6。
> 顺序上的两个纪律：**P0 未收口不迁移下一个 producer**（无 P2-D 的可观测性，后续每次迁移都是盲调）；**生产迁移之前不得删除 legacy**（staging 迁完时生产后端仍 100% 在 legacy 上，webhook fallback 是共享代码分支）。

### 2.1 P0 收口明细（步骤 2）

| ID | 问题 | 状态 |
|---|---|---|
| P1-A | 空目录 → 批量误关闭全部 incident + 清历史（40 分钟盲区） | ✅ PR #1042（含 Codex 补充：空周期 state 保留、空白 id 拒绝） |
| P2-D | Control Plane 拒绝完全无声 | ✅ PR #1042 日志层 + closeout PR `/api/health` `pendingControlPlaneTransitions` 字段 |
| P2-C | role-specific RPC 未锁 `alert_type` | ✅ PR #1042 |
| P1-B | storm 无 legacy destination 时完全静默 | ✅ closeout PR（undeliverable 日志，每周期重复直到可投递；storm 归属本身仍等 D1） |
| P2-G | `postSlack` 原始异常可能把 webhook URL 写进日志 | ✅ closeout PR（redact，镜像 `postCodexAlert`） |
| 文档漂移 | README 4 处失实声明 | ✅ closeout PR |
| 真实验证 | 一次真实探测故障 firing → resolved 走 binding（专用验证目录条目即可 —— 判据是"真实部署的完整链路"，非条目的商业用途），仓库内留存消息全文 + permalink（证据力 ≥ 截图，可实时复核） | ✅ **2026-07-27 完成** —— `docs/reviews/2026-07-27-c3c-staging-validation.md`（incident_4a6fc4e5，12:41Z fire → 14:01Z 同 thread 恢复）。等价性裁决见该文档 Adjudication 节；下一次自然发生的真实模型故障应链接至该文档作为补强证据（不重新阻塞） |

已知边界（刻意不在本批）：目录**部分塌缩**（网关只返回大目录一小部分）仍读作大量 departure，需收缩阈值（调参决策）；P2-F（pending 表不变量违例杀掉全部 per-model 告警且不自愈，当前推演不出可达路径）；P3-I（`event_receipts` 无清理 + O(n) snapshot）。

---

## 3. 两个功能回归风险（迁移必做项）

### 3.1 全局 snooze 会丢失

`alert_slack` 每次发送查 `alert_snooze.py` 的 `is_snoozed()`（管理员"全局暂停告警到某时刻"）。**新链路无对应物** —— 后端迁移并删 legacy 后，"闭嘴"按钮消失。列为步骤 5 必做。

相关差异：后端去重是**进程内**的（模块级 dict），控制平面是持久化全局去重。是改进，但迁移后告警量与节奏会变，建议灰度观察。

### 3.2 Codex 分析是逐 producer 的回归

旧分析回复发进**旧链路的 thread**。producer 迁移的那一刻即失去分析，直到新链路实现。最需要根因分析的恰是后端那批（provider circuit、5xx rate）。它也不是一个函数——`apps/backend/serving/oncall/`（1195 行/10 文件）+ `codex-oncall.yml` 是完整子系统，步骤 6 实质是移植。取舍见 D6。

---

## 4. 需要 PM 拍板的决策点

| ID | 决策 | 选项与价签 | 影响 |
|---|---|---|---|
| **D1** | 一次挂 20 个模型，on-call 想看到什么？ | (a) 1 个聚合帖 + 展开列表：**2–4 人周**（新 alert type + 新状态机段）<br>(b) 20 个独立 thread：**~1 人周**（契约已支持，停止折叠即可；代价是大故障刷屏）<br>(c) 按 provider 聚合：**3–4 人周** | 🔴 **阻塞步骤 3**。cycle 告警无论选哪个都要新类型（+1–2 人周） |
| **D2** | "原地更新、不重复响铃"，on-call 真的想要吗？ | 当前设计不响 | 影响 `update_parent` 策略 |
| **D6** | Codex 分析在后端迁移之前还是之后？ | (a) 之前：无回归窗口，推迟迁移 2–4 周<br>(b) 之后：接受"最有价值的告警恰好没分析"窗口<br>(c) 之后，但依赖分析的 producer 排最后迁 | 决定步骤 4/6 顺序 |
| D3 | 生产上线时间窗口与审批路径 | — | 步骤 7 排期 |
| D4 | 要不要企业微信 / 第二渠道？何时？ | 现在加成本最低 | 独立于主线 |
| D5 | ack / 静默 / 转派 是否本季度立项？ | — | 决定是否预留 incident 状态字段 |

---

## 5. 两条终点线（建议分开立项）

### 🏁 近端：「统一告警管道」交付完成 = 步骤 1–8

- [ ] staging 与 prod 的 Slack 里再也找不到旧格式裸文本告警
- [ ] 老 `SLACK_WEBHOOK_URL` / `SLACK_ALERTS_WEBHOOK_URL` / relay secrets 已删除，删除后经真实故障验证
- [ ] 任何一次告警未发出都有可见信号（health 字段 + 日志），不静默
- [ ] 全局 snooze 已在新链路对齐
- [ ] 回滚是"退回旧路"而非"卡死"，经演练验证（P3-H）
- [ ] `docs/reviews/` 有端到端验证档案：故障 → 帖子 → 恢复的仓库内消息全文 + permalink（或截图），staging（✅ 2026-07-27）+ prod 各一份

### 🏁 远端：「好用的告警产品」（新需求面）

- [ ] on-call 能在帖子上 ack / 静默 / 转派，且真的生效
- [ ] 有页面能回答"现在有哪些告警卡住了"
- [ ] 帖子自动附带根因线索
- [ ] 至少接入第二个通知渠道，证明可插拔性

---

## 6. 生产上线的额外前置（步骤 7 内）

| 项 | 说明 |
|---|---|
| 生产 identity policy | 代码中**无任何可发 `environment: "production"` 的路径**：环境/trusted metadata 硬编码 staging，运行时配置硬编码 `refs/heads/dev` / `staging` / `workflow_dispatch`，workflow-ref 正则要求 `@refs/heads/dev` |
| 生产运行模式 | `CONTROL_PLANE_MODE` 无 production 档位 |
| 🔴 回滚安全（P3-H） | incident 活跃期间转 dormant（或配置打错字）会把排队中的 `post_parent`/`post_recovery` 变成**终态**而非延后，并级联终结依赖动作；且无运维面可列出 `manual_reconciliation_required`。回滚是安全叙事的底牌，必须先修 |
| 部署时序（P2-E） | deploy 先于 attest；attest 失败时新版本已在跑 cron 但永不在 registry → 全部提交被拒。workflow 从不 retire 被取代版本（README 写明的 C3b 退出条件未实现） |
| Slack 回读验证 | 对生产 workspace / channel 重跑 readback gate |
| 配额取值 | `PRINCIPAL_ACTIVE_LIMIT` 对生产 incident 量级未验证 |

---

## 附录 A — 旧 Slack writer 完整清单（收口依据）

### A.1 status-monitor-worker（Cloudflare Worker）

| 路径 | 源码 | 状态 |
|---|---|---|
| 单模型 `model_unavailable` | `alerts.ts` `deliverModelAlert` | ✅ 已迁移（≤ storm 阈值） |
| storm 汇总 | `alerts.ts` `modelsDownEvent` / `modelsRecoveredEvent` | ❌ legacy |
| cycle 网关级 | `alerts.ts` `runCycleAlert` | ❌ legacy |

投递出口：`postCodexAlert`（relay）→ `postSlack`（webhook fallback）

### A.2 后端 `alert_slack` —— 单一收敛点，11 个调用点

定义：`apps/backend/serving/observability/alerts.py:310`

| 模块 | 行 | 告警 | 严重度 |
|---|---|---|---|
| `routing/endpoint_health.py` | 251 | Provider circuit opened | ERROR |
| `serving/observability/alert_rules.py` | 147 | Failed-request rate exceeded | ERROR |
| ″ | 201 | 5xx rate exceeded | ERROR |
| ″ | 253 | p95 latency exceeded for provider `{provider}` | WARN |
| ″ | 301 | Auth failure spike | WARN |
| ″ | 363 | RouteWise pending prefix-cache entries leaking | WARN |
| ″ | 415 | Tracked-task failure rate exceeded for `{task_name}` | ERROR |
| ″ | 451 | User cost overrun | WARN |
| ″ | 489 | Provider hourly spend exceeded budget for `{provider}` | WARN |
| `serving/servers/routers/health.py` | 87 | Database disconnected（operational_store） | CRITICAL |
| ″ | 99 | Database disconnected（log_store） | CRITICAL |
| `serving/admin/failed_request_alerter.py` | 250 | Failed request | — |

**迁移接口适配点**：`alert_slack(severity, title, context, *, dedupe_key, cooldown_sec, status)`
- `dedupe_key` → fingerprint；`status` → status；`severity` → severity（CRITICAL/ERROR/WARN/INFO 映射）
- ⚠️ `cooldown_sec` 与进程内去重语义需重定义（§3.1）
- ⚠️ `is_snoozed()` 无对应物（§3.1）

### A.3 oncall relay + Codex 分析子系统

`apps/backend/serving/oncall/`（1195 行 / 10 文件）+ `.github/workflows/codex-oncall.yml`。核心：`store.py`(293) 自有状态、`service.py`(243)、`gha.py`(222) 把分析回复进原 thread、`dispatcher.py`(71) `repository_dispatch` 触发工作流。**完整子系统，不是一个函数。**

### A.4 待清理的 secret / 环境变量

| 变量 | 位置 |
|---|---|
| `SLACK_ALERTS_WEBHOOK_URL` | 后端（优先级高于下一个） |
| `SLACK_WEBHOOK_URL` | 后端 + status-monitor Worker |
| `CODEX_ONCALL_RELAY_URL` / `CODEX_ONCALL_RELAY_TOKEN` | 后端 + status-monitor Worker |

---

## 附录 B — 现状自查

```bash
curl -s https://<monitor-host>/api/health | jq .pendingControlPlaneTransitions
```
（closeout PR 合并后可用。）持续 >0 跨周期（~20 分钟）= Control Plane 持续拒绝，个体模型告警静默停摆。等价 D1 查询：

```bash
npx wrangler d1 execute freeinference-monitor --remote --command "SELECT key FROM meta WHERE key LIKE 'alert_delivery_pending:v1:%'"
```

```bash
npx wrangler d1 execute freeinference-monitor --remote --command "SELECT key, value FROM meta WHERE key LIKE 'alert_delivery_owner:v1:%'"
```
有 `control-plane` 行 = 新路径至少接管过一次真实故障。

```bash
npx wrangler secret list --name freeinference-monitor
```
`SLACK_WEBHOOK_URL` 必须仍在 —— storm 与 cycle 目前只走它。

**最直接**：翻 staging Slack 频道，有真实目录模型（非 runner 本地伪造的 `synthetic-control-plane-gate-*` id）的 incident 帖子 = 真实链路已通。该判据区分的是"runner 本地合成 RPC"与"部署链路真实穿越"，与条目的商业用途无关 —— 2026-07-27 的 `staging-alert-validation` incident 属于后者。

---

## 附录 C — 缺陷 ID 对照

`P1-A` / `P2-C` 等标签来自 2026-07-25 累计 correctness/security review。修复落点：#1042（P1-A/P2-C/P2-D 日志层 + Codex 两条）、closeout PR（P1-B/P2-G/P2-D health 层/README）。转 Issue 时建议各自开子 Issue。
