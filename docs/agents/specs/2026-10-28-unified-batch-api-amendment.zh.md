# 统一 Batch API：设计修订

日期：2026-10-28。状态：已批准并在本分支实现（PR #1687，未合并、未部署）。基线：dev `df40a9a82`。本记录描述最终设计；评审中改动过的决定见第7节。

本记录取代 [2026-10-26 Batch API 扩展设计](2026-10-26-batch-api-expansion-design.zh.md) 中“Anthropic Message Batches 作为独立 API 面”的决定：第1节第3项、第2节架构图与模块表中的 `message_batches.py`、第4节的 Anthropic 状态投影（`processing`/`ended`）与“预检失败的项直接存为失败”、第5节“幂等键范围含 api_surface”对 Anthropic 的部分，以及第9节 `message_batch.ended` 事件类型。也取代 [2026-10-27 修订](2026-10-27-batch-default-availability-amendment.zh.md) 第1节中“Anthropic Message Batches”作为可创建种类的表述。两份记录的其余内容不变。用户文档见 [docs/developer/batches.md](../../developer/batches.md)。

## 1 决定

网关只有一个 Batch API：`/v1/batches`。直接提交的 `requests` 与 JSONL 文件的 `input_file_id` 是同一资源的两种输入方式，不是两个API。目标（target）是批次的一个属性：`/v1/chat/completions`、`/v1/embeddings`、`/v1/responses`，以及新增的 `/v1/messages`。一个批次只有一个目标。

- 移除 `/v1/messages/batches` 与 `/anthropic/v1/messages/batches` 的全部操作（创建、列表、读取、取消、删除、结果），不保留重定向或兼容路由。原先存在的12个“方法+路径”组合各登记一个只返回 `410 Gone` 的提示路由（`serving/servers/routers/retired_message_batches.py`）：Anthropic 错误形状、`error.type` 为 `invalid_request_error`，说明改用 `/v1/batches` 的 Messages 目标、旧 `msgbatch_` id 在 `/v1/batches` 管理，并给出迁移文档链接。它不读请求体、不鉴权、不查任何批次、不创建任何工作，对任何调用者、任何 id 都是同一段静态文字，因此不泄露信息；它不经过 `X-On-Behalf-Of` 检查。批处理可用性开关照旧优先：没有 batch store 或 `BATCH_ENABLED` 关闭时返回404。其他路径与方法仍是普通的404/405。这些路由不在 OpenAPI 中。410 只是更清楚的错误，不是兼容期或宽限期：旧客户端照样失败，也没有预定的移除时间或配置。在线 `/v1/messages` 与 `/anthropic/v1/messages` 不变。
- 官方 Anthropic SDK 的 `messages.batches` 不再可用。OpenAI SDK 的 `client.batches` 对 Chat、Embeddings 与 Responses 目标保持兼容；Messages 目标是本网关的扩展。
- 不新增上游原生 Batch 执行，也没有折扣价。

## 2 Messages 目标

- 内联：`requests[i].url = "/v1/messages"`，`body` 是 Anthropic Messages 请求体。文件：每行同样形状，批次 `endpoint = "/v1/messages"`。
- 版本只有一个来源：创建请求的 `anthropic-version` 请求头（内联与文件相同，去掉首尾空白，空白值视为没有）。Messages 批次必须提供，且必须是已知版本，否则在判断幂等重放之前就返回400；`BATCH_ANTHROPIC_VERSIONS` 只限制新建，不限制重放。非 Messages 批次带这个请求头返回400。请求体顶层的 `anthropic_version` 字段一律400（即使为 null、即使请求头有效）：信封模型忽略未知字段，不显式拒绝就会被静默丢弃。`anthropic-beta` 在创建时返回400，属于独立的能力限制。读取、列表、取消、删除与下载都不需要版本请求头。
- 版本冻结在已有的 `protocol_profile` 列，批次对象以 `anthropic_version` 字段返回它；dispatch 照旧读取它，原生 Anthropic adapter 只对批处理请求发送该版本（`req_ctx.ANTHROPIC_VERSION`），在线请求仍发送固定的默认版本，不随客户端的请求头变化。准入、文件校验与执行使用同一套 profile 规则（`serving.batch.profiles`），能力白名单、可翻译子集、执行时的 adapter 过滤都不变；admission 不把 Messages 转成 Chat。
- 准入语义统一为 `/v1/batches` 已有的严格语义：内联批次中任何一项不合格整批400并指明是哪一项；文件批次整份校验通过后才可执行，任何一行不合格整批失败。两者都发生在执行之前，被拒的批次没有任何推理调用。执行开始后，单项的运行时失败（模型拒绝、重试耗尽）仍只影响该项，其他项照常运行。`custom_id` 规则也统一为 `/v1/batches` 的（1–512 字符）。
- 取舍：已移除的 Anthropic 入口曾逐项准入（参数或模型不合格的项成为 errored，其余照常），迁移过来的程序会失去这一行为，需要自行预检或去掉出错的项后重交，文档的迁移一节专门说明了这一点。保持严格是为了让一个 Batch 产品只有一套准入语义，并且不改变 Chat、Embeddings、Responses 以及文件批次已上线的严格行为。逐项准入失败若要提供，应作为单独、显式、跨所有目标的产品能力另行设计：包括文件校验的暂存与计数、准入期失败与运行期失败在结果和计数中的区分，以及滚动升级时旧 worker 的行为。本 PR 不做。
- 幂等摘要在 Messages 批次中以规范化的 `anthropic_version` 键包含请求头里的版本；非 Messages 批次的摘要逐字节不变，因此升级前写下的幂等记录在升级后重放仍得到同一批次。

## 3 存储种类与滚动升级

`api_surface` 继续作为内部元数据，表示输入方式：新批次只写 `openai_inline` 或 `openai_file`（值沿用历史名称，不改库）。新的 Messages 批次是 `(openai_inline, messages)` 与 `(openai_file, messages)`，`schema_version` 仍为2。`(anthropic, messages)` 保留在 `SUPPORTED_JOB_KINDS` 中，只用于排空和读取旧批次，不再有入口创建它。

- #1660 及之后的旧 worker：其种类过滤不含这两个新组合，因此不会领取、校验、过期、收尾、封存或清理新的 Messages 批次；新批次只由本版本的 worker 执行。旧进程仍能读取、取消、删除这些批次（这些操作不按种类过滤，语义与本版本相同）。混合部署期间，由旧进程处理的 Messages 目标创建请求会得到400（旧进程不认识该目标），客户端重试或等待全部进程升级即可；旧进程会忽略非 Messages 批次上的 `anthropic-version` 请求头（它从不读这个请求头）和请求体里的 `anthropic_version` 字段（旧的信封模型忽略未知字段），升级完成后才会一律拒绝；旧进程仍提供 Message Batches 路由，本版本的进程对它们返回410。不需要提升 `schema_version`。
- 支持的滚动升级边界：包含 #1660 种类过滤的版本。这一过滤写在 claim、过期、收尾、校验、封存、清理共用的 SQL 谓词里，是真实的运行时保护。
- 有 batch worker 但早于 #1660 的版本没有种类过滤，会把任何批次（包括新的 Messages 批次）当作 Chat 执行。本版本写入数据库的任何东西、启动时做的任何检查，都改变不了、也看不见一个已在运行的这种进程；新建的版本表或心跳表它永远不会读，“没有看到新心跳”也证明不了没有这种进程。因此不加这类机制。能依靠的只有它自己每轮都读的 `BATCH_ENABLED`，以及停掉全部旧进程：在旧版本上关闭 `BATCH_ENABLED` → 停止全部旧进程并阻止自动重启 → 用编排系统自己的进程身份（镜像、revision、release 或 deployment id）确认不再有旧进程（数据库会话的客户端地址不能证明版本：NAT、共享主机与连接池都会掩盖）→ 等待 gateway 数据库用户没有会话 → 以关闭状态启动新版本 → 确认只剩新版本后再开启。不知道各进程运行的版本时，按这种情况处理。本记录不判断任何外部部署是否运行过这类版本。
- 回滚到本版本之前（仍 ≥ #1660）：那个版本不运行、也不过期 `target='messages' AND api_surface <> 'anthropic'` 的批次（可以取消和删除），所以要由本版本先排空它们。只查一次未封存的此类批次是不够的：本版本仍有进程在运行时，已通过准入的创建仍可能写入新批次。因此沿用 [2026-10-27 修订](2026-10-27-batch-default-availability-amendment.zh.md) 第4节的静默步骤，只把排空查询换成上述条件：限制创建（`BATCH_MIN_ROLE=admin`，管理员自己也不创建）→ 取消或等待完成这些批次直到查询为空 → 关闭 `BATCH_ENABLED` → 停止本版本全部进程并保持停止 → 确认 gateway 数据库用户已无会话 → 再查一次，为空才启动旧版本。用户文档按目标版本给出两种排空查询，共用这一流程。
- 回滚后：仍实现 `BATCH_EXPANSION_ENABLED` 的版本（#1660 至 #1673 之前；#1673 移除了它）会重新读取该开关，默认关闭；#1673 及之后的版本不受它控制。≥ #1660 的版本会重新提供 Message Batches 路由（不再是410），只能看到 `api_surface='anthropic'` 的批次。回滚不改写任何已存的行。

## 4 统一的读取与管理

- `/v1/batches` 的列表、读取、取消、删除、`/results` 与 `/output.jsonl` 覆盖所有种类，包括已存的 `api_surface='anthropic'` 批次（`msgbatch_` 前缀的 id）。属主隔离与管理员规则不变：属主或管理员可读、取消、删除；列表只列调用者自己的批次。
- 批次对象统一为 OpenAI 形状（`status`、`request_counts {total, completed, failed}` 等），新增我们的字段 `anthropic_version`（Messages 批次）。状态投影只按输入方式区分：文件批次照旧显示 `validating`/`finalizing`/`cancelling` 直到封存；内联批次（含旧 Anthropic 批次）显示执行状态。
- 结果行统一为 OpenAI 的输出行（`{"id","custom_id","response":{"status_code","request_id","body"},"error"}`），Messages 的 `body` 是原样的 Messages 响应。Anthropic 结果行渲染器与 `message_batch` 对象被删除；旧批次没有封存的 manifest（只有文件批次有），所以这不改变任何已封存的文件。
- 删除不再要求批次已结束；与其他批次一样，只在有未过期的运行租约时返回409。
- 控制台与管理员页显示一个 Batch 产品：主标签是目标与模型，输入方式为次要信息；去掉 API 下拉框与“OpenAI 内联/OpenAI 文件/Anthropic”标签。管理员状态过滤按显示状态匹配（包括文件批次的 `finalizing`），与列表中显示的状态一致；旧 Anthropic 批次显示真实的终态（completed/cancelled/expired/failed），不再是 `ended`。

## 5 旧批次的例外

以下是无法避免、需要明确告知的差异，均不改写已存的数据：

- 旧 Message Batch 不再能以 `message_batch` 形状读取，结果也以统一的输出行下载（`/v1/batches/{id}/output.jsonl` 或控制台），不再是 Anthropic 的 `{"custom_id","result"}` 行。
- 旧 Message Batch 入口写下的 `Idempotency-Key` 记录（`api_surface='anthropic'`）原样保留至自然过期，但不会被 `/v1/batches` 的请求重放：请求形状不同，摘要不可能相同。
- Webhook：旧 Anthropic 批次（`api_surface='anthropic'`）不论由哪个版本收尾，结束时都写 `message_batch.ended`，即它创建时订阅者约定的事件，事件体只含 id，该 id 在 `/v1/batches` 上可用；通过 `/v1/batches` 新建的批次一律写 `batch.<status>`。已写入 outbox 的事件按原样投递。新旧进程在滚动期间对同一种批次写同一种事件。
- 旧客户端仍调用已移除的操作时得到410（见第1节），不会重复创建批次。
- 旧批次在准入时被拒的项照旧以 failed 项保存并计入 `failed`。

## 6 验证

- 单元与 API 测试：版本请求头规则（缺失、空白、首尾空白、未知、未允许、非 Messages 上提供、body 字段含 null 一律拒绝、`anthropic-beta`、管理操作不需要请求头）、摘要包含版本且非 Messages 摘要不变；12个旧操作在真实应用上返回410（无鉴权、坏 key、`X-On-Behalf-Of` 都一样，且不触碰 store），开关关闭或没有 store 时404，其他路径与方法不是提示路由，OpenAPI 中没有它们；在线 Messages 不变；统一的状态与计数投影；管理员显示状态过滤与 Python 投影一致。
- 真实 Postgres：内联与文件 Messages 批次的执行、版本持久化与 dispatch、文件整份校验、能力拒绝、取消、属主隔离、版本参与幂等、已不允许的已知版本仍可重放而不能新建；store 与封存器实际写入的 outbox 事件：旧批次（完成与取消）为 `message_batch.ended`，新的内联与文件批次为 `batch.completed`，原有待投递事件不变；HTTP 级真实投递的签名事件同样区分新旧批次；旧 `(anthropic, messages)` 批次经 `/v1/batches` 与控制台的列表、读取、结果、取消、删除；以旧版本的种类集合构造谓词，确认它不匹配新的 Messages 种类。
- 本地隔离验收：真实 uvicorn gateway、独立 Postgres 与假上游；另起一个基线版本（dev `df40a9a82`）的 gateway 共用同一数据库，确认它不执行新的 Messages 批次。脚本保留在仓库中以便独立复验。假上游只证明协议与存储行为，不构成真实供应商验收，也不是对任何部署环境的授权。

## 7 评审中的修订

PR 评审后，本记录最初的三项决定被取代，以上各节已是最终设计：

- 旧路由由“直接404”改为“12个旧操作返回410提示”（第1节）。理由：404 看不出原因；410 不读取、不执行任何旧资源，也不是兼容窗口。
- 版本由“信封字段 `anthropic_version`，请求头若出现须一致”改为“只取 `anthropic-version` 请求头，body 字段显式拒绝”（第2节）。理由：同一个值不应有两个来源；请求头与 Anthropic 的约定一致，SDK 用 `extra_headers` 传入。该字段仅在本未合并 PR 中出现过，没有对外发布。
- 旧 Anthropic 批次的 webhook 由“升级后一律 `batch.<status>`”改为“仍为 `message_batch.ended`”（第5节）。理由：只有新批次才应使用新约定，旧批次的订阅者不应因升级而漏收事件。

同时明确保留严格准入并记录其取舍（第2节），以及不能用版本表或心跳隔离 #1660 之前的 worker、只能依靠其 `BATCH_ENABLED` 与停止全部旧进程（第3节）。
