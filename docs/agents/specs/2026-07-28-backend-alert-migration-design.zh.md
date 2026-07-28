# 后端 `alert_slack` 迁移设计（roadmap 步骤 4）

> 状态：**部分定案（2026-07-28）** —— G1/G2/G3 已定；**G4（恢复语义）为新发现，阻塞 4.2** —— G1 采纳方案 B（部署期签发长期令牌，不新增组件）；G2 采纳按形状归并的 2 个新类型；G3 采纳类型化字段（保住 IP/subject 的可操作性，舍弃凭证前缀与无界文本）。4.1 起可依序实施。
> 日期：2026-07-28
> 前置：status-monitor 三类告警已迁移（roadmap 步骤 2/3 完成）
> 关联：[roadmap](../plans/2026-07-25-alert-control-plane-roadmap.zh.md)、[C3c 验证档案](../../reviews/2026-07-27-c3c-staging-validation.md)

---

## 0. 为什么这一步不是"照着 status-monitor 抄一遍"

roadmap 里把后端迁移估成 2–3 人周，依据是"12 个调用点收敛于单一函数 `alert_slack`，只需一处适配"。**这个判断只对了一半。**

接线确实是一处。但实际调查发现三个 status-monitor 迁移时不存在的缺口：

| # | 缺口 | 性质 |
|---|---|---|
| **G1** | 长驻 Python 进程无法持有 CI 签发的短期能力凭证（注：**今天后端根本无凭证**，故这是「往前走多远」而非「能否做」） | ✅ 已定 = B（部署期令牌），**但见 G1 附加条件** |
| **G2** | 12 个调用点中 11 个在契约里没有对应类型 | 🟡 设计，已有推荐 |
| **G3** | 后端告警当前携带 IP 与 API key 前缀，**契约明确拒绝** | ✅ 已定：类型化字段保住操作能力，仅舍弃凭证材料与无界文本 |

status-monitor 三个都不存在：它是 Cloudflare Worker（有 Service Binding，无凭证生命周期问题）、只有模型 id 一种上下文、且数据本来就干净。

---

## G1 — 身份与传输：长驻进程持不住短期凭证

### 先校准基线：今天后端根本没有「凭证」

把这条描述成「架构缺口」是不准确的框架。准确的说法是**要在既有基线上往前走多远**。

后端今天的认证方式（生产在跑）：

```python
# alerts.py：两条出口，两个永不过期的静态 secret
relay_token = os.environ.get("CODEX_ONCALL_RELAY_TOKEN", "")   # relay 侧仅 compare_digest 比对
webhook_url = os.environ.get("SLACK_ALERTS_WEBHOOK_URL", "")   # URL 本身即凭证
```

拿到环境变量的任何人都能以网关名义伪造任意告警。无身份认证、无过期、无吊销（除非改 secret 重新部署）。

### 事实

| 生产者 | 认证方式 | 长驻可用？ |
|---|---|---|
| 后端网关（**今天**） | 静态 bearer / webhook URL，永不过期 | ✅ 但无任何保证 |
| lifecycle workflow（合成） | OIDC → 能力令牌，CI 作业内用完即弃 | ✅ 作业短暂 |
| status-monitor Worker | Service Binding，**无令牌** | ✅ Cloudflare 内部能力 |

约束链（为什么不能直接照搬 status-monitor）：

- Service Binding 是 **Cloudflare Worker 之间**的内部能力，非 Worker 的 Python 进程无法使用 —— 平台事实，不是设计缺陷
- 能力令牌 TTL 上限 **3600 秒**（`runtime-config.ts` 硬校验）
- 签发唯一入口 `/v1/deployments/attest` 要求 **GitHub OIDC 令牌**，只在 CI 作业内可得
- 网关是 Docker Compose 长驻进程，运行周期以天/周计

### 选项（按相对今天的改善排序）

| 方案 | 相对今天 | 代价 |
|---|---|---|
| **B. 部署期签发长期令牌**（推荐） | **明显更好**：令牌绑定到一个 CI 认证过的确切部署，registry retire 即刻吊销 | 需放宽 TTL 上限；短期令牌这条属性消失 |
| A. 中继 Worker | 略好：中继侧可加限流与来源约束 | 新增可部署组件；**网关→中继一跳仍是静态 secret** |
| C. 令牌续签端点 | 不改善：泄露令牌可无限续签，实际等价长期令牌 | 多一个端点和一套逻辑。❌ |

### 推荐：B —— ✅ **已采纳（2026-07-28）**

关键判断：**A 与 B 的弱点是同构的** —— A 里网关→中继那一跳一样是永不过期的静态 secret，泄露一样能伪造告警。A 的真实增量只有「中继侧可加限流/来源约束」与「轮换 secret 不必重走认证流程」。

这个增量不值一个新的可部署组件。B 不新增组件、实现最简单，且相对今天已是实打实的提升：从「永久静态 secret、无法定位来源」变成「绑定到确切部署版本、可在 registry 即刻吊销」。

**若选 A，理由应当是明确想要中继层做限流/来源约束**，而非安全强度 —— 那一项两者基本相同。

### ✅ 已采纳 B（2026-07-28），但带一个**强制前置条件**

评审指出：方案 B 的令牌长期有效，而 `deploy-status-monitor.yml` / `deploy-staging.yml`
**从不 retire 被取代的部署版本**（这正是评审 P2-E 记录过、至今未修的问题）。短期令牌
时这只是隐患；长期令牌时它变成实质漏洞 —— 旧部署的令牌永远有效，等于每次部署都
新增一把永不失效的钥匙。

**因此 B 的前置条件：部署流程必须在激活新版本后 retire 旧版本。** 未实现前不得启用
长期令牌。这一条不是可选优化。

---

## G4 — 后端告警**从不发送 resolved**（本轮调查最严重的发现）

### 事实

`grep 'status="resolved"'` 在整个后端返回**零结果**。12 个调用点全部只发 firing，
靠 `cooldown_sec` 抑制重复，从不宣告恢复。

### 后果

控制平面是**有生命周期的 incident 模型**：firing 开启 incident，resolved 关闭它。
把只发 firing 的生产者直接接上去，结果是：

- 每个后端告警开一个 incident，**永远不关**
- 频道里堆积永久 open 的 thread，`PRINCIPAL_ACTIVE_LIMIT` 配额被逐步吃满
- 配额耗尽后新 incident 被 suppressed —— **真实故障不再告警**

这明确劣于今天（今天只是一条条独立消息，没有"永久未解决"这个概念）。

### 结论

**迁移必须同时为这 12 个告警补上恢复语义**，否则不能接线。这是 4.2 的一部分，
不是可以推后的优化。工作量因此高于最初估计。

每个告警的恢复条件需逐个定义（例如失败率跌回阈值以下持续 N 个窗口）。这是本设计
未覆盖的部分，需在 4.2 前补齐。

---

## G2 — 契约形状：10 个告警无对应类型

### 事实

契约今天只放行三种类型：`provider_circuit_open`、`model_unavailable`、`monitoring_cycle_failure`。12 个后端调用点里只有 `Provider circuit opened` 对得上。

### 推荐：按**形状**而非按名字，新增 2 个类型 —— ✅ **已采纳（2026-07-28）**

| 新类型 | 覆盖的后端告警 | context 字段（全部有界、闭集） |
|---|---|---|
| `metric_threshold_breach` | 9 个：Failed-request rate（含 DB-query detector 变体）、5xx rate、p95 latency、Auth failure spike、RouteWise prefix-cache leak、Tracked-task failure rate、User cost overrun、Provider hourly spend | `metric`（闭集枚举）、`observed`、`threshold`、`window_sec`、`scope`（可选，如 provider 名） |
| `dependency_unavailable` | Database disconnected ×2 | `dependency`（闭集枚举）、`backend`（有界标签）、`reason`（闭集枚举，替代后端今天的自由文本 `error`） |

按形状归并的好处：9 个"某指标越过阈值"的告警共享一套渲染和一套校验，新增告警只加枚举值而不加类型。**不推荐**为每个告警各开一个类型（契约面爆炸），也**不推荐**开一个 `gateway_alert` 万能类型（context 只能松散校验，等于放弃分类型验证这条核心安全属性）。

---

## G3 — 数据最小化冲突：后端告警携带契约拒绝的数据

### 事实（这是本次调查最重要的发现）

后端告警的 context 当前包含：

```python
# alert_rules.py Auth failure spike
"top_ips": "1.2.3.4 (12), 5.6.7.8 (3)"          # IP 地址
"top_key_prefixes": "hyi-abc (7), hyi-def (2)"   # API key 前缀

# alert_rules.py Failed-request rate
"rate": "12.3% (45 of 366 requests, last 300s)"  # 预格式化自由文本
"top_paths": "/v1/chat/completions (12), ..."     # 预格式化自由文本

# alert_rules.py User cost overrun
"user_id": ...                                     # 用户标识
```

而 `untrustedString`（`validation.ts:161`）对每个 context 文本字段执行：

- `SECRET_PATTERNS` —— 含 `\bhyi-[A-Za-z0-9_-]{20,}\b`，**直接命中 API key 前缀**
- `containsIpAddress` + `EMAIL_RE` —— **直接命中 top_ips**
- `PROMPT_INJECTION_PATTERNS`

**结论：这些告警按当前形态提交会被规范校验器拒绝。** 迁移不是适配，是一次数据最小化改造。

### 产品后果（需要知情）

on-call 今天能在"Auth failure spike"告警里直接看到攻击来源 IP 和被试的 key 前缀 —— 这对立即封禁是有用的。迁移后这些**不会出现在 Slack 里**。

### 决定（2026-07-28 修订）：保留可操作数据，但改为**类型化字段**

初版决定是「全部改为计数」，并接受 on-call 失去从 Slack 直接封禁 IP 的能力。
**该决定已被推翻** —— 目标「效果至少不输之前」意味着不接受可操作性倒退。

核实过：`_format_message` 把每个 context 键值**原样**拼进 Slack 消息，所以
on-call 今天确实看得到 IP / key 前缀 / user id。改成纯计数就是实打实的降级。

修订后的做法 —— 区分「承载操作的值」与「顺带的自由文本」：

| 现在 | 迁移后 | 是否损失能力 |
|---|---|---|
| `top_ips: "1.2.3.4 (12), ..."` | `source_addresses: ["1.2.3.4", ...]` —— **类型化 IP 列表**，每项必须解析为合法 IP，上限 5 条 | ❌ 不损失，封禁工作流原样保留 |
| `user_id` / `provider` / `task_name` | `subject: "4711"` + `scope: "user"` —— 有界标识符（`IDENTIFIER_RE`），非自由文本 | ❌ 不损失 |
| `rate: "12.3% (45 of 366...)"` | `observed` / `threshold` / `window_sec` / `sample_count` | ❌ 不损失，渲染层可格式化得更好且更可比 |
| `top_key_prefixes: "hyi-abc (7)"` | `distinct_sources: N` | ⚠️ **有意损失** —— 凭证材料，且封禁按地址进行不需要它 |
| `top_paths` / `top_status_codes` | 无 | ⚠️ **有意损失** —— 无界自由文本；分诊信息在 dashboard |

关键点：**类型化字段不等于「放宽校验器」**。`source_addresses` 的每一项必须解析为
合法 IP，任何非 IP 内容直接拒绝 —— 它无法像旧 context 那样夹带任意文本。相对今天
（自由文本可承载任何东西），这是**更强**的姿态，同时保住了操作能力。

两处有意损失已明确标注，不冒充零倒退。已实现于 #1072。

## 建议的执行顺序

| # | 步骤 | 依赖 |
|---|---|---|
| 4.0 | **为 12 个告警定义恢复条件**（G4）；部署流程实现 retire 旧版本（G1 前置） | 🔴 新增，阻塞 4.2/4.3 |
| 4.1 | 契约扩展：新增 `metric_threshold_breach` + `dependency_unavailable`（TS 侧类型/校验/渲染 + 测试），dormant | G2 推荐 |
| 4.2 | Python 生产者适配层：`control_plane_contract.py` 补 builder，`alert_slack` 的 12 个调用点改为结构化字段，dormant（不接传输） | G2 + G3 推荐 |
| 4.3 | 传输接线 | ✅ G1 已定 = B（部署期令牌，无新组件） |
| 4.4 | snooze 能力对齐（roadmap 步骤 5） | 4.3 |

4.1 已完成（#1072）。**4.2 之前必须先做 4.0** —— G4（恢复语义缺失）会让迁移后的
效果劣于今天，G1 的 retire 前置未做则长期令牌不可启用。

### 修订后的工作量判断

最初估 2–3 人周，依据是「一处适配」。实际至少需再加：为 12 个告警逐个定义并实现
恢复条件（G4）、部署流程的 retire 自动化（G1 前置）、以及 snooze 能力对齐
（roadmap 步骤 5，同属「不得倒退」范畴）。**修正估算：4–6 人周。**
