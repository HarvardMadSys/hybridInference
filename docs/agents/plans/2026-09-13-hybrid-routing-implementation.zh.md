# Hybrid Routing 抽象重构实施计划

- 日期：2026-09-13
- 修订：2026-09-14，按设计文档收缩为抽象重构；同日完成实现
- 状态：已完成（生产接入未启用）
- 分支：`murphy/dev/hybrid-routing-abstract`
- 设计：[抽象重构设计](../specs/2026-09-12-hybrid-routing-abstraction-design.zh.md)

## 1. 交付物

| 文件 | 内容 |
|---|---|
| `apps/backend/routing/route_scope.py` | `RouteScopeView`：按模型与 endpoint 过滤的只读 `RouteTableView` 投影；`scope_view_for_endpoints` 构造助手；endpoint 归属判定 |
| `apps/backend/routing/backends.py` | `RoutingBackend` 协议、`RoutingBackendBase` 委托基类、`LocalBackend`、`RouteWiseCloudBackend` |
| `apps/backend/routing/hybrid.py` | `HybridRouter`、`BackendSelection` 策略协议、`HybridRoutingError`、流式转发与关闭、反馈归属 |
| `apps/backend/routing/__init__.py` | 导出上述公开符号 |
| `tests/unit/routing/test_route_scope.py` | 候选范围投影契约 |
| `tests/unit/routing/test_routing_backends.py` | 两个 backend 的调用、委托、范围与反馈契约 |
| `tests/unit/routing/test_hybrid_router.py` | 组合、策略替换点、流式、反馈、生命周期契约 |
| `tests/unit/routing/test_router_contract.py` | 共享 router 契约参数化加入 `local-backend` 与 `cloud-backend` |

未改动 `apps/backend/routing/executor.py`（保持兼容导出），未改动生产 registry、bootstrap 与任何启动路径。

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
- 全量 `pytest -m "not external and not dbtest"`：通过，无回归
- Review 的三份复现脚本（`test_pr_1454_repros.py`，审阅版本 86db016e 下 3/3 失败）在修复后 3/3 通过
- ruff format / ruff check / pydocstyle：通过

## 4. 遗留任务（不在本次范围）

1. **生产策略。** 需要注册一个新的 strategy（名称与参数模型）才能让 `models.yaml` 选择 hybrid。本次只提供替换点与测试策略。
2. **bootstrap 识别。** `_collect_routewise_routers` 依赖 `isinstance(..., RouteWiseRouter)`；嵌套后需要改为从组合中取出 cloud router，否则 RouteWise 的生命周期与 operational store 绑定不会发生。
3. **本地候选拆分。** 生产 `FixedRouter` 持有全部路由；组装 hybrid 时需要构造只含本地候选的 router，或为本地侧提供同样显式的范围输入。
4. **纳入 registry 的刷新语义。** `HybridRouter.refresh_route_table()` 已具备委托能力，但尚未在真实启动路径上验证。
