# 后端 `alert_slack` 迁移设计（roadmap 步骤 4）

> 状态：**待决策** —— 三个缺口中 G1 需要产品/安全拍板，G2/G3 已给出推荐实现方案
> 日期：2026-07-28
> 前置：status-monitor 三类告警已迁移（roadmap 步骤 2/3 完成）
> 关联：[roadmap](../plans/2026-07-25-alert-control-plane-roadmap.zh.md)、[C3c 验证档案](../../reviews/2026-07-27-c3c-staging-validation.md)

---

## 0. 为什么这一步不是"照着 status-monitor 抄一遍"

roadmap 里把后端迁移估成 2–3 人周，依据是"11 个调用点收敛于单一函数 `alert_slack`，只需一处适配"。**这个判断只对了一半。**

接线确实是一处。但实际调查发现三个 status-monitor 迁移时不存在的缺口：

| # | 缺口 | 性质 |
|---|---|---|
| **G1** | 长驻 Python 进程无法持有 CI 签发的能力凭证 | 🔴 架构，需拍板 |
| **G2** | 11 个告警中 10 个在契约里没有对应类型 | 🟡 设计，已有推荐 |
| **G3** | 后端告警当前携带 IP 与 API key 前缀，**契约明确拒绝** | 🟡 产品取舍，已有推荐 |

status-monitor 三个都不存在：它是 Cloudflare Worker（有 Service Binding，无凭证生命周期问题）、只有模型 id 一种上下文、且数据本来就干净。

---

## G1 — 身份与传输：长驻进程持不住短期凭证

### 事实

| 生产者 | 认证方式 | 长驻可用？ |
|---|---|---|
| lifecycle workflow（合成） | OIDC → 能力令牌，在 CI 作业内用完即弃 | ✅ 作业短暂 |
| status-monitor Worker | Service Binding，**无令牌** | ✅ Cloudflare 内部能力 |
| **后端网关（Python，长驻）** | **无可用路径** | ❌ |

约束链：

- 能力令牌 TTL 上限 **3600 秒**（`runtime-config.ts` 的 `parseInteger(..., 60, 60*60)` 硬校验）
- 签发唯一入口是 `/v1/deployments/attest`，要求 **GitHub OIDC 令牌**，只在 CI 作业内可得
- 网关是 Docker Compose 长驻进程，运行周期以天/周计

即：部署时 CI 能签发一个 ≤1 小时的令牌，但网关活得比它长得多，且无法自己续签。

### 选项

**A. 中继 Worker（推荐）**
新建一个极小的 Cloudflare Worker，持有 Service Binding，像 status-monitor 一样被 CI 认证；网关用普通 bearer secret 投递给它。

- ✅ 完全复用已验证、已加固的 Service Binding 路径，无新的令牌生命周期
- ✅ 架构上已有先例 —— 现有 Codex oncall relay 就是这个形状，后端本来就在往 relay 投递
- ✅ 中继侧可加限流、来源约束，且它自己的身份仍由 CI 认证
- ⚠️ 新增一个可部署组件；网关→中继这一跳的 secret 成为新的弱点

**B. 部署期签发的长期令牌**
放宽 TTL 上限，CI 在部署时签发并注入容器环境变量。

- ✅ 无新组件
- ⚠️ TTL 必须长于部署间隔（天/周级），短期令牌这条安全属性直接消失
- ✅ 注册表逐请求复核仍在 —— retire 即刻吊销，这条不丢

**C. 令牌续签端点**
网关用未过期令牌换新令牌。

- ⚠️ 泄露的令牌可无限续签，实际等价于长期令牌，却多了一个端点和一套逻辑
- ❌ 不推荐

### 推荐：A

理由不是"更安全"——A 和 B 的弱点其实同构（都存在一个长期 secret，泄露后可伪造告警）。**理由是可运维性**：A 的 secret 是普通 Docker secret，可独立轮换、可加限流；B 的令牌轮换必须走重新部署 + 重新认证。且 A 复用的是今天已经被真实流量验证过的路径。

**这一条需要拍板** —— 它决定是否新建一个可部署组件。

---

## G2 — 契约形状：10 个告警无对应类型

### 事实

契约今天只放行三种类型：`provider_circuit_open`、`model_unavailable`、`monitoring_cycle_failure`。11 个后端告警里只有 `Provider circuit opened` 对得上。

### 推荐：按**形状**而非按名字，新增 2 个类型

| 新类型 | 覆盖的后端告警 | context 字段（全部有界、闭集） |
|---|---|---|
| `metric_threshold_breach` | Failed-request rate、5xx rate、p95 latency、Auth failure spike、RouteWise prefix-cache leak、Tracked-task failure rate、User cost overrun、Provider hourly spend | `metric`（闭集枚举）、`observed`、`threshold`、`window_sec`、`scope`（可选，如 provider 名） |
| `dependency_unavailable` | Database disconnected ×2 | `dependency`（闭集枚举：`operational_store` / `log_store`）、`kind` |

按形状归并的好处：8 个"某指标越过阈值"的告警共享一套渲染和一套校验，新增告警只加枚举值而不加类型。**不推荐**为每个告警各开一个类型（契约面爆炸），也**不推荐**开一个 `gateway_alert` 万能类型（context 只能松散校验，等于放弃分类型验证这条核心安全属性）。

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

### 推荐：结构化计数替代明文值

| 现在 | 迁移后 | on-call 损失什么 |
|---|---|---|
| `top_ips: "1.2.3.4 (12), ..."` | `distinct_sources: 2`、`top_source_share: 0.8` | 不能直接从 Slack 复制 IP 去封禁 |
| `top_key_prefixes: "hyi-abc (7)"` | `distinct_principals: 2` | 同上 |
| `rate: "12.3% (45 of 366...)"` | `observed: 0.123`、`window_sec: 300` | 无（渲染层重新格式化即可，且更可比） |
| `user_id: 12345` | `scope: "user"` + 不带 id | 需要去 dashboard 查是谁 |

明文值仍然完整保留在**网关自己的日志和 admin dashboard** 里 —— 它们只是不再跨越到 Slack 这个信任边界之外。这与契约既有立场一致（模型告警里的原始 upstream 错误、cycle 告警里的原始错误文本，都是同样的理由被排除的）。

**如果 on-call 强烈需要 Slack 里直接可见 IP**，那是一个明确的反对意见，应当在这里推翻我的推荐，并接受相应的契约松绑成本 —— 但不应当靠"迁移时悄悄放宽 `untrustedString`"来实现。

---

## 建议的执行顺序

| # | 步骤 | 依赖 |
|---|---|---|
| 4.1 | 契约扩展：新增 `metric_threshold_breach` + `dependency_unavailable`（TS 侧类型/校验/渲染 + 测试），dormant | G2 推荐 |
| 4.2 | Python 生产者适配层：`control_plane_contract.py` 补 builder，`alert_slack` 的 11 个调用点改为结构化字段，dormant（不接传输） | G2 + G3 推荐 |
| 4.3 | 传输接线 | 🔴 **G1 拍板后才能开始** |
| 4.4 | snooze 能力对齐（roadmap 步骤 5） | 4.3 |

4.1 与 4.2 不依赖 G1，可立即开工。
