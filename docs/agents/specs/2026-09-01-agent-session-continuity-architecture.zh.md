# Agent 会话连续性与共享沙盒架构

**日期：** 2026-09-01

**状态：** 已实现预览 v6，按 runtime、镜像与主机能力门禁

**实现仓库：** `freeinference-cloud-agent`

**文档级别：** 开发者架构说明；不替代实现仓库中的版本化设计规范

**规范源：**

- `docs/specs/2026-08-28-one-chat-one-restartable-sandbox-design.md`：v5 retained sandbox 基线；
- `docs/specs/2026-08-31-native-session-fork-shared-workspace-design.md`：v6 shared-live 当前规范；
- `docs/specs/2026-08-19-observability-and-resource-accounting-design.md`：连续性与资源状态的观测契约。

**相关说明：**

- [Agent 沙盒安全模型与能力边界](2026-09-01-agent-sandbox-security-model.zh.md)
- [FreeInference / HybridInference 系统、发行版与服务边界](2026-09-01-freeinference-system-boundaries.zh.md)
- [Agent Operation 可观测性与资源归属设计](2026-09-01-agent-operation-observability-design.zh.md)

## 1. 目的与范围

本文定义平台在以下操作中承诺保持什么状态：

- 同一 chat 的连续 follow-up；
- 空闲超时后的 `docker stop` / `docker start`；
- Agent CLI 进程退出或 Supervisor 重启后的 native resume；
- 从最新已稳定 turn 创建 native session Fork；
- Fork 后多个 chat 在同一个 workspace 中并发运行；
- 工作区快照、Git publication 与会话执行之间的协调。

本文不承诺：

- 丢失 Docker container 或 Docker data root 后恢复；
- 跨主机迁移或 cold restore；
- 从任意历史 turn Fork；
- Fork 后获得独立文件系统、Git branch 或 PR；
- 自动删除停止的沙盒；
- PID、进程内存、socket 或后台进程跨 stop/start 保持不变。

## 2. 核心决策

当前架构采用以下五项决策：

1. **会话连续性由 Agent CLI 的原生 session 与 Docker-owned workspace 共同定义。** 消息历史只是 ledger，不是完整 checkpoint。
2. **进程不是持久化身份。** 正常路径可以复用 live process；恢复路径可以创建新进程，只要它严格加载同一 native session。
3. **Docker writable layer 是当前本地持久状态。** `/workspace`、CLI session state、依赖和未提交修改都保存在 retained container 中，不使用 per-chat host bind mount。
4. **Fork 是 native conversation fork，不是容器克隆。** 子 chat 与父 chat 拥有不同 native session，但共享 sandbox、workspace 和 Git lineage。
5. **失败必须显式。** 无法证明原生上下文被恢复时，状态是 `lost`，不得通过 transcript replay 或空白 session 假装成功。

## 3. 术语与稳定身份

| 名称 | 含义 | 生命周期 | 稳定键 |
|---|---|---|---|
| Thread / Chat | 用户看到的长期会话 | 多个 turn | `thread_id` |
| Job / Turn | 一条用户消息及其执行结果 | 单次提交 | `job_id` |
| Attempt | Job 的一次具体执行，可重试或被替代 | 单次 claim | `attempt_id` + lease generation |
| Activation | 一次 prompt 被准许进入 native session 的窗口 | 单 turn | `activation_generation` |
| Sandbox group | 一个 Docker container 及其共享 workspace | 多个 chat、多个 turn | `sandbox_id` + `container_generation` |
| Membership | 一个 chat 在 sandbox group 中的执行身份 | Chat 存续期 | `thread_id` |
| Native session | Agent CLI 自己持久化的会话 | 多个进程 | `(sandbox_id, native_session_id)` |
| Resident transport | Supervisor 持有的 CLI stdin/RPC/HTTP 通道 | Docker running interval 内可复用 | `process_generation` |
| Workspace epoch | 共享 workspace 的有序修改边界 | 每次稳定快照递增 | `sandbox_id` + epoch |
| Fork operation | 跨控制面与 host 的幂等 saga | 单次 Fork 请求 | `operation_id` / client idempotency key |

`native_session_id` 不是全局唯一标识。相同字符串只在对应 sandbox 与 runtime state root 内有意义。

## 4. 状态所有权

### 4.1 持久状态与易失状态

| 状态 | Owner | 是否跨 `docker stop` 保留 |
|---|---|---|
| Thread、Job、Attempt、事件 ledger | Control plane database | 是 |
| Sandbox group、membership、generation、native locator | Control plane database | 是 |
| `/workspace`、`.git`、依赖、CLI session files | Docker writable layer | 是 |
| Fork receipt journal | Docker writable layer | 是 |
| Resident process、stdin/stdout、RPC connection | Host Supervisor | 否 |
| PID、进程内存、open socket、shell environment | Kernel / process | 否 |
| Inference grant、MCP token、local-control password | Activation/residency tmpfs 或进程环境 | 否 |
| UI event stream cache | Frontend / transport | 否；可从 ledger 重建 |

### 4.2 目标拓扑

普通 chat 保持一对一：

```text
thread A
  -> membership A
  -> native session A
  -> resident process PA
  -> sandbox group G
  -> container C
  -> /workspace
```

Fork 后只增加 membership、native session、slot 与 resident process：

```text
thread A -> membership A -> session F -> process PA ─┐
                                                     ├─> sandbox G -> container C -> /workspace
thread B -> membership B -> session B -> process PB ─┘
```

Fork 不创建第二个 container、第二份 workspace 或第二条 Git publication lineage。

## 5. 连续性语义

### 5.1 `stop` 不是 `pause`

`docker stop` 会终止容器进程；`docker start` 会重新执行容器入口进程。以下内容不会恢复：

- Agent CLI PID 和进程内存；
- 打开的 PTY、socket 与 stdin/stdout pipe；
- shell 环境变量和内存缓存；
- Agent 启动但尚未被平台收敛的后台进程；
- 未完成、未持久化的 turn 边界。

以下内容由 retained container 保留：

- container identity、label 与 writable layer；
- workspace、Git index、staged/unstaged/untracked 文件；
- 已安装依赖和生成文件；
- Agent CLI 原生 session locator 与 session state；
- credential-free 平台 journal。

因此 PID 不属于连续性契约。

### 5.2 产品状态

| `context_status` | 定义 | 必须证明的事实 |
|---|---|---|
| `new` | 新 chat 的第一个 turn | 此前不存在 native session |
| `live` | 在同一 retained sandbox 中继续 native session | 当前 turn 绑定正确 locator；不要求 PID 相同 |
| `restored` | 先恢复 stopped container 或替换进程，再继续 native session | native resume 成功并恢复到 `last_settled_job_id` |
| `lost` | 无法证明 native context 可恢复 | 不发送新 prompt，不回退到空白 session |

`hot`、`warm_native`、`cold_native`、`replay` 只描述 transport 或恢复方法，不能替代上述产品状态：

- Docker `running` 不等于某个 membership `hot`；
- 新进程成功 native resume 仍可得到 `live` 或 `restored`；
- transcript replay 是显式降级方法，不是 native continuation。

## 6. 生命周期协议

### 6.1 第一个 turn

1. Control plane 创建 thread、job 与 queued attempt。
2. Scheduler 选择具有所需 runtime、镜像与隔离能力的 host。
3. Host 创建 sandbox group 与 credential-free container。
4. Trusted source preparer 在沙盒外拉取仓库、去除 credential，并通过 Docker archive API 导入 `/workspace`。
5. Setup 在 setup egress phase 中执行；完成后撤销 setup network，并切到 agent egress phase。
6. Supervisor 创建 membership slot，注入临时能力，启动 Agent CLI native session。
7. Prompt admission 先持久化，再只发送一次。
8. Turn 结算后写入 native locator、`last_settled_job_id` 与 `context_status=new`。

### 6.2 Docker 已运行时的 follow-up

正常路径：

1. 对 membership 做单 turn CAS fence；
2. 验证 resident process generation 与 native locator；
3. 直接向 live transport 发送新 prompt；
4. 只把该 activation 的 framed events 返回给对应 runner；
5. Turn 结算后保留 resident process idle，不刷新 group activity deadline。

恢复路径允许替换该 membership 的 process，但必须先 native resume。兄弟 membership 不应被重启、撤销 credential 或重标状态。

### 6.3 Docker 已停止时的 follow-up

1. 预留 host capacity，CAS `stopped -> starting`；
2. 验证 container id、label、image/runtime compatibility 与 generation；
3. `docker start` 同一个 container；
4. 为请求方 membership 创建新的 process generation 与新 credential；
5. 使用保存的 locator 执行 native resume；
6. 验证恢复边界至少覆盖 `last_settled_job_id`；
7. 成功后才 admission 并发送新 prompt；
8. 记录 `context_status=restored`。

如果步骤 2、5 或 6 不能证明正确性，则停止新进程并标记 `lost`。不得先发送 prompt 再尝试判断它是否恢复成功。

### 6.4 Settled-idle stop

只有满足以下条件才能停止 sandbox group：

- 所有 membership 均无 active/admitted turn；
- 没有 terminal、file/Git mutation、Fork 或 snapshot operation；
- 所有输出已 drain，Attempt 结果与 native locator 已持久化；
- Supervisor ownership 与 container generation 匹配；
- active operation count 为零且 idle deadline 已到。

Stop 顺序：

1. flush 并关闭所有 idle resident transport；
2. 清理每个 member slot 的 turn descendants；
3. 撤销 residency/activation credential；
4. 执行有界 `docker stop`；
5. inspect 确认 container stopped 且没有 Agent CLI PID；
6. 由 heartbeat 把 Docker truth 写回控制面。

停止 container 不删除 membership、workspace 或 native session。

### 6.5 Supervisor 重启

Supervisor 从 immutable Docker label 重建 sandbox group。数据库 membership manifest 在 acquire、Fork 或 snapshot 时按需 hydrate。

旧 exec pipe 无法安全 reattach 时：

- generation-fence 旧 handle；
- 只把受影响 membership 从 HOT 降为 WARM；
- 清理其 slot 后 native resume；
- 不因为内存 member map 丢失而创建新 container；
- heartbeat 中缺少某个 member 不能被解释为该 member 已删除。

## 7. Prompt admission 与并发

每个 membership 最多只有一个 unresolved prompt；不同 membership 可以并发。

```text
group G
  member A: activation 41 active
  member B: activation 12 active
  member C: resident idle
```

正确性依赖以下 fence：

- `attempt_id` 与 lease generation：拒绝 superseded runner；
- `activation_generation`：拒绝上一 turn 的 late writer/event；
- `process_generation`：拒绝旧 process handle；
- `container_generation`：拒绝旧 container observation；
- `residency_generation`：拒绝旧 credential renew/revoke；
- durable admission receipt：防止控制面或 host 在 stdin 前后崩溃时重复 prompt。

Sandbox group 没有 turn mutex。A 正在执行不能让 B 收到 `sandbox busy`。

## 8. Native Fork

### 8.1 语义

Fork 复制的是 source native session 的最新 settled context：

```text
source session F --native fork-only--> child session B
```

Fork 操作本身：

- 不发送 prompt；
- 不调用 provider；
- 不 replay transcript；
- 不创建 Docker image/container/volume；
- 不创建独立 branch；
- 成功返回前必须已经创建并验证 child native session 与 idle resident transport。

只允许最新 settled turn。历史消息存在并不能证明历史时刻对应的 workspace 仍然存在。

### 8.2 幂等 saga

Fork 跨越数据库、control plane 与 owning host，不在网络调用期间持有数据库事务。

状态机：

```text
PREPARING -> MATERIALIZED -> READY
     \             \
      +-------------+-> FAILED
```

- `PREPARING`：预分配 child identity，fence source membership，记录 in-container receipt 基线；
- `MATERIALIZED`：runtime 已持久化新的 native id，但 child 仍不可见；
- `READY`：child membership、visible history、native edge 与 resident process 在一个最终事务中发布；
- `FAILED`：没有可验证 target，或恢复存在歧义；不暴露半成品 child。

客户端必须重用同一个 idempotency key。数据库唯一约束关闭“响应丢失后重试创建第二个 child”的窗口；container-local receipt 关闭“host 已完成但 control plane 未记录”的窗口。

### 8.3 Shared workspace 后果

Fork 后 native transcripts 分叉，filesystem 不分叉。以下行为属于产品语义，不属于平台 bug：

- A 与 B 同时写同一文件，最终内容由真实写入顺序决定；
- 一个 chat 可以看到另一个 chat 的部分修改；
- `.git/index.lock`、HEAD、index、stash 与 package install 互相影响；
- 取消 B 不回滚 B 已经写入 workspace 的内容；
- 一个 turn 的 diff 可能包含 sibling 的修改。

UI、API 与文档必须明确返回 `workspace_shared=true`，不能把 Fork 描述成独立工作区。

### 8.4 Runtime capability

Shared Fork 依赖精确 runtime/image 能力，而不是仅检查 CLI 名字。

| Runtime | Resident transport | Prompt-free native fork |
|---|---|---|
| Claude Code | stream-json live transport | pinned Agent SDK `fork_session` helper |
| Codex | app-server | `thread/fork` |
| Pi | RPC mode | fork-only RPC launch |
| OpenCode | authenticated loopback server | native session fork endpoint |
| Kilo | authenticated loopback server | native session fork endpoint |

精确版本、镜像 digest、helper、slot pool、Supervisor protocol 与 runner concurrency 必须共同 attestation。Capability 不能从 runtime 名称推断。

## 9. 进程与 credential 隔离

Shared workspace 不等于 shared authority。每个 ready membership 占用一个稳定 execution slot：

- 独立 Linux UID；
- 共享非特权 workspace GID；
- 独立 tmpfs credential/config 路径；
- 独立 process、residency 与 activation generation；
- 独立 inference grant、MCP token 和 local-control credential。

不同 slot 必须不能读取 sibling 的 `/proc/<pid>/environ`、ptrace 或 signal sibling。不能在隔离验证失败时回退到共享 UID。

Turn 结束时只保留经过验证的 resident CLI root。Supervisor 对该 UID 做 PID + start-time census，并清除 turn 新建的 descendants；仅杀 process group 不够，因为工具可以 `setsid` 或 double-fork。

无法证明 slot 已清空时：

- 当前 membership fail closed；
- 关闭其 resident process；
- 不把 slot 重新绑定给其他 membership；
- 不影响 sibling slot；
- 后续只有在 whole-UID zero-process proof 后才能 native resume。

需要长期运行的 credential-free dev server 应通过受控 terminal/command facility 创建，而不是作为 Agent turn 的未追踪后代保留。

## 10. Git、快照与 publication

一个 sandbox group 只有一份 `.git` 与一条 publication lineage。系统不能为每个 sibling 自动生成彼此独立的 patch 或 PR。

Turn settlement 与 publication 解耦：

1. Turn 输出先结算并返回对应 chat；
2. Workspace epoch 标记为 pending；
3. Coordinator 等待 `active_mutation_count == 0`；
4. 通过 container/process census 验证没有未知 writer；
5. 在短暂 bounded barrier 中生成相对 `published_base_sha` 的 immutable patch；
6. 立即释放 workspace，后续 secret scan、apply、commit、push 在 trusted publisher 中完成；
7. Snapshot 按 epoch 严格有序发布。

这个 barrier 不是 cross-chat turn lock：已经运行的 turn 不被中断，普通 turn 也不等待网络 publication。只有捕获 immutable diff 的短窗口会阻止新的 mutation admission。

如果一直无法达到 quiescence，snapshot 保持 pending；如果外部 push 结果不确定，禁止自动重复产生外部副作用。

## 11. Durable schema 边界

实现将 container lifecycle 与 chat execution 拆成两个 authority：

### 11.1 Sandbox group

`agent_sandboxes` 至少拥有：

- host、container id/name/generation；
- runtime、image digest、execution profile；
- lifecycle state 与 idle deadline；
- resident slot capacity；
- workspace epoch、active mutation/operation count；
- publication lineage 与 published base；
- group-scoped error。

它不拥有单个 `active_attempt_id` 或 `native_session_id`。

### 11.2 Membership

`agent_thread_sandbox_memberships` 至少拥有：

- `thread_id -> sandbox_id`；
- native session id/state；
- resident state 与 slot；
- residency/process/activation generations；
- active attempt、admitted job 与 last settled job；
- Fork source、anchor 与 operation；
- member-scoped error。

数据库必须对 `(sandbox_id, native_session_id)` 建立非空唯一约束，防止两个 chat 被错误绑定到同一个 writable native conversation。

## 12. 不变量

1. 一个 membership 最多有一个 unresolved prompt。
2. 不同 membership 可以在同一 sandbox 中同时运行。
3. 一个 ready membership 只拥有一个 native session。
4. Native session 的平台身份始终包含 `sandbox_id`。
5. Prompt 在 durable admission 前不进入 CLI；同一 activation 最多发送一次。
6. Context restore 在 prompt admission 前完成并验证。
7. PID 变化不能单独导致 context loss；PID 相同也不能单独证明 continuity。
8. Docker stop 清除所有 process 与 credential，但不删除 writable layer。
9. Fork 成功前 child 不可见；成功后 child transport 已 ready。
10. Fork 不创建独立 workspace、container 或 branch。
11. Sibling 可以共享文件，但不能共享 credential、event stream、cancel authority 或 process fence。
12. Group idle stop 只能在 active operation count 为零时执行。
13. Container totals 不能重复记到多个并发 member/job。
14. 无法证明恢复或清理时 fail closed，不 replay prompt。

## 13. 失败语义

| 场景 | 影响范围 | 必须行为 |
|---|---|---|
| Resident process crash | 单 membership | 清理 slot，native resume；失败则 member `lost` |
| Runner crash | 单 activation | lease expiry/fence；settled resident process 可继续保留 |
| Supervisor restart | Host 上的 HOT memberships | 通过 label 与 durable manifest 重建，按需 native resume |
| Post-admission cancel/timeout 边界不明确 | 单 membership | 关闭 process 并标记 `lost`，不重放 prompt |
| Container OOM/exit | 整个 sandbox group | 所有 member 受影响；按 Docker truth reconciliation |
| Container 暂时不可达 | 整个 group | 保持 host affinity，报告 unavailable，不迁移制造副本 |
| Container 或 Docker data root 确认丢失 | 整个 group | 所有 member `lost` |
| Fork RPC response 丢失 | 单 Fork operation | 读取 receipt 并幂等 finalize；不再次执行不确定 native mutation |
| Fork target 歧义 | 单 Fork operation | `failed`，不暴露 child，不猜测 target |
| Workspace writer 无法归属 | Snapshot | 保持 pending/dirty，不宣称 quiescent |
| Disk pressure | 新 chat admission | 告警并拒绝新建，不自动删除 retained sandbox |

## 14. Rollout 与兼容性

Shared-live 只在能力门禁通过时启用。旧 retained container 不因为数据库 backfill 或 host 升级就自动获得新镜像中的 UID、helper、slot 和权限；它继续使用 `legacy_1to1` native resume，并拒绝 retained Fork。

生产启用前至少验证：

- 每个精确 pinned runtime 的真实 provider 多 turn、Fork、interrupt、restart；
- 两个 member 同时执行与同时修改 workspace；
- sibling credential、`/proc`、signal 与 local-control isolation；
- `setsid`、double-fork、忽略 TERM 的 descendant cleanup；
- Supervisor/runner/broker crash 与 lost-response reconciliation；
- container OOM、unexpected exit 与 disk-pressure admission；
- Kata/gVisor 或目标生产隔离后端；
- snapshot ordering、external publication ambiguity 与 rollback；
- feature flag 关闭后停止创建新 shared group，但不破坏既有 membership。

## 15. 实现导航

在 `freeinference-cloud-agent` 中，关键实现位于：

| 组件 | 路径 |
|---|---|
| Control plane API 与 Fork 协调 | `cloud_agent/api/routes.py` |
| Durable group/member/fork/snapshot 状态 | `cloud_agent/storage/store.py`、`migrations/` |
| Shared Session Supervisor | `cloud_agent_host/shared_session_supervisor.py` |
| Resident transport 与 native Fork | `cloud_agent_host/resident_transport.py`、`cloud_agent_host/drivers.py` |
| Workspace 与 terminal broker | `cloud_agent_host/workspace_broker.py` |
| Runner claim/attempt | `cloud_agent_host/runner.py` |
| Runtime 与沙盒执行 | `cloud_agent_host/runtimes.py`、`cloud_agent_host/sandbox.py` |
| 资源采样 | `cloud_agent_host/resources.py` |

实现细节、精确版本与 acceptance gate 以实现仓库中对应版本化规范为准。
