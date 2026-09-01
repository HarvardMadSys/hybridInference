# Agent 沙盒安全模型与能力边界

**日期：** 2026-09-01

**状态：** 当前安全契约；部分强化隔离与生产对抗测试仍是 rollout gate

**实现仓库：** `freeinference-cloud-agent`

**适用对象：** Control plane、runner host、sandbox runtime、MCP relay、source preparer、publisher 的开发与运维人员

**相关文档：**

- [Agent 会话连续性与共享沙盒架构](2026-09-01-agent-session-continuity-architecture.zh.md)
- `freeinference-cloud-agent/AGENTS.md`
- `freeinference-cloud-agent/ENVIRONMENT.md`
- `freeinference-cloud-agent/docs/specs/2026-08-31-native-session-fork-shared-workspace-design.md`

## 1. 安全目标

平台允许 Agent 对用户授权的仓库执行动态代码。安全设计必须满足：

1. 沙盒内代码不能获得 control plane、runner host、Docker daemon 或 source-control publisher 的控制权。
2. 沙盒只得到完成当前任务所需的短期能力；能力有 scope、subject、generation、TTL 与撤销边界。
3. Setup 与 Agent 执行使用不同网络策略，Agent phase 不能恢复 package-registry egress。
4. Repository credential、长期模型 credential、MCP upstream credential 和发布 credential 不进入 workspace 或 Docker writable layer。
5. 同一 shared sandbox 内的 sibling chat 可以共享文件，但不能读取彼此 credential、控制彼此进程或接收彼此事件。
6. 安全能力无法验证时 fail closed；不得静默降级到更弱 runtime、开放网络或共享 credential 路径。
7. 观测、审计和资源采样不依赖沙盒内代码配合，也不能被其伪造为成功。

## 2. 非目标

本文不声称：

- 普通 Linux container 等同于独立内核；
- Docker 本身足以承载所有 public multi-tenant workload；
- 模型能可靠识别 prompt injection；
- Shared workspace 在 sibling chat 之间提供文件机密性；
- 安全策略可以阻止用户授权域内的 Agent 故意破坏共享代码；
- 容器隔离可以修复宿主内核、Docker daemon 或镜像供应链漏洞；
- 仅靠日志或 eBPF 可以构成访问控制边界。

## 3. 威胁模型

### 3.1 不可信输入

以下内容均按不可信处理：

- 用户仓库中的源码、脚本、Git hooks 与构建配置；
- package manager 安装脚本和传递依赖；
- README、AGENTS.md、issue、网页与检索结果中的 prompt injection；
- MCP server 返回的文本与结构化数据；
- Agent 自己生成的 shell command、patch 和后台进程；
- 编译器、测试工具、插件和 language server 的子进程；
- 通过共享 workspace 由 sibling chat 写入的文件。

一个边界如果要求 Agent “自觉不使用”某项已进入沙盒的能力，则不属于安全边界。

### 3.2 受保护资产

| 资产 | 主要风险 |
|---|---|
| 用户源码与未发布修改 | 跨租户读取、意外发布、供应链泄露 |
| Source-control access | 读取未授权仓库、push/PR 到错误目标 |
| Inference capability | 冒用用户身份、绕过模型 scope 或 quota |
| MCP upstream credential | 调用未授权工具或窃取用户第三方数据 |
| Control-plane database | 修改 job/attempt/fence、横向读取其他用户数据 |
| Runner host / Docker socket | 接管其他 sandbox 或宿主机 |
| Publisher credential | 绕过 patch gate 直接写远端仓库 |
| Sibling activation credential | 将 A 的消耗、事件或权限归到 B |
| Telemetry 与审计记录 | 隐藏安全事件或伪造归属 |

### 3.3 信任假设

Trusted Computing Base 包括：

- control plane 与数据库；
- runner、Session Supervisor 与 workspace broker；
- Docker daemon / 目标隔离 runtime；
- source preparer、gateway/MCP relay 与 publisher；
- sandbox base image、runtime pins、seccomp/capability 配置；
- host kernel，或 Kata/gVisor 提供的额外边界。

用户仓库、Agent CLI 执行的工具、workspace 内容与上游 MCP 响应不在 TCB 中。

## 4. 信任边界与数据流

```text
                      trusted control domain

 browser ──identity──> control plane ──dispatch──> runner/supervisor
                           │                         │
                           │                         ├── Docker Engine
                           │                         ├── source preparer
                           │                         └── workspace broker
                           │
                           ├── gateway: identity/model catalog/grants
                           ├── MCP registry + per-user encrypted connections
                           └── publisher + source-control credential

                                      capability boundary
                                                │
                                                ▼
                                      untrusted sandbox
                                      ├── /workspace
                                      ├── Agent CLI
                                      ├── build/test tools
                                      └── child processes

 sandbox network destinations during agent phase:
   1. fixed gateway relay  -> model inference only
   2. fixed MCP relay      -> selected MCP tools only

 sandbox cannot choose an arbitrary upstream and cannot reach the control-plane listener.
```

Relay 是固定目的地转发器，不是让 caller 自选目标的通用 HTTP proxy。

## 5. 权限所有权

| 能力 | Authority / Owner | 沙盒是否持有 | 约束 |
|---|---|---|---|
| 用户登录与角色 | Gateway identity | 否 | 短期 identity token 供 control plane 验证 |
| Job、Attempt、lease fence | Cloud Agent control plane | 否 | Runner 只持有当前 claim |
| Repository clone | Trusted source preparer | 否 | 短期 token 使用后删除 |
| Model inference | Gateway | 是，短期 `agr` capability | User、model scope、TTL、quota |
| MCP call | Cloud Agent MCP relay | 是，独立 `mcp` capability | User、job、attempt、server/tool allowlist |
| MCP upstream credential | Cloud Agent encrypted store / relay | 否 | Relay 代为调用 upstream |
| Publish/push/PR | Trusted publisher | 否 | Patch gate、secret scan、目标校验后使用短期 token |
| Docker lifecycle | Host Supervisor | 否 | Generation-fenced Docker API |
| Terminal/file/Git operation | Workspace broker | 不持有 broker authority | Membership auth + operation fence |

模型 grant 与 MCP token 必须是两个 audience。泄露 inference grant 不能调用工具；泄露 MCP token 不能调用模型或 control-plane API。

## 6. Credential 生命周期

### 6.1 禁止持久化的 credential

以下位置不得出现 clone token、inference grant、MCP token、publisher token、local-control password 或长期 provider secret：

- Docker image layer 与 `docker history`；
- container creation environment / `docker inspect Config.Env`；
- CLI argv；
- `/workspace`；
- durable `$HOME`、provider state root 或 native session transcript；
- Fork receipt journal；
- patch、artifact、日志 body 与 frontend event；
- host 的 per-chat 文件目录。

### 6.2 注入与撤销

Credential 只通过以下临时路径进入执行：

- process-scoped environment；
- membership-owned tmpfs activation directory；
- in-memory bridge local-control credential；
- Supervisor 与 relay 间的受控请求 metadata。

每个 activation/residency 的 credential：

1. 在 generation 确认后签发；
2. 只绑定一个 subject、scope 和 execution identity；
3. 使用短 TTL；
4. 由 heartbeat 在 fence 仍有效时续期；
5. turn settle、cancel、supersede、process replacement 或 idle stop 时撤销；
6. 即使 revoke 请求丢失，也会在 TTL 到期后失效。

长 TTL + 仅依赖远程 revoke 不是可接受设计，因为 control plane 故障时 abandoned attempt 会继续持有能力。

### 6.3 Retained sandbox 与 Fork

Credential 不是 session state：

- `docker stop` 后 tmpfs 清空；
- `docker start` 后重新签发；
- Fork 不复制 credential；
- child membership 使用新的 slot、generation 与 credential；
- native session 文件可复制/分叉，但其中不得含 provider key。

## 7. Repository ingress

沙盒不直接持有 repository clone credential。流程为：

1. Control plane 校验用户、repository 与授权；
2. Trusted source preparer 在临时目录中使用短期 token fetch；
3. 删除 credential 与远端中可能携带的敏感配置；
4. 校验 tar path、file type、size、uid/gid 与目标边界；
5. 通过 Docker archive API 流入 `/workspace`；
6. 在 prompt admission 前删除临时 checkout。

Source preparer 的临时目录不是 checkpoint，也不能作为恢复或 Fork 的隐式第二份真相来源。

后续 published base 更新使用 credential-free Git bundle 导入 object；不得 reset shared worktree、index、local commit 或 uncommitted changes。

## 8. 两阶段网络策略

### 8.1 Setup phase

目的：允许安装依赖，但不提供开放互联网。

- 所有访问通过 allowlist proxy；
- CONNECT 按 hostname 与端口策略校验；
- proxy 记录 allow/deny、目标、字节与时长；
- 不允许 caller 把 proxy 变成任意目标 relay；
- repository credential 不进入 setup process；
- setup 完成后 detach network，并验证 detach 成功。

### 8.2 Agent phase

目的：执行不可信代码，同时把可达面限制到显式能力服务。

- 只连接固定 gateway relay 与可选 MCP relay；
- relay address 由 host 配置，沙盒不能指定 upstream；
- gateway 只接受 model grant；
- MCP listener 只暴露 MCP routes，并拒绝 model/control token；
- control-plane、runner API、Docker socket、database 和 source-control upstream 均不可达；
- setup proxy 不得重新连接。

如果 deployment 未配置某个 relay，相关能力为空；系统应在 job admission 或 runtime launch 时拒绝不满足的请求，不能把 MCP URL 回退到 gateway 或打开普通网络。

### 8.3 Network transition 不变量

```text
PROVISIONING -> SETUP_EGRESS -> NETWORK_DETACHED -> AGENT_EGRESS
```

进入 `AGENT_EGRESS` 后，同一 container generation 不允许回到 `SETUP_EGRESS`。确需重新 setup 时必须设计新的可信 operation，而不是临时打开网络。

## 9. 容器与 runtime 隔离

选择执行 runtime 时，首先要回答的不是“它是不是容器”，而是：**工具进程发出的 syscall，最后由谁执行。**

[![runc、gVisor 与 Kata 的 syscall 执行边界：普通容器直接进入宿主机内核，gVisor 由 Sentry 承接，Kata 由客户机内核执行](assets/sandbox-syscall-execution-boundaries.svg)](assets/sandbox-syscall-execution-boundaries.svg)

*图 1：隔离边界越强，宿主机越难直接观察并归因工具进程的原始 syscall。*

### 9.1 三种执行边界

#### 普通容器（runc）：共享宿主机内核

容器没有第二个内核。工具进程是宿主机上的普通进程；namespace 改变它能看见的进程、网络和挂载，cgroup 约束它能消耗的资源，但 `open()`、`mmap()`、`clone()` 等 syscall 仍由宿主机 Linux 内核直接执行。

因此普通容器启动快、兼容性完整，Host eBPF 也能直接观察工具进程的 syscall。代价是所有租户共享同一内核攻击面；namespace、cgroup、capability 与 seccomp 是必要 hardening，但不能把共享内核变成 VM 边界。

#### gVisor：用户态内核承接 syscall

gVisor 在工具进程和宿主机内核之间插入 Sentry。工具 syscall 先由 Sentry 拦截，并由其在用户态实现大部分 Linux syscall 语义；只有底层资源操作再通过一组受限 syscall 进入宿主机内核。

这显著缩小不可信代码直接触达的宿主机内核攻击面，并保留接近容器的启动特性。代价是额外的用户态路径、I/O 开销和 syscall/内核特性兼容缺口。Host eBPF 看到的是 Sentry、Gofer 等代理进程的宿主机行为，而不是工具原本发出的完整 syscall 流。

#### Kata：客户机内核执行 syscall

Kata 为沙盒创建轻量虚拟机。工具进程运行在 guest 中，syscall 由独立的客户机 Linux 内核执行；宿主机只运行 VMM，并处理 virtio、tap 等设备侧行为。

硬件虚拟化提供三者中最强的 kernel boundary，并保留接近原生 Linux 的 guest 兼容性。代价是更高的启动时间、固定内存与设备配置开销。Host eBPF 只能观察 VMM、virtio 和宿主网络设备，无法直接看到 guest 中的工具进程及其 syscall。

### 9.2 边界与观测对比

| 维度 | 普通容器 / runc | gVisor | Kata |
|---|---|---|---|
| 工具 syscall 的执行者 | 宿主机内核 | Sentry 用户态内核 | 客户机内核 |
| 与宿主机内核的边界 | namespace/cgroup/seccomp，共享内核 | 用户态 syscall implementation，缩小 host syscall 面 | 硬件虚拟化，独立 guest kernel |
| 启动与固定开销 | 最低，通常毫秒级 | 较低，通常仍是容器量级 | 最高，通常百毫秒至秒级且有 VM 固定内存 |
| Linux 兼容性 | 最高 | 有 syscall 与内核特性缺口 | 接近原生 guest Linux |
| Host eBPF 直接看到 | 工具进程的真实 syscall | Sentry/Gofer 的 host syscall | VMM、virtio、tap 等 host-side 行为 |
| Operation 归属策略 | 可按 PID/cgroup 直接关联 | 需要 runtime event 与代理侧 identity 关联 | 需要 guest agent 或 guest telemetry 回传 |

这不是“安全与可观测性只能二选一”，而是测量位置必须随隔离边界移动：runc 可以主要依赖 host sensor；gVisor 需要把 Sentry/runtime event 纳入归属；Kata 的 syscall/process 级观测必须进入 guest，host 侧只保留 VM 与设备总量。

### 9.3 当前实现与准入规则

当前 `cloud_agent_host/sandbox.py` 只定义三个 backend：`process`、`container` 和 `kata`。

- `container` 且 `runtime=None` 表示普通 runc 共享内核。Preflight 记录 `agent_sandbox_shared_kernel`，该档位只适合可信仓库或已有外层 VM 隔离的场景。
- `kata` 使用 `io.containerd.kata.v2`。Preflight 会启动 exact-image probe 执行 `uname -r`；若 guest kernel 与 host kernel 相同，则拒绝把该 host 宣告为 VM-isolated。
- gVisor 当前没有独立 backend、执行档位或安全 probe。管理员能够填写 runtime 字符串不等于平台已经验证 gVisor 隔离；面向不可信仓库的强制档位当前只接受经过 probe 的 Kata。

`is_vm_isolated` 当前以“配置了非空 runtime”表达非默认 runtime，而不是独立的安全证明。安全决策不得单独依赖该字段；准入必须依赖明确的 profile、runtime allowlist 与 exact-image probe。

| 场景 | 当前最低边界 | 准入说明 |
|---|---|---|
| 一次性、隔离 VM 中的内部任务 | Hardened container | 外层 VM 承担租户 kernel boundary |
| 可信内部仓库、自托管 host | Hardened runc container | 仍需 non-root/cap/seccomp/resource limits |
| Public multi-tenant、不可信仓库 | Probed Kata VM | 当前只认 `io.containerd.kata.v2`；gVisor 需新增独立 profile 与 probe 后才能准入 |

Runtime capability 必须由 preflight 与 exact-image probe 证明。配置要求强化隔离但 host 只提供普通 Docker 时，host 不得 advertise capability，也不得 claim 对应 job。

### 9.4 基线 hardening

所有执行 profile 至少要求：

- 非 root user；
- `no-new-privileges`；
- drop all capabilities，只按审核结果增加最小集合；
- seccomp；
- CPU、memory、PID、disk 与 timeout 限制；
- `--init` 或等价 init shim；
- 无 Docker socket、host `/proc`、host cgroupfs 与 host path mount；
- credential-free immutable base image；
- 精确 runtime/helper pin 与 image digest attestation。

## 10. Docker-owned storage boundary

Host service 只通过 Docker Engine API 操作 chat state：

- create/start/stop/inspect/exec/stats/archive；
- 不读取 `/var/lib/docker`；
- 不解析 storage-driver 私有格式；
- 不维护 per-chat host workspace；
- 不为 `/workspace` 或 native state 配置 bind/named/anonymous volume。

这减少 host-path 攻击面和路径漂移，但不提供 host-independent persistence。删除 container、误运行 prune、丢失 data root 或丢失 host 仍会使 session `lost`。

Session host 禁止 generic container/system prune。Disk pressure 通过 admission 与告警处理，不能通过静默删除 retained sandbox 处理。

## 11. Shared sandbox 的 member 隔离

Fork sibling 属于同一个用户/security domain，并有意共享 `/workspace`。Credential 与 process control 仍是硬边界。

### 11.1 Execution slot

每个 membership 使用预制 slot：

```text
member A: uid 11001, shared gid, private tmpfs A, process generation PA
member B: uid 11002, shared gid, private tmpfs B, process generation PB
workspace: shared setgid group, no credential files
platform journal: sandbox owner, credential-free
```

具体 UID 只是实现参数；安全契约是：

- sibling UID 不可读取 `/proc/.../environ`；
- 不可 ptrace 或 signal sibling；
- 不可读取 sibling tmpfs/config；
- slot reuse 前必须完成 secret/process cleanup；
- 无安全的 UID/slot pool 时不 advertise shared capability。

### 11.2 Shared state 与 private state

| 内容 | Shared | Private |
|---|---|---|
| `/workspace` 与 `.git` | 是 | 否 |
| 依赖、构建产物、项目级 instruction | 是 | 否 |
| Native conversation id | 否，per membership | 是 |
| Provider home | 视 runtime；并发不安全者 per member | 是 |
| Activation credential/config | 否 | 是，tmpfs |
| Event stream 与 cancellation | 否 | 是，generation-fenced |
| Terminal filesystem effect | 是 | Terminal ownership 与 cancel 是 per member |

文件由 sibling 故意 chmod、删除或覆盖属于 shared-workspace interference，可能导致某个 member `lost`；它不应因此获得 sibling credential。

### 11.3 Loopback service

在同一 container network namespace 中，`127.0.0.1` 不是 membership 边界。使用 loopback HTTP server 的 runtime 必须：

- 绑定随机端口，不发布到 host；
- 使用 process-generation scoped authentication；
- credential 只存在 bridge process environment/memory；
- 未认证请求返回拒绝；
- sibling 不可从 `/proc` 或持久文件读取 credential。

“只监听 localhost”本身不足以隔离 sibling process。

## 12. 进程生命周期与取消

安全清理不能依赖 Agent CLI 正常退出。

### 12.1 Turn boundary

Turn 开始前记录 member UID 的 PID + start-time baseline。Turn 结束或取消时：

1. 优先调用 runtime-native interrupt；
2. 对本 activation 新增的同 UID descendants 发送 TERM；
3. 超时后 KILL；
4. 重复 census，处理 `setsid`、double-fork 与 PID reuse；
5. 仅在 zero-descendant proof 后把 resident root 标为 idle/HOT。

Process group 只是一条线索，不是完整边界。PID 必须与 start time 组合，避免 PID reuse 误杀。

### 12.2 不明确的取消边界

Prompt admission 前取消可以安全释放 attempt。Prompt admission 后，如果 runtime 无法证明 prompt 未被消费或已经到达 settled boundary：

- 停止该 membership transport；
- 标记 member `lost` 或明确 dirty failure；
- 不 replay 同一 prompt；
- 不停止 sibling process 或整个 group，除非 container 本身失效。

## 13. File、Terminal 与 Git API

这些 API 必须通过 workspace broker，而不是 host filesystem：

- 路由先授权 `user -> thread -> membership -> sandbox`；
- 每个 mutation 创建 generation-fenced operation token；
- operation 参与 group activity/refcount，防止 idle stop；
- file path 规范化并限制在 workspace root；
- archive upload 校验 path traversal、symlink 与 file type；
- command/PTY 具有 idle timeout 与 hard lifetime；
- terminal 没有 inference/MCP credential；
- terminal ownership 与 cancellation per membership；
- Git view 可共享，但 publisher authority 不在 broker 内。

Read-only operation 可在 sibling turn 期间继续；writer operation 必须被 snapshot quiescence census 计入。

## 14. Publication boundary

沙盒不能 push。发布路径为：

1. 沙盒内部生成相对可信 base 的 patch；
2. Control plane 存储 immutable artifact 与 workspace epoch；
3. Trusted publisher 在外部 checkout 应用 patch；
4. 校验 path、mode、symlink、binary policy 与 patch base；
5. 执行 secret scan 与 workflow approval policy；
6. 使用短期 source-control credential commit/push/PR；
7. 外部副作用结果不明确时停止自动重试。

Publisher 不能信任沙盒声明的目标 repository、branch 或 base；必须从 control-plane authority 重新解析。

## 15. Fail-closed 规则

| 检查失败 | 禁止行为 | 必须结果 |
|---|---|---|
| Identity / authorization 不可判定 | 创建 job | 4xx/5xx，不能匿名降级 |
| Runtime/image attestation 失败 | Claim shared/untrusted job | Host 不 advertise capability |
| Gateway grant 无法验证 | Model call | 401/403/503；不得 unmetered 通过 |
| MCP token/fence 无法验证 | Tool call | Relay 拒绝 |
| Setup network detach 未证明 | 启动 Agent | 停止 activation |
| Agent relay probe 失败 | 发送 prompt | 明确 provisioning failure |
| Native resume 不可证明 | 发送 follow-up | `lost`，不 blank-session fallback |
| Slot cleanup 不明确 | Reuse slot | Member fail closed |
| Snapshot writer census 不一致 | 宣称 quiescent/publish | 保持 pending/dirty |
| Publisher external result 不确定 | 自动重推 | 等待人工 reconcile |
| Required strong isolation 不可用 | 普通 Docker fallback | 拒绝 job |

## 16. 审计与观测

安全相关记录至少包括：

- Job/Attempt/lease/generation transition；
- Grant mint/renew/revoke/expiry，不记录 token；
- Network phase transition 与 relay probe；
- Egress allow/deny verdict 与 unclassified bytes；
- Slot allocation、process cleanup 与 ambiguity；
- Native resume/Fork receipt validation；
- Container OOM、unexpected exit、missing object；
- Snapshot gate、secret scan、publisher result；
- Sampler coverage 与 sensor gap。

日志属性只使用 bounded identifiers/enums。Prompt、tool argument/result、credential、repository token 与完整环境变量不得进入共享 telemetry sink。

## 17. 验收矩阵

### 17.1 Credential

- `docker inspect`、image history、workspace、native state 与 Fork journal 中无 credential；
- revoke 丢失时短 TTL 仍会终止能力；
- suspend user 后 model call 立即拒绝；
- inference token 不能访问 MCP，MCP token 不能访问 inference/control plane；
- sibling slot 不能读取 credential 或 local-control password；
- slot reuse 前不存在旧 secret 或 process。

### 17.2 Network

- Setup 只可达 allowlist target；
- Agent phase 不能访问 package registry、control plane、Docker daemon 或任意公网地址；
- gateway/MCP relay 只能到固定 upstream；
- 未配置 relay 时请求明确失败；
- denied CONNECT 可归属、计数与告警；
- phase transition 后 setup network 不可重新出现。

### 17.3 Runtime isolation

- non-root、cap drop、seccomp 与 resource limit 在真实 runtime 生效；
- public multi-tenant profile 在当前实现中通过 Kata exact-image probe；新增 gVisor 等后端前必须提供独立 profile、runtime allowlist 与等价 probe；
- sibling `/proc`、ptrace、signal、tmpfs 测试通过；
- `setsid`、double-fork、ignore TERM 能被清理；
- container OOM 观察为 group failure；
- 无静默 fallback 到共享 UID 或普通 Docker。

### 17.4 Data 与 publication

- Clone token 不进入 sandbox；
- tar/archive path traversal 与异常 file type 被拒绝；
- shared workspace race 在 UI/API 中明确，不虚构 per-chat patch；
- patch gate、secret scan 与 target validation 在 trusted publisher 中执行；
- ambiguous push 不自动重复；
- retained container 不被 generic prune 删除。

## 18. 已知剩余风险

- Docker writable layer 仍是单一持久副本；host/data-root 丢失导致 `lost`。
- Shared workspace 允许同一授权域内的 sibling 相互影响文件和项目级 state。
- 普通 Docker 共享宿主内核，只适合相应信任等级。
- Runtime-native state format 与并发行为受精确版本影响，升级必须重新 attestation。
- Relay 与 publisher 扩大 TCB，需要独立 patch、依赖和部署审计。
- Resource/operation sensor 故障不会停止 turn；必须通过 coverage 与 ops event 暴露，而不能误报为零风险。

## 19. 实现导航

| 边界 | 主要路径 |
|---|---|
| Sandbox 与 Docker lifecycle | `cloud_agent_host/sandbox.py` |
| Egress phase / allowlist proxy | `cloud_agent_host/egress.py`、`cloud_agent_host/egress_proxy.py` |
| Source setup/import | `cloud_agent_host/setup.py` |
| Supervisor、slot 与 process cleanup | `cloud_agent_host/shared_session_supervisor.py` |
| Runtime credential/config | `cloud_agent_host/runtimes.py`、`cloud_agent_host/resident_transport.py` |
| Workspace broker | `cloud_agent_host/workspace_broker.py` |
| MCP registry/relay/proxy | `cloud_agent/mcp_registry.py`、`cloud_agent/api/mcp_relay.py`、`cloud_agent/mcp_proxy.py` |
| Inference grant client | `cloud_agent/grants.py`、`cloud_agent/gateway.py` |
| Publisher | `cloud_agent/publisher.py`、`cloud_agent/publish_worker.py` |
| Resource sampling | `cloud_agent_host/resources.py` |

任何放宽 network、credential、UID、capability、seccomp、publisher 或 Docker API 边界的变更，都需要安全评审与对应 acceptance test，不能作为普通兼容修复静默落地。
