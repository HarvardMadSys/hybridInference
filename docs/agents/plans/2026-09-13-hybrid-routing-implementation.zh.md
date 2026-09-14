# Hybrid Routing 抽象重构实施计划

- 日期：2026-09-13
- 修订：2026-09-14，按用户确认将 Fixed / RouteWise 对齐为同层 routing policy
- 状态：实施范围已对齐；代码验收结果按对应提交记录
- 分支：`murphy/dev/hybrid-routing-abstract`
- 实现参照：`3ed570362`
- 设计：[Hybrid Routing 抽象重构设计](../specs/2026-09-12-hybrid-routing-abstraction-design.zh.md)

## 1. 本次交付边界

目标是整理 policy 与执行域的接口，同时保留已有请求行为：

- Fixed 与 RouteWise 是同层策略；Greedy / Nimbus 是后续策略扩展。
- LocalBackend 与 CloudBackend 提供执行能力，RouteWise 本身可以选择两域的候选。
- Fixed 组合保留全局权重、候选顺序、目标、错误、流式和反馈语义。
- `router: routewise` 继续使用原 RouteWiseRouter 与完整候选池，保留 local / concurrency、quota、hedging 和反馈行为。
- 不把“RouteWise 迁入 cloud”列为待办，不为本次重构增加本地资源管理或新调度算法。

本轮更新设计、实施计划与 AGENTS.md 的描述。`RouteWisePolicy` 是目标职责名称，本次不要求立即拆出同名类，也不改变现有配置入口。

## 2. 当前实现落点

| 文件 | 当前内容与定位 |
|---|---|
| `apps/backend/routing/decisions.py` | `BackendSelection`、`RoutingDecision`、`RoutingTarget`、`FallbackAttempt`；策略与调度结果契约 |
| `apps/backend/routing/policies.py` | FixedPolicy；复用 FixedRouter 的全局选择，提供 route 顺序的候选计划 |
| `apps/backend/routing/hybrid.py` | HybridRouter；消费策略结果，派发计划，处理流式、错误与反馈归属 |
| `apps/backend/routing/backends.py` | RoutingBackend、委托基类、LocalBackend、CloudBackend；FixedCloudBackend 复用共享执行能力，RouteWiseCloudBackend 保留为可选范围封装 |
| `apps/backend/routing/route_scope.py` | 路由范围投影、provider 标签到 endpoint 的解析与反馈归属 |
| `apps/backend/routing/routers.py` | 共享 FixedRouter；选择、claim、执行与兼容控制项 |
| `apps/backend/routing/model_router_registry.py` | 同层的 `fixed` / `routewise` 配置入口；仅 Fixed 分支使用 hybrid factory |
| `apps/backend/serving/servers/bootstrap.py`、`hybrid_composition.py` | 真实入口接线，组装符合条件的 Fixed 混合模型 |
| `apps/backend/routing/routewise/` | 原 RouteWise 算法适配与生产运行逻辑；继续调用 `llm_routewise.core` |

`routing/executor.py` 保持兼容导出。现有 FixedRouter 包含决策与执行两类职责，backend 复用它不意味着全局 Fixed 策略属于 LocalBackend。RouteWiseCloudBackend 的存在同样不限制 RouteWise 的全局策略定位。

## 3. 执行与验收顺序

### 3.1 对齐文档

设计图、组件表与当前调用链分开说明：目标上 Fixed / RouteWise 同层，当前 RouteWiseRouter 仍保留自身完整的决策与执行流程。清除“必须先 Fixed 分域、RouteWise 只能选择 cloud”以及“未迁入 cloud 就未完成”的要求。

### 3.2 保留 Fixed 组合的行为

按原实现与组合实现做受控对照，确认：

1. 全局权重、模态、健康、亲和与 prefill 参数沿用原逻辑。
2. 逐 endpoint 计划按原 route 顺序执行；只分域策略保留域内及跨域 fallback。
3. 首选替换后按实际 endpoint 去重；严格目标不能被亲和、重新抽样或 claim 失败后的域内重选替换。
4. 不可派发的候选与已经执行失败的上游分开处理；错误优先级和完整失败记录保持原形状。
5. 流式在客户端可见输出前后保留原 fallback 边界；取消、刷新、反馈与生命周期保持原规则。

已有测试包括 `test_hybrid_composition.py`、`test_hybrid_router.py`、`test_routing_backends.py`、`test_router_contract.py` 及 `test_hybrid_bootstrap_wiring.py`。具体回归是否通过，以实际提交的验证记录为准。

### 3.3 保留现有 RouteWise

本项通过维持原入口完成，不增加一层 Fixed 决策，也不缩窄原候选池：

- 保留 `router: routewise` / `router_params` 配置语义。
- 保留 local 候选及其显式 `provider_type`；原 concurrency 槽位、共享池、预留与释放继续生效。
- 保留原 quota、成本预算、延迟画像、hedging、反馈、后台探测、operational store 与启动 / 关闭流程。
- 保留现有 HTTP pin 直通共享 FixedRouter 的兼容路径，不新增第三个 backend。
- 继续复用 `llm-routewise` library；不另写算法，也不因类名变化重复维护状态。

现有 RouteWiseCloudBackend 的局部测试验证可选封装契约，不代表必须把 `router: routewise` 改接到它。

## 4. 验证命令

从相应 worktree 根目录执行。文档修改只需链接与 diff 检查：

```bash
uv run --frozen python ops/ci/check_docs_links.py
git diff --check
```

涉及运行代码时，按变更范围运行相应检查：

```bash
uv run --frozen pytest -q tests/unit/routing tests/unit/servers/test_hybrid_bootstrap_wiring.py
uv run --frozen pytest -m "not external and not dbtest"
uv run --frozen ruff format --check apps/backend/routing tests/unit/routing
uv run --frozen ruff check --no-fix apps/backend/routing tests/unit/routing
uv run --frozen pydocstyle apps/backend/routing
```

记录被验证的 commit、命令及结果；区分仓库测试、行为对照与实际部署验证，不沿用旧提交的全绿结论。

## 5. 后续工作边界

- 将来若需要统一内部接口，可以从 RouteWiseRouter 提取同层 RouteWisePolicy；前提是保留完整候选范围、library 算法、预留与反馈所有权及请求行为。这不是本 PR 的必做项。
- 本地归属的显式配置与 `_LOCAL_HOSTS` 一致性仍需按部署情况处理；不要用 local / cloud 域分类覆盖 RouteWise 的资源类型。
- Greedy / Nimbus 及其新增状态或资源需求由对应任务定义。
- 不要求删除现有公开包装类，不要求新增“Fixed 分域 + RouteWise 云内选择”的生产配置。
