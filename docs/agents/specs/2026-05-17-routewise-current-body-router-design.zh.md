# RouteWise 当前主路由实现 - 设计文档

**日期：** 2026-05-17
**状态：** Draft -> ready for plan
**作者：** RouteWise 集成讨论

## 问题

FreeInference 需要一个符合当前 RouteWise paper 和 simulator 语义的生产实现。
仓库里现有的 `apps/backend/routing/routewise/` 代码不是这次工作的目标语义，可以把它当作可替换的脚手架。

本 PR 的语义来源是当前 RouteWise paper/simulator：

- 每个可行 provider，不管是 API、quota，还是 concurrency，都先被转换成统一的 effective cost。
- 主路由在所有可行 provider 上解一个 cost-budgeted mean-TTFT LP。
- quota 里的 PD 逻辑仍然存在，但只作为 quota effective cost 的 shadow-price 曲线，不再作为顶层路由策略。
- output length prediction 是单请求 cost estimation 的输入，不再是 `PD` / `LA-PD` 这种顶层策略名。
- `L/U` 是 workload 级别的 API-equivalent request cost 分位数；如果某个
  RouteWise route 没有真实 API provider，可以使用 reference API price 作为
  价格尺子。
- hedging 的目标语义是 probability-targeted、multi-checkpoint、in-flight；但真实 backup dispatch 不在本 PR 范围内。

本 PR 会把新的 body router 接到已有的 `router: routewise` 策略名上。真实 hedging 留给后续 PR。

## 目标

1. 围绕当前 RouteWise 语义实现生产 `routewise` router：统一 effective cost，并在所有可行 provider 上解 cost-budgeted mean-TTFT LP。
2. 以 paper/simulator 语义作为唯一 RouteWise 目标。
3. 支持两种价格参照：真实 P_O/on-demand provider，或无 on-demand route 下配置的 reference API price。
4. 增加 bucket-mean output length predictor，用于 route-time cost estimation。
5. 扩展 latency profiling，让所有 provider category 都能进 LP，不只 on-demand provider。
6. 保持现有 request execution、fallback、request logging 和 adapter 语义。
7. 输出足够的 RouteWise decision metadata，方便和 simulator / real-eval 对齐排查。

## 非目标

- 不做真实 backup dispatch、stream cancellation 或 probability-targeted hedging。
- 不解决多 backend worker 下的分布式 quota / concurrency state；当前 prod/staging
  部署是单 backend worker，因此这不是本 PR 的上线 gate。
- 不做复杂 cache-locality predictor；第一版 route-time cache estimate 保守处理。
- 不做完整 admin UI。
- 不重写 `completions.py` 或 serving protocol。
- 不把 simulator 或 `experiments/real_evaluation` 模块直接 import 到生产代码里。

## 当前集成点

相关文件：

| 文件 | 当前职责 |
|---|---|
| `config/models.yaml` | 模型 routes、pricing、provider metadata。 |
| `apps/backend/serving/servers/registry.py` | 构造 adapters，并把 route-level `provider_type` 传进 `ModelConfig`。 |
| `apps/backend/routing/model_router_registry.py` | 按 model 选择 router，并 attach `FixedRouter`。 |
| `apps/backend/routing/routewise/router.py` | 新的 current RouteWise router 实现目标。 |
| `apps/backend/routing/routewise/lp_solver.py` | 替换为 cost-budgeted mean-TTFT LP。 |
| `apps/backend/routing/routewise/predictor.py` | 替换或绕过为 bucket-mean output predictor。 |
| `apps/backend/routing/routewise/latency.py` | rolling provider latency profiles 和 SWRR sampler。 |
| `apps/backend/serving/admin/provider_quotas.py` | 现有 provider quota dashboard fetchers；RouteWise S_Q 应复用它获取 provider-side quota truth。 |
| `apps/backend/serving/storage/database.py` | `api_logs` schema，包含 model、provider、tokens、cache、TTFT、cost 字段。 |
| `apps/backend/serving/storage/utils.py` | 当前 cache-aware request cost 公式。 |

## 方案结构

围绕当前 RouteWise 语义整理 `apps/backend/routing/routewise/`：

```text
apps/backend/routing/routewise/
  __init__.py
  config.py                 # 当前 RouteWise 参数和默认值
  candidates.py             # adapter -> ProviderCandidate
  output_predictor.py       # bucket-mean output token predictor
  envelope.py               # L/U bootstrap 和 online estimator
  effective_cost.py         # API/quota/concurrency effective cost
  lp.py                     # cost-budgeted mean-TTFT LP
  router.py                 # 当前 RouteWiseRouter
```

策略名继续使用 `routewise`：

```yaml
models:
  - id: minimax-m2.5
    router: routewise
    router_params:
      alpha: 0.75
      slo_ms: 3000
      envelope:
        bootstrap_window_hours: 168
        lower_percentile: 10
        upper_percentile: 90
      output_predictor:
        type: bucket_mean
      reference_api_price:
        prompt: "1.2"
        completion: "4.0"
```

如果某个 model 需要关闭 RouteWise，operator 可以把它切到 `router: fixed`。本设计里没有第二个 RouteWise 策略。

## 核心概念

### Output Length Prediction

output length prediction 估计当前请求未来会产生多少 completion tokens。单位是 tokens。

第一版 predictor：

```text
key = (model_id, log2_bucket(prompt_tokens))

prediction fallback order:
  1. bucket mean for (model_id, bucket)
  2. model-level mean
  3. global mean
  4. configured cold-start default
```

如果请求带了 `max_tokens` / `max_completion_tokens`，route-time prediction 需要 clamp：

```text
predicted_output_tokens = min(prediction, max_tokens)
```

请求完成后，用真实 `completion_tokens` 更新 predictor。

### Reference API Price

RouteWise 需要一个 API-equivalent price scale 来估算每个 request 的 opportunity cost：

```text
v_t = prompt_tokens * prompt_price + predicted_output_tokens * completion_price
```

这个价格尺子有两种来源：

1. 如果 route 中存在真实 S_A API provider，使用当前 feasible API candidates 中的 cheapest API-equivalent cost。
2. 如果 route 是 subscription-only，也就是只有 S_Q / S_C provider，没有真实 S_A provider，则必须配置 reference API price，或显式使用 model-level `pricing` 作为 reference。

subscription-only mode 中，reference API price **不参与真实 routing**。它只用于：

- 计算 request value / API-equivalent request cost。
- 更新 `L/U` envelope。
- 给 S_Q shadow price 提供同一把 USD 尺子。

如果一个 RouteWise route 没有 S_A provider，也没有 reference API price，不能启用 S_Q 的经济决策；最多只能退化成简单 capacity scheduler，这不是本 PR 目标。

推荐默认：

```text
if any S_A provider exists:
  reference = cheapest S_A request cost
else:
  reference = model-level pricing or router_params.reference_api_price
```

### Cost Envelope `L/U`

`L/U` 估计 workload 的 request cost 尺度。单位是 USD，不是 tokens。

对每条请求 `r`，按当前 pricing 和同一套路由时估计假设，计算 API-equivalent reference cost：

```text
if S_A candidates exist:
  v(r) = min_j cold_cache_api_cost_j(r)
else:
  v(r) = cold_cache_reference_api_cost(r)
```

然后：

```text
L = P10({v(r)})
U = P90({v(r)})
```

关键点：

- historical bootstrap 用真实 `completion_tokens`，不用 bucket-mean 预测。
- historical bootstrap 不能直接把 `api_logs.cache_read_tokens` 当成所有候选 provider 都会命中的 cache tokens。cache hit 是 provider/session dependent 的。除非我们有 provider-specific route-time cache estimator，否则这里也使用 conservative cold-cache assumption。
- 不直接用 `api_logs.cost_usd`。它是当时实际 provider 的成本，不一定是当前 cheapest API-equivalent opportunity cost。
- 在线 routing 使用当前 envelope。请求完成后，用真实 token 数生成新样本并更新 envelope。
- 如果某个 pool 样本太少，用配置 seed，并在 metadata 里保留 `sample_count`。

注意：`api_logs` 适合 bootstrap / 更新 `L/U` 的 price scale，但不应该作为 provider quota truth 的首选来源。S_Q 的真实剩余额度应该优先来自 provider-side quota snapshot。

### Effective Cost

对 provider candidate `j`：

```text
API:
  c_eff_j = estimated request token cost

Quota:
  c_eff_j = L * (U / L) ** quota_used_fraction

Concurrency:
  c_eff_j = 0 if a slot is available
  infeasible if saturated
```

quota 公式就是保留下来的 primal-dual 部分。PD 不再是顶层 router decision；它只是 quota provider 的 effective cost。

没有 API candidate 时，LP 仍然可以在 S_Q / S_C provider 上运行，但必须有 reference API price 来校准 `L/U`。reference API 不作为 candidate 出现在 LP 里。

### S_Q Quota Source

S_Q 的 `used / limit / reset_at` 不应只存在 `QuotaManager` 内存里。当前 FreeInference 已经有 provider quota dashboard：

- 前端 Providers tab 调 `/admin/provider-quotas`
- 后端通过 `serving.admin.provider_quotas.gather_all()` 拉 provider-side quota
- 截图中的 Chutes `Daily requests 0 / 5,000 requests` 就是这一路数据

RouteWise S_Q 应复用这套 fetcher，采用：

```text
provider quota snapshot + local optimistic increments
```

具体语义：

1. 后台周期性刷新 provider quota snapshot。
2. 请求路径不实时调用 provider quota API。
3. 选中 S_Q 后，本地 optimistic increment 一次，避免刷新间隔内重复超用。
4. 下次刷新成功后，以 provider-side truth 校正 used/limit/reset_at，并清理对应 local increments。
5. provider quota snapshot 不可用时，默认 mask 掉对应 S_Q candidate，而不是猜。

第一版先只支持可直接映射到 request quota 的 provider quota signal：

```text
provider = chutes
usage_label = "Daily requests"
unit = requests
```

也就是说，可以先做“一个 model 对应一个 Chutes account / key”的窄范围，不自动支持所有 provider。ZAI 的 time/token quota、MiniMax 的复杂 model/interval label、Featherless 的 `no_quota_api` 都不在第一版自动映射范围内。

`api_logs` 可以作为 fallback 或 diagnostics，但不是 S_Q 的主 truth source。这样 deploy/restart 后，新进程可以从 provider quota snapshot 恢复真实 quota state，而不是从 `used_today = 0` 开始。

### Prefix Cache 处理

cache 通过 API request cost 影响 RouteWise。它不会直接改变 quota shadow-price 公式，但会通过 estimated API-equivalent request cost 分布影响 `L/U`。

这里要区分三个 cost 概念：

1. completion 后的 actual billing cost：使用 provider 返回的真实 `cache_read_tokens` 和 `cache_write_tokens`。这是 accounting 和 `api_logs.cost_usd` 的来源。
2. route-time API cost estimate：只能使用 estimated cached-token count，因为 provider 还没有返回 usage。
3. RouteWise effective cost：API provider 使用 route-time API cost estimate；quota/concurrency provider 使用 shadow price。

本 PR 的 route-time cache estimate 在 routing 和 `L/U` calibration 里都先保守处理：

```text
estimated_cached_input_tokens = 0
```

这和当前 RouteWise simulator 的方向一致：没有可信 trace 或 request signal 时，不主动合成 provider-local prefix-cache hit。这样可以避免在路由前把 API provider 错误估得太便宜。

actual billing 和 diagnostics 仍然要 cache-aware：

```text
actual_cost =
  input_price * (prompt_tokens - actual_cache_read_tokens)
+ cache_read_price * actual_cache_read_tokens
+ output_price * completion_tokens
```

后续可以加 session-aware cache estimator：

```text
key = (endpoint_id, model_id, session_id or prefix_id)
estimated_cached_input_tokens = recent_cache_read_ratio * prompt_tokens
```

但这个 estimator 应该在 body router 对齐之后再做。因为过度预测 cache hit 会让 API provider 在 route-time 看起来过于便宜，从而扭曲 LP weights 和 quota usage。

### LP Body Router

对所有 feasible candidates：

```text
budget = c_min + alpha * (c_max - c_min)

minimize    sum_j pi_j * mean_ttft_j
subject to  sum_j pi_j * c_eff_j <= budget
            sum_j pi_j = 1
            pi_j >= 0
```

从 LP 得到的稀疏 mixture 里 sample primary provider。LP 可以用当前 RouteWise simulator 里的 two-provider support enumeration，不需要在生产请求路径里依赖 PuLP。

## 请求流程

请求到达时：

```text
1. 把 model route entries 转成 ProviderCandidate。
2. 如果 context 里没有 prompt_tokens，就估算 prompt_tokens。
3. 用 bucket mean 预测 output tokens。
4. 读取当前 model 或 routewise pool 的 L/U。
5. 构造 feasible candidates：
   - 有效 pricing 的 API candidates
   - quota 未耗尽的 quota candidates
   - slot 可用的 concurrency candidates
6. 给每个 feasible candidate 计算 c_eff。
7. 读取每个 feasible candidate 的 rolling mean TTFT。
8. 解 RouteWise LP。
9. sample primary。
10. 只对被 sample 到的 primary commit quota 或 concurrency。
11. 把 decision metadata 存到 request_id 下。
12. 走现有 BaseRouter execution flow。
```

请求完成时：

```text
1. 用真实 completion_tokens 更新 bucket-mean output predictor。
2. 用 ttft_ms 更新 rolling latency profile；错误请求写入惩罚样本。
3. 用真实 token 数计算 API-equivalent reference cost，更新 L/U envelope。
4. release 已 commit 的 concurrency slot。
5. 把 RouteWise observation metadata 传给日志和测试路径。
```

## Provider Candidate 模型

内部 candidate 结构：

```python
@dataclass(frozen=True, slots=True)
class ProviderCandidate:
    endpoint_id: str
    model_id: str
    adapter: BaseAdapter
    provider_type: Literal["on_demand", "quota", "concurrency"]
    weight: float
    pricing: Pricing
    routewise_pool: str
    quota_pool: str | None
    concurrency_pool: str | None
```

`endpoint_id` 继续作为 health、latency-profile 和 observation key。

`routewise_pool` 控制使用哪组 `L/U` envelope。默认是 `model_id`。如果一个 quota subscription 被多个 model 共享，这些 model 应该配置同一个 `routewise_pool`。

## 配置新增项

model-level router selection：

```yaml
models:
  - id: minimax-m2.5
    router: routewise
    router_params:
      alpha: 0.75
      slo_ms: 3000
      envelope:
        bootstrap_window_hours: 168
        lower_percentile: 10
        upper_percentile: 90
        min_samples: 100
        seed_l: 0.001
        seed_u: 0.05
      output_predictor:
        cold_start_tokens: 512
        min_bucket_samples: 20
```

route-level metadata：

```yaml
route:
  - kind: zai
    weight: 1.0
    provider_type: on_demand
    routewise_pool: glm-paid-pool

  - kind: chutes
    weight: 1.0
    provider_type: quota
    routewise_pool: glm-paid-pool
    quota_pool: chutes-glm-daily
    quota:
      limit: 5000
      window: daily
      reset_timezone: UTC

  - kind: featherless
    weight: 1.0
    provider_type: concurrency
    routewise_pool: glm-paid-pool
    concurrency_pool: featherless-glm
    concurrency:
      limit: 4
```

无 on-demand baseline 示例：

```yaml
models:
  - id: glm-5-turbo
    router: routewise
    router_params:
      reference_api_price:
        prompt: "1.2"
        completion: "4.0"
    route:
      - kind: chutes
        weight: 1.0
        provider_type: quota
        quota_source:
          provider: chutes
          usage_label: "Daily requests"
          unit: requests
      - kind: featherless
        weight: 1.0
        provider_type: concurrency
        concurrency:
          limit: 4
```

如果保留真实 P_O baseline，也可以用更标准的配置：

```yaml
route:
  - kind: zai
    weight: 1.0
    provider_type: on_demand
  - kind: chutes
    weight: 1.0
    provider_type: quota
    quota_source:
      provider: chutes
      usage_label: "Daily requests"
      unit: requests
```

第一版实现可以支持更窄的子集，因为当前 model config 不一定已经声明所有 pool 字段。缺失的 pool ID 默认：

```text
provider-local state: {model_id}:{endpoint_id}
envelope: model_id
```

## 从 `api_logs` Bootstrap `L/U`

增加一个 storage-facing helper，用来取最近成功请求样本：

```python
async def fetch_routewise_cost_samples(
    *,
    model_ids: list[str],
    since: datetime,
    limit: int,
) -> list[RouteWiseCostSample]:
    ...
```

样本字段：

```python
model_id: str
prompt_tokens: int
completion_tokens: int
cache_read_tokens: int
timestamp: datetime
```

router 使用当前 route pricing 或 reference API price，把这些样本转换成 API-equivalent reference cost。这样 bootstrap 不依赖历史 provider 选择，也不依赖历史 `cost_usd`。

冷启动策略：

```text
if sample_count >= min_samples:
  use P10/P90
else:
  use seed_l / seed_u and report envelope_source="seed"
```

## 可观测性

在现有 routing metadata 里加一个 `routewise` metadata object：

```json
{
  "version": "current_body",
  "selected_endpoint": "minimax-m2.5:zai",
  "selected_provider_type": "on_demand",
  "alpha": 0.75,
  "budget_usd": 0.0123,
  "envelope": {
    "L": 0.001,
    "U": 0.050,
    "source": "api_logs",
    "sample_count": 4821,
    "pool": "glm-paid-pool"
  },
  "c_eff": {
    "endpoint-a": 0.002,
    "endpoint-b": 0.008
  },
  "lp_weights": {
    "endpoint-a": 0.74,
    "endpoint-b": 0.26
  },
  "lp_status": "optimal",
  "predicted_output_tokens": 640,
  "prompt_bucket": "8192-16383",
  "hedging": {
    "mode": "disabled"
  }
}
```

本 PR 不新增数据库列。metadata 放进现有 routing observation / request log 使用的 metadata 路径。

## Shadow Hedging 占位

如果不改变 request execution，也可以在本 PR 里只计算并记录 shadow hedging decision：

```text
hedging.mode = "shadow" | "disabled"
hedging.would_dispatch = true | false
hedging.backup_endpoint = ...
hedging.checkpoint_ms = ...
```

本 PR 不发真实 backup request。

## 失败行为

fallback 顺序：

1. 如果 LP 失败但仍有 feasible candidates，选择 budget 内 mean-TTFT 最低的 candidate。
2. 如果 budget 内没有 candidate，选择 feasible candidates 中 effective cost 最低的 candidate。
3. 如果没有任何 RouteWise candidate 可用，回到该 model 现有 `BaseRouter` / `FixedRouter` 行为。
4. 如果 envelope bootstrap 失败，用 seeds，并输出 `envelope_source="seed"`。
5. 如果 latency profiles 冷启动，用配置的 unprofiled latency penalty 或现有 profile fallback。

quota 和 concurrency 只能在 sampled primary 确定后 commit。LP 失败或未选中的 candidate 不能消耗 capacity。

## 测试

单元测试：

- `output_predictor`：bucket 选择、fallback 顺序、max-token clamp、completion update。
- `envelope`：从 actual-token samples bootstrap、P10/P90 计算、seed fallback、非法样本过滤。
- `effective_cost`：on-demand cost math、quota `exp_lu`、concurrency feasible / saturated 行为。
- `lp`：和 RouteWise simulator 示例对齐、稀疏 support、budget 边界情况。
- `router`：provider-category candidate collection、sampling、quota commit、concurrency acquire/release、metadata shape。

集成测试：

- 使用 fake adapters 覆盖 on-demand + quota + concurrency routes。
- 用 deterministic `L/U` 测 historical log bootstrap。
- `router: routewise` 使用当前 RouteWise body router。
- 现有 checked-in RouteWise decision semantics 不再作为 selectable mode 保留。
- non-streaming 和 streaming observation paths 都能更新 predictor 和 profiles。

Replay validation：

- 基于最近 `api_logs` 构造 offline replay harness。
- 在相同 token/pricing/profile 输入下，尽可能比较 `routewise` decisions 和 RouteWise real-eval `BudgetRangePolicy`。

## Rollout

1. 落代码，让 `router: routewise` 映射到 current body router。
2. staging 验证通过前保持 cost adjustment 关闭。
3. 先选少数明确模型，不自动覆盖所有 provider。
4. 如果 route 有真实 P_O provider，先用 `ZAI on-demand baseline + Chutes S_Q` 验证 reference price、`L/U` 和 quota usage。
5. 如果 route 没有 on-demand baseline，必须配置 `reference_api_price` 或使用 model-level pricing 作为 reference。
6. Chutes S_Q 先只接 `Daily requests`，并复用 provider quota snapshot。
7. slot accounting 验证后再加入 S_C provider。
8. 如果某个 model 的 RouteWise 有问题，临时把该 model 切到 `fixed`。

## 开放问题

1. 一旦配置了共享 quota pool，`routewise_pool` 默认应该是 model-level 还是 subscription-level？
2. 第一批启用 RouteWise 的具体 model 是哪些？是否先采用“一 model 对一 Chutes key”的窄映射？
3. provider quota snapshot 刷新频率用 1 分钟、5 分钟，还是按 provider reset/TTL 自适应？
4. online envelope update 只用 completed actual-token samples，还是也把 route-time predicted samples 暂时写入，完成后再修正？
5. 第一版 route-time cache-aware cost 是否继续保守地设 cached tokens 为 0，还是用 session affinity 做第一个 cache estimator？
6. 被替换的文件是直接删除，还是只保留仍符合当前 RouteWise 语义的 pure utilities？

## 验收标准

- `router: routewise` 选择 current RouteWise body router。
- 不暴露第二个 RouteWise mode。
- 支持真实 P_O/on-demand baseline 和无 on-demand reference API price 两种价格参照。
- 可以从现有 `api_logs` bootstrap `L/U`，并在 decision metadata 中暴露。
- Chutes S_Q 使用 provider quota snapshot 恢复 `used/limit/reset_at`，deploy/restart 后不会从 0 开始。
- router 能解 unified provider-category cost-budgeted mean-TTFT LP。
- router 记录足够 metadata，能解释每个 provider selection。
- 现有 request execution、cost logging、fallback 和 response wire format 不变。
- 本 PR 不发生真实 backup hedging dispatch。
