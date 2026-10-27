# Batch 默认可用：设计修订

日期：2026-10-27。状态：已批准并在本分支实现（未合并、未部署）。基线：dev `a94aa9d83`。

本记录取代 [2026-10-26 Batch API 扩展设计](2026-10-26-batch-api-expansion-design.zh.md) 第11节中 `BATCH_EXPANSION_ENABLED` 的决定（新种类默认关闭、需单独开启）以及据此写的升级与回滚步骤。该记录的其余部分不变，仍是当时的历史记录。用户与运维文档见 [docs/developer/batches.md](../../developer/batches.md)。

## 1 决定

Batch 是标准推理功能，不再是需要单独开启的扩展。`BATCH_ENABLED` 开启时，本版本支持的所有种类都可创建：内联与文件批次、Anthropic Message Batches、Chat / Embeddings / Responses 目标，以及 Webhook 设置。

保留不变：

- `BATCH_ENABLED` 仍是整体开关：关闭时所有 Batch、Files、Webhook 路由返回404，worker 不领取、不 sweep。
- `BATCH_MIN_ROLE`（默认 `internal`）、日配额、幂等先于角色与配额、活跃批次与排队请求上限、文件存储与上传频率上限、每角色并发配额（`BATCH_ROLE_CONCURRENCY_<ROLE>`，#1671）全部照旧。

移除：`BATCH_EXPANSION_ENABLED` 的注册项、`BatchConfig.expansion_enabled`，以及五处创建检查（内联非Chat目标、文件批次创建、文件上传、Message Batches 创建、Webhook 保存）。

## 2 遗留取值

旧值不做破坏性清理，也不再起作用：

- 注册表新增 `RETIRED_SETTINGS`（键 → 取代它的规则）。`app_config` 表中遗留的行原样保留：不列在配置页（否则会被当成“管理员添加的 provider 变量”显示），启动时不从环境导入，不能写入（400）。
- 每次启动若在数据库或环境中发现该键，记录一条 WARNING，说明它已不控制任何功能。
- 管理员可主动删除该行（`DELETE /admin/config/BATCH_EXPANSION_ENABLED` 或 `manage reset`），这是唯一删除它的途径。
- 配置备份恢复：备份中的该键被忽略，部署当前已有的行保留。
- 保留的理由：回滚到 #1660 版本时，它会重新读取这个值，保持原来的行为。

## 3 已在运行的旧 worker

新代码无法改变已经在运行的旧进程。

- #1660 及之后的 worker 有种类过滤：claim、`active_models`、过期、收尾、校验、封存、清理共用 `_supported_kinds_sql`，只处理本版本认识的 `(api_surface, target)` 且 `schema_version` 不超过本版本的批次。因此从 #1660 版本滚动升级是安全的，以后新增种类时也同样安全。本修订没有改动这个过滤，只加了真实 Postgres 测试：未知 surface、target 或更高 schema version 的批次不被领取、不出现在模型工作列表、过期后不被过期或收尾、不被校验或封存租约取走。
- #1660 之前的 worker 没有该过滤，会把新种类当作 Chat 执行。原来的开关在混合部署期间挡住了这种情况；开关移除后，改为运维步骤：在旧版本上关闭 `BATCH_ENABLED`（旧 worker 每轮读取它，关闭后不领取也不 sweep）→ 升级所有进程（新进程启动时仍是关闭）→ 确认没有旧进程后再开启。也可以先停掉全部旧进程再启动新版本。
- 不引入版本或心跳协调器：上述过滤已覆盖以后新增的种类，剩下的风险只来自 #1660 之前的版本，这些版本无论加什么新机制都无法感知。

## 4 回滚

- 回滚到 #1660 或之后的版本：直接回滚。该版本重新读取 `BATCH_EXPANSION_ENABLED`（默认关闭），新种类的创建被拒绝，已接受的批次继续执行；保留下来的数据库行会恢复它原来的值。
- 回滚到 #1660 之前的版本：在本版本仍运行时进行：`BATCH_MIN_ROLE=admin` 限制创建（其他用户排队的请求也会因此暂停，需要取消而不是等待；也可以跳过这一步）→ 取消或等待所有非“内联 Chat”的批次，直到 `phase <> 'sealed'` 的此类批次为0 → 关闭 `BATCH_ENABLED` 后再查一次 → 停止全部进程，再启动旧版本 → 只剩旧版本运行时，开启 `BATCH_ENABLED` 并恢复 `BATCH_MIN_ROLE`。只关闭 `BATCH_ENABLED` 不够，因为它也会让新版本 worker 停止，新种类批次就无法排空。具体 SQL 见用户文档。

## 5 验证

- 单元测试：遗留值（缺省、`0`、`false`、`1`）不改变 `BatchConfig`；三个内联目标在任何遗留值下都被接受；`BATCH_ENABLED=0` 时都返回404；角色限制仍然有效；遗留行原样保留、不列出、不导入、不能写、可以删除、恢复备份时保留，并在启动时警告。
- 真实 Postgres：HTTP 集成测试整体在 `BATCH_EXPANSION_ENABLED=0` 下运行（Message Batches、文件批次、Webhook、幂等）；`BATCH_ENABLED` 一个开关控制所有入口；未知种类不被任何路径处理。
- 本地隔离验收：真实 uvicorn gateway、独立数据库、假上游，在不设置该键与将它设为 false 两种情况下验证默认可用。命令与结果记录在PR说明中。这不是真实供应商验收，也不是对任何部署环境的授权。
