# Cloud Agent 可选接入：分步实施与 P0 契约

- 日期：2026-09-09
- 状态：P0 契约草案；尚未实现新的运行时接口、修改部署或迁移数据库。
- 范围：承接 2026-09-05《FreeInference 独立部署与 Cloud Agent 即插即用实施计划》，固定三个仓库的边界与下一批改动。
- 契约入口：[Cloud Agent integration](../../../contracts/cloud-agent-integration/README.md)。

## 1. 先做什么

先完成 **P0 接口约定**，随后推进两条可以独立验收的工作：

1. FreeInference 没有 Agent 也能发布、登录、推理、升级和回滚。
2. Cloud Agent 只有自己的配置也能启动，显示受保护的“未连接”设置入口。

接下来接通一次完整的配对、登录、任务和撤销流程，再实施轮转、旧部署迁移、删除与回滚。
各批次独立评审。第一批契约合并不代表相应接口已经可用。

本次提交包含机器可读 OpenAPI、允许/拒绝的示例数据，以及实现必须满足的状态约束。
它不新增运行时依赖、通用插件框架或用户资料镜像。

## 2. 本次实际核对的基线

这些是读取 `origin/dev` 后核对的代码版本，不是生产部署版本。

| 仓库 | dev commit | 本次核对的内容 |
|---|---|---|
| HybridInference | `6437fd410330eb9e69b889ee98158aac81257894` | Console 已优先支持 `AGENT_PUBLIC_URL` 外链，回退到两个内部代理 URL；入口仍以 env 为权威；identity 使用固定 client，grants 仍用共享 dispatch token |
| hybridInference-cloud-agent | `f73838155ba72d7cb46b84611bcd2a0a45f68056` | Compose 仍要求 Gateway 配置；`ROLE_RANK` 仍存在；credential resolver 仍尝试查询 `users` |
| FreeInference | `2b32efc3a3869ecdd53721cd412554ed538e7a41` | staging workflow 的 `EXPECT_CLOUD_AGENT` 默认仍为 true；上游通过 `upstream.lock` 消费 |

Agent 仓库的 GitHub 规范名称已是 `HarvardMadSys/hybridInference-cloud-agent`；旧的
`freeinference-cloud-agent` 地址会重定向。本地目录名不是仓库版本或实现状态的依据。

本次没有读取生产数据库、登录部署主机或修改线上设置。迁移前必须重新盘点实际镜像、
数据库 revision、入口模式和凭据归属，不能使用本表替代环境盘点。

## 3. 配置和数据归属

| 内容 | 唯一归属 |
|---|---|
| Gateway / Console 通用代码，identity、access、catalog、grant、installation 契约 | HybridInference |
| FreeInference 的品牌、模型、路由、主站部署模板和安装授权策略 | FreeInference |
| Agent 的 Web、Control Plane、Runner、Sandbox、部署模板、任务与业务连接 | Cloud Agent |
| 主站用户资料、登录凭据、全局权限、额度及安装授权记录 | Gateway 自己的数据库与运行环境 |
| Agent binding、自己的 session、任务和加密 Git/MCP/安装凭据 | Agent 自己的数据库与运行环境 |

模板和配置定义归各自仓库；生产 secret 不进 Git。配对产生的可变状态分别存入两边数据库，
不写回仓库，也不读取对方 `.env`。Cloud Agent 配置不会整体搬进 FreeInference 仓库。

一个 Agent 数据库固定一个 `(gateway_instance_id, issuer)` 身份空间。业务表继续使用
opaque `user_id`；不重写已有 ULID，不把知道一个 ID 当作授权。数据库即使共用物理服务器，
也使用不同 database/role 并禁止互读。

## 4. P0 补齐的约束

### 4.1 多进程必须观察同一绑定版本

`BindingResolver` 的进程内原子引用只解决单个 worker 的一致性。绑定记录还必须有数据库
CAS/version 控制，所有 API worker、scheduler 和 relay 在规定的新鲜度期限内观察变更。
一个请求使用完整不可变 snapshot，不能组合旧 issuer 与新 credential。

激活、轮转、断开更新持久化 generation；每个进程在请求/调度边界检查，或使用有明确期限
的失效通知与重新加载。错过通知的进程必须能自行恢复。超过期限无法确认授权时拒绝受保护
操作；不能继续使用无限期的正向缓存。

`config_generation` 是客户端配置版本；`binding_epoch` 是安装级 session 失效版本。
普通密钥轮转只改变前者。撤销和重新绑定改变后者。已有 attempt 的 lease/fencing 仍由
Agent 自己维护，配置切换不能重置 fence 或导致重复执行。

### 4.2 用户重新 consent 不恢复旧 session

Gateway 为每个 `(installation, subject)` 保存单调递增的 `authorization_version`。
撤销、重新 consent 和授权范围变更按事务更新版本；不能删除记录后把版本重新从 1 开始。

最小 identity claims、Agent session 和 access 校验带此版本。Agent 必须比较 session 中的
版本与 Gateway 当前版本；旧版本返回稳定错误，要求重新登录，不能把 cookie 自动升级为
新版本。现有 grant 也固定签发时的授权版本，重新 consent 不复活旧 grant。

这不是复制平台角色或策略，只是授权失效控制。`agent.use` / `agent.admin` 的实时判断
仍由 Gateway 完成；角色更名不要求 Agent 跟着修改排序规则。

验收必须覆盖：同一用户在安装 A 撤销 → 在另一浏览器重新 consent → A 的旧 cookie、
旧 grant 仍被拒绝，而其他用户和安装不受影响。

### 4.3 purge 完成前禁止在途写入复活数据

删除操作先持久化 subject 删除标记和 operation ID，停止该 subject 的新任务、claims、
发布与凭据签发，并使已有执行不能继续提交业务写入。随后按现有 lease/fencing 协议停止
或隔离任务、撤销连接凭据，清理数据库、对象存储、工作区、Sandbox 和索引。

必须拒绝迟到的 Runner 回传、工件上传、工具回调和发布重试；不能只删当前查到的行。
执行中与完成分别记录；失败可从已完成步骤恢复，全部在线清理完成后才 ack。备份保留窗口
和恢复后的再清理单独记录，不能把尚未过期的备份声称为已物理删除。

用户已撤销 consent 或已注销，不妨碍有效安装凭据消费本安装的删除指令；生命周期权限
与使用模型的权限分开。安装已撤销/断开则由本地 purge 或管理员明确承接未完成操作。
断开连接本身不删除业务数据。

### 4.4 入口工作包依赖安装存储

P1a 的外链展示部分已由 `68f0f228`（PR #1390）实现，应直接复用；读取 ACTIVE installation
的接入和完整验收依赖 P2。目标以安装记录为唯一运行时权威，现有 `AGENT_PUBLIC_URL`
通过显式 legacy 导入和兼容窗口退出，不扩展成另一套长期存在的 `enabled + public_url` 配置。

旧代理入口只在明确的 legacy 兼容路径中使用。该路径何时删除由迁移和回滚窗口决定，
不能仅通过修改 `EXPECT_CLOUD_AGENT` 默认值让现有环境静默改变行为。

## 5. 分批交付

| 批次 | 工作包 | 交付与前置 | 验收 |
|---|---|---|---|
| 0 | P0 | 版本化契约、字段 allowlist、错误码、状态与本计划；更新旧规格 | OpenAPI 与正反例通过静态校验；明确标注尚无运行时实现 |
| 1A | P1b 核心部署准备 | 复用已完成的 P1a 外链展示；先盘点并显式保存现有入口模式，解除核心 readiness/release 对 Agent 的依赖；入口安装记录接入等待 P2 | 无 Agent 的隔离部署可登录、推理、升级和回滚；原部署兼容模式可验证 |
| 1B | P4 | 在 Agent 仓库实现 UNBOUND 启动、受保护本地 setup、binding storage/resolver | 本服务依赖齐全即可启动；登录/任务返回 `gateway_unbound`；多 worker 观察绑定版本 |
| 2 | P2 + P3 | Gateway 安装存储、配对、动态 identity client、access、catalog 和安装级 grants | 并发/重放不重复安装；跨安装拒绝；用户重新授权不复活旧 session/grant |
| 3 | P5 + P6 + P1a 接入 | Agent setup CLI/UI、最小 session、runtime Web 配置；删除 profile/角色规则/users fallback；主站读取 ACTIVE installation | Gateway URL + 连接码完成真实登录和任务；安装撤销后模型调用与交互按时停止 |
| 4 | P7 + P8 + P9 | purge、旧绑定导入、独立 artifacts、完整故障/版本矩阵与回滚演练 | 历史 ownership 不变；迟到回传无法复活删除数据；真实两端与 Runner 验证通过 |
| 5 | P10 | staging 后 production 迁移 | 记录真实配置、镜像和数据核对证据；两边可独立升级与恢复 |
| 6 | P11 | 回滚窗口结束后单独收缩 migration 与兼容代码 | 无隐藏 dispatch/profile/env fallback；遗留项如有保留须明确列为未完成 |

第一批代码优先推进 1B：UNBOUND 启动有独立的验收边界，不需要先实现配对协议，也能立即
改善部署体验。1A 的部署盘点与核心健康检查解耦可同步准备。P2/P3 完成后再把入口接到真实
安装记录，避免为赶进度增加第二套配置权威。

## 6. 测试与完成定义

P0 的 schema 校验只证明文档和 fixtures 一致，不证明授权、并发、数据库事务、真实签名或
网络调用正确。不得用 OpenAPI 校验代替后续实现的 provider/consumer contract tests。

后续每批按修改仓库的质量门槛执行。涉及 storage 的实现覆盖项目承诺的 PostgreSQL/D1
路径；可选 integration 未实现或关闭时，基础 Gateway 仍须正常启动。Agent 使用自己的
数据库迁移，不在 Gateway 新建任务表。

运行时验收需要实际启动服务：既覆盖无 Agent 的 Gateway，也覆盖未绑定 Agent；接入批次
需要独立 origin → Gateway 登录 → Agent BFF → grant → 真实推理 → 任务结果的完整往返。
涉及生产迁移时才更新“已部署”状态，并附镜像/配置/schema 和回滚证据。

开源准备由代码、部署私有信息和发布制品的实际检查证明；数据库分开或契约草案合并本身
不等于已经完成开源准备。
