# RouteWise 会话级 Cache-aware 路由 — 设计

**日期：** 2026-06-01
**状态：** Draft → 需 Juncheng 对「与论文的边界」拍板（见「与论文的关系」）
**作者：** RouteWise 集成讨论（Murphy + agents）
**取代：** 2026-05-29 讨论里那版全局「prefix cache tree」草案。本设计是**会话级**的，不是全局 trie。
**说明：** 本文为 `2026-06-01-routewise-session-cache-aware-routing-design.md` 的中文版，**以英文版为准**。代码、公式、字段名保持原文。

## 问题（Problem）

RouteWise 当前按冷缓存假设路由：on-demand（S_A）的 effective cost 用
`estimated_cached_input_tokens = 0`，而且 cache 项已经从 `api_request_cost_usd`
里被删掉了。对多轮 / agentic 流量，大量 input token 其实是 provider 的 prefix
cache 命中返回的，所以冷成本会**系统性地高估**一个已经为这个 session warm 过前缀
的 provider。我们希望 RouteWise 把这个 cache benefit 计入成本，从而**倾向把一个
session 留在 warm 过它的 provider 上**——作为 *cost signal*，**不是硬 pin**——并且
始终可被 latency、budget、quota、health 推翻。

## 目标（Goals）

1. 在同一 user / project / session 内，若上一轮 provider A 成功服务、且本轮与之
   共享很长 prefix，则在算 A 的 candidate cost 时计入一个**保守的** cache benefit，
   使 router 更倾向 route 回 A。
2. 一切留在现有 cost–latency LP 内：cache 只作为 S_A effective cost 的**减项**进入，
   因此它在 `α` 预算下与 latency 竞争、且可被推翻。
3. 用真值校准：用 normalized provider-returned cached input tokens 去校正估计
   （闭环），并且**先测量再启用**。

## 非目标（Non-goals）

- 不做跨 user / 跨 project 的 prefix 复用（KV-cache 复用边界不跨租户；假设它存在
  会带来误估和隐私解释成本）。
- v1 不做全局 prefix trie（见「为什么 v1 不用树」）。
- 不接 LMCache，不做 gateway response cache，不做 semantic cache。
- 不改 billing / logging 真值：持久化成本继续来自 normalized provider usage，
  **不来自** predicted cache savings。
- 不引入独立的 session-affinity 子系统，且**不启用现有的 5 分钟硬 affinity pin**
  （它绕过 cost LP，会盖掉本机制——见「affinity 来自 cost formula」）。
- 不假设「同一个 provider」一定命中——只做概率估计。

## 与论文的关系（需 Juncheng 拍板）

本机制是 **§6 生产增强，不是 paper 核心 cost-layer cache 项的重写**。paper /
simulator / real-eval 用的是 **uniform exogenous**（均匀外生）cached-token 信号、
施加给所有候选；那里 cache **不**产生 provider 粘性。本设计**刻意**用 cache 制造
per-provider 粘性。

两者的成本数学相同：`adjusted = cold − (p_in − p_cache)·expected` 代数上就是 paper
的 `p_in·(n − cached) + p_cache·cached + p_out·out`（令 `cached = expected`）。
**唯一区别是喂给 `expected_cached_tokens` 的是什么：** uniform 外生信号（paper）
vs per-`(session, provider)` 估计（本设计）。

待办：与 Juncheng 确认 (a) 这是 production-only / §6；(b) paper 的 cache-on 评测数
维持 uniform-signal 那一档，**不**由本 per-provider 机制重新得出。

## affinity 来自 cost formula（为什么没有 affinity 子系统）

provider A 一旦 warm，它的 effective cost 就下降，成本层自然偏向 A。**这本身就是
emergent 的 session affinity**，而且是更 principled 的形式——因为它仍可被
latency / budget / health 推翻、且整件事留在 LP 内。

两个注意点：

- **强度随 `α` 变化。** RouteWise 是 `min latency s.t. cost ≤ budget`，不是
  `min cost`。cache 折扣主要影响 A 是否最便宜 / 是否在预算内。低 `α`（重成本）时
  拉力强；高 `α`（重延迟）时选最快的，折扣几乎不动选择。
- **LP 会采样混合。** 即使 A 占优，LP 也可能输出两-provider 混合、并在某些轮采样
  到别家。这是 LP 本身的概率语义，v1 不应该在 cost formula 之外再加一层
  session-sticky override。

因此 v1 把 affinity 完全留在 cost formula 里。若未来要把 deterministic sampling
作为生产稳定性特性评估，应另写设计，并保持它不属于 cache-aware routing 的核心算法。

## 核心抽象（Core abstractions）

```text
SessionProviderPrefixMemory
  lookup(scope, current_blocks) -> CacheSignal
  observe(scope, selected_success_blocks, provider_usage) -> None

CacheAwareCostEstimator
  adjust(candidate_cold_cost, CacheSignal, price_delta) -> adjusted_cost
```

`scope` 至少包含：

```text
user_hash
project/org_hash
session_hash
provider_id
endpoint_id
model/profile
key_slot_id
cache_affecting_params_hash
```

v1 按 `(session, provider)` scope 存最近一次（或最近 N 次）成功请求的 block 序列
——**不用 trie**：

```text
last_blocks
prefix_token_sums
last_seen_at
```

节点**只存元数据**（hash + token 数）。**绝不**存 raw prompt、canonical
bytes、token ids、tool-schema 原文、credential。block hash 用 per-process secret
的 HMAC。

### 为什么 v1 不用树

trie 的价值在于「在很多条**分叉**序列里找最长匹配」。一旦收窄到单 session + 单
provider，相关历史本质上是一条 append-only 序列（不断变长的对话），匹配就退化成
「拿当前请求的 block 列表和上一条比」——一个数组对比，不是树。全局树要利用的跨序列
共享，正好是我们排除掉的跨 user/session 复用。per-session 的 trie 只是后续升级项，
仅当会话内分叉（编辑历史、regen、工具重排、并行子线程）被证明常见时才需要。

## 路由流程（Routing flow）

```text
1. canonicalize current request into blocks
2. enumerate RouteWise candidates
3. for each candidate: lookup memory[user/session/candidate_scope]   # provider not known in advance
4. matched_prefix_tokens   (deterministic exact block-by-block compare)
5. n_cache = min(matched_prefix_tokens, n_in)   (matched prefix == cached; no hit-probability model)
6. cache_discount = n_cache × (p_in − p_cache) of THAT candidate
7. adjusted_cost = cold_cost − cache_discount
8. router selects with adjusted_cost + existing latency/quota/health
9. on selected success: update that scope's memory with provider usage
```

算 cost 时不需要预先知道 winner：每个候选查自己的 `(session, provider)` memory，
warm 过前几轮的那个 provider entry 非空、自然更便宜。

## 成本公式（Cost formula）

命中的前缀**直接当作缓存**——就是 paper 的 on-demand effective cost，**不建模命中概率**：

```text
n_cache        = min(matched_prefix_tokens, n_in)        # 命中的前缀即缓存
cache_discount = n_cache × (p_in − p_cache)              # 各候选自己的价
adjusted_cost  = cold_cost − cache_discount              # 下限到 0，成本不为负
```

即 `c_OD = p_in·(n_in − n_cache) + p_cache·n_cache + p_out·n_out`。**刻意不要
`calibrated_hit_rate` / `confidence_discount` 这类因子**：simulator 当初把
provider-local 命中预测器删了（太激进），paper / Juncheng 的口径是「分类为命中就是
命中——不建模 provider 特定命中概率」。保守性来自 scope + 最小匹配前缀阈值，不是魔法
常数。provider 返回的 cached tokens 用来**校正 `n_cache` 这个 count**（闭环），不是
引入概率权重；上线初期想整体收一点，用 canary fraction，不要塞进每条请求的公式。

## Guardrails

仅当以下全部满足时启用 cost adjustment：same user/session、same
provider/model/key_slot、`matched_prefix_tokens ≥ threshold`（threshold 按 provider
经 allowlist 取，携带各家**最小可缓存长度**及**是否真的缓存**）、`last_seen_at`
在 TTL 内、candidate healthy、且该 attempt 是 selected 非 synthetic 的成功
（非 fallback loser / 非 failed attempt）。

provider 返回的 cached-token count 不存入 prefix memory，也绝不参与 routing cost。
后续 metrics 如果要做 calibration，应该从 route logs 派生。

key 轮换使 cache 失效：key pool 轮换 key（~5 分钟 TTL）；`key_slot_id` 一变就当作
cache 失效，**不得**用旧 key 的历史去估。**现状：`key_slot` 暂未进入 scope**。
因此 guarded cost adjustment 必须跳过走轮换 key pool 的 endpoint，直到
`key_slot` 能进入 scope。

## 实现计划

1. **Prefix memory primitives** — scope、block 序列、最长前缀比对、TTL/LRU cap。
   只单测；不接 router。
2. **Observation hook** — selected success 后写 memory（流式仅 successful finalize
   后写）。失败、取消、超时、未被选中的 attempt 不写 memory。
3. **Route-time cache signal** — 在 `prefix_cache_cost_adjustment_enabled` 后
   build blocks 一次、lookup eligible on-demand candidates、调整 effective
   cost，并 stash selected candidate 的 scope 用于 selected-success 后写
   memory。没有单独 shadow-only 模式。
4. **Metrics / report** — 按 provider/model/session bucket：`matched`、
   `expected`、`observed_cached_tokens`、adjustment_applied、route_stayed/switched、
   calibration error。

## 前置 gate（先选目标 model）

cache 折扣是 `expected × (p_in − p_cache)`。`minimax-m2.5` 当前配置的 S_A 腿是
OpenRouter→DeepInfra、`input_cache_reads: "0"`，所以 `(p_in − p_cache) = 0`、整套
是 no-op；它还是聚合层，背后的 warm node pin 不到。**先选一个直连、非聚合、有真实
cached-input 价的 provider** 当首个目标，否则实现能 ship，但观测不到任何东西。

## 验收标准（第一版）

成功标志不是「省了多少钱」，**也不是「route-stay rate 上升」**（那是循环——给了
粘性折扣，stay rate 必然上升）。改为测量：

```text
conditional on staying: observed_cached_tokens > 0 and close to predicted   # staying actually warms cache
predicted vs observed calibration error, bucketed by provider/scope          # the estimate is honest
cached-token reporting coverage is controlled
no latency / cost regression; no quota/health/fallback semantics broken
counterfactual benefit validated via the cost-adjustment small-traffic canary
```

## 待决策（Open decisions）

1. **paper 边界（Juncheng）：** 确认这是 production-only / §6，且 paper 的 cache-on
   数维持 uniform-exogenous 那一档。
2. **目标 model：** 哪个直连、有真实 cached 价的 provider 作为首个目标。
