# Hybrid Routing 抽象重构实施计划

- 日期：2026-09-13
- 修订：2026-09-14，按设计文档收缩为抽象重构；同日完成实现
- 状态：已完成并接线（`router: fixed` 生效；`router: routewise` 未迁移）
- 分支：`murphy/dev/hybrid-routing-abstract`
- 设计：[抽象重构设计](../specs/2026-09-12-hybrid-routing-abstraction-design.zh.md)

## 1. 交付物

| 文件 | 内容 |
|---|---|
| `apps/backend/routing/route_scope.py` | `RouteScopeView`：按模型与 endpoint 过滤的只读 `RouteTableView` 投影；`scope_view_for_endpoints` 构造助手；endpoint 归属判定 |
| `apps/backend/routing/backends.py` | `RoutingBackend` 协议、`RoutingBackendBase` 委托基类、`LocalBackend`、`RouteWiseCloudBackend` |
| `apps/backend/routing/hybrid.py` | `HybridRouter`、`BackendSelection` 策略协议、`HybridRoutingError`、全局候选计划与派发、流式转发与关闭、反馈归属 |
| `apps/backend/routing/decisions.py` | `RoutingDecision` / `RoutingTarget` / `FallbackAttempt`（逐候选 fallback 计划项） |
| `apps/backend/routing/__init__.py` | 导出上述公开符号 |
| `tests/unit/routing/test_route_scope.py` | 候选范围投影契约 |
| `tests/unit/routing/test_routing_backends.py` | 两个 backend 的调用、委托、范围与反馈契约 |
| `tests/unit/routing/test_hybrid_router.py` | 组合、策略替换点、流式、反馈、生命周期契约 |
| `tests/unit/routing/test_router_contract.py` | 共享 router 契约参数化加入 `local-backend` 与 `cloud-backend` |

未改动 `apps/backend/routing/executor.py`（保持兼容导出）。

生产路径**已改动**（原本文档称未改动，与提交内容不符，此处更正）：`model_router_registry.py` 增加 `HybridRouterFactory` 与 `set_hybrid_router_factory()`；`bootstrap.py` 增加 `_build_model_router_registry()`，在建 registry 的同一次调用里安装 factory，使 `router: fixed` 模型经 `HybridRouter` 进入；`hybrid_composition.py`、`policies.py`、`decisions.py`、`route_scope.py` 为新增文件。

`apps/backend/routing/routers.py` 与 `routewise/router.py` 各有一处必要改动：`ManagedRouter.start` 的返回类型与 `RouteWiseRouter.start()` 现在返回"本次调用是否真正拉起后台任务"的布尔值。调用方一律忽略返回值，行为不变；包装层用它判断生命周期所有权。

## 2. 分步执行记录

1. **候选范围视图。** 先实现 `RouteScopeView`，使 cloud 范围成为可注入的只读对象，而不是在 RouteWise 内部加分支。权重保留原值，不重归一化。
2. **backend 封装。** `RoutingBackendBase` 逐项转发 `RouterProtocol` 的请求面，`LocalBackend` 只声明身份，`RouteWiseCloudBackend` 额外把 scoped view 绑定给被包装的 `RouteWiseRouter`，并提供 endpoint 归属判定。
3. **组合对象。** `HybridRouter` 每请求调用一次策略，委托选中的 backend；流式返回转发迭代器并在关闭时关闭下游；反馈按 owner 投递一次。
4. **共享契约扩面。** 把两个 backend 加入 `test_router_contract.py` 的参数化，使它们与 `FixedRouter`/`RouteWiseRouter` 受同一组行为契约约束。
5. **回归与静态检查。** 跑 routing 单测、servers/routing 入口测试、全量非 DB/非 external 测试、ruff 与 pydocstyle。
6. **Review 修复（2026-09-14，PR #1454）。** 三处封装正确性问题：反馈归属、生命周期所有权、刷新语义。详见设计文档第 4、6 节与下方验收结果。

## 3. 验收命令

```bash
cd .worktrees/hybrid-routing-abstract
uv sync --frozen
uv run pytest tests/unit/routing/ -q -m "not external and not dbtest"
uv run pytest tests/ -q -m "not external and not dbtest"
uv run ruff format --check apps/backend/routing tests/unit/routing
uv run ruff check --no-fix apps/backend/routing tests/unit/routing
uv run pydocstyle apps/backend/routing
```

在本 worktree 上执行结果：

- `tests/unit/routing/`：通过（性能与池内规避用例按既有标记跳过）
- `tests/unit/servers/test_hybrid_bootstrap_wiring.py`：通过（接入从生产入口 `_build_model_router_registry` 验证）
- 全量 `pytest -m "not external and not dbtest"`：通过，无回归
- Review 的三份复现脚本（`test_pr_1454_repros.py`，审阅版本 86db016e 下 3/3 失败）在修复后 3/3 通过
- ruff format / ruff check / pydocstyle：通过

## 4. 遗留任务（不在本次范围）

1. **routewise 迁移。** `router: routewise` 仍是全池 `RouteWiseRouter`，绕过 `HybridRouter`。迁移前置条件见设计文档 §3.5.4.1（pin 通路、首选目标语义）与 §5（生命周期、operational store、顶层策略）。设计定义已固定：新架构中 RouteWise 位于 `CloudBackend` 内，`router: routewise` 是迁移前的旧路径。
2. **本地归属的显式来源。** `HybridFixedRouterFactory` 已支持 `local_scope` / `local_ownership`，但生产 bootstrap 目前走 `_LOCAL_HOSTS` 兼容默认；LAN 或集群 DNS 上的自建 server 会被判为远端。部署应显式传入归属来源。
3. **`_LOCAL_HOSTS` 一致性。** `servers.registry._LOCAL_HOSTS` 与 `servers.observability.alerts._LOCAL_HOSTS` 不一致（后者含 `::1`），因此 IPv6 loopback 上的自建 server 被当作远端。改动会影响出站限流豁免范围，需单独评估。
4. **行为保留项（已修）。** 跨域 fallback 恢复 route 全局候选顺序；最终错误复用 `select_surfaced_error` 的 400/404/413/422 规则；每次 attempt 逐候选记录，`failed_attempts` 不再丢域内失败；`FixedPolicy.select_backend` 传入请求的 `prefill_tokens`。为此新增 `RoutingRequestOptions.allow_fallback`：hybrid 层自己驱动候选序列时，被包装的 router 只打当前候选。
