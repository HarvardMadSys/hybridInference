# Agent Operation 可观测性与资源归属设计

**日期：** 2026-09-01

**状态：** 分层设计；Attempt 级 Docker Engine 采样已落地，operation/eBPF 归属尚未实现

**实现仓库：** `freeinference-cloud-agent`；Inference cost truth 位于 `hybridInference`

**相关文档：**

- [Agent 会话连续性与共享沙盒架构](2026-09-01-agent-session-continuity-architecture.zh.md)
- [Agent 沙盒安全模型与能力边界](2026-09-01-agent-sandbox-security-model.zh.md)
- `freeinference-cloud-agent/docs/specs/2026-08-19-observability-and-resource-accounting-design.md`
- `freeinference-cloud-agent/docs/specs/2026-08-31-native-session-fork-shared-workspace-design.md`

## 1. 实现状态

本文首先区分当前实现与后续设计，避免把研究方向写成已交付能力。

| 能力 | 状态 | 当前 truth |
|---|---|---|
| Canonical Agent event ledger | 已实现 | `agent_job_events`，按 `(attempt_id, client_seq)` 幂等 |
| Attempt 资源采样 | 已实现 | Host 通过 Docker Engine stats 采样，写 `agent_attempt_resources` |
| Measurement coverage | 已实现 | `coverage` + nullable counter；不可读不伪造为零 |
| Gateway token/cost attribution | 已实现 | Gateway `api_logs.agent_job_id` 与 grant usage contract |
| Shared sandbox per-member CPU/memory/I/O | 未实现 | 明确 `coverage=0`，不能复制 container total |
| Per-tool resource window | 设计中 | Runtime event/hook boundary 与 overlap 规则已定义 |
| Egress flow parsing 与 unclassified byte ledger | 设计中 | Proxy log 存在，完整 projection 尚未落地 |
| General operation ledger | 本文提案 | 需要统一 turn/tool/terminal/file/Git/Fork/snapshot identity |
| eBPF process/operation sensor | 本文提案 | 当前代码中没有 eBPF collector 或 BPF program |
| Admin aggregate、alert checker、OTLP export | 延后 | 不属于当前 per-chat 首发范围 |

因此，当前可以回答“某个 attempt 的 container-level 资源大约是多少”；还不能普遍回答“共享容器内某个 operation 的每一段 CPU、I/O 与网络分别属于谁”。

## 2. 问题定义

传统 HTTP trace 常假设一个 request 包含一棵短生命周期调用树。Agent workload 不满足这个假设：

```text
HTTP request
  -> create Job
      -> Attempt 1
          -> long-lived resident Agent CLI
              -> model calls
              -> tool A
                  -> shell
                      -> compiler
                          -> workers
              -> tool B (may overlap)
              -> detached child
      -> Attempt 2 after retry
```

另外：

- HTTP response 可以在 Agent 完成前很久返回；
- 同一个 resident process 可以服务多个 turn；
- PID 会退出和复用；
- 子进程可以 `setsid`、double-fork 或把工作延后；
- Fork sibling 可以在同一个 container/workspace 中并发；
- terminal、file/Git、snapshot 并不一定属于 Agent turn；
- container memory/page cache、network socket 与 Git mutation 可能是 group-scoped。

**核心归属问题：** 对一项被平台承认的 operation，哪些资源和副作用可以精确归属，哪些只能按窗口估计，哪些只能留在 sandbox group 总量中？

## 3. 目标与非目标

### 3.1 目标

1. 每个 Attempt 可查询“发生了什么、消耗了什么、平台做了什么”。
2. Shared sandbox 中不把同一 container counter 重复记到多个 chat/job。
3. 资源值同时携带 measurement coverage 与 attribution quality。
4. Operation identity 在 HTTP、process restart 与 PID reuse 之外保持稳定。
5. Sensor 故障不影响 turn 正确性，但必须显式产生 gap。
6. Kernel/process telemetry 不复制 prompt、tool result 或 credential。
7. Gateway inference ledger、Agent event ledger 与 resource telemetry 各自保持单一 truth。
8. eBPF 只作为增强 sensor；无 eBPF 时系统仍能以较低精度工作。

### 3.2 非目标

- v1 不建设 distributed tracing system；
- 不把 eBPF 作为 sandbox isolation 或 authorization mechanism；
- 不声称精确计量共享 page cache、GPU 或 runtime 内部线程到单个 tool；
- 不用 Agent 自报 CPU/网络作为 authority；
- 不把 resource telemetry 直接变成计费依据；
- 不部署第二套 Prometheus/Grafana estate；
- 不捕获文件内容、prompt 或完整 command environment；
- 不保证任意内核/runtime 上都有同等 eBPF capability。

## 4. Identity 模型

| 层级 | 定义 | 能否重试/复用 |
|---|---|---|
| Thread | 用户长期会话 | 跨多个 job |
| Job / Turn | 一条用户输入 | 可有多个 attempt |
| Attempt | 一次 lease-fenced execution | retry 后新 id |
| Activation | Prompt admission generation | resident process 可跨 activation 复用 |
| Operation | 一个可独立开始、结束、取消和归属的动作 | 有稳定 `operation_id` |
| Process instance | Kernel task/process | PID 可复用，不是业务 identity |

Operation 至少包括：

- `agent_turn`；
- `tool_call`；
- `terminal_session` / `terminal_command`；
- `file_read` / `file_write`；
- `git_read` / `git_mutation`；
- `native_fork`；
- `workspace_snapshot`；
- `setup`；
- trusted `publish`（在 sandbox 外观测）。

Operation 可嵌套：

```text
agent_turn op_turn
  ├── model_call op_model_1
  ├── tool_call op_tool_a
  │     └── process subtree
  └── tool_call op_tool_b
        └── process subtree
```

`operation_id` 由 control plane、Supervisor 或 workspace broker 生成，不能由不可信 repository 自行声明。

## 5. 三类事实

### 5.1 Ledger：发生了什么

Ledger 是 canonical event stream 的 projection：

- user message；
- assistant output；
- tool use/result；
- usage；
- terminal state；
- continuity/context transition。

Ledger record 携带 `(attempt_id, client_seq)` 作为 dedupe identity。Telemetry export 可以丢失或重复，canonical store 不因 export backend 改变。

### 5.2 Resources：消耗了什么

Resource 是 monotonic counter 或 window rollup：

- CPU time；
- memory current/peak/average；
- PID count；
- block/file I/O；
- network ingress/egress；
- retained writable-layer storage；
- running-idle 与 stopped duration。

Resource record 必须说明测量 scope、窗口、coverage、sampler version 和 attribution quality。

### 5.3 Ops：平台做了什么

Ops signal 没有 canonical transcript 对应项，例如：

- claim loop lag；
- lease takeover；
- native resume/Fork reconciliation；
- sampler start/stop/gap；
- container OOM/exit；
- denied egress；
- snapshot backlog；
- grant mint/renew/revoke degradation；
- slot cleanup ambiguity。

Ops 用于告警与诊断，不能与可求和 ledger event 混为一类。

## 6. 当前采样架构

```text
sandbox container                  runner/host                    control plane
─────────────────                 ───────────                    ─────────────
Agent CLI + children              ResourceSampler               worker report API
                                  1 Hz                           │
Docker writable layer      <── Docker Engine stats API ────────┤
                                                                  ▼
event stream  ───────────────────────────────────────────> agent_job_events
attempt window ── resource rollup ───────────────────────> agent_attempt_resources

gateway model calls ─────────────────────────────────────> gateway api_logs
                                                          joined by external job id
```

### 6.1 为什么通过 Docker daemon 采样

Runner 自身运行在 container 中，没有 host PID namespace、host `/proc` 或 cgroupfs。直接从 runner 解析 host PID/cgroup 会在真实部署中系统性失败。

当前 sampler 使用：

```text
GET /containers/{id}/stats?stream=false&one-shot=true
```

优点：

- Runner 已有 Docker API authority；
- 不要求额外 `pid: host`、cgroup namespace 或 mount；
- 本地 host、containerized runner 与远端 Docker daemon 语义一致；
- Sandbox 无法看到、拒绝或伪造采样。

### 6.2 当前 counter 语义

- CPU 使用 daemon cumulative counter 做窗口 delta；
- Memory 与 Docker CLI 对齐，排除 `inactive_file`；
- I/O row 不可用时记为 unmeasured/null，不记 0；
- Sampler window 使用 host monotonic time；
- 一个成功 reading 贡献 measurement coverage；heartbeat 本身不算 sample；
- sampler failure 不终止 turn。

### 6.3 当前限制

Docker stats 的最小 scope 是 container。一个 shared sandbox 同时运行 A/B 时：

```text
container total != member A total + member B total known exactly
```

现有实现的正确行为是：

- group/container metrics 可以继续记录；
- per-member attempt resource 返回 nullable counters + `coverage=0`；
- 禁止把同一 group total 写到 A 与 B 两行；
- runtime-reported token usage 仍可按 activation event 归属；
- Gateway inference cost 仍以 thread/job attribution 记录。

## 7. Coverage 与归属质量

单一 `coverage` 不足以同时表达“有没有测到”和“测到后能否归属”。目标 schema 分开两类质量：

| 字段 | 含义 |
|---|---|
| `measurement_coverage` | operation window 中有有效 sensor sample 的时间占比 |
| `attribution_coverage` | 已测总量中能绑定到该 operation/member 的比例 |
| `boundary_quality` | `exact_process` / `exact_runtime` / `message_window` / `member_window` / `group_only` / `none` |
| `sampler_version` | 计数语义版本 |
| `missing_reason` | bounded enum，例如 `sensor_down`、`shared_scope`、`runtime_no_boundary` |

示例：

- `measurement=1, attribution=0, group_only`：container 全程测到，但共享并发时无法分给 member；
- `measurement=.7, attribution=1, exact_process`：该 process tree 可精确归属，但 sensor 中途缺失；
- `measurement=1, attribution=.4, message_window`：资源总量完整，只有部分落入近似 tool window；
- `measurement=0`：没有测量，所有 counter 为 null，而不是 0。

## 8. Operation ledger 提案

在引入更细 sensor 前，先建立稳定业务边界。建议增加 canonical operation record：

```text
agent_operations
  operation_id primary key
  operation_kind
  parent_operation_id nullable
  sandbox_id
  thread_id nullable
  job_id nullable
  attempt_id nullable
  membership_generation nullable
  activation_generation nullable
  lifecycle_generation
  state: preparing | active | settled | cancelled | failed | abandoned
  started_at / ended_at
  start_mono / end_mono (host receipt)
  boundary_source
  error_class nullable
```

规则：

1. Control plane 创建 intent；Host receipt 提供真实 local monotonic boundary。
2. `operation_id` 随 retry 保持或明确创建 successor，不能复用旧 generation。
3. Process telemetry 只引用 operation key，不携带完整业务 payload。
4. Operation state 不能由 eBPF event 单独推进；eBPF 是 observation，不是 lifecycle authority。
5. Unknown/background work 可以绑定 sandbox/member，但 `operation_id=null`，进入 unclassified bucket。
6. Fork、snapshot 等已有 operation id 与 saga id 应复用，不再创建平行 identity。

## 9. 分层测量方案

### Layer 0：Group/container totals（已实现）

数据源：Docker Engine stats、container inspect/size、lifecycle timestamps。

回答：

- sandbox group 总 CPU/memory/I/O；
- running-idle 与 stopped duration；
- retained writable-layer storage；
- container OOM/exit。

不能回答：共享 group 内 member/operation split。

### Layer 1：Runtime/event windows（部分已有，资源关联待实现）

数据源：Agent CLI event、tool_use/tool_result、passive runtime hooks。

Boundary capability：

| Capability | 边界 |
|---|---|
| `exact` | Runtime/tool hook 明确发出 execution start/end |
| `message` | 从 tool_use arrival 到 matching tool_result arrival |
| `none` | 无可靠 tool boundary，只保留 turn/member/group total |

`message` 包含 transport 和模型等待时间，必须显示为 approximate。

### Layer 2：Member/process-tree sampler（Shared mode 的最低补全）

数据源可组合：

- member UID process census；
- `/proc/<pid>/stat` + start time；
- taskstats/process accounting；
- runtime wrapper 的 root/child receipt；
- cgroup 或 pidfd-backed host collector（部署允许时）。

它首先解决 per-member，而不是 per-tool。Memory/page cache 和跨进程 FD 仍可能只能 group-scoped。

### Layer 3：eBPF operation sensor（提案）

eBPF 用于补足 process lineage、exec/exit、network connect、OOM 与部分 I/O 事实。它不替换上面三层，也不自动解决业务边界。

## 10. eBPF 架构提案

### 10.1 部署位置

```text
host kernel
  └── BPF programs/maps/ring buffer
           ▲
           │ read-only telemetry stream
  host OperationSensor daemon
           ▲
           │ narrow registration protocol
  Supervisor / Workspace Broker
```

Sensor 运行在 host trusted domain，不在 sandbox 内。建议独立 daemon，而不是给 runner container 增加广泛 host namespace/capability：

- Sensor 持有运行 BPF 所需的 `CAP_BPF`、`CAP_PERFMON` 或该内核要求的最小权限；
- Runner/Supervisor 只通过受限 Unix socket 注册 operation root 与 generation；
- Sensor 不接受任意 BPF program、path 或 filter；
- Sandbox network/filesystem 不可达 sensor socket；
- Sensor 不需要 model、MCP、source-control credential。

### 10.2 业务 identity 注入

eBPF 无法从 PID 自己推断 `thread_id`。绑定协议为：

1. Control plane/Supervisor 创建 `operation_id` 与 generation；
2. Supervisor 启动可信 wrapper 或得到 runtime child receipt；
3. 使用 pidfd + process start time 验证 root process 未退出/复用；
4. 向 sensor 注册短整型 `operation_key`、sandbox key、member slot 与 generation；
5. Sensor 将 key 写入 task-local map；
6. fork/clone 时 BPF 继承 parent task context；
7. exec/exit event 与 resource delta 携带短 key；
8. Userspace collector 再把短 key join 回数据库业务 id。

BPF map 不保存完整 UUID、repository path、prompt 或用户 email。

优先使用 `bpf_task_storage` 绑定 task instance；不支持时，key 至少包含 `(tgid, task start_boottime)`，不能只用 PID/TGID。

### 10.3 Process lineage

最低事件集合：

- `sched_process_fork` / clone；
- `sched_process_exec`；
- `sched_process_exit`；
- OOM kill tracepoint；
- 可选 signal/ptrace denial ops record。

用途：

- 继承 operation context；
- 识别 `setsid`/double-fork 后仍属于原 operation 的 descendants；
- 记录 command basename、executable identity 与 exit status；
- 对比 Supervisor census，发现 untracked writer/process；
- PID reuse 时按 task instance 区分。

Command argv 与 environment 默认不采集。

### 10.4 CPU

可选实现：

- 在 `sched_switch` 上累积 operation/member task runtime；或
- 在 process exit/periodic taskstats 中读取 CPU counter；
- 与 Docker group total 做 conservation check。

高频 `sched_switch` program 必须做 host-level overhead benchmark。若开销或内核兼容性不满足门禁，退回 periodic process sampler，不影响 turn。

### 10.5 Memory

eBPF 不应宣称能低成本、精确地把 shared address space、page cache 与 allocator memory 分给 tool operation。

目标分层：

- container memory：Docker stats，group truth；
- member process RSS/PSS：periodic process-tree sample，近似；
- operation peak：只对独立 process subtree 给出 `process_rss_peak`；
- resident Agent CLI 自身 memory：member-scoped，不硬分到重叠 tool；
- page cache/shared memory：group-only 或 unclassified。

禁止为了“完整”而让 operation memory 总和超过真实 container peak 并仍标记 exact。

### 10.6 File 与 block I/O

建议分两种信号：

- **Resource counter：** 按 process tree 的 read/write bytes；
- **Security/audit event：** open/write/rename/unlink 的 bounded metadata。

全量 VFS path event 成本和敏感性高，默认只启用：

- workspace root 内的 mutation class；
- credential/tmpfs boundary 的 denial；
- snapshot census 期间的未知 writer；
- executable load/exec identity。

不采集文件内容。Path 在 userspace 规范化、相对 workspace root 存储，并应用长度限制与 redaction。Async I/O、shared FD 与 kernel writeback 无法总是精确归属时进入 `shared` 或 `unclassified`。

### 10.7 Network

网络有三种已有 authority：

- Gateway model bytes/cost：Gateway ledger；
- MCP request：MCP relay 与 attempt token；
- Generic setup egress：allowlist proxy log。

eBPF 用于 host-side residual 与 process association：

- `cgroup/connect4`、`connect6` 记录连接目的地 class；
- socket/process context 关联 operation/member；
- cgroup skb/sock accounting 统计 byte delta；
- DNS/name 与 allow/deny truth 仍来自 relay/proxy，不从 IP 反推 authorization；
- socket handoff、long-lived connection 或未知 owner 记为 shared/unclassified。

Byte conservation：

```text
group_net_total
  >= gateway_bytes + mcp_bytes + proxy_egress_bytes + attributed_other

unclassified = group_net_total - classified_total
```

由于不同层计数口径可能包含 framing/retransmission，比较需要定义 tolerance；出现负 residual 时记录 accounting mismatch，不能 clamp 后假装一致。

### 10.8 Long-lived resident process 的特殊情况

Resident Agent CLI 跨多个 turn 存活，不能永久绑定到一个 operation。

采用混合策略：

- Process 固定绑定 member/residency identity；
- Activation window 绑定当前 `agent_turn operation_id`；
- Resident root 在窗口内的 CPU/network 归到 turn/member，quality=`member_window`；
- Runtime hook 能返回具体 tool child root 时，把 child subtree 精确绑定 `tool_call operation_id`；
- 没有 child root 时，tool 只使用 event window attribution；
- Turn 后仍存活的未授权 child 被 Supervisor reaper 清除；
- 明确允许的 terminal/dev server 使用独立 operation identity，而不是继承旧 turn。

这避免了把数小时的 idle resident process 消耗错误记到最后一个 prompt。

## 11. Window 与 overlap 规则

Sampler interval 可能同时覆盖多个 operation。归属规则：

- 一个 exact process owner：记入 `exclusive_*`；
- 多个 message window 重叠：每个记录同一 shared delta，并携带 `overlap_n`；
- shared delta 不能参与求和；
- 没有 operation boundary：留在 attempt/member/group residual；
- Agent CLI/model streaming 本身的资源归到 turn，不强塞给最近 tool；
- Fork/snapshot 等平台 operation 与 Agent turn 并发时，按 process owner 或 group operation ref 分开；无法区分则 shared。

展示层必须使用以下词义：

| 展示 | 含义 |
|---|---|
| exact | Process/runtime boundary 直接证明 |
| approximate | Message/window correlation |
| shared upper bound | 与其他 operation 重叠，不可求和 |
| group only | 只知道 sandbox 总量 |
| incomplete | measurement coverage < 1 |
| unavailable | counter 为 null，不是 0 |

## 12. 数据模型提案

### 12.1 Attempt rollup（当前表扩展）

```text
agent_attempt_resources
  attempt_id primary key
  cpu_usec nullable
  mem_peak_bytes nullable
  mem_avg_bytes nullable
  io_read_bytes nullable
  io_write_bytes nullable
  net_rx_bytes nullable
  net_tx_bytes nullable
  sample_count
  measurement_coverage
  attribution_coverage
  boundary_quality
  sampler_version
  missing_reason nullable
  created_at
```

### 12.2 Operation rollup

```text
agent_operation_resources
  operation_id primary key
  resource_scope: operation | member | group
  cpu_usec_exclusive nullable
  cpu_usec_shared nullable
  process_rss_peak_bytes nullable
  io_read_bytes_exclusive nullable
  io_write_bytes_exclusive nullable
  net_bytes_classified nullable
  overlap_n_max
  measurement_coverage
  attribution_coverage
  boundary_quality
  sensor_version
  truncated
```

### 12.3 Process projection

```text
agent_operation_processes
  operation_id
  process_instance_key
  parent_process_instance_key nullable
  member_slot
  executable_class
  exec_count
  exit_class
  first_seen_at / last_seen_at
  cpu_usec nullable
  io_bytes nullable
```

不建议永久存储每次 `sched_switch` 或每个 syscall。Kernel event 在 host collector 中 roll up，只有 bounded lifecycle/security event 和窗口结果写入 Postgres。

### 12.4 Egress flow

```text
agent_egress_flows
  sandbox_id
  thread_id nullable
  attempt_id nullable
  operation_id nullable
  started_at / ended_at
  destination_class
  target_host_redacted
  verdict
  bytes_in nullable
  bytes_out nullable
  attribution_quality
```

## 13. Gateway cost 与 resource telemetry 的关系

Gateway `api_logs` 是 inference token/cost 的唯一 billing authority。Agent resource telemetry：

- 可以显示 CPU、memory、I/O、network 与 runtime-reported usage；
- 可以通过 `external_job_id` join Gateway cost projection；
- 不能覆盖 Gateway token/cost；
- 不能用 eBPF network bytes 推导模型费用；
- 不能把跨 turn 复用 credential 的 Gateway usage 伪拆成不存在的 per-tool cost；
- 如未来用于配额或计费，需要独立精度、争议处理和审计设计。

## 14. Cardinality、隐私与保留

### 14.1 Bounded dimensions

Metrics/aggregate label 只允许 bounded enum：

- host class；
- runtime；
- context/continuity state；
- operation kind；
- terminal/error class；
- egress verdict；
- boundary quality；
- known tool name 或 `other`。

`thread_id`、`attempt_id`、`operation_id`、domain、path 与 PID 不作为 metrics label；它们只进入 indexed row 或 exemplar。

### 14.2 Redaction

共享 telemetry sink 默认 deny body：

- 不导出 prompt、assistant text、tool args/results；
- 不导出 argv、environment、credential、file content；
- executable/path/domain 经过 allowlist、分类或 hash/redaction；
- redaction failure 按 record fail closed，不影响 canonical DB；
- event size/type/severity/identity 可导出。

### 14.3 Retention

建议：

- Raw 1 Hz / kernel event：host ring buffer，窗口关闭后丢弃；
- Process lifecycle/security event：短期保留；
- Attempt/operation rollup：长期保留；
- Proxy raw log：短期保留，解析后删除；
- Gateway billing ledger：按 Gateway retention policy；
- 研究数据集导出必须再做去标识化与访问控制。

## 15. Failure 与降级矩阵

| 情况 | Turn 行为 | Telemetry 行为 |
|---|---|---|
| Docker sampler 中途失败 | 不影响 turn | `measurement_coverage < 1`，counter 按已测部分或 null |
| Shared mode 无 member sampler | 不影响 turn | per-member `coverage=0`；group total 保留 |
| eBPF 不可加载/BTF 不兼容 | 不影响 turn | Layer 0/1 fallback，ops event，quality 降级 |
| Sensor daemon restart | 不影响 turn | map/sequence gap；不得猜补历史 |
| Operation root 注册晚到 | 不影响 turn | 注册前数据 member/group-only，attribution coverage 降低 |
| PID reuse | 不影响 turn | start-time/task-storage identity 防止串账；无法证明则 unclassified |
| Hook 缺失 | Tool 正常执行 | `message_window` 或 `none` |
| Hook 写失败/超时 | Tool 必须继续 | exact coverage 缺口；hook 不能成为执行依赖 |
| Parallel tool calls | 正常 | shared fields + `overlap_n`，不记 exclusive |
| Egress log parse gap | 正常 | unclassified residual 增大并产生 ops signal |
| Sub-second operation 无 sample | 正常 | sample_count 0、coverage 0，不伪造零资源 |
| Container OOM | Group failure | OOM event + 最后可用 sample；所有 member 受影响 |
| Collector queue 满 | 正常 | 丢低优先级 raw event、保留 gap counter，不 block sandbox |
| Postgres report 重试 | 正常 | `(operation_id, kind/version)` 幂等 upsert |

## 16. 安全约束

eBPF/host sensor 增加 TCB，必须满足：

1. 不把 BPF capability、host `/proc`、cgroupfs 或 sensor socket交给 sandbox。
2. Runner 不可上传任意 BPF program 或修改 filter code。
3. Registration 验证 sandbox/container/member/generation 与 process start identity。
4. Stale Supervisor 不能给新 process generation 绑定旧 operation。
5. Map key 使用短内部 id，ring buffer 不含 secret/body。
6. Sensor failure 不放宽 network、credential 或 process isolation。
7. File/network observation 不被解释为 authorization；authorization 仍由 relay/broker/policy enforcement。
8. Kernel compatibility 与 program verifier failure 必须可见且可安全 fallback。
9. Overhead 有 host-level budget，超限自动停用高频 probe 并报告 degradation。
10. BPF program、loader 与 CO-RE artifact 纳入供应链签名、版本 pin 和升级回滚。

## 17. Product 与运维 surface

### 17.1 Thread UI

每个 settled turn 可以显示：

- context status；
- duration；
- Gateway inference cost；
- CPU、peak memory、network；
- blocked connection count；
- measurement/attribution badge。

展开后：

- operation/tool timeline；
- per-tool duration/resource，标明 exact/approximate/shared；
- process tree 摘要；
- network destination class 与 denied event；
- group-only/unclassified residual；
- sampler gap 与 missing reason。

Shared workspace 下，container group total 与 member/turn total必须分开展示。

### 17.2 Ops/Admin（延后）

未来 aggregate surface 可包括：

- claim/resume/fork failure；
- live/restored/lost funnel；
- sampler/attribution coverage；
- container capacity 与 stopped disk；
- denied egress 与 unclassified growth；
- OOM、unknown writer、cleanup ambiguity；
- operation latency 与 resource outlier。

这些 projection 读取 Postgres，不要求先部署 Prometheus/Grafana。OTLP 只作为可插拔 export seam。

## 18. 分阶段实现

### OBS-0：Contract 与基线

- 固化当前 Docker Engine counter 语义；
- 区分 nullable counter 与 measured zero；
- 将 `coverage` 拆为 measurement/attribution quality；
- 添加 shared mode 不重复 container total 的测试；
- 定义 bounded dimension/redaction allowlist。

### OBS-1：Operation ledger

- 统一 turn/tool/terminal/file/Git/Fork/snapshot operation id；
- Control plane intent + host monotonic receipt；
- Workspace broker/Supervisor generation fence；
- UI/API 暴露 boundary source 与 missing reason。

### OBS-2：Member process accounting

- UID + PID/start-time census；
- member CPU/RSS/I/O periodic sampler；
- background/unclassified process detection；
- 与 Docker group total conservation；
- shared-mode per-member coverage 从 0 提升。

### OBS-3：Runtime/tool boundary

- 支持 runtime-native exact event 的 adapter；
- passive hook；
- message window fallback；
- overlap/shared rollup；
- per-tool rows 与 UI。

### OBS-4：eBPF proof of concept

- 独立 host sensor；
- process lineage/exec/exit/OOM；
- operation registration 与 task identity；
- CPU/network 最小 counter；
- overhead、kernel matrix、安全 review；
- fallback 与 gap tests。

### OBS-5：eBPF production gate

- 真实 host 与隔离 runtime 验证；
- rolling upgrade/map compatibility；
- collector crash/backpressure；
- file/network privacy；
- shared resident process 与 simultaneous operation stress；
- signed artifact 与 rollback。

### OBS-6：Egress 与 ops surface

- Proxy flow parser；
- Gateway/MCP/egress/unclassified byte ledger；
- per-chat network panel；
- 后续 admin aggregate/alerts。

## 19. 验收标准

1. 一个未测量的 turn 返回 null counters + coverage 0，不返回全 0。
2. Shared A/B 并发时，container total 不会复制到两个 attempt。
3. Serial CPU tool 的 exact subtree 可解释大部分 operation CPU，并与 group delta 在 tolerance 内。
4. Parallel tool 的 shared delta 永不进入 exclusive sum。
5. PID reuse、`setsid` 与 double-fork 不导致跨 operation 串账。
6. Resident process 跨两个 turn 时，idle interval 不归到上一个 turn。
7. Terminal/dev server 使用自己的 operation id，不继承过期 activation。
8. Sensor/collector 被 kill 后 turn 继续，coverage/gap 明确。
9. eBPF 不可用的 host 自动降级，且不会 advertise 不存在的精度 capability。
10. File/network telemetry 不包含 prompt、credential、argv/environment 或 file content。
11. Gateway inference cost 与 Agent resource row join 后仍各自可追溯到原 authority。
12. Byte/resource conservation mismatch 可见，不通过 clamp 或 duplication 隐藏。
13. BPF overhead 在目标并发下低于约定 budget；超过时可独立关闭高频 probe。
14. Stale generation 的 process registration 被拒绝。
15. Raw kernel event 不无限写 Postgres，rollup 与 retention 上限生效。

## 20. 实现导航

### 当前已落地

| 能力 | 路径 |
|---|---|
| Docker Engine sampler | `freeinference-cloud-agent/cloud_agent_host/resources.py` |
| Runner window/report | `freeinference-cloud-agent/cloud_agent_host/runner.py` |
| Attempt resource schema/API | `freeinference-cloud-agent/cloud_agent/schemas.py`、`cloud_agent/api/routes.py` |
| Resource store | `freeinference-cloud-agent/cloud_agent/storage/store.py` |
| Canonical Agent event | `freeinference-cloud-agent/cloud_agent/storage/store.py` |
| Shared Supervisor/process census | `freeinference-cloud-agent/cloud_agent_host/shared_session_supervisor.py` |
| Gateway job cost attribution | `apps/backend/serving/storage/log_schema.py`、`apps/backend/serving/servers/routers/agent_grants.py` |

### 需要新增

- `agent_operations` 与 operation resource schema；
- Supervisor/Workspace Broker operation registration protocol；
- Member process sampler；
- Runtime boundary adapter 与 hook journal；
- Host OperationSensor daemon、BPF programs、loader 与 map schema；
- Egress flow parser 与 byte conservation projection；
- Coverage/quality-aware API 与 UI。

在这些组件落地并通过验收前，对外只能声明 Attempt/container-level 资源观测，不能声明 eBPF operation 级精确归属。
