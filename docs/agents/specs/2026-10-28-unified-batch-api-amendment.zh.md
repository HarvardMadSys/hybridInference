# 统一 Batch API：设计修订

日期：2026-10-28。状态：已批准并在本分支实现（单个PR，未合并、未部署）。基线：dev `df40a9a82`。

本记录取代 [2026-10-26 Batch API 扩展设计](2026-10-26-batch-api-expansion-design.zh.md) 中“Anthropic Message Batches 作为独立 API 面”的决定：第1节第3项、第2节架构图与模块表中的 `message_batches.py`、第4节的 Anthropic 状态投影（`processing`/`ended`）与“预检失败的项直接存为失败”、第5节“幂等键范围含 api_surface”对 Anthropic 的部分，以及第9节 `message_batch.ended` 事件类型。也取代 [2026-10-27 修订](2026-10-27-batch-default-availability-amendment.zh.md) 第1节中“Anthropic Message Batches”作为可创建种类的表述。两份记录的其余内容不变。用户文档见 [docs/developer/batches.md](../../developer/batches.md)。

## 1 决定

网关只有一个 Batch API：`/v1/batches`。直接提交的 `requests` 与 JSONL 文件的 `input_file_id` 是同一资源的两种输入方式，不是两个API。目标（target）是批次的一个属性：`/v1/chat/completions`、`/v1/embeddings`、`/v1/responses`，以及新增的 `/v1/messages`。一个批次只有一个目标。

- 移除 `/v1/messages/batches` 与 `/anthropic/v1/messages/batches` 的全部路由（创建、列表、读取、取消、删除、结果），不保留重定向或兼容路由；这些路径返回404（`/v1/messages` 前缀下仍是 Anthropic 形状的404），也不出现在 OpenAPI 中。在线 `/v1/messages` 与 `/anthropic/v1/messages` 不变。
- 官方 Anthropic SDK 的 `messages.batches` 不再可用。OpenAI SDK 的 `client.batches` 对 Chat、Embeddings 与 Responses 目标保持兼容；Messages 目标是本网关的扩展。
- 不新增上游原生 Batch 执行，也没有折扣价。

## 2 Messages 目标

- 内联：`requests[i].url = "/v1/messages"`，`body` 是 Anthropic Messages 请求体。文件：每行同样形状，批次 `endpoint = "/v1/messages"`。
- 批次信封新增 `anthropic_version`（内联与文件都在 `POST /v1/batches` 的请求体中）。Messages 批次必须提供，且新建时须在 `BATCH_ANTHROPIC_VERSIONS` 允许的版本中；非 Messages 批次提供它返回400，而不是忽略。`anthropic-beta` 请求头在创建时返回400；`anthropic-version` 请求头若出现，必须与信封中的值一致，否则400（避免请求头里的版本被静默忽略）。
- 版本冻结在已有的 `protocol_profile` 列，dispatch 照旧读取它（原生 Anthropic adapter 发送该版本）。准入、文件校验与执行使用同一套 profile 规则（`serving.batch.profiles`），能力白名单、可翻译子集、执行时的 adapter 过滤都不变；admission 不把 Messages 转成 Chat。
- 准入语义统一为 `/v1/batches` 的：内联批次中任何一项不合格整批400并指明是哪一项（不再有“单项 errored、其余照常”）；文件批次整份校验通过后才可执行，任何一行不合格整批失败、零上游调用。`custom_id` 规则也统一为 `/v1/batches` 的（1–512 字符）。
- 幂等摘要在 Messages 批次中包含 `anthropic_version`；非 Messages 批次的摘要不加这个键，因此升级前写下的幂等记录在升级后重放仍得到同一批次。

## 3 存储种类与滚动升级

`api_surface` 继续作为内部元数据，表示输入方式：新批次只写 `openai_inline` 或 `openai_file`（值沿用历史名称，不改库）。新的 Messages 批次是 `(openai_inline, messages)` 与 `(openai_file, messages)`，`schema_version` 仍为2。`(anthropic, messages)` 保留在 `SUPPORTED_JOB_KINDS` 中，只用于排空和读取旧批次，不再有入口创建它。

- #1660 及之后的旧 worker：其种类过滤不含这两个新组合，因此不会领取、校验、过期、收尾、封存或清理新的 Messages 批次；新批次只由本版本的 worker 执行。旧进程仍能读取、取消、删除这些批次（这些操作不按种类过滤，语义与本版本相同）。混合部署期间，由旧进程处理的 Messages 目标创建请求会得到400（旧进程不认识该目标），客户端重试或等待全部进程升级即可；旧进程会忽略非 Messages 批次信封里的 `anthropic_version`（旧的信封模型忽略未知字段），升级完成后才会一律拒绝。不需要提升 `schema_version`。
- #1660 之前的旧 worker 没有种类过滤，会把任何批次当作 Chat 执行；[2026-10-27 修订](2026-10-27-batch-default-availability-amendment.zh.md) 第3节的运维步骤继续适用。
- 回滚到本版本之前（仍 ≥ #1660）：取消或等待所有 `target='messages' AND api_surface <> 'anthropic'` 且 `phase <> 'sealed'` 的批次再回滚；遗留的此类批次在旧版本上不会运行或过期，但可以取消和删除。回滚后旧的 Message Batches 路由重新出现，只能看到 `api_surface='anthropic'` 的批次。

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
- 已写入 outbox 的 `message_batch.ended` 事件按原样投递。升级后结束的任何批次（包括仍在排空的旧 Anthropic 批次）都写 `batch.<status>` 事件；滚动升级期间由旧进程收尾的旧 Anthropic 批次仍可能发出 `message_batch.ended`。
- 旧批次在准入时被拒的项照旧以 failed 项保存并计入 `failed`。

## 6 验证

- 单元与 API 测试：信封版本规则（缺失、未知、未允许、非 Messages 上提供、请求头冲突、`anthropic-beta`）、摘要包含版本且非 Messages 摘要不变、两条旧路由不在 OpenAPI 且返回404、在线 Messages 不变、统一的状态与计数投影、管理员显示状态过滤与 Python 投影一致。
- 真实 Postgres：内联与文件 Messages 批次的执行、版本持久化与 dispatch、文件整份校验、能力拒绝、取消、属主隔离、版本参与幂等；旧 `(anthropic, messages)` 批次经 `/v1/batches` 与控制台的列表、读取、结果、取消、删除；以旧版本的种类集合构造谓词，确认它不匹配新的 Messages 种类。
- 本地隔离验收：真实 uvicorn gateway、独立 Postgres 与假上游；另起一个基线版本（dev `df40a9a82`）的 gateway 共用同一数据库，确认它不执行新的 Messages 批次。脚本保留在仓库中以便独立复验。假上游只证明协议与存储行为，不构成真实供应商验收，也不是对任何部署环境的授权。
