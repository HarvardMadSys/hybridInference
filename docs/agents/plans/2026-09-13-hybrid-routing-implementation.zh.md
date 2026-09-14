# Hybrid Routing 抽象重构实施计划

- 日期：2026-09-13
- 修订：2026-09-14，公共 Router 契约、独立策略实现与共享 Backend 执行契约
- 状态：本轮仅更新文档；以下代码调整与验收尚未据此执行
- PR 分支：`murphy/dev/hybrid-routing-abstract`
- 当前代码参照：`ea9ad22fe14d96b9b2d414e337cba040d9ce28a4`
- 设计：[Hybrid Routing 抽象重构设计](../specs/2026-09-12-hybrid-routing-abstraction-design.zh.md)

## 1. 已确认的交付边界

- HybridRouter 是公共路由抽象的设计名称，代码以现有 RouterProtocol 为唯一公共契约，不新增重复接口。
- FixedRouter / RouteWiseRouter 是同层实现，各自保留决策、重试及状态流程。现有具体 `routing.hybrid.HybridRouter` 是组合实现，不是公共抽象本身。
- 不要求独立提取 RouteWisePolicy，不要求把 RouteWise 接入现有具体组合 Router。它由 Registry 直接返回可以满足统一抽象。
- Router 决定目标及失败后怎么办。Backend 分两种角色：`LeafBackend` 只执行绑定的那一个 endpoint（要求被包装 Router 声明 `supports_exact_dispatch`），`TreeBackend` 是路由子树入口、把请求委托给范围受限的内部 Router；`LocalBackend` / `CloudBackend` 属于后者。指令显式区分 `ExecuteEndpoint` 与 `DelegatePool`，不匹配的指令在 I/O 前按组合错误拒绝。
- 沿用模型配置、默认策略、Registry 缓存、别名和 Admin 切换；不新增请求级策略选择。
- 保留 Fixed / RouteWise 行为与 RouteWise 全池候选，包括显式 concurrency 的 local；继续复用 adapter、共享状态和 library。
- 不新增本地资源管理、队列、预测器或 Greedy / Nimbus，不增加“Fixed 分域 + RouteWise 云内选择”的默认路径。

这些要求取代旧计划中的统一执行引擎终态。本轮只更新设计、实施计划和 AGENTS.md，不修改运行代码、部署配置或线上入口。

## 2. 可复用的现有实现

| 文件 | 参照提交的能力与注意事项 |
|---|---|
| `apps/backend/routing/protocols.py` | RouterProtocol、RouteTableRefreshable 与请求参数；公共契约的基础 |
| `apps/backend/routing/routers.py` | Fixed 全局选择、claim、执行、反馈及共享状态；不要复制第二套算法 |
| `apps/backend/routing/routewise/` | 原全池 RouteWise、预留、hedging、执行和学习；保留 `llm_routewise.core` 调用 |
| `apps/backend/routing/backends.py` | 执行域和兼容包装；当前无目标选择、内部 fallback 不代表最终单 endpoint 契约 |
| `apps/backend/routing/policies.py`、`decisions.py` | 当前 Fixed 组合的内部选择和计划能力，其他 Router 不必采用同一接口 |
| `apps/backend/routing/hybrid.py` | 已有具体组合 Router；复用其有效逻辑，不误写为公共抽象的现成实现 |
| `apps/backend/routing/route_table.py`、`route_scope.py` | 候选范围、标签解析和归属投影 |
| `apps/backend/routing/model_router_registry.py` | 按模型配置构建与缓存、别名和策略切换；保持该机制 |
| `apps/backend/serving/servers/bootstrap.py`、`hybrid_composition.py` | 真实入口与生命周期接线；后续与执行边界一起核对 |

`routing/executor.py` 保持兼容导出，不修改该 shim。已有 FixedPolicy、组合 Router 和公开 Backend 包装无需因文档更新立即删除；兼容保留不等于允许它们在目标路径中重复决策。

## 3. 后续代码调整顺序

以下步骤描述后续实现顺序，不是本轮已完成工作，也不要求推倒现有 PR 重写。

### 3.1 固定公共契约与兼容边界

1. 以 RouterProtocol 为 serving / Registry 的统一依赖，保留普通请求、流式、反馈和状态语义。
2. 区分公共抽象与现有具体 HybridRouter；如改名，保留公开导入与调用兼容，不新增同义接口。
3. 明确 Backend 的单 endpoint 执行入口、实际目标身份、不可派发与上游失败的区分、迭代器关闭语义。
4. 保留旧包装入口的 target / preference / pin / fallback 参数语义；通过适配过渡，不直接破坏外部调用。

验收：同一 Router 契约适用于 Fixed / RouteWise；严格执行入口不会在目标缺失、范围不符或 claim 被拒时调用另一个 endpoint。

### 3.2 建立薄的 Backend 派发边界并保持 Fixed 行为

1. 复用现有 adapter 和必要的单目标执行辅助逻辑；Backend 不再借完整 Router 自主选路。
2. Fixed 的全局权重、模态、健康、亲和与 prefill 逻辑仍由对应 Router 流程使用，重试控制只有一个所有者。
3. 保留原 route 顺序、实际 endpoint 去重、错误优先级和完整失败记录。只分域的旧组合入口在兼容层仍按原约定工作，不能静默丢掉 fallback。
4. 保持共享健康、候选表、连接和反馈状态；单域部署无须创建空 Backend。

验收：从真实 Registry / bootstrap 入口做旧新行为对照；`L1 → cloud → L2` 顺序不变，指定目标不被重抽，未派发不制造上游失败样本。

### 3.3 RouteWise 保留完整流程，接入相同执行契约

1. 继续由 Registry 返回 RouteWiseRouter，保持 `router: routewise` / `router_params` 和完整候选池。
2. 只把实际 endpoint 调用接到约定的 Backend / adapter 边界；选择、预留、重新求解、hedging 和反馈流程继续由 RouteWiseRouter 及原协作者管理。
3. 保留 local 候选、显式 provider_type、原 concurrency 共享池、quota 消费与释放语义，不因 Backend 划域复制或拆分容量。
4. 保留探测、operational store、校准、启动 / 关闭和学习状态；不提取 RouteWisePolicy，不用 Fixed 计划替换 RouteWise 流程。
5. 对普通、流式以及实际 hedge leg 验证派发边界和资源所有权。直接返回 RouteWiseRouter 或满足接口本身，不足以证明这些已完成。

验收：在候选、时间、随机性和资源状态受控时，与旧实现比较选择、准入、失败、取消、hedging 和反馈；本地 concurrency 仍参与全池，槽位满时不可选、释放后恢复。

### 3.4 核对配置、生命周期与真实请求路径

- 保留模型配置、默认策略、canonical model / alias、缓存和启动校验；不新增必填配置。
- 保留通过现有校验的 HTTP pin 到共享 FixedRouter 的兼容路径，不增加第三个 Backend。
- 复用策略切换的准备与发布机制，验证失败恢复、新请求绑定、旧请求与反馈所有权；共享池不因切换重置。
- 路由刷新更新范围和归属，但保留在途预留与 pending 状态。重复 attach、重复 claim 或重复生命周期操作不能造成状态丢失或重复记账。
- 兼容包装若继续存在，诊断、存储和生命周期访问到实际所有者；包装层只关闭自己启动的对象。
- LocalBackend / CloudBackend 的执行归属按部署配置解释，与 RouteWise 资源类型独立。

验收：真实 serving 路径覆盖普通 / SSE、pin、错误、取消、刷新和配置切换。先记录代码行为验证，再记录实际部署验证。

## 4. 验证命令

从对应 worktree 根目录执行。本轮文档修改检查链接与 diff：

```bash
uv run --frozen python ops/ci/check_docs_links.py
git diff --check
```

后续涉及运行代码时，根据实际变更运行相关检查，例如：

```bash
uv run --frozen pytest -q tests/unit/routing tests/unit/servers
uv run --frozen pytest -m "not external and not dbtest"
uv run --frozen ruff format --check apps/backend/routing tests/unit/routing
uv run --frozen ruff check --no-fix apps/backend/routing tests/unit/routing
uv run --frozen pydocstyle apps/backend/routing
```

保留既有组合回归测试，围绕新的执行边界补充有鉴别力的行为对照。新增或修改 serving 文件时，将静态检查范围扩至相应文件。记录基线、被验证提交、命令与结果，不复用旧提交的 CI 全绿结论。

## 5. 不属于本次实现的要求

- 不强制所有策略共用具体 HybridRouter 的执行循环。
- 不要求提取 RouteWisePolicy、改名 FixedRouter 或删除公开的 RouteWiseCloudBackend。
- 不把 RouteWise 迁入 cloud，不新增默认的两阶段分域选择。
- 不实现请求级策略选择、Greedy / Nimbus、GPU 预算或队列系统。
- 不把只有文档、类型标注或类名的变化当作运行边界已接入。
