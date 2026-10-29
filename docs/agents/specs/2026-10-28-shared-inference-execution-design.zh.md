# 在线接口与 Batch 共用的协议执行：四个目标逐个从 HTTP handler 中抽出

日期：2026-10-28。**状态：已在分支 `codex/feature/shared-inference-all-targets-20261028` 上实施（S0–S6 检查点），待评审；未合并、未部署。** 第1–14节是实施前写下的设计，类型与函数名以实施为准；实施与设计的差异、验证证据与未完成项见第16节。

基线：dev `a738f4915`（#1687 已合并）。上一版只覆盖 Messages 试点，以 `5f3747b1d` 为基线；两者之间 `anthropic_messages.py`、`completions*.py`、`responses.py`、`compat.py`、`embeddings.py`、`serving/batch/*`、`concurrency.py` 都没有改动，所以沿用的行号仍然有效。期间改动的 `adapters/base.py`（#1613，Messages 翻译）和 `utils/request_ip.py`（#1691，动态代理信任）不改变本稿依赖的接口：`get_client_ip_info(request)`、`preset_client_ip_info`、`BaseAdapter.messages/stream_messages` 的签名都没有变。

关联：[issue #1690](https://github.com/HarvardMadSys/hybridInference/issues/1690)。实施计划：[2026-10-28 四目标实施计划](../plans/2026-10-28-shared-inference-execution-all-targets.md)。

前序记录：[2026-10-25 Batch 推理设计](2026-10-25-batch-inference-design.md)（D5：Batch 走真实 handler，不发 HTTP 回环请求）、[2026-10-26 Batch API 扩展](2026-10-26-batch-api-expansion-design.zh.md)、[2026-10-27 默认可用性修订](2026-10-27-batch-default-availability-amendment.zh.md)、[2026-10-28 统一 Batch API 修订](2026-10-28-unified-batch-api-amendment.zh.md)、[2026-09-14 可组合混合路由设计](2026-09-14-composable-hybrid-routing-design.zh.md)。本稿不取代其中任何决定，只改变执行代码的组织方式：D5 的“不发回环请求、复用同一份推理实现”保持，“调用 handler”换成“调用协议执行函数”。

文中凡未经测试或实测的判断都标为“**推断**”或“**待验证**”；“基线事实”只指可以从源码直接读出的内容；“提案”指本稿新增的设计。

---

## 0 一页结论

1. **四个目标都迁，按依赖分阶段。** Messages、Chat Completions、Embeddings、Responses 各自抽出一个协议执行函数（下称“核心”），在线路由和 Batch 都直接调用它。Batch 不再构造 Starlette `Request`、不再经 `call_handler` 调用路由 handler、不再解析 `JSONResponse`。阶段：S0 只加测试 → S1 共享上下文与显式准入 → S2 Embeddings、S3 Messages（两者互不依赖）→ S4 Chat → S5 Responses 与 compat（依赖 S4）→ S6 删除伪造层与守卫。上一版的 Messages 试点是其中的 S3，不再是终点。
2. **共享的只是“执行上下文”，不是请求格式。** 共享层只有：调用来源的值（`ClientOrigin`）、已认证主体的只读视图、准入能力（`Admission.retarget`）、Batch 归属（`BatchAttribution`）和超时探针。请求体、选项、结果、错误、流对象都按协议分别定义，各协议的路由、计费、日志口径原样保留（第3.7节）。
3. **四个协议今天的实现并不相同，迁移后也不会相同。** Messages 自己挑一个 adapter，没有 fallback，持有 health claim；Chat 经 `FixedRouter`/`RouteWise` 执行，重试、fallback、hedging、health 与 feedback 都在 router 内；Embeddings 走专属的 `embedding_adapters` 表，`FallbackEmbeddingAdapter` 在 adapter 内回退，不经过 health registry；Responses 与 compat 不自己推理，而是委派 Chat。本稿不把它们合并成一个执行循环。
4. **Responses 委派 Chat 改为“函数调用 Chat 核心”。** 同一个 `ExecutionContext`（同一个准入能力）传下去，不再改写 `request._json`/`_body`/`_headers`，不再读 `StreamingResponse.body_iterator`。在线槽只由 Responses 路由的依赖获取一次；路由、计费、`api_logs` 只在 Chat 核心里发生一次。
5. **准入与配额分开，所有权不变。** 在线：`enforce_user_concurrency` 获取每用户槽与 interactive-load 计数，并在响应体发送完毕后释放。Batch：scheduler 的角色配额、模型容量（减去在线负载与预留）和租约。核心不获取、不释放其中任何一项，只通过 `retarget` 把在线负载移到具体模型上；Batch 传入 no-op。
6. **不加 `skip_auth` / `skip_billing` / `skip_concurrency`。** 核心每次执行都复核模型权限，按各协议现有条件计费和记录日志。准入限制（Messages 的 adapter 过滤、禁止换模型）只收窄可执行范围。**冻结的协议版本不同于准入限制**：`anthropic-version` 只能取创建时验证并存储的值，并保留该版本原有的语义，但它不一定比另一个版本“更严格”，不能用“只能收窄”来论证它的安全性（第5.4节）。
7. **流式行为逐字节保持。** 校验与预检错误仍在响应头发出之前以 HTTP 状态回答；Chat 仍先拉取首项再决定状态码；Messages 仍在 HTTP 200 之后以 SSE error 帧报告上游错误；Responses 仍在发出终止事件之前落库。核心返回的是可迭代的流对象，不缓冲。
8. **不承诺上游恰好执行一次，不承诺跨尝试不重复计费。** 同一个 attempt 只写入一次结果，同一次核心执行只有一个计费点；不同 attempt 或租约丢失后的重复执行可能各自计费（第6.5节）。
9. **默认保持行为。** 会改变外部可观察结果的点（retarget 不匹配即拒绝、流式 claim 获取点、Batch 请求 id 的上报范围、错误载体替换等）都只列为开放问题，不在迁移中顺带实现。

### 0.1 与 issue #1690 的对照

| issue 中的要求 | 本稿处理 |
|---|---|
| 抽出按协议分别实现的应用执行函数；代码边界，不是新服务、第二个 router 或万能格式 | 采纳，四个目标各一个核心（第4节）。Chat 核心调用的仍是 `RouterProtocol` 的现有实现，不新建通用 router |
| 边界职责：在线入口、Batch API 与 scheduler、协议执行、现有 router 与 adapter | 采纳并细化（第4.1节）。**偏差**：issue 的图是“执行 → 现有 router”。实际上只有 Chat（以及委派它的 Responses、compat）调用 router；Messages 用 `router_exec.eligible_adapters` 自选 adapter，Embeddings 直接调用 `embedding_adapters[model]`。本稿把这两段选择原样保留在各自的核心里，不搬进 router，也不给它们加 fallback |
| 显式可信上下文：已认证主体、请求或作业标识、冻结的协议选项、取消与生命周期信息 | 采纳（第4.2节）。取消仍以 asyncio 取消表达，加一个只读的超时探针；不新增取消令牌（Q10） |
| 准入所有权显式，获取与释放各恰好一次 | 采纳，按“唯一责任方、唯一释放点”表述，并逐项说明哪些释放函数可以重复调用（第5.2节） |
| 不依赖 `Depends`、伪造 ASGI 请求或 HTTP 响应对象；在线流式不缓冲 | 采纳。OpenAI 形状的三个核心在过渡期仍以 `HTTPException` 作为错误载体（它是异常类，不是请求或响应对象），是否替换列为 Q12（第4.8节） |
| 验收：在线配额耗尽时 Batch 仍真实执行；在线槽与在线负载计数不变 | 现有 #1687 真实 Postgres 测试覆盖 Messages（[:459](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/tests/integration/test_batch_expansion_http_postgres.py#L459)）；S0 为 Chat、Embeddings、Responses 补同类测试。每个阶段都必须为绿 |
| 验收：模型容量或在线预留不足时 Batch 等待 | Messages 已有（[:561](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/tests/integration/test_batch_expansion_http_postgres.py#L561)）；S0 补其他目标。scheduler 逻辑不改 |
| 验收：执行期复核 owner/key 与模型可见性；原生字段、冻结版本、结果与错误信封、usage、日志、计费保持；流式与断连有回归；隔离网关 + 合成上游 + 真实 PostgreSQL | 第7、9节与实施计划 |
| 增量交付：先加测试 → 最小契约与 Messages 试点 → 逐个迁移，调用方消失后再删 handler 守卫 | S0 → S1 → S2/S3 → S4 → S5 → S6 |
| 把同样的 handler 包进新类而不去掉 HTTP 耦合解决不了问题 | 采纳：任何阶段都不构造 `Request` 再调回 handler，也不把 `Request` 改名传入核心 |
| `anthropic-version` 只取请求头已在 #1687 完成；严格准入与逐项准入是另一个产品决定 | 不在本稿范围 |

### 0.2 相对上一版的变化

- 范围从“只做 Messages、其余三个保持 `call_handler`”扩展为四个目标全部迁移。上一版第3节的 Messages 事实、第5.5节的类型草图、第6节的流式与异常矩阵保留在第3.2、4.3、6节。
- 更正：上一版第0节第5点把准入约束与协议版本写在一起，容易被理解为“版本也只能收窄”。本版把两者分开（第5.4节）。
- 新增 Chat、Responses、compat、Embeddings 的现状核实（第3.3–3.5节）、四协议计费与日志口径对照（第3.7节）和逐协议的流式边界（第6节）。

### 0.3 独立评审后的更正（2026-10-28，第二轮）

1. **请求头必须保持 Starlette `Headers` 的语义。** 上一轮把 `ClientOrigin.headers` 设计成小写键的普通映射，会让 `session_identity` 读 `X-Session-ID`、Chat 读 `X-Route-Pin` 失效，也丢掉重复头的取值规则。改为由原始条目构造的不可变 `Headers`（第4.2节），并要求对五类读取补大小写与重复头测试。
2. **不按阶段分别提 PR、合并或部署。** S0–S6 是同一分支上可以在本地逐个评审的检查点（本地提交），最后由协调方开一个覆盖四个目标的草稿 PR。任何阶段都不需要合并或部署才能进入下一阶段；过渡桥在 S6 之前全部删除（实施计划第0节）。
3. 实施细化：Messages 拒绝日志改用取值的 `RejectionSource`（第4.2节）；Messages 在线路由对非法 JSON 的处理顺序保持不变（请求 id、`note_request_id`、affinity 写入发生在 JSON 校验之前），核心以一个“请求体不可解析”的哨兵值接收这种情况。

---

## 1 动机

**对产品**：Batch 的价值在于和在线请求是“同一个产品”：同样的模型可见性、路由、计费、日志与错误语义。`call_handler` 拦得住“漏传了注入参数”，拦不住“handler 从 `Request`、`scope["state"]` 或 contextvar 读到了 Batch 伪造环境里没有的东西”。

**对维护者**（基线事实）：

- Batch 专用策略经 contextvar 进入 Messages handler：[`req_ctx.BATCH_EXECUTION`](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/utils/context.py#L89-L94) 在 [`_pick_dispatch_adapter`](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/anthropic_messages.py#L550-L564) 中过滤候选 adapter，在 [`_maybe_reroute_small_reasoning_call`](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/anthropic_messages.py#L784-L786) 中禁止换模型。这个键没有来源约束，任何代码都能写入。[routing/AGENTS.md](../../../apps/backend/routing/AGENTS.md) 要求“正确性关键的生命周期状态不能只依赖环境化的请求上下文”。
- Batch 归属合并进认证上下文 `user_ctx`（[dispatch.py:273-280](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/batch/dispatch.py#L273-L280)），三个 handler 再用 [`batch_log_attribution(user_ctx)`](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/batch/attempts.py#L111-L118) 读回来。已认证身份与普通数据混在同一个 dict 里。
- 在线负载的 retarget 依赖 `request.scope["state"]` 里的 tracker（[concurrency.py:229-240](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/concurrency.py#L229-L240)）。Batch 恰好因为伪造请求里没有 tracker 而得到 no-op；Responses 与 compat 恰好因为复用同一个 `Request` 而得到正确的 retarget。两者都来自对象共享，而不是显式声明。
- Responses 改写 Starlette 的私有属性来委派 Chat（第3.4节），Chat 的流式结果被当作 `StreamingResponse` 读取内部迭代器。

---

## 2 范围

**在范围内**：

- 在线入口：`/v1/messages` 与 `/anthropic/v1/messages`；`/v1/chat/completions`；`/v1/embeddings`；`/v1/responses`（创建）；compat 的 `/completion` 与 `/v1/completions`（它们委派 Chat，必须随 Chat 一起改）。
- Batch 的四个目标：`chat`、`embeddings`、`messages`、`responses`（[`dispatch.py`](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/batch/dispatch.py)）。
- 为此所需的最小共享上下文、准入能力与测试。

**不在范围内**：`GET/DELETE /v1/responses/{id}`（只读写 response store，不推理）；`count_tokens`（只在共享 `_resolve` 时随 Messages 调整）；Batch 的提交、准入、存储、调度算法；router 内部实现；其余见第10节。

---

## 3 现状核实（基线事实，`a738f4915`）

### 3.1 Batch 的公共外壳（四个目标相同）

```text
BatchScheduler._round → _top_up(model)                     scheduler.py:554
   cap = 模型上限 − 在线 in-flight（本进程或共享视图）− interactive_reserve
   claim_items（租约）→ 每项一个任务（空 Context）
  └─ _attempt(entry, item)                                  scheduler.py:700
       ├─ authenticate_stored_key(key_hash, op_store)       :721（撤销、日额度 → 暂停 owner）
       ├─ may_use_batch(role)                               :743
       ├─ _dispatch_body：body["model"] = 准入时固定的 canonical（四个目标都如此）  :785-800
       ├─ _take_user_slot：角色批量配额（独立 limiter，不是在线槽）  :802-833
       ├─ store.mark_dispatched(item, worker, attempt)       :760
       ├─ dispatch_item(...)                                 :769
       │    └─ asyncio.create_task(_dispatch, context=Context())   dispatch.py:211
       │         ├─ stream 强制为 False（Embeddings 除外）       :247
       │         ├─ 种子作用域 reset_request_scope(request_id, UA, 定价时间与快照)  :253-272
       │         │    Messages 另种 BATCH_EXECUTION、ANTHROPIC_VERSION          :260-263
       │         ├─ observing() 计费观察者（contextvar）          :269
       │         ├─ publish_caller_role({**user_ctx, batch_job_id, batch_item_id, batch_attempt})  :273
       │         ├─ _request(...)：伪造 Request，client=None，预置 _BATCH_CLIENT   :281、:442-480
       │         ├─ anyio.move_on_after(timeout_s)
       │         ├─ _EXECUTORS[target] → call_handler(handler, ..., _conc/_concurrency_slot=None)  :336-422
       │         ├─ _completed：JSONResponse/dict → DispatchResult   :569-590
       │         └─ cost_status：观察者看到计费 → billed/unknown；否则按分支推导   :305-318
       └─ finally: give_back()（角色配额）                    :781
  → _settle → complete_item / retry_item（以 attempt_seq 为栅栏）  :835
```

### 3.2 Messages（上一版第3节，核实仍成立）

**在线调用链**：

```text
RequestIdMiddleware：reset_request_scope(request_id, UA, 定价时间/快照)   middleware/request_id.py:52
→ 路由 anthropic_messages                                           anthropic_messages.py:1550
   Depends: verify_api_key、enforce_user_concurrency
   ── 预检（全部在返回响应之前；失败即 HTTP 状态 + Anthropic 错误体）──
   ├─ request_id、note_request_id；affinity 写入 req_ctx                 :1563-1586
   ├─ request.json()；必填与形状校验 → 400                               :1588-1604
   ├─ normalize_inline_system；auto 选型（request.app.state.services）  :1611-1669
   ├─ _resolve：别名、model_access_denial、eligible_adapters、key tier、定价检查  :1672（定义 :576）
   ├─ session、consume_session_fields、小预算换模型、retarget             :1722-1766
   ├─ anthropic-beta 透传；health_registry.begin_dispatch（抢输 → 503）  :1768-1794
   ├─ OpenAI 格式 adapter 的 effort 翻译与字段清理；prefill 测量；metadata  :1810-1947
   ── 执行 ──
   ├─ stream=true → StreamingResponse(_gen())                          :1949-2347
   │    _gen：首次迭代时取 prefill 租约；reader 任务读 adapter.stream_messages；
   │          keepalive / 空闲上限；上游错误转 SSE error 帧（HTTP 200 已发出）；
   │          finally：释放 prefill、end_dispatch、记录日志并计费
   └─ 非流式：adapter.messages；except 分支返回错误；finally 释放；成功则记录并计费   :2349-2592
```

**原生与翻译**：Messages 从不调用 `chat_completions`、`FixedRouter` 或 `RouteWise`，通过 `router_exec.eligible_adapters` 按 route 顺序选**一个** adapter，没有 fallback（[:513-570](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/anthropic_messages.py#L513-L570)）。原生 Anthropic adapter 原样转发请求体；OpenAI 格式的 adapter 走 [`BaseAdapter.messages`/`stream_messages`](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/adapters/base.py#L273-L330) 的默认实现，在 **adapter 内部**把请求译为 OpenAI 格式、调用同一个 adapter 的 `chat_completion`/`stream_chat_completion` 再译回。翻译路径没有穿过任何 handler 边界，也不经过 router。

**计费与日志**：都在 [`_schedule_log_store_task`](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/anthropic_messages.py#L1009)：`schedule_log` 写 `api_logs`（同步入队），状态为 200 时以 `tracked_task` 调度日额度增量（[:1119-1127](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/anthropic_messages.py#L1119-L1127)），并同步调用 `observe_billing`。两条调用路径都只在 `log_store` 存在时调用它（[:2295](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/anthropic_messages.py#L2295)、[:2573](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/anthropic_messages.py#L2573)）。

**对 `Request` 与 contextvar 的用法**：

| # | 用法（位置） | 性质 | 去向（提案） |
|---|---|---|---|
| R1 | `await request.json()`（:1589） | 输入 | 入口解析；核心接收 `dict` |
| R2 | `get_client_ip_info(request)`（:1575；metadata :1898-1902） | 入口事实 | `ClientOrigin.ip`；Batch 用 `_BATCH_CLIENT` |
| R3 | `user-agent`、`referer`（:1896-1897） | 输入，仅日志 | `ClientOrigin` |
| R4 | `session_identity(request.headers, body)`（:1640、:1722） | 输入 | 白名单请求头视图 |
| R5 | 透传 `anthropic-beta`（:829-841） | 输入 | 同一白名单；Batch 创建时就拒绝它 |
| R6 | `request.app.state.services`（:1623） | 服务查找 | 显式服务 `auto_services` |
| R7 | `retarget_interactive_load(request, …)`（:1766） | 准入资源 | `ctx.admission.retarget` |
| R8 | `log_rejection(request=…)`（:1645、:1683） | 读路由路径、IP、探针头、`app.state` | `log_rejection_from(source=RejectionSource(...))`，取值来自 `ctx.origin` 与应用服务对象 |
| C1 | 读 `BATCH_EXECUTION`（:550、:784） | Batch 策略经无来源约束的环境通道传入 | 删除，改为显式参数 `MessagesConstraints` |
| C2 | `ANTHROPIC_VERSION`（dispatch 写入；adapter 在 [anthropic.py:110-120](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/adapters/anthropic.py#L110-L120) 读取，只接受已知版本） | 冻结的协议选项 | 值以参数进入核心，由核心的兼容桥写入作用域（第5.3节）；是否改为 adapter 参数见 Q4 |
| C3 | 写 `auth_key_hash`、`affinity_key`、`UPSTREAM_PRIORITY`、`CONCEALED_MODEL`、`CLIENT_ERROR_KIND` | 供 adapter、key pool、中间件读取的输出 | 保留，时机不变，集中到兼容桥 |
| C3′ | 读回 `affinity_key`（:1846） | 自读 | 改为局部变量 |
| C4 | `USER_ROLE`（key pool 读取） | 认证结论的派生 | 不变，仍由入口发布 |
| C5 | `note_request_id` / `observe_billing` | Batch 结算 | 保留（Q5） |

### 3.3 Chat Completions

**在线调用链**（[`completions.py`](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/completions.py)）：

```text
路由 chat_completions                                                 :451-465
   Depends: verify_api_key、get_runtime_settings、enforce_user_concurrency 等
   ├─ request.json() + ChatCompletionRequest 校验；失败统一 400           :472-476
   ├─ is_trusted_probe(request, user_ctx)；立即写 req_ctx synthetic_probe   :483-489
   ├─ probe 时读 runtime_settings 的 log_synthetic_probes                :495-505
   ├─ session_identity(request.headers, body)                           :531
   ├─ 强制流式：仅当未请求流式且 runtime_settings 打开                       :534-538（:168-179）
   ├─ metadata：UA、referer、get_client_ip、authorization、batch/ledger 归属  :540-562
   ├─ auto：auto_mode_for(model, request.app.state.services) → 改写 payload  :577-629
   ├─ 路由表中无模型 → 记 404 日志 + HTTPException 404                     :632-653
   ├─ model_access_denial → 404（UNPUBLISHED 不写日志）                    :655-699
   ├─ retarget_interactive_load(request, route_canonical_model_id(route, model))  :706
   ├─ require_route_pricing(route)                                     :708
   ├─ req_ctx.update(request_id, auth_key_hash, affinity_key, user_id, user_name)  :758-771
   ├─ X-Route-Pin（仅 admin）；模态检查 → 400；pin 校验 → 400               :775-867
   ├─ 选择 router：有 pin 或无 registry → router_exec；否则 registry.get_router(model)  :880-882
   ├─ stream：
   │   ├─ active_router.stream_chat_completion(...)（异步生成器，调用时无 I/O） :896
   │   ├─ StreamSession(..., request_headers, timeout_fired_probe(request))     :902-923
   │   ├─ 强制流式：_streaming_response_with_keepalive 包装；先拉首项；
   │   │   HTTPException → 先 publish_upstream_provider 再抛；写 scope["state"] 流式标记；
   │   │   media_type application/json                                    :927-976
   │   └─ SSE：先拉首项；未提交时首项是错误帧 → 关闭流、发布 provider、HTTPException(状态)；
   │       否则 StreamingResponse(_chain_first_then_rest(首项, 其余))          :988-1022
   └─ 非流式：active_router.chat_completion → sanitize → 计费 → 日志 → routing 观察
       → probe 时写 http_response.headers["X-Provider"]                      :1025-1188
       异常：ProviderPinError → 400；AllCircuitsOpen/TargetUnavailable → 503；
       其余 → 失败观察 + 失败日志 + HTTPException(上游状态)                    :1190-1279
```

**Fixed / RouteWise / fallback**：选择、重试、fallback、hedging、health claim、prefill 预留、RouteWise 的预约与学习都在 `active_router` 内部（[routing/AGENTS.md](../../../apps/backend/routing/AGENTS.md) 与 AGENTS.md 第6.2节）。handler 只负责调用 `RouterProtocol.chat_completion`/`stream_chat_completion`，并在结束后以 `completions_logger.record_routing_observation` 报告结果（非流式 [:1162-1181](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/completions.py#L1162-L1181)、失败 [:1218-1235](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/completions.py#L1218-L1235)、流式在 `StreamSession` 中）。

**流式会话** [`StreamSession.stream`](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/completions_stream.py#L422-L731)：role chunk 推迟到第一个可见内容或 15 秒 keepalive 到期才发出（[:446-470](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/completions_stream.py#L446-L470)），所以“未提交”时上游的快速失败是第一项；后台 reader 任务读取 router 流；成功时 `_finalize_success` 计费、记日志、报告观察；异常时 `_finalize_failure` 记失败、产出错误帧；`CancelledError`/`GeneratorExit` 时同步调用 [`_finalize_cancelled`](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/completions_stream.py#L1078-L1152)：用超时探针区分 504 与 499，**只记日志、不计费、不报告观察**。

**计费**：[`CostTracker.schedule_increment`](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/completions_cost.py#L242-L318)，非流式在 usage 非空时调用，**不依赖 `log_store`**，probe 也计费；它同时调用 `observe_billing`（无 request_id，Batch 从 req_ctx 读回 request_id，见 [dispatch.py:503-513](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/batch/dispatch.py#L503-L513)）。日志仅在 `log_store` 存在且未被 probe 抑制时写。

**对 `Request`、`Response` 与 scope 的用法**：

| # | 用法（位置） | 去向（提案） |
|---|---|---|
| H1 | `request.json()`（:473） | 入口解析；核心接收 `dict` 并自行做 `ChatCompletionRequest` 校验（Responses 依赖这一点，见第3.4节） |
| H2 | `is_trusted_probe(request, …)`、`X-Route-Pin`、`resolve_mode(request.headers)`、`session_identity(request.headers, …)`、UA、referer | `ClientOrigin` 的值与白名单请求头 |
| H3 | `get_client_ip(request)`、`get_client_ip_info(request)`（:543、:752） | `ClientOrigin.ip` |
| H4 | `request.app.state.services`（:578） | 显式服务 `auto_services` |
| H5 | `retarget_interactive_load(request, …)`（:706） | `ctx.admission.retarget` |
| H6 | `_timeout_fired_probe(request)` 读 `scope["state"]` 中 `TimeoutMiddleware` 的 deadline（:185-198、:921） | `ctx.timeout_probe`（HTTP 入口绑定；Batch 为 `None`） |
| H7 | 强制流式写 `scope["state"][STREAMING_RESPONSE_SCOPE_STATE_KEY]`（:962，[timeout.py:112](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/middleware/timeout.py#L112) 读取） | 留在 HTTP 渲染层 |
| H8 | `http_response.headers["X-Provider"]`（:957、:1184） | 结果字段 `provider_header`，由 HTTP 渲染层写 |
| H9 | `runtime_settings`（强制流式、probe 日志开关） | 显式服务；Responses 与 Batch 今天传 `None`，迁移后由 `ChatOptions` 工厂显式给出（第4.4节） |
| H10 | 写 req_ctx：`synthetic_probe`、`request_id`、`auth_key_hash`、`affinity_key`、`user_id`、`user_name`、`mark_model_not_found`、`publish_upstream_provider`、`hide_served_model` | 输出通道，保留，时机不变 |

### 3.4 Responses 与 compat 的委派

**Responses**（[`responses.py`](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/responses.py)）：

- 校验 JSON、`model`、`input`/`previous_response_id`（[:132-143](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/responses.py#L132-L143)）；按 owner 读取 `previous_response_id` 的历史（[:148-151](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/responses.py#L148-L151)）；把 Responses 请求译为 Chat 请求体 `chat_body`，并带上 `metadata`/`client_metadata` 供 Chat 解析 session（[:153-186](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/responses.py#L153-L186)）。
- **改写私有属性**：`request._json = chat_body`、`request._body = …`（[:191-192](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/responses.py#L191-L192)），并从 `scope["headers"]` 删掉 `X-Reasoning-Passthrough`、重建 `request._headers`（[:80-93](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/responses.py#L80-L93)），让 Chat 不剥离 reasoning。
- **非流式**：以 `runtime_settings=None`、`_concurrency_slot=None` 直接调用 `chat_completions`（[:237-251](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/responses.py#L237-L251)）；结果不是 dict → 502；译为 Responses 对象；`store` 时落库；返回 `JSONResponse`。
- **流式**：同样调用 `chat_completions`，它已在内部拉取首项（第3.3节），预检与“未提交错误”以 `HTTPException` 抛出，因此 Responses 流式的这些错误也是 HTTP 状态。随后读取 `delegate.body_iterator`，经 `ResponsesStreamTranslator` 重新封帧；**终止事件发出之前先落库**；被断连或取消打断时，以 `incomplete` 状态后台落库（[:309-404](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/responses.py#L309-L404)）。响应头是 `_SSE_HEADERS`（含 `Connection: keep-alive`），不带 Chat 的 `X-Provider`。
- 在线槽：Responses 路由的 `enforce_user_concurrency` 以 Responses 请求体的 `model` 计数；Chat 内部的 retarget 通过**同一个** `Request` 的 `scope["state"]` 找到 tracker（[auto_mode 测试 :755](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/tests/servers/test_auto_mode_surfaces.py#L755) 覆盖非流式）。
- `api_logs.request_payload` 是译后的 `chat_body`（Chat 从被改写的 `request.json()` 读取）。

**compat**（[`compat.py`](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/compat.py)）：

- `/completion` 原样委派，**传入真实 `runtime_settings`**（所以强制流式对它生效），**不传 `_concurrency_slot`**（Chat 收到的是 `Depends` 标记对象；Chat 不读这个参数，所以无害）。
- `/v1/completions` 未加保护地 `await request.json()`（非法 JSON 或非对象请求体今天会变成 500），原地改写缓存的 JSON 对象（`prompt` → `messages`），再改写 `request._body`（[:80-85](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/compat.py#L80-L85)）。返回的仍是 Chat 形状，没有译回旧版 completion 格式。compat 路由没有 `response_model`，返回 handler 的原始 dict。

**Batch 的 Responses 项**：`dispatch → create_response → chat_completions` 三层，`response_store=None`（所以 `store` 恒为假），`previous_response_id` 在准入时就被拒绝（不在 [`_RESPONSES_KEYS`](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/batch/targets.py#L561-L584) 中）。

### 3.5 Embeddings

[`create_embeddings`](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/embeddings.py#L133-L485)：

- 请求体由 FastAPI 校验为 `EmbeddingRequest`（失败是 FastAPI 的 422）；为了解析 session，又读一次原始 JSON（[:166-170](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/embeddings.py#L166-L170)）。
- **专属模型表**：模型在 `embedding_adapters`（按调用方给的名字查找）中，不在 chat 路由表中；访问门禁记录在 `router_exec.embedding_routes`，**缺失即拒绝**（fail-closed，[:303-322](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/embeddings.py#L303-L322)）；`model_access_denial` 的所有拒绝都回答 404。
- 没有 auto、没有 retarget、没有 health claim、不经过 router；`FallbackEmbeddingAdapter` 在 adapter 内部回退，实际服务的端点由 `serving_config` 报告（[:372-376](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/embeddings.py#L372-L376)）。
- 成功前先用 `EmbeddingResponse` 校验上游返回，畸形返回记 500 且不计费（[:382-395](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/embeddings.py#L382-L395)）。
- **计费**：`_schedule_cost_increment(op_store, user_id, usage, pricing)`（成本 > 0、有 user_id、有 op_store 时），**不依赖 `log_store`**；`observe_billing` 带 request_id（[:418-425](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/embeddings.py#L418-L425)）。日志需要 `log_store` 与 `completions_logger` 都存在且未被 probe 抑制；只存摘要，不存向量。
- 错误：上游 `ClientResponseError` 原样转发状态；`KeyPoolExhausted` → 429；其他 → 500；都先发布上游 provider。
- 在线槽：`enforce_user_concurrency` 通过 `embedding_adapters` 找到 canonical 并计入 interactive load；Embeddings 本身不 retarget。

**Batch 的 Embeddings 项**：dispatch 先 `EmbeddingRequest.model_validate`，失败为 **400**（与在线 422 不同，[dispatch.py:362-365](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/batch/dispatch.py#L362-L365)）；不强制 `stream`；body 的 `model` 被固定为准入时的 `adapter.config.id`（[targets.py:529-531](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/batch/targets.py#L529-L531)），执行时再按这个名字查 `embedding_adapters`。

### 3.6 Batch 中四个目标的差异（基线事实）

| 方面 | Chat | Embeddings | Messages | Responses |
|---|---|---|---|---|
| 执行器 | `call_handler(chat_completions, request, Response(), …, runtime_settings=None, _concurrency_slot=None)` | 先校验 `EmbeddingRequest`，再 `call_handler(create_embeddings, …)` | `call_handler(anthropic_messages, …, _conc=None)`，加 `BATCH_EXECUTION` 与 `ANTHROPIC_VERSION` 种子 | `call_handler(create_response, …, response_store=None, _concurrency_slot=None)` |
| handler 返回 | dict（成功）/ 抛 `HTTPException` | dict / 抛 `HTTPException` | `JSONResponse`（成功与错误都是） | `JSONResponse` / 抛 `HTTPException` |
| 结果整形 | `_client_body`：按 `ChatCompletionResponse` 校验并 `exclude_none`（在线由路由的 `response_model` 完成） | `json_safe` | `json_safe`；`type == "error"` → 502；错误带 `anthropic` 子对象 | `json_safe` |
| request_id 来源 | req_ctx（handler 在 :758 写入；更早的拒绝没有） | `note_request_id` | `note_request_id` | 同 Chat |
| 计费观察来源 | `CostTracker` | `embeddings` 自身 | `_schedule_log_store_task` | 同 Chat |
| 冻结协议选项 | 无 | 无 | `protocol_profile`（`anthropic-version`） | 无 |
| 准入时额外检查 | 模式校验、禁止 stream | 门禁与可见性 | 字段、版本规则、至少一个 adapter 能原样服务 | 字段白名单、禁止宿主工具 |

### 3.7 四个协议的计费与日志口径（基线事实，迁移中逐一保持）

| 情形 | Chat | Messages | Embeddings | Responses |
|---|---|---|---|---|
| 计费点 | `CostTracker.schedule_increment` | `_schedule_log_store_task` | `_schedule_cost_increment` | Chat 的 |
| `log_store` 为空时是否计费 | **是** | **否**（Q6） | **是** | 同 Chat |
| probe 是否计费 | 是 | —（Messages 不识别 probe） | 是 | 同 Chat |
| 定价缺失 | 观察者收到 `cost_usd=None` → Batch `unknown` | 同左 | 同左 | 同 Chat |
| 流式中途断连 | 499/504 日志，**不计费** | 以估算 usage、状态 200 **计费** | 无流式 | Chat 的口径；Responses 另以 `incomplete` 落库 |
| 流式上游失败 | 失败日志，不计费 | `stream_failed`，计 0 | — | 同 Chat |
| 预检拒绝 | 404/400 日志（UNPUBLISHED 除外），不计费 | 拒绝日志（`log_rejection`），不写成功行 | 404 日志（UNPUBLISHED 除外） | Chat 的；Responses 自己的 400 不写日志 |

这些差异是现有产品语义。迁移不统一它们；任何统一都需要单独的产品决定。

### 3.8 其他基线事实

- 流式时，`enforce_user_concurrency` 在响应体发送完毕后才释放槽和负载计数，由 [`test_streamed_auto_chat_counts_on_the_concrete_model_until_eof`](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/tests/servers/test_auto_mode_surfaces.py#L392) 在锁定版本 FastAPI 0.137.1 下验证，只覆盖 **Chat**；非流式 retarget 覆盖 Responses、`/completion`、`/v1/completions`、Messages（[:755](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/tests/servers/test_auto_mode_surfaces.py#L755)）。**Messages 与 Responses 的流式全程计数没有测试。**
- 按测试名检索，未找到专门断言 Messages 在 `stream=true` 时预检错误仍以 HTTP 状态返回的测试（检索可能遗漏）。
- `concurrency.user_slot_for` 的 docstring 说它供 Batch worker 使用，但 worker 已改用角色配额，基线中只剩测试调用它。
- Batch 取消不中断运行中的项（[`cancel_batch`](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/storage/batch_store.py#L1527-L1537)）；租约丢失时 `heartbeat` 只续期仍属本 worker 的项，调度器不据此取消 dispatch，`complete_item` 因 claim 栅栏而不记录结果（[scheduler.py:944-951](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/batch/scheduler.py#L944-L951)）。
- **模型固定**：Batch 执行时 body 的模型名是准入时的 canonical id。Chat 与 Responses 直接按它查路由表；Messages 先 `resolve_anthropic_alias` 再查；Embeddings 按它查 `embedding_adapters`。别名表、route 表或 route 首个 adapter 在准入与执行之间变化时，执行时算出的 canonical 能否与固定值不同，本稿**未能证明不可能**（Q3）。
- **Chat 与 Responses 的流在开始迭代前断连**：Chat 在返回 `StreamingResponse` 之前已经启动了 `StreamSession.stream`（拉取了首项，reader 任务在运行）。如果 Starlette 从未迭代 body，`_chain_first_then_rest` 的 `finally` 不会执行，已启动的会话流与 router 流靠什么关闭——**待验证**，与 Messages 的 claim 问题同类（第6.3节）。
- 锁定版本：`uv.lock` 中 FastAPI 0.137.1、Starlette 1.3.1。本机另有 Starlette 0.47.3 的源码，不作为锁定版本的证据。

---

## 4 目标设计

### 4.1 分层与职责

```text
 在线 HTTP：中间件（打开并持有请求作用域、超时 deadline）
           路由：Depends(verify_api_key) → 已认证 user_ctx
                 Depends(enforce_user_concurrency) → InteractiveAdmission（槽 + tracker，依赖拆除时释放）
                 解析 JSON；ClientOrigin.from_request；构造协议选项
                                     │
 Batch：   scheduler：claim/租约 → authenticate_stored_key → may_use_batch → 角色配额 → mark_dispatched
           dispatch_item：新任务 + 空 Context（打开并持有作用域）；observing()；move_on_after
                 ClientOrigin.batch()；BatchAdmission（no-op）；BatchAttribution；协议选项的 for_batch 工厂
                                     │
                                     ▼
           协议核心（每个协议一个，互不调用，Responses 除外）
             execute_messages ─ 自选 adapter + health claim + prefill ─▶ adapter
             execute_chat ───── RouterProtocol（FixedRouter / RouteWise / 组合实现，选择不变）─▶ adapter
             execute_embeddings ─ embedding_adapters[model]（含 FallbackEmbeddingAdapter）
             execute_responses ─ 翻译 + response store ─▶ execute_chat（同一个 ctx）
                                     │
           HTTP 渲染（路由模块内）                  Batch 映射（dispatch 内）
           JSONResponse / StreamingResponse /       → DispatchResult
           错误体与响应头
```

| 职责 | 在线入口 | Batch 入口 | 共享层 | 协议核心 |
|---|---|---|---|---|
| 解析与鉴权 | `verify_api_key`（日额度、grant、`X-On-Behalf-Of`、IP 封禁）；JSON 解析 | 创建时的身份与准入；执行时 `authenticate_stored_key`、`may_use_batch` | 只传递已认证上下文的只读视图 | 不鉴权，**必须**接收已认证上下文 |
| 模型可见性与权限 | — | 准入时一次 | — | **每次执行都复核**，拒绝一律回答 404 |
| 在线每用户槽与 interactive load | 依赖获取并在响应发送完毕后释放 | 不占 | `Admission` 协议 | 只调用 `retarget`（Chat、Messages） |
| Batch 角色配额、模型容量、租约、重试、取消、持久化 | — | scheduler（不变） | — | 不涉及 |
| 协议语义、模型解析、端点选择的调用、health/prefill（Messages）、计费、`api_logs` | — | 只观察计费 | — | 负责，与今天的 handler 相同 |
| router 内的选择、重试、fallback、hedging、health、feedback | — | — | — | 不涉及，只调用 `RouterProtocol` |
| 线上错误形状、响应头、SSE 封帧之外的传输细节 | 渲染 | 映射为 `DispatchResult` | — | 返回协议结果或抛协议错误 |

### 4.2 共享执行上下文（提案伪代码）

位置：`serving/surfaces/context.py`（目录名见 Q2）。这个模块不导入 FastAPI，也不导入 Starlette 的请求或响应类型；它唯一用到的 Starlette 类型是不可变的头部多值字典 `starlette.datastructures.Headers`（见下文“请求头的语义”）。`ClientOrigin.from_request` 放在 HTTP 侧的辅助模块 `serving/servers/surface_http.py`，只被路由调用。

```python
#: 核心可以读取的请求头（按小写比较）。其余请求头不进入核心。
ORIGIN_HEADERS = frozenset({
    "x-session-id", *AGENT_SESSION_HEADERS,   # session_identity（基线 _AGENT_SESSION_HEADERS）
    "anthropic-beta",                         # Messages 透传
    "x-reasoning-passthrough",                # Chat 序列化模式
    "x-route-pin",                            # Chat，仅 admin 生效
    "x-probe",                                # probe 标记，仅可信调用方生效
})

@dataclass(frozen=True, slots=True)
class ClientOrigin:
    ip: ClientIpInfo                 # 基线已有类型
    user_agent: str | None
    referer: str | None
    route_path: str                  # 拒绝日志使用
    authorization_header: bool       # metadata["authorization"] 的输入（今天是 bool(authorization)）
    headers: Headers                 # starlette.datastructures.Headers，只含 ORIGIN_HEADERS 的原始条目
    def without(self, name: str) -> "ClientOrigin": ...     # 删除该名字的全部条目（任何大小写）
    @classmethod
    def batch(cls, path: str) -> "ClientOrigin": ...        # _BATCH_CLIENT、BATCH_USER_AGENT、无请求头


@dataclass(frozen=True, slots=True)
class BatchAttribution:
    batch_id: str
    item_id: int | None
    attempt: int | None
    def log_metadata(self) -> dict[str, object]: ...   # 与 batch_log_attribution 输出相同的键


class Admission(Protocol):
    def retarget(self, canonical_model_id: str) -> None: ...

class InteractiveAdmission:       # 由 enforce_user_concurrency 构造并 yield；包装同一个 tracker
    def retarget(self, canonical_model_id: str) -> None: ...

class BatchAdmission:             # no-op：与今天 Batch 中找不到 tracker 的结果相同
    def retarget(self, canonical_model_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    user_ctx: Mapping[str, object]          # 认证结论的只读视图（MappingProxyType）
    origin: ClientOrigin
    admission: Admission
    batch: BatchAttribution | None          # 归属是普通数据，不进 user_ctx
    timeout_probe: Callable[[], bool] | None  # HTTP：TimeoutMiddleware 的 deadline 是否已触发；Batch：None
```

**请求头的语义（评审更正）**：上一版把 `headers` 写成小写键的普通映射，这会破坏今天依赖 Starlette `Headers` 的读取方式：`session_identity` 以 `X-Session-ID` 查找，Chat 以 `X-Route-Pin` 查找，二者都靠大小写不敏感的查找成立。本版规定：

- `ClientOrigin.headers` 就是一个 `starlette.datastructures.Headers`，由入口从 `request.headers.raw` 中**按顺序**挑出名字（`.lower()` 后）属于 `ORIGIN_HEADERS` 的原始 `(bytes, bytes)` 条目构造，原始键字节不改写。因此 `get`/`[]` 的大小写不敏感查找、同名多值时取第一个、`getlist` 返回全部、`in` 判断，都与今天直接读 `request.headers` 逐字节相同。
- `without(name)` 删除名字（`.lower()` 后）等于 `name` 的**全部**条目，与今天 Responses 的 `_force_reasoning_passthrough` 对 `scope["headers"]` 的过滤规则相同（`k.lower() != b"x-reasoning-passthrough"`）。
- Batch 的 `headers` 是 `Headers(raw=[])`。今天伪造请求带的 `content-type`、`content-length`、`user-agent`、`anthropic-version` 都不在白名单中，也没有任何核心读取它们（UA 单独存放在 `user_agent` 字段）。
- 测试覆盖大小写变体与重复头：session 头、admin 的 route pin、可信 probe、Messages 的 `anthropic-beta`、Responses 删除 `X-Reasoning-Passthrough` 的每一种大小写变体。

说明：

- 没有 `skip_*` 字段，没有 `raw_request`、`scope`、`app` 之类的逃生口。
- 不另建 `Caller` 类型：Python 中“只能由认证器构造”只是约定，不构成安全边界。真正的边界是“入口完成认证 + 核心每次复核模型权限”（Q11）。
- Embeddings 核心不调用 `admission`。这不是遗漏：它今天就不 retarget，在线槽与负载计数由路由依赖持有。
- 只有 Messages 在 handler 内调用 `log_rejection`（Chat、Embeddings、Responses 都不调用）。`log_rejection(request=…)` 从请求中只取四样东西：路由路径、`get_client_ip(request)`、`x-probe` 标记、`request.app.state.services` 上的 `log_store` 与 `runtime_settings`。实施中给它加一个取值的孪生函数 `log_rejection_from(source=RejectionSource(...))`，`log_rejection(request=…)` 改为先构造 `RejectionSource` 再调用它，门禁侧（`enforce_user_concurrency`、`verify_api_key`）的调用不变。Messages 核心从 `ctx.origin` 与应用服务对象构造 `RejectionSource`：Batch 下路径是 `TARGET_PATHS["messages"]`、IP 是 `"unknown"`、服务对象就是 dispatch 拿到的同一个 `services`——与今天伪造请求得出的值相同。不另建 `RejectionSink` 协议。
- probe 信任仍由核心判断：`x-probe` 标记来自 `origin.headers`，可信与否由 `user_ctx` 决定。需要给 [`synthetic_probe.py`](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/utils/synthetic_probe.py) 加一个接受请求头映射的变体，保留现有的 `Request` 版本。

### 4.3 Messages 契约（S3）

沿用上一版第4–6节的设计，类型草图如下（**提案**）：

```python
@dataclass(frozen=True, slots=True)
class MessagesConstraints:
    adapter_requirement: AdapterRequirement | None   # 基线已有（batch/targets.py:361）
    pin_model: bool                                  # True：禁止 auto 与小预算换模型
    anthropic_version: str | None                    # 冻结的协议选项，不是准入限制
    @classmethod
    def online(cls) -> "MessagesConstraints": ...
    @classmethod
    def for_batch_item(cls, body: dict, protocol_profile: str | None) -> "MessagesConstraints": ...

@dataclass(frozen=True, slots=True)
class MessagesServices:
    router: object; log_store: object | None; op_store: object | None
    visibility: object | None; completions_logger: object | None
    auto_services: object | None    # 应用服务对象：auto 选型与拒绝日志的 log_store/runtime_settings

MessagesOutcome = MessagesResponse | MessagesError | MessagesStream

async def execute_messages(body: dict, ctx: ExecutionContext, services: MessagesServices,
                           constraints: MessagesConstraints) -> MessagesOutcome: ...
```

- `_prepare` 完成今天的全部预检（校验 → auto（仅在线）→ 权限 → 选 adapter（准入规则 + 约束过滤 + key tier）→ `ctx.admission.retarget` → 兼容桥写作用域 → health claim）。失败返回 `MessagesError`，此时尚未发出任何字节。
- 非流式在一次调用内完成（含释放、日志、计费），返回 `MessagesResponse` 或 `MessagesError`。
- 流式返回 `MessagesStream`：claim 的所有权移入流对象，尚未触碰上游；`__aiter__` 就是今天的 `_gen()`；`aclose()` 在从未迭代时直接 `end_dispatch`（可重复调用）。
- 不引入 Chat 那种“先拉首帧再定状态码”的机制，那会改变 Messages 的外部行为。
- Batch：`dispatch._run_messages` 调用核心，`MessagesResponse` → 成功，`MessagesError` → `_failure(..., anthropic={...})`；若意外得到 `MessagesStream`，在同一作用域 `aclose()` 并按 502 处理（与今天“handler 返回了流”的分支结果相同）。

### 4.4 Chat 契约（S4）

```python
@dataclass(frozen=True, slots=True)
class ChatOptions:
    """调用方的交付约定。runtime_settings 只影响两件事：强制流式与 probe 日志开关。"""
    runtime_settings: RuntimeSettings | None
    @classmethod
    def online(cls, runtime_settings: RuntimeSettings | None) -> "ChatOptions": ...  # /v1/chat/completions、compat
    @classmethod
    def delegated(cls) -> "ChatOptions": ...   # Responses：None，与今天传 runtime_settings=None 相同
    @classmethod
    def batch(cls) -> "ChatOptions": ...       # Batch：None，同上

@dataclass(frozen=True, slots=True)
class ChatServices:
    router: object                      # router_exec（RoutingManager），pin 时直接使用
    model_router_registry: object | None
    visibility: object | None
    log_store: object | None
    completions_logger: CompletionsLogger
    pricing_lookup: PricingLookup
    cost_tracker: CostTracker
    auto_services: object | None        # 今天经 request.app.state.services 读取

@dataclass(frozen=True, slots=True)
class ChatResponse:
    body: dict[str, object]             # 今天 handler 的返回值（usage 已补全）
    request_id: str
    provider_header: str | None         # probe 时的 X-Provider；其余为 None

class ChatStream:
    """已通过全部预检；router 流已创建（异步生成器，尚未迭代），StreamSession 已构造。"""
    request_id: str
    provider_header: str | None
    buffered: bool                      # True：强制流式（只可能来自 ChatOptions.online）
    response_model: str | None
    async def prime(self) -> str | None:
        """SSE 用：拉取首项。未提交且首项是错误帧时，关闭流、发布上游 provider，
        抛 HTTPException(该状态)；与今天 :988-1006 相同。"""
    def frames(self) -> AsyncIterator[str]:
        """SSE 用：产出已拉取的首项与其余各项，finally 中安静关闭（今天的 _chain_first_then_rest）。"""
    def session_stream(self) -> AsyncIterator[str]:
        """强制流式用：未拉取的会话流，交给 HTTP 层的 keepalive 包装。"""
    async def aclose(self) -> None: ...

ChatOutcome = ChatResponse | ChatStream

async def execute_chat(body: dict, ctx: ExecutionContext, services: ChatServices,
                       options: ChatOptions) -> ChatOutcome:
    """今天 chat_completions 从 :478 到 :1279 的主体，逐行搬移。错误以 HTTPException 抛出（第4.8节）。"""
```

- **核心负责**：`ChatCompletionRequest` 校验（400 文案不变）；probe 信任与 req_ctx 发布；session；强制流式判断；metadata（含 `ctx.batch.log_metadata()`、ledger 归属）；auto；路由表与权限复核及其日志；`ctx.admission.retarget`；定价检查；req_ctx 输出；pin（admin）；模态；选择 `active_router`；调用 `RouterProtocol`；非流式的计费、日志、观察；流式的 `StreamSession`（`timeout_fired_probe=ctx.timeout_probe`、`request_headers=origin.headers`）。
- **HTTP 渲染负责**（`completions.py` 路由模块内，`render_chat_http(outcome, request)`）：`ChatResponse` → 返回 `body`，写 `X-Provider`；`ChatStream` 且非 buffered → `await prime()` 后返回 `StreamingResponse(frames(), text/event-stream, 原响应头)`；buffered → 今天的 `_streaming_response_with_keepalive(session_stream())` 加首项拉取、`HTTPException` 时的 provider 发布、`scope["state"]` 流式标记与 `application/json`。路由仍保留 `response_model=ChatCompletionResponse`，序列化不变。
- **Fixed/RouteWise**：核心只决定调用哪个 router 实例（规则与今天相同：有 pin 或无 registry → `router_exec`，否则 `registry.get_router(model)`），不触碰选择、重试、fallback、hedging、预约、health、feedback；`endpoint_id` 与资源归属不变。不新建通用 router。
- **Batch**：`dispatch._run_chat` 调用 `execute_chat(body, ctx, chat_services(services), ChatOptions.batch())`。`ChatResponse.body` 经 `_client_body` 整形（与今天相同）。意外得到 `ChatStream` → `aclose()` 并按 502 处理。`HTTPException` 照旧由 `_dispatch` 映射。
- **S4 到 S5 之间的过渡桥**：Responses 与 compat 在 S4 仍以今天的方式调用 `chat_completions` 路由函数（改写过的 `Request`、`_concurrency_slot=None` 或缺省）。路由函数据此构造上下文：`_concurrency_slot` 是 `InteractiveAdmission` 时直接使用；否则使用 `ScopeAdmission(request)`，它的 `retarget` 就是今天的 `retarget_interactive_load(request, …)`，在 Responses/compat 下找到同一个 tracker，在没有 tracker 时什么也不做。这是唯一的过渡桥，S5 删除；它不是新的扩展点。

### 4.5 Responses 与 compat 契约（S5）

```python
@dataclass(frozen=True, slots=True)
class ResponsesServices:
    chat: ChatServices
    response_store: object | None       # Batch：None

@dataclass(frozen=True, slots=True)
class ResponsesResult:
    body: dict[str, object]

class ResponsesStream:
    """包装已 prime 的 ChatStream：逐项喂给 ResponsesStreamTranslator；终止事件之前落库；
    被打断时以 incomplete 后台落库。即今天 _stream_response 中的 _gen。"""
    def __aiter__(self) -> AsyncIterator[bytes]: ...
    async def aclose(self) -> None: ...

async def execute_responses(body: dict, ctx: ExecutionContext,
                            services: ResponsesServices) -> ResponsesResult | ResponsesStream:
    ...  # 校验 → 历史 → 翻译出 chat_body（含 metadata/client_metadata）
    chat_ctx = replace(ctx, origin=ctx.origin.without("x-reasoning-passthrough"))
    outcome = await execute_chat(chat_body, chat_ctx, services.chat, ChatOptions.delegated())
    if isinstance(outcome, ChatStream):
        await outcome.prime()               # 与今天 Chat 内部的 prime 相同：预检与未提交错误仍是 HTTP 状态
        return ResponsesStream(outcome, translator, ...)
    if not isinstance(outcome.body, dict):  # 502，同今天
        ...
    ...  # 翻译、store 时落库
    return ResponsesResult(responses_obj)
```

- **不重复抢在线槽**：只有 Responses 路由声明 `Depends(enforce_user_concurrency)`；`execute_chat` 是函数调用，没有依赖；同一个 `ctx.admission` 传下去，Chat 的 retarget 落在同一个 tracker 上。
- **不重复路由、计费、日志**：Responses 核心不调用 router，不计费，不写 `api_logs`；这些只在 `execute_chat` 中发生一次。response store 的落库是 Responses 自己的持久化，不是推理日志。
- **不使用伪 `Request`**：不再改写 `_json`/`_body`/`_headers`；`api_logs.request_payload` 仍是 `chat_body`（它就是传给 Chat 核心的 body）；`X-Reasoning-Passthrough` 以 `origin.without(...)` 去掉，效果与今天删除请求头相同。
- `ChatOptions.delegated()` 把今天的 `runtime_settings=None` 变成具名约定：强制流式关闭；probe 日志开关视为关闭（今天的现象，Q13）。
- Responses 流式响应头保持 `_SSE_HEADERS`，不加 `X-Provider`。
- **compat**：`/completion` 调用 `execute_chat(body, ctx, services, ChatOptions.online(runtime_settings))`，用 Chat 的渲染函数，但仍返回原始 dict（没有 `response_model`）；`/v1/completions` 在路由中把 `prompt` 转为 `messages` 后同样调用。两者都从依赖拿到 `InteractiveAdmission`。`/v1/completions` 未加保护的 JSON 解析保持原样（Q14）。
- **Batch**：`dispatch._run_responses` 调用 `execute_responses(body, ctx, ResponsesServices(chat_services(services), None))`；`ResponsesResult.body` → `json_safe`。

### 4.6 Embeddings 契约（S2）

```python
@dataclass(frozen=True, slots=True)
class EmbeddingServices:
    embedding_adapters: Mapping[str, object]
    router: object                  # 只读 embedding_routes（门禁记录）
    visibility: object | None
    log_store: object | None
    completions_logger: object | None
    op_store: object | None
    runtime_settings: RuntimeSettings | None   # 只用于 probe 日志开关；Batch：None（同今天）

async def execute_embeddings(request: EmbeddingRequest, raw_body: object,
                             ctx: ExecutionContext, services: EmbeddingServices) -> dict[str, object]:
    """今天 create_embeddings 的主体，逐行搬移。错误以 HTTPException 抛出。"""
```

- 在线路由：FastAPI 继续把请求体校验为 `EmbeddingRequest`（422 不变），`raw_body = await http_request.json()`（同今天），再调用核心。
- Batch：`_run_embeddings` 继续自行校验（400 不变），`raw_body` 是 item 的 body（与今天伪造请求的内容相同）。
- 门禁、fail-closed、`serving_config` 归属、响应校验、计费与 `observe_billing`、日志摘要、错误映射与 provider 发布全部在核心中，不变。

### 4.7 Batch dispatch 的目标形态

- `_dispatch` 保留：空 Context 的任务、种子作用域、`publish_caller_role`（`USER_ROLE` 供 key pool 读取）、`observing()`、`move_on_after`、异常到结果的映射、`cost_status` 推导。
- 执行器签名改为 `async (services, ctx: ExecutionContext, body: dict) -> 协议结果`；`ExecutionContext` 由 `_dispatch` 构造：`user_ctx` 的只读视图、`ClientOrigin.batch(TARGET_PATHS[target])`、`BatchAdmission()`、`BatchAttribution(batch_id, item_id, attempt)`、`timeout_probe=None`。
- `_completed` 从“读 `JSONResponse`”改为按协议结果映射；`_error_from_body` 只保留给仍在使用它的目标，S6 删除。
- request_id：成功时与今天一致（Messages/Embeddings 由观察者、Chat/Responses 由 req_ctx）。Chat 早期拒绝不上报 request_id 的现状保持（Q15）。
- 过渡期：未迁移的目标仍经 `call_handler`；`user_ctx` 中的归属合并保留到最后一个目标迁移完成（S5），S6 删除合并、`_request`、`call_handler`、`HANDLERS`、`_INJECTED_DEFAULTS` 和 `BATCH_EXECUTION` 键。

### 4.8 错误载体

- Messages 今天以返回 `JSONResponse` 的方式回答错误，所以核心返回 `MessagesError` 值；在线渲染为 Anthropic 错误体，Batch 映射为带 `anthropic` 子对象的错误。
- Chat、Embeddings、Responses 今天以 `HTTPException` 回答错误，由全局 `anthropic_aware_http_exception_handler` 渲染（[app.py:172-179](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/app.py#L172-L179)），Batch 由 `_dispatch` 映射。共用的辅助函数（如 `model_not_found_detail`、auto 的错误映射）也围绕它。**过渡决定**：这三个核心在迁移中继续抛 `fastapi.HTTPException`。它是携带状态、detail 与响应头的异常类，不是请求或响应对象，也不是依赖标记；保留它让错误体、状态码、`Retry-After` 与 Batch 映射都不需要同时改动。是否换成不依赖框架的协议错误类型列为 Q12。
- 治理检查（S6）：核心模块不导入 `Request`、`Response`、`StreamingResponse`、`JSONResponse`、`Depends`、`Header`；可以导入 `HTTPException`，直到 Q12 有结论。

---

## 5 横切约束

### 5.1 身份、权限、模型解析与别名固定

| 项 | 在线 | Batch | 核心 |
|---|---|---|---|
| 身份 | `verify_api_key` 的返回值 | 每次尝试 `authenticate_stored_key` 的返回值；`may_use_batch(role)` | 只读视图；不改写 |
| 日额度 | `verify_api_key` 拒绝 | `StoredKeyQuotaExceeded` → 暂停 owner | 不重复检查 |
| 模型权限 | — | 准入时 `model_access_denial` | 每次执行复核：Chat/Responses 查路由表，Messages 经 `_resolve`，Embeddings 查门禁；拒绝一律 404 |
| auto | 支持 | 准入拒绝；执行时 body 已是具体模型 | 照旧调用 `auto_mode_for`；Batch 下它返回 None |
| 别名 | Messages 解析 Anthropic 别名；`registry.get_router` 内部 canonical 化 | body 的模型固定为准入时的 canonical | 解析规则不变 |
| retarget | `InteractiveAdmission` 移动负载计数 | `BatchAdmission` no-op | 位置与今天相同 |

Batch 不重新解析调用方保存的别名（[`_dispatch_body`](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/batch/scheduler.py#L785-L800)）。执行时算出的 canonical 与固定值不一致时是否拒绝，是新增行为，列为 Q3，迁移中保持 no-op。

### 5.2 资源：唯一责任方与唯一释放点

规则：每种资源只有**一个**责任方和**一个**释放点（某个 `finally` 或依赖拆除）。“幂等”只描述释放函数本身能否安全重复调用，不能代替“只有一个释放点”。

| 资源 | 获取者 / 释放点 | 释放函数可否重复调用（基线事实） |
|---|---|---|
| 在线每用户槽 | `enforce_user_concurrency` / 同一依赖的 `finally`（[concurrency.py:465-473](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/concurrency.py#L465-L473)）。Responses 与 compat 只有一次 | **否**（[:176-181](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/concurrency.py#L176-L181)） |
| interactive-load tracker | 同一依赖的 `with tracker`（[:210-226](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/concurrency.py#L210-L226)）；`InteractiveAdmission` 只暴露 `retarget` | **否**（`__exit__` 每次递减）；`retarget` 可多次调用 |
| Batch 角色配额 | `_take_user_slot` / `_attempt` 的 `finally: give_back()` | **否** |
| Batch 模型容量、租约 | scheduler `_track`/`_forget`、`complete_item`/`retry_item`/`release_item` | 结算以 `attempt_seq` 栅栏防重复写入 |
| Messages health claim | 核心预检 / 非流式在核心 `finally`；流式在流对象 `finally` 或未迭代时的 `aclose` | **可以**（[endpoint_health.py:992-1005](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/routing/endpoint_health.py#L992-L1005)） |
| Messages prefill 租约 | 核心（流式在首次迭代中获取）/ 首 token 释放，`finally` 兜底 | **可以**（[prefill_load.py:903-907](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/routing/prefill_load.py#L903-L907)） |
| Chat 的 router 资源（health claim、prefill、RouteWise 预约、hedge 腿） | router 实现内部 / router 自己的 `finally` | 由 router 负责，迁移不改 |
| Chat `StreamSession` reader 任务、keepalive reader 任务 | 会话流与 keepalive 生成器 / 各自 `finally` 中 `cancel` | — |
| Embeddings 上游连接 | adapter 内部 | — |
| Responses 落库任务 | `ResponsesStream` 的 `finally` 中 `_schedule(...)`，由模块级集合持有 | — |
| 请求作用域（contextvar） | 入口打开（中间件或 `_dispatch` 的新任务）；核心不打开也不退出 | — |
| Batch 计费观察者 | `_dispatch` 的 `observing()` | — |

### 5.3 contextvar：长期目标与过渡边界

**长期目标**（不是本工作的交付物）：可信输入只经参数传递；contextvar 只作为向 adapter、key pool、router、中间件传值的输出与兼容通道，每个键都能追溯到唯一的写入者。

**本工作的边界**：

1. 作用域由入口打开并持有。核心不调用 `reset_request_scope`，也不在返回值外层套 `req_ctx.push(...)` 之类会在 `return` 时退出的上下文管理器，所以返回尚未消费的流对象不会提前退出作用域。
2. 入口写入的键不变：种子键（request_id、UA、定价快照）与 `USER_ROLE`。
3. 核心写入的键与今天的 handler 相同、时机相同（Messages 第3.2节 C3，Chat 第3.3节 H10，Embeddings 的 `auth_key_hash`/`affinity_key`/`synthetic_probe`）。reader 任务在创建时复制上下文，写入顺序必须保持。
4. 唯一迁移的键是 `ANTHROPIC_VERSION`：值来自 `MessagesConstraints.anthropic_version`（只有 `for_batch_item` 设置，取自存储中冻结的 `protocol_profile`），由 Messages 核心的兼容桥写入，不再由 dispatch 种子写入。adapter 仍只接受已知版本。
5. 删除：`BATCH_EXECUTION` 的写入与全部读取（S3），键名在 S6 从 `REQUEST_SCOPED_KEYS` 删除。核心不从 contextvar 读取任何可信输入。Chat 读回 router 写入的 `provider`（[:1061-1063](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/servers/routers/completions.py#L1061-L1063)、`StreamSession._capture_provider_from_ctx_once`）属于读取输出，保留。
6. 留下的环境通道是 `_OBSERVER`（Batch 计费观察者，Q5）与 `ANTHROPIC_VERSION`（协议选项，Q4）。两者都不承载“这是 Batch，所以可以放宽”的语义。

### 5.4 不加 skip 标志；准入限制与冻结版本的区别

1. 核心必须拿到已认证上下文与 `Admission` 才能调用，没有省略它们的重载或默认值。
2. 选项对象（`MessagesConstraints`、`ChatOptions`）没有默认构造，由入口工厂构造。工厂是工程约定，不是 Python 安全边界；所以选项中**不能有**任何能绕过模型权限、计费、日志、health 记录或准入的字段。
3. **准入限制**（Messages 的 adapter 过滤、`pin_model`）只收窄可执行范围，可以用“只收窄”论证其安全性。
4. **冻结的协议版本**（`anthropic_version`）不是准入限制：它只能取创建时已验证、已存储的值，并按该版本原有的语义执行（今天由 [`profiles.py`](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/batch/profiles.py) 在准入时检查规则，由 adapter 发送对应请求头）。一个版本不一定比另一个更严格，安全性来自“值的来源唯一且经过验证”，而不是“收窄”。
5. `ChatOptions.runtime_settings` 只影响强制流式与 probe 日志开关；probe 的计费不受它影响（今天的事实：“the marker affects noise, never money”）。
6. 计费与日志由核心按各协议现有条件执行（第3.7节），入口不能跳过；包括 Messages 在 `log_store is None` 时不计费的既有行为（Q6）。
7. `BatchAdmission.retarget` 是纯 no-op。“不匹配即拒绝”是新增行为（Q3）。

### 5.5 重试、health、熔断与 feedback

| 层 | 归属 | 迁移后 |
|---|---|---|
| Chat 的单次请求内重试、fallback、hedging、熔断、health 证据、RouteWise 学习 | router 实现 | 不变；核心照旧调用 `record_routing_observation`，取消时照旧不报告 |
| Messages 的 adapter 选择与 health claim | Messages 核心 | 不变；没有 fallback |
| Embeddings 的回退 | `FallbackEmbeddingAdapter` | 不变；不报告 health |
| Batch 的跨尝试重试 | scheduler：按状态判定 `retryable`，指数退避，`max_attempts` | 不变；与 router 内重试相互独立，所以一次 Batch 项可能触发多次上游调用 |
| Batch 的模型级冷却 | scheduler `_model_failed`/`_model_succeeded` | 不变 |

### 5.6 计费与日志：已计费与未知分开

- `cost_status` 的推导留在 `_dispatch`，不变：观察者看到计费报告 → 全部带费用为 `billed`，否则 `unknown`；没有看到 → 超时、异常、成功、502/504/500 为 `unknown`；其余错误为 `not_billed`（[dispatch.py:305-318](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/apps/backend/serving/batch/dispatch.py#L305-L318)）。
- 观察者的报告来源不变（第3.6节）。“没有观察到计费”不表示上游没有向我们收费。
- 日志入队是同步的（`schedule_log`），日额度增量是独立任务，所以“已调度”不等于“已提交”。

---

## 6 流式与生命周期

### 6.1 逐协议的边界（迁移前后相同）

| 阶段 | Chat SSE | Chat 强制流式 | Messages SSE | Responses SSE |
|---|---|---|---|---|
| 校验与预检 | 核心；`HTTPException` → HTTP 状态 | 同左 | 核心；`MessagesError` → HTTP 状态 + Anthropic 错误体 | Responses 核心与 Chat 核心；HTTP 状态 |
| 首项 | 路由在返回响应前 `prime()`；未提交的错误帧 → HTTP 状态 | 路由在返回前拉 keepalive 包装的首项；`HTTPException` 照抛 | 不预拉；claim 已在预检中取得 | Responses 核心 `prime()`（今天在 Chat handler 内） |
| 响应头 | `text/event-stream`、`no-cache, no-transform`、`X-Accel-Buffering: no`、probe 时 `X-Provider` | `application/json`、同上缓存头、`scope["state"]` 流式标记 | 今天的 `sse_headers` | `_SSE_HEADERS` |
| 首帧 | role chunk 随第一个可见内容或 15 秒到期发出 | 15 秒空白 keepalive | 上游首帧 | `response.created` 等（translator） |
| 结束 | 透传 `[DONE]`；`_finalize_success` 计费与日志 | JSON 整体 | 补全末帧；`finally` 计费与日志 | 先落库，再发终止事件 |
| 提交后上游错误 | `data: {"error"…}` 帧，不计费 | JSON 错误体（已提交时） | SSE error 帧，计 0 | `response.failed`，不落库 |
| 客户端断连 / 取消 | `_finalize_cancelled`：499/504 日志，不计费 | 生成器 `finally` 关闭 | `finally`：估算 usage 计费 | Chat 的口径；`incomplete` 后台落库 |
| 资源释放 | 会话流与 router 流的 `finally` | keepalive reader 的 `finally` | claim、prefill、reader 的 `finally` | translator 外层 `finally` + Chat 的 |

**分界原则不变**：响应头发出之前的错误都是 HTTP 状态；之后都是流内报告。核心返回可迭代对象，逐项产出，不额外缓冲；强制流式的缓冲是今天就有的 HTTP 交付方式，留在 HTTP 层。

### 6.2 作用域与生命周期

- 在线：请求作用域由 `RequestIdMiddleware` 打开；超时 deadline 由 `TimeoutMiddleware` 持有并通过 `ctx.timeout_probe` 只读暴露；槽与 tracker 由依赖持有，在流迭代结束或断连之后才释放（第3.8节，Chat 已测；Messages/Responses 由 S0 补测）。
- Batch：作用域由 `_dispatch` 的新任务打开；超时由 `move_on_after` 表达，取消以 `CancelledError` 进入核心。Batch 恒为非流式，所以 `StreamSession` 与 `_finalize_cancelled` 不参与。
- 核心返回流对象时不退出任何作用域；流对象的 `finally` 在消费者的任务中执行。

### 6.3 待验证的生命周期问题

- **Messages 未开始迭代即断连**：claim 在预检中获取，只在生成器 `finally` 中释放；从未启动的异步生成器在 `aclose()` 或被回收时不会执行 `finally`。锁定版本 Starlette 1.3.1 在开始迭代前断连时是否会出现这种情况、是否对 body 迭代器调用 `aclose`，未验证（Q7）。
- **Chat/Responses 已 prime 但未迭代即断连**：`StreamSession.stream` 已启动，`_chain_first_then_rest` 尚未启动；后者的 `finally` 不会执行。会话流与 router 流（以及其中的 router 资源）何时关闭，未验证（Q7）。
- 两者都在 S0 以特征化测试**记录观察到的事实**，迁移保持现状。

### 6.4 异常场景矩阵

| 场景 | 当前事实或推断 | 迁移承诺 | 证据 |
|---|---|---|---|
| Batch 超时（`move_on_after`） | 观察者已看到计费 → `billed`/`unknown`；没有看到 → `unknown`，不会是 `not_billed`。**推断**：在 Messages 非流式与 Chat 非流式中，从上游返回到计费点之间没有挂起点，取消要么落在计费点之前，要么不会打断已开始的计费段（Chat 的 `schedule_increment` 虽是 `async def`，函数体内没有 `await`，这一点是基线事实；整段推断待 S0 验证） | 保持 dispatch 分支 | [review_contracts:125](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/tests/unit/serving/test_batch_review_contracts.py#L125)、[dispatch:321](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/tests/unit/serving/test_batch_dispatch.py#L321) |
| 在线非流式，等待上游时断连 | **推断**：只有 ASGI 层真的取消 handler 任务时才会出现 `CancelledError`，取决于锁定版本服务器与中间件 | 不变 | 待验证（S0） |
| 在线流式，开始迭代前断连 | 第6.3节 | 不变（Q7） | 待验证 |
| 在线流式，迭代中断连 | Chat：499 日志、不计费；Messages：估算计费一次；Responses：`incomplete` 落库 | 不变 | 已有测试（Messages [health_recording:492](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/tests/servers/test_anthropic_messages_health_recording.py#L492)；Responses [:864](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/tests/servers/test_responses_router.py#L864)） |
| 上游异常或空闲超时 | 各协议的错误帧与失败日志 | 不变 | 已有测试 |
| Batch 取消 | 运行中的项跑完并落库 | 不变 | [http_postgres:764](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/tests/integration/test_batch_expansion_http_postgres.py#L764) |
| worker 停机 | 宽限期后取消，`release_item(dispatched=True)` → unknown | 不变 | [test_batch_scheduler.py:801](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/tests/unit/serving/test_batch_scheduler.py#L801) |
| 租约丢失 | **推断**：原任务继续执行；另一个 worker 重新 claim 后再执行；两次都经过计费点；批次只记录持有栅栏的那一次 | 不改变；不宣称不重复计费 | 未见测试（Q8） |

### 6.5 不承诺恰好一次

同一个 attempt 的结果只写入一次（`attempt_seq` 栅栏），同一次核心执行只有一个计费点。同一个 Batch 项在不同 attempt、不同 worker 之间，上游可能执行多次并各自计费；Chat 的 router 内重试也可能在一次核心执行中多次联系上游（计费按最终成功的那次 usage）。本稿不提出幂等上游调用或计费去重（Q8）。

---

## 7 兼容、混合版本与回滚

**每个阶段都必须保持**：

- **公开 API**：路径、请求与响应 schema、`response_model` 序列化、错误体、状态码、`error.type`、`Retry-After`、`X-Provider`、SSE 字节与帧顺序。
- **Batch API**：两种输入方式、准入规则、状态投影、结果行与下载、幂等摘要、旧 `api_surface='anthropic'` 批次、410 提示路由、webhook 事件类型（新批次 `batch.<status>`，旧批次 `message_batch.ended`）、控制台与管理员 UI。
- **版本**：在线发送 adapter 的固定默认版本；Batch Messages 以冻结的 `protocol_profile` 发送。
- **存储**：不新增迁移，写入内容不变，`cost_status` 推导不变，`api_logs` 字段与计费口径不变。
- **资源**：Fixed/RouteWise 的资源归属、`endpoint_id`、策略、重试与 feedback 不变。

**混合版本**：不改数据库格式**不足以**证明新旧进程混跑安全，因为执行代码决定写入什么。这里把滚动升级定为**有条件的验收目标**：只有在下列检查通过之后，才能说某个阶段支持滚动升级。每个阶段都在隔离环境中让基线版本进程与新版本进程共享一个 PostgreSQL，二者都启用 worker：

1. 本阶段迁移的目标的批次（内联与文件）被两种 worker 都领取过，每项恰好结算一次；`request_counts`、`cost_status`、`unknown_cost_count` 正确。
2. 同一请求由旧 worker 与新 worker 执行时，结果行与 `api_logs` 的逐字段对照（S0 的字段清单）一致。
3. Messages：上游收到的 `anthropic-version` 与冻结值一致，与执行的 worker 无关。
4. 共享 interactive load（`BATCH_SHARED_INTERACTIVE_LOAD`）开启时，新进程的在线请求经 `InteractiveAdmission` 发布的计数，能被旧进程的 scheduler 读到并让位，反之亦然。
5. webhook、取消、删除、过期的行为与单一版本相同。

**已知边界**（沿用统一 Batch API 修订第3节）：早于 #1660、没有种类过滤的 worker 会把任何批次当作 Chat 执行，只能按那份记录的流程停掉全部旧进程。

**回滚**：每个阶段只改代码，撤销该阶段的提交即可；回滚本身也是一次混合版本过程，适用同样的检查。

---

## 8 分阶段计划（概要）

详细的文件、签名、迁移桥、测试与验证见[实施计划](../plans/2026-10-28-shared-inference-execution-all-targets.md)。

| 检查点 | 内容 | 依赖 | 外部行为 |
|---|---|---|---|
| S0 | 只加测试：四目标的在线/Batch 逐字段对照、流式字节快照、retarget 全程计数、在线配额耗尽与容量等待、待验证项的特征化；smoke 扩展（Embeddings 假上游、SSE） | — | 不变 |
| S1 | 共享上下文；`enforce_user_concurrency` yield `InteractiveAdmission`；`retarget_interactive_load` 接受能力对象；probe 的请求头映射变体；`log_rejection` 接受来源值 | S0 | 不变 |
| S2 | Embeddings 核心；Batch 直接调用 | S1 | 不变 |
| S3 | Messages 核心（上一版的试点）；删除 `BATCH_EXECUTION` 读写 | S1 | 不变 |
| S4 | Chat 核心与 HTTP 渲染；Batch 直接调用。Responses 与 compat 仍调用 `chat_completions` 路由函数，由一个过渡桥（`ScopeAdmission`，经 `scope["state"]` retarget）保持它们今天的行为，S5 删除 | S1 | 不变 |
| S5 | Responses 核心经函数调用委派 Chat 核心；compat 改调 Chat 核心；删除私有属性改写 | S4 | 不变 |
| S6 | 删除伪造层与守卫；`user_ctx` 归属合并；可选：核心迁入 `serving/surfaces/` 与导入检查 | S2–S5 | 不变 |

全部阶段在同一分支上依次完成，每个阶段结束时是一个可以在本地评审的检查点（本地提交），不单独提 PR、不合并、不部署；最后由协调方开一个覆盖四个目标的草稿 PR。S2 与 S3 互不依赖；先做 S2（最小、无流式），用它确定 Batch 结果映射与守卫测试的调整方式。

---

## 9 验收标准

1. Batch 的四个目标都不构造 `Request`、不调用路由 handler、不解析 `JSONResponse`；`call_handler`、`_request`、`HANDLERS`、`BATCH_EXECUTION` 在 S6 后不存在（测试 + 代码检索）。
2. 每个协议的在线入口与 Batch 走同一个核心；每次核心执行恰好一个计费点（[`test_each_target_is_billed_once_and_the_attempt_sees_that_charge`](https://github.com/HarvardMadSys/hybridInference/blob/a738f49153ffe68b0944b8b55b8ecd6b55f54371/tests/unit/serving/test_batch_targets_dispatch.py#L157) 保持为绿）；S0 的逐字段对照在每个阶段后结果不变。
3. Responses 与 compat 只获取一次在线槽；Responses 的一次请求只产生一行 `api_logs`、一次计费报告、一次 routing 观察。
4. 没有任何能跳过鉴权、模型权限、计费、日志、health 或准入的参数；准入限制只收窄；冻结版本只取已验证的存储值。
5. 在线配额耗尽时四个目标的 Batch 仍真实执行，在线槽与在线负载计数不变；模型容量或在线预留不足时 Batch 等待（S0 新增测试 + #1687 的 :459、:561）。
6. 流式：预检错误仍为 HTTP 状态；Chat 未提交错误仍为 HTTP 状态；逐帧输出在重构前后逐字节一致（假上游、固定输入、剔除时间与随机 id）；返回流时在线槽和 tracker 仍被持有，直到流结束。
7. 第5.2节的每种资源在成功、错误、取消、超时、迭代中断连、`aclose` 之下都由唯一的释放点释放；待验证场景以 S0 记录的事实为准。
8. 第7节的保持条件全部满足；混合版本检查通过后，才能声称该阶段支持滚动升级。
9. 每个阶段在隔离网关（真实 uvicorn、真实 PostgreSQL、合成上游）上通过 smoke 与本阶段的流式检查；结论只代表合成上游，不代表真实供应商的验收。

---

## 10 非目标

新服务、进程边界、公开 API 或数据库迁移；HTTP 回环调用；万能请求格式；把 Messages 转成 Chat 执行；统一 Fixed、RouteWise、Messages 自选 adapter 与 Embeddings 回退的执行循环；新建第二个通用 router（`RouterProtocol` 是唯一的通用契约）；给 Messages 增加 fallback；统一四协议的计费口径；改变 Batch 准入、调度或存储；上游恰好执行一次或计费去重；一次性删除 adapter、key pool、router、中间件对 contextvar 的全部依赖；修复本稿记录但不属于本工作的既有行为（Q13、Q14）。

---

## 11 备选方案与代价

| 方案 | 优点 | 代价 / 风险 | 结论 |
|---|---|---|---|
| A 维持现状 | 零改动；已有守卫测试 | 隐式的请求、scope 与 contextvar 假设随修改扩散 | issue 认可的短期选择；用户已要求全部迁移 |
| B 只加固伪造层 | 改动小 | HTTP 边界仍在执行路径里 | 不推荐 |
| C 先抽 Chat，再做其他 | 收益最大 | 一次牵动 `StreamSession`、强制流式、Responses 与 compat，回归面最大 | 不作为第一步；排在 S2/S3 之后 |
| D 统一请求格式与执行循环 | 看起来整齐 | 违背 issue 与路由契约；原生字段、SSE 帧、计费口径无法无损表达 | 拒绝 |
| E 进程内 ASGI 回环 | 中间件与依赖全部真实 | 存储中只有 key 的 hash，需要伪造认证；占用在线槽；重复请求副作用 | 拒绝 |
| F Responses 改为直接调用 router | 少一层 | 复制 Chat 的计费、日志、观察、auto、权限逻辑，必然漂移 | 拒绝 |
| **G 共享上下文 + 按协议的核心，分阶段（本稿）** | 每步去掉一块真实的 HTTP 依赖，可单独回滚 | 过渡期双机制并存；新增若干类型；测试 patch 目标需要调整；smoke 需要扩展 | 推荐 |

---

## 12 风险

- **行为漂移**：handler 中有大量顺序敏感的步骤（req_ctx 写入顺序、reader 任务复制上下文、日志与计费的先后）。各阶段只搬移、不改写，用 S0 的对照与字节快照兜底。
- **测试 patch 目标落空**：Messages 有 25 个测试文件引用模块、十余个私有名被 monkeypatch；Responses 与 Batch 有 5 处 `monkeypatch.setattr(..., "chat_completions", …)`（`test_responses_router.py` 3 处、`test_batch_review_contracts.py`、`test_batch_dispatch.py`），S4/S5 必须同步改写，且改写后的测试仍要能证明原断言。核心先留在原模块内，降低落空风险。
- **过渡期双机制**：`retarget_interactive_load(request)` 与 `admission.retarget`、`user_ctx` 中的归属与 `BatchAttribution`，并存到 S5；代码注释要标明是过渡状态，S6 删除。
- **Chat 的 prime 位置**：从 handler 移到核心的流对象方法，Responses 与 Chat 路由各调用一次。若某一方忘记调用，未提交错误会变成流内错误帧；S0 的字节快照与状态码测试必须覆盖两条路径。
- **待验证的推断被误读为结论**：第6.3、6.4节的条目在检查点说明与最终草稿 PR 中继续标注。
- **假上游的局限**：只能证明协议与存储行为，不能证明真实供应商的延迟与错误分布。

---

## 13 开放问题（需评审决定）

- **Q1** 过渡期双机制可接受到 S5 吗？还是要求 S1 就把 Chat 的 retarget 也改为能力对象（会提前触及 Chat 与 Responses）？建议：S1 只提供能力对象，各协议在自己的阶段改用。
- **Q2** 最终目录名（例如 `serving/surfaces/`），需避免与 `routing/dispatch.py`、`batch/dispatch.py` 混淆。
- **Q3** `BatchAdmission.retarget` 遇到不匹配是否在上游 I/O 之前拒绝？新增行为，需先取得证据（别名、route 重映射、首个 adapter 变化的组合下能否发生、今天的结果是什么）。适用于 Chat、Responses、Messages。
- **Q4** 协议版本继续经 `ANTHROPIC_VERSION` 传递，还是改为 adapter 的显式参数（会修改 adapter 与 `LeafBackend` 签名）？
- **Q5** Batch 计费观察者是否改为显式返回值？建议四个目标都迁移后再统一处理。
- **Q6** Messages 在 `log_store is None` 时不计费，而 Chat 与 Embeddings 照样计费。保持不变；是否统一是产品决定。
- **Q7** 若 S0 证实开始迭代前断连会残留 Messages claim 或 Chat 会话流：(a) 把获取移入生成器——会改变抢输或未提交错误的外部表现；(b) 由入口在可靠的生命周期边界上调用流对象的 `aclose()`——只有证明锁定版本存在这样的边界才成立，`__del__`、GC、后台任务都不算。
- **Q8** 租约丢失导致的重复执行与计费，是否单独设计（心跳失败后取消本地 dispatch、按 `(item, attempt)` 去重日额度增量等）？涉及存储与计费语义，超出本稿。
- **Q9** Batch Embeddings 以 `adapter.config.id` 固定模型、执行时按这个名字查 `embedding_adapters`。若某个 adapter 的注册名与 `config.id` 不同，执行会得到 404——这是现状还是不会发生？S0 用测试回答，迁移保持现状。
- **Q10** issue 要求上下文携带“取消与生命周期信息”。本稿用 asyncio 取消 + `timeout_probe` 表达，是否足够？若不够，需要的是哪类信息（例如“租约已丢失”，与 Q8 相关）？
- **Q11** 是否需要强类型的已认证身份取代 `user_ctx` 只读视图？应作为工程约定评估，不作为安全边界。
- **Q12** Chat、Embeddings、Responses 核心是否把 `HTTPException` 换成不依赖框架的协议错误？需要同时改全局处理器与 Batch 映射，建议 S6 之后单独评审。
- **Q13** Responses 以 `runtime_settings=None` 委派，因此可信 probe 在 Responses 上的日志永远被抑制，与 `log_synthetic_probes` 开关无关。保持；是否修正另议。
- **Q14** `/v1/completions` 对非法 JSON 或非对象请求体回答 500。保持；是否改为 400 另议（会改变公开行为）。
- **Q15** Batch 中 Chat/Responses 在 req_ctx 发布 request_id 之前就被拒绝的项，结果里没有 request_id，尽管 `api_logs` 里有一行。核心可以把 request_id 显式返回，但会改变 `batch_items.request_id` 的写入范围；保持现状，另议。

---

## 14 工作量的诚实界限

不估工时。规模信号：`anthropic_messages.py` 的 handler 主体约 1,040 行（:1550-2592）；`completions.py` 的 handler 约 830 行（:451-1279），另有 `completions_stream.py` 1,152 行（只调整构造参数，不搬移）；`responses.py` 462 行；`embeddings.py` 的 handler 约 350 行；`dispatch.py` 631 行。回归面：Messages 149 个测试函数、25 个引用该模块的测试文件；Chat 相关 11 个测试文件引用 `completions`；Responses 1 个专门测试文件；Embeddings 5 个；compat 3 个；Batch 单元与集成测试十余个文件。难度较高的新增工作：S0 的断连特征化与租约丢失复现、smoke 的 SSE 与 Embeddings 支持、每个阶段的混合版本检查、S4/S5 中改写 patch `chat_completions` 的测试。S0 若揭示当前行为与注释不符，应回到评审，而不是在阶段内悄悄扩大改动。

---

## 15 已核对的源码（基线 `a738f4915`）

- Batch：`serving/batch/dispatch.py`（全文）、`scheduler.py`（:554-860）、`attempts.py`（全文）、`targets.py`（:340-560、:690-707）。
- Messages：`anthropic_messages.py`（:500-600、:775-800、:1545-1600 及第3.2节所列位置；与上一版核对的 `5f3747b1d` 相比该文件未改动）。
- Chat：`completions.py`（全文）、`completions_stream.py`（:422-731、:1078-1152）、`completions_cost.py`（:200-318）。
- Responses 与 compat：`responses.py`、`compat.py`（全文）。
- Embeddings：`embeddings.py`（全文）。
- 准入、认证与上下文：`concurrency.py`（:150-473）、`middleware/timeout.py`（:89-138）、`app.py`（:160-210）、`utils/synthetic_probe.py`、`utils/session_identity.py`（签名）、`utils/request_ip.py`（`get_client_ip_info`、`get_client_ip`）、`openai_chat_serializer.resolve_mode`、`pricing.py`（:196-215）、`model_availability.model_not_found_detail`。
- 路由契约：`routing/protocols.py` 的 `RouterProtocol`、`model_router_registry.get_router`、`routers.py` 中 `eligible_adapters` 与 `stream_chat_completion` 的位置。
- 设计记录与 issue：根 AGENTS.md、routing/AGENTS.md、第0节列出的 Batch 记录、issue #1690 正文。
- 测试：第3、6、9节所列；`ops/dev/batch_api_smoke.py`（假上游只有 chat 与 messages 两个端点，不支持 SSE）。本稿撰写过程中未运行任何测试，也未启动服务。

---

## 16 实施记录（2026-10-28）

### 16.1 检查点

| 检查点 | 提交 | 内容 |
|---|---|---|
| S0-docs | `0f47eaa35` | 本稿与实施计划（含两条评审更正） |
| S0 | `01dbf9d5c` | 只加测试：四目标在线/Batch 逐字段对照、流式全程计数、Responses 恰好一次、请求头大小写与重复头、预检错误为 HTTP 状态、Chat/Messages/Responses SSE 逐字节快照、Chat/Embeddings/Responses 的在线槽与 Batch 隔离（真实 PostgreSQL） |
| S1 | `fe4778854` | `serving/surfaces/context.py`、`servers/surface_http.py`；依赖 yield `InteractiveAdmission`；probe 请求头变体；`RejectionSource` |
| S2 | `904266db0` | `execute_embeddings`；Batch 直接调用 |
| S3 | `b3ed1f5e7` | `execute_messages`；删除 `BATCH_EXECUTION` 读写；版本由执行写入作用域 |
| S4 | `c3f622481` | `execute_chat`、`ChatStream`、`render_chat_http`；Batch 直接调用；两个过渡桥 |
| S5 | `46b5bd5d4` | `execute_responses` 以函数调用委派 Chat；compat 调用 Chat 核心；删除过渡桥；Batch 不再调用任何 handler |
| S6 | `c92fdfbdd` | 删除 `call_handler`、伪造 `Request`、`HANDLERS`、`user_ctx` 归属合并、`batch_log_attribution`、`BATCH_EXECUTION` 键、scope 中的 tracker 与 `retarget_interactive_load`；治理测试 |

### 16.2 与设计的差异（均不改变外部行为）

1. **no-op 准入**名为 `UntrackedAdmission`（单例 `UNTRACKED`），不叫 `BatchAdmission`：它同时用于 Batch 与“路由依赖被替换”（测试中常见）的情形，二者今天都是“找不到 tracker → 不移动计数”。路由以 `admission_from(slot)` 取得：是 `InteractiveAdmission` 就用它，否则 `UNTRACKED`。
2. **拒绝日志**用取值的 `RejectionSource` + `log_rejection_from`（第4.2节已更正），不另建 `RejectionSink` 协议。
3. **`ClientOrigin.ip` 惰性解析**：`get_client_ip_info` 首次解析时会记录 `client_ip_resolution_unresolved`，所以由 `resolve_ip` 在首次读取时才解析，时机与今天读取请求时相同（例如 Responses 的 400 仍不会触发解析）。
4. **Messages 非法 JSON**：路由解析失败时把哨兵 `MALFORMED_BODY` 交给 `execute_messages`，后者在生成 request id、`note_request_id`、写 affinity 之后才回答 400，与今天的顺序相同。
5. **`MessagesError` 在创建时就已 conceal**（`conceal_served_model` 读取作用域，必须在今天 `_anthropic_error` 的同一时刻调用）；在线渲染与 Batch 映射都使用这个已处理的消息。Batch 对成功体使用与 Starlette `JSONResponse.render` 相同的 JSON 编码再解码，保持“无法编码即异常 → 500 unknown”的现状。
6. **`render_messages_http` 在函数内导入 `StreamingResponse`**，与原 handler 相同（一个现有测试依赖这一点替换它）。
7. **核心留在原路由模块**（`anthropic_messages.py`、`completions.py`、`embeddings.py`、`responses.py`），没有迁入 `serving/surfaces/`：迁移会让大量 monkeypatch 目标落空，而计划把它列为可选（Q2 仍开放）。代替导入扫描，治理测试检查四个执行函数的签名与源码 AST：不接收、不读取 `Request`/`Response`/`StreamingResponse`/`JSONResponse`/`Depends`/`Header`/`call_handler`。
8. **错误载体**：Chat、Embeddings、Responses 核心继续抛 `HTTPException`（Q12 未决）。
9. **`X-Provider` 的现状**（实施中核实，FastAPI 0.137.1）：返回 `Response` 对象时，FastAPI 不合并注入的 `Response` 的响应头。所以今天 Responses（返回 `JSONResponse`/`StreamingResponse`）从不向客户端发出 Chat 写入的 `X-Provider`，compat 与 Chat（返回 dict）会发出。实施保持这一点：`ChatResponse.provider_header` 只由 Chat/compat 的渲染写入。
10. **`dispatch._request` 删除后**，`preset_client_ip_info` 在生产代码中已无调用者，但仍被网络准入测试使用，保留，仅更正其 docstring。
11. Batch 中不可能出现的流式结果（`stream` 已强制关闭）由各执行器在本作用域内 `aclose()`，并按 502 回答，存储的错误文本与今天相同（“The handler returned a stream for a batch request.”）。

### 16.3 验证

- 门禁：`make lint` 通过；`make test` 10590 passed、4 skipped（基线 10492 passed、4 skipped）；`make test-db-local` 754 passed（共享容器中本 worktree 的数据库，未停止容器）。
- **真实隔离网关**（真实 uvicorn、共享测试容器中的独立数据库 `hi_e2e_sharedinf_test_db`、合成上游；结论只代表合成上游）：
  - `ops/dev/batch_api_smoke.py check` 在本分支网关上输出 `BATCH_API_SMOKE_OK`。
  - **基线与本分支逐字节对照**：同一数据库上同时运行基线 `a738f4915` 网关与本分支网关，对 25 个探针（Chat 一元/SSE/未提交 422/未知模型/非法 JSON/大写 session 头/严格 reasoning；compat 两个端点含 SSE；Responses 一元/SSE/未提交 422/缺 input/未知 previous_response_id；Messages 原生一元与 SSE、翻译一元与 SSE、未知模型的流式请求、缺字段、非法 JSON；Embeddings 与未知模型）发送相同请求：状态码、保留的响应头与规范化后的响应体**全部相同**；对应 `api_logs` 行（模型、provider、状态、token、费用、session、端点、params、metadata，剔除逐请求生成的 id 与来源字段）**全部相同**。
  - **中途断连**：Chat 与 Responses 都记 499、`client_disconnect`、不计费；原生 Messages 以上游 usage 计费；翻译 Messages 以 `usage_estimated` 计费——两版相同。
  - **混合版本**：两个进程的 worker 同时运行，四个目标各 24 项（基线 worker 与新 worker 各执行 12 项）全部恰好结算一次，状态、token、费用、`cost_status`、结果体与 usage 在两种 worker 之间相同，每项一行 `api_logs`；`anthropic-version: 2026-10-21` 的 Messages 批次由两种 worker 执行时，上游收到的版本都是 2026-10-21。
  - 验证后停止全部进程、删除该数据库与基线 worktree。

### 16.4 未完成或未验证

- ~~计划 S0 中的 T7、T8、T9 没有写成测试~~：已在 `1a0808415` 补上，见第16.5节。
- `ops/dev/batch_api_smoke.py` **没有扩展** SSE、Embeddings 与 `stream-check`；流式、Embeddings 与断连的网关验证用的是仓库外的临时合成上游与脚本，没有提交。
- 混合版本检查只覆盖“基线与本分支同时运行”；早于 #1660 的 worker 的已知边界（统一 Batch API 修订第3节）未重新验证。
- 没有真实供应商、真实账户或生产环境的验证；未推送、未开 PR。

### 16.5 T7、T8、T9 的结论（`1a0808415`）

- **T7（开始迭代前断连，Q7）**：锁定的 uvicorn 0.46 报告 ASGI spec 2.3。此时即使客户端已经断开，Starlette 仍会启动响应体生成器，再由断连监听取消它，各流自己的 `finally` 照常释放——测试确认，行为不变。spec ≥ 2.4 的服务器在首次 `send` 失败时**从不启动**生成器，测试在本分支与基线 `a738f4915` 上同样证明了两个既有缺陷：Messages 在预检中取得的半开探测权一直被占用到截止时间（其后请求全部 503）；Chat 与 Responses 为决定状态码而预读的会话流（连同上游流、reader 任务、router 的 claim）只有在被垃圾回收时才关闭，异常 traceback 被保留时会无限推迟。修复：三个路由（及 compat）各用一个 `scope="request"` 的 yield 依赖持有本次的流，请求结束时只在响应体**从未开始**时释放——`MessagesStream.release_if_unstarted()` 归还 claim，`ChatStream.close_if_unstarted()` 关闭预读的流，使会话按客户端断连结束（499、不计费）。已开始的响应体仍由自身释放；运行中的流在结束前一直持有探测权（有测试）。Q7 就此关闭；方案即原 Q7 的 (b)，边界是 FastAPI 0.137 的 request 作用域依赖。
- **T8（租约丢失，Q8）**：没有发现缺陷，现状以真实 PostgreSQL 测试固定：另一 worker（或同一 worker 在自己的租约失效后）重新领取并再次执行；`attempt_seq` 栅栏只结算一次，落在较新的 attempt 上，旧 attempt 迟到的结果不改变任何东西；丢失的 attempt 记为 `abandoned`、费用 `unknown`，计入 `unknown_cost_count`；上游执行两次、用户被计费两次。测试把这记录为当前行为而非保证，**不承诺跨 attempt 恰好一次**。同一 worker 会重新领取自己仍在执行的过期项，这是调度器的既有行为，未修改；是否改变它仍属 Q8。两项测试在基线上同样通过。
- **T9（embedding 别名与 `config.id`，Q9）**：没有发现缺陷。注册表只在 `embedding_adapters` 中以模型 id 与别名为键，适配器的 `config.id` 就是模型 id（含 `provider_model_id` 不同的路由与 fallback 链），准入固定的 id 总能找到同一个适配器；准入后别名被改指向别的模型，项目仍在原模型上执行；模型被删除时以未知模型（404、not_billed）失败且不触达上游。测试在基线上同样通过。Q9 就此关闭。
- 门禁：`make lint` 通过；`make test` 10603 passed、4 skipped；`make test-db-local` 756 passed；`gitleaks git` 对本分支提交无发现。
- 隔离网关（真实 uvicorn、独立数据库、合成上游；本机没有可用的 staging，也不在授权范围内部署）上，基线与 `1a0808415` 的 25 个在线探针与对应 `api_logs` 全部相同，中途断连四种情形相同；混合版本下四个目标共 288 项由两种 worker 执行，结果一致。spec 2.4 的断开情形无法用 uvicorn 在网关上复现，只由测试覆盖。
