# Batch API 扩展：实施设计

日期：2026-10-26。状态：已批准并在本分支实现（单个PR，未合并、未部署）。基线：dev `fcd7a660`。

本记录修订并扩展 [2026-10-25 批处理设计](2026-10-25-batch-inference-design.md)。该记录“13. Deferred”中的 Embeddings/Responses 目标、Files API 与输出文件、完成Webhook、原子的活跃批次上限、按用户而非按批次的公平性、配额用尽时读取自己的批次、跨进程让位、管理员全用户总览和批次token统计，均由本设计实现；批处理折扣与auto模型仍不在范围内。用户文档见 [docs/developer/batches.md](../../developer/batches.md)。

## 1 范围

一个PR内实现以下全部内容（原P0–P4只是实现与验证顺序）：

- 旧的内联 `/v1/batches` 保持兼容，并新增 Embeddings 与独立 Responses 目标。
- OpenAI 文件式 Batch：`purpose=batch` 的 Files API（分块上传、列出、读取、下载、删除）、后台异步校验、虚拟输出/错误文件。
- Anthropic Message Batches（`/v1/messages/batches` 与 `/anthropic/v1/messages/batches`），`anthropic-version` 版本 profile。
- 免日配额的管理鉴权；创建幂等（含删除墓碑）；原子的活跃批次与排队请求上限。
- 每次尝试的记录与费用观察，不重复扣费，未知费用不记为0。
- Webhook：账户级配置、outbox、Standard Webhooks 签名、重试上限、SSRF 防护。
- 控制台用户批次页与 Webhook 设置；管理员全用户总览。
- 先用户后批次的公平领取、跨进程共享在线负载、每用户全部署运行上限与在线保留量。

不在范围：上游原生 Batch 接口与折扣、恰好一次执行、Anthropic workspace、beta 特性、auto 模型。默认创建角色仍为 `internal`，默认数量与大小上限不变（每批50,000条、32MiB）。

## 2 架构与所有权

```text
旧 inline /v1/batches ───────┐
OpenAI /v1/files + /v1/batches┼─ admission（幂等、角色、配额、上限） ─ PG batch_jobs / batch_items
/v1/messages/batches ─────────┘                                         │
                                        worker：claim（共享谓词、公平、上限） + 租约
                                                                        │
                                  重新鉴权创建key → 用户并发槽 → 标记 dispatched
                                                                        │
                                      dispatch：chat / embeddings / messages / responses
                                         （现有handler、路由、fallback、计费、日志）
                                                                        │
                                    尝试回执 → 结果 → 封存（sealing） → JSONL / 虚拟文件 / Webhook
```

| 模块 | 职责 |
|---|---|
| `serving/storage/batch_store.py` | 表结构、claim 谓词与公平领取、尝试、幂等、上限、阶段迁移、封存提交、共享负载、读取租约、清理 |
| `serving/storage/batch_file_store.py` | 上传与分块、配额、软删除、GC |
| `serving/storage/batch_webhook_store.py` | Webhook 配置与 outbox |
| `serving/batch/targets.py` | 四个目标的准入与能力规则，固定 canonical 模型 |
| `serving/batch/profiles.py` | Anthropic 版本规则 |
| `serving/batch/dispatch.py` | 以显式参数调用 handler（`call_handler` 拒绝漏传的 `Depends`/`Header`），解析 `JSONResponse`，汇总费用 |
| `serving/batch/attempts.py` | 计费点观察者（只观察，不扣费） |
| `serving/batch/admission.py` | 入口共享的准入顺序 |
| `serving/batch/validation.py` / `delivery.py` | 文件校验；封存与流式下载 |
| `serving/batch/webhooks.py` | 目标检查、签名、投递 |
| `serving/batch/scheduler.py` | worker 循环与后台工作（校验、封存、Webhook、清理） |
| `servers/routers/batches.py`、`message_batches.py`、`files.py`、`batch_console.py`、`admin/batch.py` | HTTP 面 |

Router/adapter 的路由、fallback、健康状态与费用计算仍归现有路径所有；Batch 只在请求上下文中发布执行约束（Messages 的可用 adapter 过滤、Anthropic 版本），不复制路由生命周期。

## 3 存储

所有迁移只新增表或带常量默认值的列（PostgreSQL 只改目录，不重写不扫描）。旧行得到 `api_surface='openai_inline'`、`target='chat'`、`schema_version=1`、`phase='runnable'`。

- `batch_jobs` 新增：`api_surface`、`target`、`schema_version`、`protocol_profile`、`phase`、`pending_terminal_status`、`validation_error`、`input/output/error_file_id`、`retention_seconds`（创建时冻结）、`results_expires_at`、`seal_generation`、`work_owner/work_expires_at`（校验与封存租约）、`finalizing_at`、`sealed_at`、`cancelled_count`、`expired_count`、token 合计、`unknown_cost_count`、`delete_requested_at`、`idempotency_key`、`claim_seq`、`read_lease_until`。
- `batch_items` 新增：`attempt_seq`（只增不减的fencing令牌）、`usage`（原生usage）、`cost_status`、`line_no`。
- 新表：`batch_attempts`、`batch_idempotency`（不级联job，删除后作墓碑）、`batch_files`/`batch_file_chunks`、`batch_read_leases`、`batch_user_state`、序列 `batch_claim_seq`、`batch_interactive_load`、`batch_webhooks`、`batch_webhook_events`。

## 4 阶段、状态与谓词

`phase` 与执行 `status` 分离：`validating → runnable → sealing → sealed`。claim、`active_models`、sweep（过期、收尾、清理）共用一个谓词：`phase='runnable'`、`status ∈ {validating,in_progress}`、未暂停、未过期、未请求删除，且 `(api_surface, target, schema_version)` 属于本 worker 支持的种类。未知种类既不被领取，也不被过期、收尾或清理。

- 所有项最终时：内联与 Anthropic 批次在同一事务内直接 `sealed`；文件批次冻结 `pending_terminal_status` 进入 `sealing`，由 worker 渲染 manifest 后在一个事务内发布文件与终态（按 `seal_generation` 与租约防护，旧代无法发布）。
- `sealing` 期间过期与取消都不改变结果；封存失败只重试封存，绝不重跑推理。
- 文件批次投影：校验中 `validating`，封存中 `finalizing`（取消则 `cancelling`），封存后才显示终态；旧内联批次的状态展示不变。Anthropic：未 `sealed` 前所有项计为 `processing`，之后 `ended`。
- Anthropic 预检失败的项直接存为失败、不可领取；全部失败的批次在创建事务里结束。

## 5 身份、幂等与上限

`verify_batch_management_identity` 做完整的key与账户检查（撤销、过期、停用、邮箱/手机验证、IP封禁），只跳过日配额，并拒绝 agent grant 与 `X-On-Behalf-Of`；所有 Batch/Files 路由使用它。创建顺序：身份 → 读取请求体 → 幂等（命中直接返回原批次）→ 角色、日配额、功能开关 → 校验 → 用户锁下的创建事务（再查幂等、活跃批次上限、排队请求上限、写入）。

- 幂等键范围 `(owner, api_surface, key)`，摘要含规范化请求、endpoint、metadata、协议版本，不含认证key；同用户其他key可重放，重放不改变执行key。异摘要409；删除后墓碑410；记录保留至批次结束（或删除）后24小时。带幂等键的请求不做提前的上限预检，由事务裁决，因此并发相同请求返回同一批次而非429。
- 文件批次校验时分块写入暂存项，每块在用户锁下预留排队配额，拆成多个批次也无法绕过。

## 6 目标与能力

- 任何字段类型不符（例如 `type` 不是字符串）都作为该项的校验错误返回（Anthropic 为单项 errored，文件批次为带行号的整批失败），不会让整个请求或校验任务异常。
- Chat、Embeddings 与在线接口接受相同内容；执行时模型固定为准入时的 canonical id（Embeddings 也不回退到别名）。
- Messages：固定的参数、内容块与工具白名单；服务端工具、Files 来源、MCP、container、beta 拒绝。OpenAI 兼容 adapter 只承载可翻译子集（含 `supports_tools`、reasoning effort 声明）；执行时只在能原样承载的 adapter 中选择，无可用候选时失败（暂时不可用为503，路由不具备能力为400），并禁用会改变模型的小预算重路由。
- Responses：只接受独立请求（`store` 省略或 false；拒绝 `previous_response_id`、`conversation`、`background`、`stream`、托管工具、非文本输入、非文本工具输出与无法翻译的 `text` 选项）。
- `2026-10-21`：依据官方迁移说明，批处理相关变化均在请求侧（拒绝 `temperature`/`top_p`/`top_k`、assistant 消息中的 document 块、user 文本块上的 citations）。准入按 profile 校验；原生 Anthropic adapter 发送批次保存的版本，其它 adapter 收到两版语义等价的请求；在线 Messages 的默认版本不变。

## 7 费用与尝试

每次 claim 打开一条 `batch_attempts`；dispatch 前标记 `dispatched_at`。三个计费点（Chat/Responses 的 `CostTracker`、Embeddings、Messages）向观察者报告实际计费，批次不再另行扣费。成功但未被定价、超时、500/502/504、dispatch 后丢失租约或停机释放的尝试记为 `unknown`：计入 `unknown_cost_count`，不计入 `cost_usd`；未 dispatch 即释放的为 `not_billed`。回执重放受 `attempt_seq` 防护，不重复累加。Messages 只在有日志存储时计费（既有行为），否则费用为 unknown。

## 8 文件与交付

上传以流式 multipart 解析，整体字节、part 数与字段受限，分块（1MiB）写库并增量计算 sha256，最后一次提交变为 ready；未完成的上传会被回收。输入文件被校验中的批次 pin 住：创建事务以 `FOR KEY SHARE` 锁住上传行并重新确认其未删除、未过期，GC 先以 `FOR UPDATE` 锁行、再用新语句检查 pin 与读取租约，因此删除先赢则创建被拒，创建先赢则字节保留。删除只隐藏，物理字节在无 pin、无读取租约后由 GC 删除，删除前仍计入用户存储配额。

结果文件是虚拟的：封存时用固定 renderer（版本1）扫描不可变的 items，记录字节数、sha256 与行数；下载按 manifest 的版本流式重建并逐块校验，不一致或数据被删时中断而不是返回“完整”的短文件。下载持有读取租约：租约写在资源行上，独立任务续期（最长2小时），清理与删除会等它；用户删除仅隐藏批次，sweep 在租约过期后物理删除。永久删除用户例外，会立即删除并使下载中断。

## 9 Webhook

账户级一个 HTTPS 地址，签名密钥加密存储、仅在创建或轮换时返回一次。事件在批次封存的同一事务写入 outbox，因此晚于结果可读；不含prompt与结果。写入事件时以 `FOR KEY SHARE` 锁住该用户的 Webhook 行，删除 Webhook（或永久删除用户）先删配置行再删事件，因而不会留下无配置的事件；旧版本遗留的这类事件不会被领取、不阻塞他人投递，并由 sweep 标为失败。投递每次领取一个事件，租约长于最慢一次投递，结果按领取者与尝试号防护；最多8次，指数退避。URL 在保存时、每次重定向、每次建立连接时都检查，拒绝私网、回环、链路本地、多播与云元数据地址（`BATCH_WEBHOOK_ALLOW_PRIVATE_TARGETS` 仅供隔离测试，元数据地址始终拒绝）。设置写入时检查账户仍有效且未在删除中。

## 10 调度与在线保护

- 公平：候选先按用户、再按该用户的批次交错排序；每次领取后按“最后服务位置”分配 `claim_seq`，下一轮先服务最近最少被服务的用户，拆批不增加份额。
- 每用户全部署运行上限 `BATCH_USER_MAX_CONCURRENCY`（默认8），在用户领取锁下复核。
- 共享在线负载：每进程每2秒发布本进程在途交互请求；读取时一次快照取其他进程10秒内的新鲜行，加上本进程实时计数。读取失败时本轮不启动新的批处理请求（fail closed），已运行的不受影响；进程死亡后其行在TTL后失效。`BATCH_INTERACTIVE_RESERVE` 在此之外为在线流量预留槽位。
- 原方案中可选的“每日金额预算”经评审不纳入：Batch 用户预算由上述并发上限与保留量体现，金额仍受既有日配额约束。

## 11 开关、升级与回滚

- `BATCH_ENABLED`：整个面与 worker，关闭时所有批处理路由404（既有行为）。
- `BATCH_EXPANSION_ENABLED`（默认关闭）：只控制新种类的创建（文件批次、Message Batches、Embeddings/Responses、Webhook设置）；已接受的任务继续执行，结果管理不受影响。
- 升级：旧 worker 会把新种类当作Chat执行，因此先让所有进程运行本版本，再打开开关。
- 回滚到本版本之前，须在新版本仍运行、仍能管理这些批次时进行：关闭 `BATCH_EXPANSION_ENABLED`，取消（或等待完成）所有新种类批次，确认没有处于排队、执行、校验或封存中的新种类批次，再停止全部 gateway 进程，之后才启动旧版本。只关闭 `BATCH_ENABLED` 不能使旧 worker 安全，它只会隐藏批处理路由。

## 12 验证

单元、真实 Postgres 与 HTTP 级集成测试覆盖上述约束，包括：校验中不可领取；谓词一致；封存不受过期/取消影响且不重跑；同用户换key只管理；幂等先于配额与角色、并发只建一份、墓碑410；原子上限；四目标各计费一次、未知费用不补0；全部预检失败即结束；文件删除/GC/下载竞态；handler 参数不漏传；拆批不增份额；共享负载单快照；Webhook 不访问私网、不随重定向绕过、事件晚于结果；控制台分页与过滤的过期响应。命令与结果记录在PR说明中。

独立验收在本地隔离环境中进行：两个真实 uvicorn gateway 进程、独立 Postgres 与假的 OpenAI/Anthropic 兼容上游，使用官方 OpenAI Python SDK 与 Anthropic Python SDK 2.x 完成文件批次（Chat/Embeddings/Responses）、两个 Anthropic 版本、无效末行整批失败且零上游调用、旧内联批次、配额为0时幂等重放与下载、跨进程删除与下载、取消后已完成结果保留，以及计费回执与日费用一致。上游均为假服务：这证明协议与存储行为，不构成真实供应商验收，也不是对任何部署环境的授权。
