# 四个推理目标共用协议执行：实施计划

日期：2026-10-28。**状态：计划，待评审。** 未开始实施。

设计：[2026-10-28 在线接口与 Batch 共用的协议执行](../specs/2026-10-28-shared-inference-execution-design.zh.md)（下称“设计稿”）。本计划只写“怎么分步做、每步怎么验收”，不重复设计理由；凡与设计稿冲突，以设计稿为准并回到评审。

基线：dev `a738f4915`。分支：`codex/feature/shared-inference-all-targets-20261028`（worktree `/tmp/claude/worktree/shared-inference-all-targets-20261028`）。文中的类型、函数、测试名都是**提案**，基线中不存在，除非注明“基线已有”。

---

## 0 总则

1. **一个分支、顺序检查点、一个最终草稿 PR。** S0–S6 全部在本分支上依次完成；每个阶段结束时形成一个可以在本地评审的检查点（一个或几个本地提交，提交信息以 `checkpoint S<n>:` 开头），不单独提 PR、不合并、不部署，进入下一阶段也不以合并或部署为前提。全部完成后由协调方（Codex）复核并开一个覆盖四个目标的草稿 PR，目标 `dev`；实施方不推送、不开 PR。阶段内只搬移、不改写逻辑；发现当前行为与注释或设计稿不符时，在检查点说明中记录并按设计稿的“保持现状”方案继续，不在阶段内扩大改动。
1a. **过渡桥在最终检查点之前全部删除。** S4 的 `ScopeAdmission`、`_dispatch` 中的 handler 执行器、`user_ctx` 归属合并等，最晚在 S6 删除；最终草稿 PR 中不留过渡桥。
2. **外部行为不变。** 公开 API、错误体、SSE 字节、`api_logs` 字段、计费口径、Batch 结果与 webhook 在每个阶段前后一致（设计稿第7节）。
3. **任何阶段都不构造 `Request` 调回 handler，不把 `Request` 改名传入核心，不加 `skip_*` 参数。**
4. **Fixed/RouteWise 的资源归属、`endpoint_id`、策略、重试、feedback 不变**；`routing/` 目录在本计划中不改代码。
5. **门禁**（每个阶段）：
   - `make format`、`make lint`、`make test`；
   - 触及 `serving/batch/*`、`storage`、Batch 集成测试时：`make test-db-local`（可以先用 `ARGS="tests/integration/test_batch_expansion_http_postgres.py"` 缩小范围，最后跑全量）；不运行 `make test-db-local-down`；
   - 改到 `ops/` 下脚本时，额外跑一次 pre-commit 的 pydocstyle 钩子（`make lint` 不覆盖 `ops/`）；
   - 推送前本地跑 gitleaks，测试中的假 key 按 `.gitleaks.toml` 的现有方式加入 allowlist。
6. **隔离网关验证**（每个改运行时代码的阶段）：真实 uvicorn + 本 worktree 的一次性 PostgreSQL + `ops/dev/batch_api_smoke.py` 的合成上游。结论只代表合成上游，检查点说明与最终草稿 PR 中照此表述。
7. **混合版本检查**（每个改运行时代码的阶段）：同一 PostgreSQL 上同时运行基线进程与本阶段进程，二者都启用 worker，按设计稿第7节的 1–5 项检查。未做或未通过前，检查点说明中写明“未验证混跑，不声称支持滚动升级”。
8. **回滚**：本地撤销到上一个检查点（`git revert` 或重置到检查点提交，不改写他人的工作）。阶段之间没有数据格式依赖；若最终 PR 合并后需要回滚，也按第7条做一次混合版本检查。

阶段依赖：

```text
S0 ──▶ S1 ──┬──▶ S2 Embeddings ──┐
            ├──▶ S3 Messages ────┼──▶ S6 清理
            └──▶ S4 Chat ──▶ S5 Responses + compat ─┘
```

S2、S3、S4 都只依赖 S1，互不依赖；在本分支上按 S2 → S3 → S4 → S5 → S6 顺序完成。

---

## S0 加强跨层回归测试（只加测试与开发工具，不改生产代码）

### 目标

在现有实现上把“迁移后必须保持的行为”变成可执行的断言，并记录设计稿中标为待验证的事实。所有新测试在基线上为绿。

### 新增测试（全部是新增）

| # | 测试（建议位置） | 内容 |
|---|---|---|
| T1 | `tests/integration/test_batch_surface_parity_postgres.py`（`dbtest`） | 四个目标的在线与 Batch **逐字段对照**。固定条件：同一 owner 与 key；非 auto 模型；单 adapter route（Messages 原生与 OpenAI 格式各一组；Chat 的 Fixed 与 RouteWise 各一组；Embeddings 单 adapter 与 `FallbackEmbeddingAdapter` 各一组）；静态定价；确定性假上游；在线请求不带 session 头；关闭小预算换模型。**应当相同**：`model_id`、`provider`、`endpoint_id`/`served_endpoint_id`、`status_code`、各 token 计数（含缓存列 NULL 与 0 的区别）、`cost_usd`、`params`、响应体（剔除上游 id）、`metadata.authenticated`、`user_id`、计费报告次数。**允许不同（逐项列明）**：`request_id`、时间与延迟字段、UA/referer/IP 相关字段、`batch_*` 归属、`ledger_attribution` 起始时间、Messages 不同版本组的 `anthropic-version`。不笼统断言整行相同。Chat 结果对照使用 `_client_body` 之后的形状 |
| T2 | 扩展 `test_batch_expansion_http_postgres.py` 的 [:459](../../../tests/integration/test_batch_expansion_http_postgres.py#L459) | 参数化到 Chat、Embeddings、Responses：owner 在线配额耗尽时 Batch 仍真实执行，在线槽与 interactive-load 计数不变 |
| T3 | 扩展同文件 [:561](../../../tests/integration/test_batch_expansion_http_postgres.py#L561) | 参数化到其余三个目标：模型容量或在线预留不足时 Batch 等待，释放后继续 |
| T4 | `tests/servers/test_surface_stream_bytes.py` | **流式字节快照**（规范化时间戳、随机 id 后逐字节比较）：Chat SSE 成功；Chat 未提交 4xx → HTTP 状态 + JSON 错误；Chat 提交后错误帧；Chat 强制流式（JSON + 空白 keepalive，状态码与 `scope["state"]` 标记的效果）；Messages SSE 原生与翻译；Responses SSE 成功与 `response.failed`。同时断言响应头集合 |
| T5 | 扩展 `test_auto_mode_surfaces.py` | Messages 与 Responses **流式** auto 请求在整个流的生命周期内计入具体模型（Chat 已有 [:392](../../../tests/servers/test_auto_mode_surfaces.py#L392)）；compat `/completion` 强制流式时同样 |
| T6 | `tests/servers/test_anthropic_messages_edges.py` 追加 | `stream=true` 时未知模型、无权限、全部熔断、抢输探测权都是 HTTP 404/503 + Anthropic 错误体，不是 SSE |
| T7 | `tests/servers/test_surface_disconnects.py` | 特征化（只记录观察到的事实，docstring 注明“记录现状，参见设计稿 Q7”）：Chat 迭代中断连 → 499 日志、无计费报告、无 routing 观察；Messages 开始迭代前断连且熔断器半开时 claim 是否残留；Chat 已 prime、未迭代时会话流与 router 流是否关闭。结论只覆盖测试传输层 |
| T8 | `tests/unit/serving/test_batch_scheduler.py` 或新文件 | 租约丢失后重复执行与计费（Q8）：能稳定复现就写成特征化测试；不能就在检查点说明中以评审证据记录并标为待验证。不使用无条件的 `xfail(strict=True)` |
| T9 | `tests/unit/serving/test_batch_targets_dispatch.py` 追加 | Q9：embedding adapter 的注册名与 `config.id` 不同时，Batch 执行得到什么（记录现状） |
| T10 | `tests/servers/test_responses_router.py` 追加 | Responses 一次请求：在线 limiter `try_acquire` 恰好一次；`api_logs` 恰好一行；计费报告恰好一次；routing 观察恰好一次（为 S5 的不变量建立基线） |

### 开发工具（`ops/dev/batch_api_smoke.py`，新增能力）

- 假上游增加 `/v1/embeddings`；`chat` 与 `messages` 在 `stream: true` 时返回确定性的 SSE。
- `models` 子命令为 smoke 模型增加一个 Embeddings 模型，以及一个 `router: routewise` 的 Chat 模型。
- 新增 `stream-check` 子命令：对网关发 Chat SSE、Chat 未提交错误、Messages SSE（原生与翻译）、Responses SSE，逐字节比较规范化后的输出，并做一次中途断连后检查 `api_logs`。
- `check` 子命令覆盖四个目标的 Batch（内联与文件），以及 T2 的“在线配额耗尽”场景。

### 验收

- `make test`、`make test-db-local` 全绿；新增测试在基线上为绿（特征化测试断言的是观察到的现状）。
- 基线网关上 `batch_api_smoke.py check` 与 `stream-check` 通过，保存规范化输出作为后续阶段的对照基准（只放在本地或 PR 附件，不提交生成物）。
- 若 T7/T8/T9 揭示与设计稿不同的事实，先更新设计稿再进入 S1。

### 回滚

只有测试与开发工具。

---

## S1 共享上下文与显式准入（外部行为不变）

### 改动文件

| 文件 | 改动 |
|---|---|
| `apps/backend/serving/surfaces/__init__.py`、`surfaces/context.py`（新） | `ORIGIN_HEADERS`、`ClientOrigin`（不含 `from_request`）、`BatchAttribution`、`Admission`、`InteractiveAdmission`、`BatchAdmission`、`ExecutionContext`。不导入 FastAPI/Starlette |
| `apps/backend/serving/servers/surface_http.py`（新） | `client_origin(request) -> ClientOrigin`（白名单过滤）；`timeout_probe(request) -> Callable[[], bool]`（今天 `completions._timeout_fired_probe` 的逻辑）。`client_origin` 从 `request.headers.raw` 按顺序挑出白名单条目，构造不可变 `Headers`，原始键字节不改写 |
| `apps/backend/serving/servers/concurrency.py` | `enforce_user_concurrency` 改为 `AsyncGenerator[InteractiveAdmission, None]`，在原位置 `yield InteractiveAdmission(tracker)`（两个分支都是）；获取与释放位置不变；`retarget_interactive_load` 不变 |
| `apps/backend/serving/utils/synthetic_probe.py` | 新增 `is_trusted_probe_headers(headers: Mapping[str, str], user_ctx)`；`is_trusted_probe(request, …)` 改为调用它 |
| `apps/backend/serving/observability/rejection_log.py` | `log_rejection` 增加接受 `ClientOrigin` 的形式，保留 `request=` 形式 |
| `apps/backend/serving/servers/routers/anthropic_messages.py` | 内部改用：`_conc` 是 `Admission` 时调用 `_conc.retarget`，否则走 `retarget_interactive_load(request, …)`；IP、UA、referer、白名单请求头取自 `client_origin(request)`；`affinity_key` 改为局部变量 |
| `apps/backend/serving/batch/dispatch.py` | `_run_messages` 传 `_conc=BatchAdmission()` 取代 `None`（按代码效果相同：今天 retarget 找不到 tracker） |

### 签名（提案）

```python
class InteractiveAdmission:
    __slots__ = ("_tracker",)
    def __init__(self, tracker: InteractiveLoadTracker) -> None: ...
    def retarget(self, canonical_model_id: str) -> None:
        self._tracker.retarget(canonical_model_id)
```

### 测试

- 现有：`test_enforce_user_concurrency.py`、`test_concurrency_endpoint.py`、`test_concurrency_runtime_resize.py`、`test_user_concurrency_limiter.py`、`test_auto_mode_surfaces.py`、`tests/servers/test_anthropic_messages_*.py`、`test_batch_*` 全绿；S0 全绿。
- 新增（评审更正 1）：请求头大小写与重复头的等价性——`session_identity` 对 `x-session-id`/`X-SESSION-ID`/混合大小写与重复值的结论，与直接读 `request.headers` 相同；admin 的 `x-route-pin` 任意大小写都生效、非 admin 都被忽略；可信 probe 的 `X-PROBE: synthetic` 任意大小写都识别；Messages 的 `Anthropic-Beta` 任意大小写都透传、重复时取第一个；Responses 删除 `X-Reasoning-Passthrough` 的每一种大小写变体（含重复条目），删除后 Chat 的序列化模式为 passthrough。
- 新增：`InteractiveAdmission` 只移动计数、不增减（`in_flight` 前后之和不变），依赖拆除时只递减一次；`client_origin` 丢弃 `Authorization`、`Cookie`、`X-Api-Key` 等非白名单头；`ClientOrigin.batch` 的 IP 是 `_BATCH_CLIENT`；`is_trusted_probe_headers` 与 `is_trusted_probe` 对同一组输入结论相同；`log_rejection` 两种形式写出的行相同。

### 隔离网关与混合版本

`batch_api_smoke.py check` 与 `stream-check` 输出与 S0 基准一致；混合版本检查第4项（共享 interactive load 互读）重点验证。

### 回滚

撤销 S1。S2–S4 依赖 S1，必须先撤销它们。

---

## S2 Embeddings 核心

### 改动文件

| 文件 | 改动 |
|---|---|
| `apps/backend/serving/servers/routers/embeddings.py` | 新增 `EmbeddingServices`、`execute_embeddings(request, raw_body, ctx, services)`：今天 `create_embeddings` 从 :154 到 :485 的主体逐行搬移，`http_request` 的用法换成 `ctx.origin`，`batch_log_attribution(user_ctx)` 换成 `ctx.batch.log_metadata() if ctx.batch else {}`。路由 `create_embeddings` 签名不变，主体变为：读 `raw_body` → 构造 `ExecutionContext`（`admission=_concurrency_slot`）与 `EmbeddingServices`（来自注入值，`dependency_overrides` 继续生效）→ `return await execute_embeddings(...)` |
| `apps/backend/serving/batch/dispatch.py` | `_run_embeddings(services, ctx, body)`：校验（400 文案不变）→ `execute_embeddings(payload, body, ctx, embedding_services(services))`。`_dispatch` 只为仍走 handler 的目标构造 `_request`（集合 `_HANDLER_TARGETS`）；`ExecutionContext` 对所有目标构造。`HANDLERS` 删除 embeddings 项 |

### 迁移桥

- `_dispatch` 中执行器分两类：`_CORE_EXECUTORS[target](services, ctx, body)` 与 `_HANDLER_EXECUTORS[target](services, request, caller, body)`。S6 删除后者。
- `user_ctx` 中的 Batch 归属合并继续保留（其余目标还在用）；Embeddings 核心只从 `ctx.batch` 读归属，值相同，所以日志不变。

### 测试

- 现有全绿：`test_embeddings_logging.py`、`test_embeddings_tier_gate.py`、`test_embedding_fallback.py`、`test_admin_embedding_denylist.py`、`test_batch_targets_dispatch.py` 中全部 embeddings 用例（:286–:393）、[`test_each_target_is_billed_once…`](../../../tests/unit/serving/test_batch_targets_dispatch.py#L157)、S0 的 T1/T2/T3/T9。
- 调整：[`test_every_injected_argument_reaches_the_handler_explicitly`](../../../tests/unit/serving/test_batch_targets_dispatch.py#L213) 与 [`test_each_target_has_an_executor_and_a_handler`](../../../tests/unit/serving/test_batch_targets_dispatch.py#L480) 只对 `HANDLERS` 中剩余的目标参数化；新增“每个目标恰好在核心执行器或 handler 执行器中之一”。
- 新增：Embeddings Batch 项在 `dispatch._request` 与 `call_handler` 被替换为抛错函数时仍成功；在线 `execute_embeddings` 收到的 `ctx.admission` 是依赖给出的同一个对象。

### 隔离网关与混合版本

smoke `check` 的 Embeddings 批次（内联与文件）通过；在线 `/v1/embeddings` 与 S0 基准一致；混合版本检查 1、2、5。

### 回滚

撤销 S2。

---

## S3 Messages 核心（上一版试点的 P2）

### 改动文件

| 文件 | 改动 |
|---|---|
| `apps/backend/serving/servers/routers/anthropic_messages.py` | 新增 `MessagesConstraints`（`online()`、`for_batch_item(body, protocol_profile)`）、`MessagesServices`、`MessagesResponse`、`MessagesError`、`MessagesStream`、`_Prepared`、`_prepare`、`_run_unary`、`execute_messages`、`render_messages_http`、`_publish_compat_scope`。路由变为“解析 → 调用 → 渲染”。`_pick_dispatch_adapter` 与 `_maybe_reroute_small_reasoning_call` 改为接收 `constraints` 参数，删除对 `BATCH_EXECUTION` 的读取 |
| `apps/backend/serving/batch/dispatch.py` | `_run_messages(services, ctx, body, protocol_profile)` 调用核心并映射结果；删除种子中的 `BATCH_EXECUTION` 与 `ANTHROPIC_VERSION`；`HANDLERS` 删除 messages 项；`_request` 不再附加 `anthropic-version` 头 |
| `apps/backend/serving/batch/targets.py` | 只改 `AdapterRequirement` 的 docstring（不再“published on req_ctx”） |

### 迁移桥

- `ANTHROPIC_VERSION` 由 `_publish_compat_scope` 在核心中写入，值来自 `constraints.anthropic_version`（只有 `for_batch_item` 设置）；adapter 读取方式不变。
- `BATCH_EXECUTION` 键名留在 `REQUEST_SCOPED_KEYS` 到 S6；不再有写入者与读取者。
- 核心先留在原模块，原因：25 个测试文件引用该模块、十余个私有名被 monkeypatch（`_MAX_STREAM_IDLE`、`_resolve`、`_schedule_log_store_task`、`_pick_adapter_for_role`、`_resolve_stream_usage` 等）。

### 测试

- 现有全绿：`tests/servers/test_anthropic_messages_*.py`（149 个测试函数）、`test_anthropic_stream_interleavings.py`、`test_models_router_anthropic.py`、`test_batch_protocols.py`、`test_batch_expansion_http_postgres.py` 的 Messages 用例（:459、:561、:598、:639、:708、:738、:764、:794、:853、:890）、S0 的 T1/T4/T5/T6/T7。
- 调整：`HANDLERS` 相关守卫同 S2。
- 新增：Messages Batch 路径在 `_request`/`call_handler` 抛错时仍成功，且全程不读写 `BATCH_EXECUTION`（对 `req_ctx` 打桩检查）；`MessagesOutcome → DispatchResult` 映射（成功、`MessagesError` 带 `anthropic` 子对象、意外的 `MessagesStream` → `aclose` + 502）；`MessagesStream.aclose()` 在已迭代与未迭代两种情况下都释放 claim 恰好一次（`end_dispatch` 调用次数）；返回 `MessagesStream` 时在线槽与 tracker 仍被持有；`for_batch_item` 的 `anthropic_version` 等于存储的 `protocol_profile`，`online()` 为 `None`。

### 隔离网关与混合版本

smoke `check`（Messages 内联、文件、两个版本）与 `stream-check`（Messages 原生与翻译，含中途断连）与 S0 基准一致；混合版本检查 1–5，重点是第3项（版本）。

### 回滚

撤销 S3（S1、S2 可保留）。

---

## S4 Chat 核心

### 改动文件

| 文件 | 改动 |
|---|---|
| `apps/backend/serving/servers/routers/completions.py` | 新增 `ChatOptions`（`online`/`delegated`/`batch`）、`ChatServices`、`ChatResponse`、`ChatStream`、`execute_chat(body, ctx, services, options)`、`render_chat_http(outcome, request) -> dict | StreamingResponse`、过渡桥 `ScopeAdmission`。`execute_chat` 是今天 :478–:1279 的逐行搬移；`request` 的用法按设计稿第3.3节 H1–H10 替换；`StreamSession` 的构造参数改为 `request_headers=ctx.origin.headers`、`timeout_fired_probe=ctx.timeout_probe`。强制流式的 keepalive 包装、首项拉取、`scope["state"]` 标记和 `X-Provider` 写入留在 `render_chat_http` |
| `apps/backend/serving/servers/routers/completions_stream.py` | 不改逻辑；如类型标注需要，把 `request_headers` 标为 `Mapping[str, str]` |
| `apps/backend/serving/batch/dispatch.py` | `_run_chat(services, ctx, body)` → `execute_chat(..., ChatOptions.batch())`；`ChatResponse.body` 经 `_client_body`；意外的 `ChatStream` → `aclose()` + 502；`HANDLERS` 删除 chat 项 |

### `ChatStream` 的行为对照（必须逐项相同）

| 今天（completions.py） | S4 |
|---|---|
| :896 创建 router 流 | `execute_chat` 内创建，存入 `ChatStream` |
| :902-923 构造 `StreamSession` | 同上 |
| :988-1006 SSE 拉首项、未提交错误 → 关闭、发布 provider、`HTTPException` | `ChatStream.prime()` |
| :1018-1022 `StreamingResponse(_chain_first_then_rest(...))` 与响应头 | `render_chat_http` 调 `prime()` 后返回，`frames()` 即 `_chain_first_then_rest` |
| :927-976 强制流式 | `render_chat_http` 在 `buffered` 时使用 `session_stream()`，其余代码原样 |

### 迁移桥（S4 引入、S5 删除）

- Responses 与 compat 仍调用 `chat_completions` 路由函数。路由函数构造上下文时：`_concurrency_slot` 是 `InteractiveAdmission` → 直接使用；否则 `ScopeAdmission(request)`（`retarget` = 今天的 `retarget_interactive_load(request, …)`）。
- `ClientOrigin` 取自 `client_origin(request)`，所以 Responses 删除 `X-Reasoning-Passthrough` 的效果在 S4 仍然生效。
- 路由函数仍读 `request.json()`，所以 Responses 改写 `_json`、compat 改写 `_body`/缓存 JSON 的效果在 S4 仍然生效。

### 测试

- 现有全绿：`test_completions.py`、`test_completions_request_contract.py`、`test_completions_session_identity.py`、`test_completions_cache_miss_surface.py`、`test_timeout_streaming_exempt.py`、`test_auto_mode_surfaces.py`、`test_compat.py`、`test_responses_router.py`、引用 `completions_stream` 的 4 个测试文件、routing 与 RouteWise 中跨 serving 边界的测试、Batch 单元与集成测试、S0 全部。
- 调整：[`test_batch_dispatch.py:308`](../../../tests/unit/serving/test_batch_dispatch.py#L308) 把 patch 目标从 `completions.chat_completions` 改为 `completions.execute_chat`，断言不变（意外异常 → 500、`unknown`）；守卫同 S2。
- 新增：Chat Batch 路径在 `_request`/`call_handler` 抛错时仍成功；`execute_chat` 不接收、不读取 `Request`（签名检查 + 对 `ctx.origin` 之外的请求头不可见的测试）；Batch 下 `retarget` 调用的是 `BatchAdmission`；`ChatOptions.batch()`/`delegated()` 下强制流式永不触发、probe 日志开关视为关闭；`ScopeAdmission` 在有/无 tracker 时的行为与 `retarget_interactive_load` 相同。
- Fixed 与 RouteWise：T1 的两组对照都通过；RouteWise 的观察与 hedging 测试不变（不改 `routing/`）。

### 隔离网关与混合版本

smoke `check`（Chat 内联与文件；Fixed 与 RouteWise 模型）与 `stream-check`（Chat SSE、未提交错误、强制流式开启时的 JSON keepalive）与 S0 基准一致；Responses 与 compat 的在线请求不变；混合版本检查 1、2、4、5。

### 回滚

撤销 S4（S5 依赖它，必须先撤销 S5）。

---

## S5 Responses 与 compat 改为函数调用 Chat 核心

### 改动文件

| 文件 | 改动 |
|---|---|
| `apps/backend/serving/servers/routers/responses.py` | 新增 `ResponsesServices`、`ResponsesResult`、`ResponsesStream`、`execute_responses(body, ctx, services)`。删除 `_force_reasoning_passthrough`、对 `request._json`/`_body`/`_headers` 的改写、对 `body_iterator` 的读取、`from …completions import chat_completions`。`_stream_response` 的 `_gen` 移入 `ResponsesStream`。路由 `create_response` 签名不变：解析 JSON（400 文案不变）→ 构造 `ExecutionContext`（`admission=_concurrency_slot`）→ `execute_responses` → `JSONResponse(result.body)` 或 `StreamingResponse(stream, text/event-stream, _SSE_HEADERS)` |
| `apps/backend/serving/servers/routers/compat.py` | 两个路由改为构造上下文并调用 `execute_chat(..., ChatOptions.online(runtime_settings))`，用 `render_chat_http` 渲染（返回原始 dict，不加 `response_model`）；`/v1/completions` 在路由中转换 `prompt`，**保留**未加保护的 `request.json()`（Q14）；删除 `request._body` 改写 |
| `apps/backend/serving/servers/routers/completions.py` | 删除 `ScopeAdmission`；`chat_completions` 路由直接使用依赖给出的 `InteractiveAdmission` |
| `apps/backend/serving/batch/dispatch.py` | `_run_responses(services, ctx, body)` → `execute_responses(body, ctx, ResponsesServices(chat_services(services), None))`；`HANDLERS` 删除 responses 项 |

### 测试

- 现有全绿：`test_responses_router.py`、`test_responses_translator.py`、`test_compat.py`、`test_auto_mode_surfaces.py`（[:726](../../../tests/servers/test_auto_mode_surfaces.py#L726)、[:755](../../../tests/servers/test_auto_mode_surfaces.py#L755)）、S0 的 T1/T4/T5/T10。
- 改写（断言不变，替身换位置）：`test_responses_router.py` 的 [:787](../../../tests/servers/test_responses_router.py#L787)、[:824](../../../tests/servers/test_responses_router.py#L824)、[:918](../../../tests/servers/test_responses_router.py#L918) 与 [`test_batch_review_contracts.py:98`](../../../tests/unit/serving/test_batch_review_contracts.py#L98) 改为替换 `responses.execute_chat`，返回 `ChatResponse` 或一个最小的 `ChatStream` 替身；`_open_stream` 辅助函数改为直接构造 `ResponsesStream`；原断言“`request.json()` 中的 messages 是译后的”改为断言传给 `execute_chat` 的 `body`。
- 新增：Responses 与 compat 在 `_json`/`_body`/`_headers` 被设为只读（对 `Request` 打桩）时仍成功；传给 `execute_chat` 的 `ctx.admission` 与路由依赖给出的是同一个对象；T10 的“恰好一次”在 S5 后保持；Responses 流式在 Chat 未提交错误时仍是 HTTP 状态（T4 覆盖）。

### 隔离网关与混合版本

smoke `check`（Responses 内联与文件）与 `stream-check`（Responses SSE 成功、失败、中途断连后的 `incomplete` 落库）与 S0 基准一致；compat 两个端点在线请求不变；混合版本检查 1、2、4、5。

### 回滚

撤销 S5，S4 的过渡桥随之恢复。

---

## S6 清理

### 改动

| 文件 | 改动 |
|---|---|
| `apps/backend/serving/batch/dispatch.py` | 删除 `call_handler`、`_INJECTED_DEFAULTS`、`_request`、`HANDLERS`、`_HANDLER_EXECUTORS`、`_read_json_response`、只为 handler 服务的 `_error_from_body` 分支；`publish_caller_role(user_ctx)` 不再合并 Batch 归属；更新模块 docstring |
| `apps/backend/serving/servers/routers/completions.py` | metadata 只从 `ctx.batch` 取 Batch 归属（`batch_log_attribution(user_ctx)` 不再有输入） |
| `apps/backend/serving/batch/attempts.py` | `batch_log_attribution` 若已无调用者则删除 |
| `apps/backend/serving/utils/context.py` | 从 `REQUEST_SCOPED_KEYS` 删除 `BATCH_EXECUTION` |
| `apps/backend/serving/servers/concurrency.py` | `retarget_interactive_load` 若已无调用者则删除；`user_slot_for` 是否删除另议（只剩测试调用，与本计划无关） |
| 可选：`apps/backend/serving/surfaces/{messages,chat,embeddings,responses}.py` | 核心迁出路由模块（目录名待 Q2），同步更新 monkeypatch 目标；加导入检查测试：核心模块不导入 `Request`、`Response`、`StreamingResponse`、`JSONResponse`、`Depends`、`Header`（`HTTPException` 待 Q12） |
| `docs/agents/specs/2026-10-25-batch-inference-design.md` 等 | 不改历史记录；在本设计稿中把状态改为“已在分支上实施，待评审”，并列出各检查点提交 |

### 测试

- 删除：`test_call_handler_refuses_a_missing_injected_argument`、`test_every_injected_argument_reaches_the_handler_explicitly`；`test_each_target_has_an_executor_and_a_handler` 改为“每个目标都有核心执行器”。
- 全量门禁、S0 全部、smoke `check` 与 `stream-check`、混合版本检查 1–5。
- 代码检索：`rg "call_handler|_request\(|BATCH_EXECUTION|request\._json|request\._body|body_iterator" apps/backend/serving` 只剩无关命中（逐条说明）。

### 回滚

撤销 S6；S2–S5 的核心不受影响。

---

## 附录 A 每个检查点说明（以及最终草稿 PR 汇总）必须包含的内容

1. 本阶段迁移了哪个目标、保留了哪些过渡桥。
2. 门禁命令与结果（含 `make test-db-local`）。
3. 隔离网关验证：命令、smoke 输出摘要、与 S0 基准的差异（应为无）；注明只代表合成上游。
4. 混合版本检查的结果，或明确写“未检查，不支持混跑”。
5. 标为待验证的条目是否有新证据；若 S0 特征化测试的结论改变，回到评审。
6. 修改了哪些测试的替身位置，以及为什么原断言仍被证明。

## 附录 B 隔离网关验证步骤（每个运行时阶段）

```bash
# 本 worktree 的一次性数据库（共享容器，每个 worktree 一个库）
eval "$(ops/dev/test-db-local.sh env)"
# 合成上游与模型表
python ops/dev/batch_api_smoke.py upstream --port <空闲端口> &
python ops/dev/batch_api_smoke.py models --upstream http://127.0.0.1:<端口> --out /tmp/<阶段>-models.yaml
# 启动网关（真实 uvicorn），随后（参数以脚本 docstring 为准）：
python ops/dev/batch_api_smoke.py seed > /tmp/<阶段>-keys.json
python ops/dev/batch_api_smoke.py check --gateway http://127.0.0.1:<网关端口> ...
python ops/dev/batch_api_smoke.py stream-check --gateway ...   # S0 新增
```

混合版本：从基线 commit 建一个分离 HEAD 的 worktree，以同一个数据库启动基线网关（不同端口），两边都开启 worker，再跑 `check`；用 `batch_attempts.worker_id` 确认两种 worker 都执行过项目。不要在主 checkout 中切换分支。

## 附录 C 与设计稿开放问题的关系

本计划不实现任何开放问题（Q1–Q15）中的新增行为。若评审对某个问题做出决定，作为本分支之外的后续工作处理，并在本计划中注明它依赖哪个检查点。
