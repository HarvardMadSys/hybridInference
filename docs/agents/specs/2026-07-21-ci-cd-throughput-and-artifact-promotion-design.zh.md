# HybridInference CI/CD 提速与制品晋级 — 设计

**日期：** 2026-07-21

**状态：** 已评审；Phase 0 正确性修复与 Phase 1 workflow 改造实施中

**作者：** HybridInference / FreeInference CI/CD 讨论

**范围：** GitHub Actions CI、Docker 构建、staging/production 部署与回滚

**说明：** 本文定义完整目标架构与迁移路径。首个实施变更只落地不依赖外部平台决策的
Phase 0 正确性修复与 Phase 1 workflow 改造；Phase 3/4 在平台 gate 通过前不得启动。

**修订：** 2026-07-22，根据代码核对评审补充平台架构 gate、端到端指标、可靠性
workaround 迁移约束、frontend 制品等价边界、release retention 与 manifest 持久化语义。

**实施记录：** 2026-07-22，首批代码合并 backend/frontend quality job，修复 pytest
发现范围并引入确定性 LPT 分片、版本化 timing manifest 与稳定 `CI Gate`。branch
protection 迁移仍需在 workflow 合并窗口由维护者同步完成；历史 timing 初始为空，后续
按本文的数据更新流程填充。平台架构矩阵与 registry 决策仍是制品晋级的硬阻塞项。

**相关文档：**

- [HybridInference 中立上游与发行版拆分 — 设计](2026-07-16-hybridinference-neutral-upstream-multi-distribution-design.zh.md)：本文对应其「CI/CD 演进路径」所描述的未来独立阶段，不改变当前发行版边界。
- [部署说明](../../developer/deployment.md)：现有 production/staging 运行拓扑。

## 摘要（Executive Summary）

当前 CI 的常态速度并不差：最近 100 次运行中，PR CI 的 P50 为 2.65 分钟、
P90 为 3.78 分钟。主要问题不是单条 lint 或 test 命令慢，而是：

1. 每个 PR 最多触发 17 个 job；其中 16 个是需要真实环境准备或构建的 job，
   会共同争抢 self-hosted runner。
2. 3 个后端质量检查分别准备 Python 环境，5 个前端检查分别执行 Node setup 和
   `npm ci`，准备成本重复。
3. 四个 pytest shard 按文件路径 CRC32 静态分配，实测单轮 test body 可出现
   19/26/71/90 秒的明显倾斜。
4. PR 无论改动范围如何都会验证 backend、frontend、oncall 三张镜像；镜像
   `push: false`，构建结果不会成为部署制品。
5. staging/production 在目标机 checkout 指定 SHA 后再次 `make build`，因此
   「CI 验证的构建」与「线上实际构建」不是同一个不可变制品。

本设计采取四个互相独立、可逐步上线的改进：

1. 收敛 job fan-out：后端质量检查合成一个 job，前端质量检查合成一个 job。
2. 保留四个 pytest job，但按历史文件耗时均衡分片，并修复
   `distributions/**` 测试未进入当前分片的问题。
3. 用保守的变更分类跳过明确无关的语言检查和镜像；无法分类时一律回退全量。
4. PR 只验证受影响镜像；对可部署 SHA 在 CI 中构建一次目标环境所需的完整 OCI
   制品集并记录 digest；staging/production 按 digest 拉取并启动，不在部署机重新构建。

目标不是把所有 PR 强行压到一分钟，而是让普通 PR 稳定在 1.5–2.2 分钟、P90
低于 3 分钟，消除几十分钟的 runner 排队长尾，并让 CI 通过的应用制品与线上制品
完全一致。

## 背景与现状（Context）

### 当前 PR 流程

~~~mermaid
flowchart LR
    PR["Pull Request"] --> CI["CI workflow"]
    PR --> DB["Docker Build workflow"]

    CI --> PS["Prepare Pytest Shards ×1"]
    CI --> BL["Backend lint ×3"]
    CI --> FE["Frontend checks ×5"]
    CI --> SEC["Security ×1"]
    PS --> PT["Pytest shards ×4"]

    DB --> BI["backend image"]
    DB --> FI["frontend image"]
    DB --> OI["oncall image"]

    BI --> DROP["push=false；结果丢弃"]
    FI --> DROP
    OI --> DROP
~~~

当前 job 数量：

| 分组 | Job | 数量 |
|---|---|---:|
| Pytest 准备 | Prepare Pytest Shards | 1 |
| 后端质量 | Ruff Format、Ruff Lint、Pydocstyle | 3 |
| 前端质量 | Audit、ESLint、Prettier、TypeScript、Tests | 5 |
| 后端测试 | Test shard 1–4 | 4 |
| 安全 | Gitleaks + pip-audit | 1 |
| Docker | backend、frontend、oncall | 3 |
| **合计** |  | **17** |

`Prepare Pytest Shards` 只运行数秒，因此本文把剩余 16 个称为「实质性 job」。
这些 job 并非同时全部运行：pytest 依赖准备 job；但两个 workflow 可以并发，
且大部分 job 共享 self-hosted runner 池，所以会产生资源竞争。

### 当前 CD 流程

~~~mermaid
flowchart LR
    PUSH["dev/main push"] --> CI["CI success"]
    CI --> WR["workflow_run"]
    WR --> SSH["SSH to target host"]
    SSH --> GIT["git fetch + reset to SHA"]
    GIT --> BUILD["make build"]
    BUILD --> HC["backend/frontend health checks"]
~~~

当前 CD 的优点是简单、部署 SHA 明确、已有健康检查与独立 rollback workflow；
但应用镜像在目标机现场构建，存在以下问题：

- CI Docker build 和 CD Docker build 重复；
- 两次构建受基础镜像、包源和构建时间影响，不能证明二进制等价；
- rollback 仍需重新构建历史 SHA，耗时和成功率受当时外部依赖影响；
- 构建需要把私有 RouteWise token 带到每台部署主机。

## 数据基线（2026-07-21）

数据来自 GitHub Actions 最近 100 次已完成 workflow run，耗时按
`updatedAt - createdAt` 计算，因此同时包含 runner 排队与实际执行时间。

| Workflow / Event | 样本 | P50 | P90 | 备注 |
|---|---:|---:|---:|---|
| CI / pull_request | 68 | 2.65 min | 3.78 min | 有两个 30–42 分钟 runner 排队长尾 |
| CI / push | 27 | 3.40 min | 4.23 min | 包含 coverage 与 Codecov 上传 |
| Docker Build / PR | 100 | 1.88 min | 4.32 min | 三张镜像均构建、不推送 |
| Deploy Staging | 100 | 0.88 min | 1.23 min | 有一个等待约 6 小时后 skipped 的并发队列样本 |
| Deploy Production | 100 | 1.50 min | 2.57 min | 包含手动 dispatch 与 workflow_run |

典型运行还显示：

- 单次 PR 的 Docker build body 约为 oncall 22 秒、backend 29 秒、frontend 54 秒；
- 前端 job 中 Setup Node + `npm ci` 往往占 30–50 秒，而实际格式、类型或 lint
  命令只占其中一小部分；
- 某次 PR 从创建到结束用了 42.15 分钟，最后一个 pytest job 等到第 40 分钟才
  开始，job 启动后 70 秒结束，test body 本身只有 17 秒；
- 某次 push 的四个 coverage shard test body 分别为 19、26、71、90 秒。

因此，本设计优先降低 job 数与排队长尾，再优化具体命令。

## 目标（Goals）

### 正确性目标

1. 不减少本次改动可能影响的测试、lint、类型检查和安全扫描。
2. 任何无法可靠分类的路径必须触发全量检查，不允许默认跳过。
3. `tests/**` 与 pytest 配置声明的 `distributions/**` 测试必须恰好进入一个 shard。
4. branch protection 只依赖始终出现的汇总 gate，不依赖可能被条件跳过的 job。
5. staging/production 使用 CI 为目标 commit 产生的不可变 image digest。
6. 部署日志能同时回答 commit SHA、image digest、配置 revision 和 GitHub run ID。
7. 保留现有健康检查、production environment gate 和 rollback 能力。
8. CI/CD 重构不得顺手删除现有 checkout、uv cache、Buildx、staging ownership 等
   可靠性 workaround；每一项只能在独立变更中用数据证明不再需要后移除。
9. push coverage 在重分片后仍生成互不覆盖的 shard XML，并保持 Codecov 合并语义。

### 性能目标

以下目标以两周滚动窗口统计；P50/P90 均包含队列时间。PR 验证、可部署 push 的
制品发布、CD 部署步骤和 push→staging healthy 端到端时间必须分别统计，不能把
「原本不构建镜像的 push CI」与「未来包含 build/push 的制品流水线」直接比较。

| 场景 | 当前基线 | 目标 |
|---|---:|---:|
| 全量 PR CI P50 | 2.65 min | ≤ 2.2 min |
| 全量 PR CI P90 | 3.78 min | ≤ 3.0 min |
| 纯前端 PR P50 | 未分类 | ≤ 1.5 min |
| 纯后端 PR P50 | 未分类 | ≤ 2.0 min |
| Phase 1 dev/main 验证 CI P50（尚未发布镜像） | 3.40 min | ≤ 2.9 min |
| Phase 3 可部署 push：验证 + build + push + manifest | 当前不存在 | Phase 3 pilot 建立基线；不能以隐藏上传时间换取达标 |
| dev push → staging healthy 端到端 P50/P90 | 未按同 SHA 关联统计 | Phase 0 建立基线；Phase 3 P50 不回退、P90 至少改善 10% |
| Staging deploy P90 | 1.23 min | ≤ 1.0 min |
| Production deploy P90 | 2.57 min | ≤ 1.5 min |
| 单个 CI job 排队 P95 | 未单独统计 | ≤ 60 sec |

Phase 3 的 registry pilot 需要分别报告 build body、cache restore、layer upload、manifest
聚合和 gate 等待时间。若 push workflow 变长、但同 SHA 的 push→staging healthy 端到端
时间下降且制品可复现，则属于成功；反之不能用「部署步骤变快」掩盖总交付时间回退。

### 资源目标

1. 全量 PR 的实质性 job 从 16 降到 10；加上轻量的 change classifier 与最终 gate，
   workflow 总 job 数约为 12（可部署 push 若单独聚合 manifest，则约为 13）。
2. 纯前端 PR 的主要 job 控制在 3 个左右：frontend quality、security、frontend image。
3. 纯后端 PR 只运行 backend quality、四个 pytest shard、security 和受影响镜像。
4. 同一个可部署 SHA、平台和环境变体只构建一次应用镜像；第一版不跨 release SHA
   复用旧 digest，以保证 manifest 完整且容易审计。

## 非目标（Non-goals）

- 不迁移到 Kubernetes、Argo CD 或其他部署平台。
- 不替换 GitHub Actions 或现有 self-hosted runner 基础设施。
- 不通过减少测试覆盖、关闭 coverage、弱化安全扫描来换取速度。
- 不在第一阶段引入 test impact analysis；pytest 仍执行相应组件的完整测试集。
- 不让 production deploy 在 SSH/Compose 操作中途被自动取消。
- 不把数据库、Postgres 或 host-mounted `config/` 打包进应用镜像。
- 不在本设计中完成仓库拆分；只保证未来发行版拆分可复用同一制品协议。
- 不承诺 frontend staging 与 production 立即复用同一 digest；当前 build-time
  `NEXT_PUBLIC_*` 配置需要先按下文处理。

## 设计原则（Design Principles）

### 1. 先去掉重复准备，再决定是否减少并发

Ruff、Prettier 等命令本身很快；真正重复的是 checkout、setup runtime、恢复缓存和
安装依赖。把相关命令放到同一个 job 内，仍可用独立 step 保留清晰失败信息。

### 2. 保守跳过，未知即全量

路径过滤是一项性能优化，不是新的依赖真值。规则遗漏可能产生 false green，因此
change classifier 失败、base SHA 不可用或遇到未知顶层路径时，必须输出 `full=true`。

### 3. 测试总量不变，只均衡工作量

四个 pytest shard 继续存在；变化仅是从路径哈希切换为历史耗时均衡。新文件、缺失
数据和异常数据均使用保守默认权重。

### 4. Build once，promote by digest

部署单位从「仓库 SHA + 在目标机重新 build」变成「仓库 SHA + release manifest +
不可变 OCI digest」。tag 用于查找，digest 才是实际部署标识。

### 5. 优化必须可观测、可回退

每个阶段先收集数据、再启用跳过或新部署路径。旧的 source-build 路径在 staging
观察完成前保留为显式应急入口，但 production 不得在制品缺失时静默回退现场构建。

## 迁移可靠性不变量（Migration Reliability Invariants）

当前 workflow 中有多项针对真实故障加入的自愈逻辑。合并 job、抽取 reusable workflow
或移动 Docker build 时，以下行为必须逐项保留；代码变少不能作为删除理由。

| 现有行为 | 所防止的故障 | 迁移要求 |
|---|---|---|
| CI/Docker checkout 使用 `fetch-depth: 0` | shallow pack 出现 unresolved delta / missing blob | 原样保留；若要恢复 shallow clone，必须独立压测并证明故障不再复现 |
| 每个 Python job 使用独立 `UV_CACHE_DIR`，关闭并发 prune | self-hosted job 共用 uv cache 时出现 hardlink ENOENT | 合并后的每个 Python job 仍使用独立目录；不能重新共用可变 cache |
| `uv sync` 有三次有界重试 | 残余安装期 cache/网络瞬时故障 | 保留重试、退避和最终明确失败 |
| Docker 首次 build 允许失败后重建 Buildx builder 再试一次 | BuildKit `graceful_stop` / gRPC transport 中断 | reusable 化后仍创建 fresh builder，并复用 GHA cache 续建 |
| staging 部署前 best-effort `chown` ownership recovery | 目标 checkout 被 root-owned 文件永久卡住 | pull-digest 部署仍保留 checkout ownership recovery |
| SSH known_hosts 清洗、严格 host key 检查 | 错误 secret 或主机身份被静默接受 | staging、production、rollback 均不得弱化 |
| 部署 SHA branch ancestry 校验 | 手动 dispatch 把非 dev/main SHA 部署到错误环境 | manifest 校验不能取代 source branch 校验，两者都要保留 |
| backend/frontend health checks 与失败诊断 | Compose 已启动但应用不可用 | digest 部署和 rollback 后都必须执行 |

此外，顶层 workflow 的 `name: CI` 是现有 staging/production
`workflow_run: workflows: ["CI"]` 的触发契约。可以重命名 job 或把内部逻辑抽成 reusable
workflow，但不得只修改顶层 workflow 名称。若确需改名，两个 deploy workflow 必须在
同一变更中更新，并用一次受控 push 验证自动部署触发，防止 CD 静默失联。

每个 CI/CD 重构 PR 都应附一份逐项迁移 checklist，说明以上行为位于新流程的哪个 step；
未列出或未说明去向即不应合并。

## 目标架构（Target Architecture）

~~~mermaid
flowchart LR
    CHANGE["PR / push"] --> CLASSIFY["Classify Changes"]

    CLASSIFY -->|backend/full| BQ["Backend Quality ×1"]
    CLASSIFY -->|frontend/full| FQ["Frontend Quality ×1"]
    CLASSIFY --> PT["Balanced Pytest ×4"]
    CLASSIFY --> SEC["Security ×1"]

    BQ --> GATE["CI Gate"]
    FQ --> GATE
    PT --> GATE
    SEC --> GATE

    CLASSIFY --> IMG["Affected Image Matrix"]
    IMG --> BUILD["Build affected images"]
    BUILD -->|PR| VERIFY["build only"]
    BUILD -->|dev/main push| REG["Push SHA tags + capture digests"]
    VERIFY --> GATE
    REG --> MANIFEST["Release Manifest"]
    MANIFEST --> GATE

    GATE -->|dev success| STG["Staging: pull digest + up --no-build"]
    GATE -->|main success + approval| PROD["Production: pull digest + up --no-build"]
~~~

## 详细设计

### 1. Change Classifier

在 `ci.yml` 增加一个始终执行的 `changes` job，输出以下布尔值：

- `backend`
- `frontend`
- `oncall`
- `status_monitor`
- `docker_shared`
- `security_only`
- `full`

可以使用固定 SHA pin 的 paths-filter action，也可以使用仓库内脚本解析 base/head diff；
无论使用哪种方式，都必须：

1. PR 比较 `base.sha...head.sha`；push 比较 `before...sha`。
2. `before` 为全零、base 不可达或 diff 命令失败时输出 `full=true`。
3. 对重命名同时考虑 old path 与 new path。
4. 将分类结果和命中文件写入 GitHub job summary，便于审计。
5. 未命中已知规则的非文档路径输出 `full=true`。

初始规则如下：

| 分类 | 明确命中路径 | 触发内容 |
|---|---|---|
| frontend | `apps/frontend/**` | frontend quality、frontend image |
| backend | `apps/backend/**`、`tests/**` | backend quality、pytest、相关镜像 |
| oncall | `apps/backend/serving/oncall/**`、`Dockerfile.oncall` | oncall image；Python 测试仍归 backend |
| status_monitor | `services/status-monitor-worker/**` | 现有 Worker workflow；主 CI 仅保留 security/gate |
| docker_shared | `.dockerignore`、`deploy/docker/docker-compose.yml` | 所有受 Compose 影响的 image build |
| full | `pyproject.toml`、`uv.lock`、`README.md`、`Makefile`、`.github/workflows/**`、`config/**`、`distributions/**`、未知顶层路径 | 全量 CI 与完整镜像矩阵 |
| docs-only | `docs/**`、除根 `README.md` 外的 `**/*.md`、`LICENSE` | 只运行 classifier、security 和最终 gate；应用检查跳过 |

规则刻意偏保守。例如 `pyproject.toml` 同时影响 backend 与 oncall Docker 依赖，即使
不影响 frontend，也先归入 `full`；积累变更分布数据后再细化。

目标 workflow 不再依靠顶层 `paths-ignore` 跳过 required workflow；它应在所有 PR 上
创建稳定的 `CI Gate`，再在 job 内根据 classifier 跳过应用检查。这样 docs-only PR 不会
因为 required check 根本没有创建而永久 pending。根 `README.md` 是 backend/oncall
Dockerfile 的显式 `COPY` 输入，因此第一版不把它当作普通 docs-only 处理。

#### Required check 稳定性

所有条件 job 汇总到一个始终运行的 `CI Gate`：

- `if: always()`；
- 检查每个 required dependency 的 result；
- `success` 和因分类导致的 `skipped` 可通过；
- `failure`、`cancelled` 或缺失 release manifest 必须失败。

branch protection 迁移为只 require `CI Gate`。更新保护规则与 workflow 合并必须在
同一维护窗口完成，避免旧 check name 消失后 PR 永久 pending。

### 2. 收敛语言质量检查

#### Backend Quality

把现有三个 matrix job 合并为一个 job，一次完成 checkout、uv setup、Python setup、
`.venv` restore 和 `uv sync`，随后用三个独立 step 执行：

1. `ruff format --check .`
2. `ruff check --no-fix .`
3. `pydocstyle ...`

这样仍能准确显示失败步骤，但只占一个 runner slot，并去掉两次环境准备。job 失败时
不要求继续运行后续 lint；开发者修复第一处确定性失败即可。

#### Frontend Quality

把现有五个 matrix job 合并为一个 job，一次 `npm ci`，随后依次执行：

1. `npm audit --omit=dev --audit-level=high`
2. `npm run format:check`
3. `npm run lint`
4. `npm run type-check`
5. `npm run test`

第一版保持单 job，优先减少 runner 争抢。若两周数据证明 frontend test body 成为新的
关键路径，可再拆成 `frontend-static` 与 `frontend-test` 两个 job，但不得重新回到五次
`npm ci`。不使用 `npm ci || npm install`；lockfile 不一致必须确定性失败。

### 3. Pytest 耗时均衡分片

#### 当前问题

当前脚本只遍历 `tests/**/test_*.py`，再按
`crc32(path) % shard_count` 分配。这同时造成：

- 文件数量接近不代表运行耗时接近；
- `pyproject.toml` 中声明的 `distributions` testpath 不在枚举范围内；
- 新增少量慢文件后可能长期集中在同一个 shard。

#### 目标算法

新增仓库内脚本，例如 `ops/ci/partition_pytest_files.py`：

1. 从 pytest 配置的 test roots 枚举全部 `test_*.py`；第一版明确包含
   `tests/` 和 `distributions/`。
2. 读取版本化的 `ops/ci/pytest-file-durations.json`。
3. 缺少历史数据的新文件使用已有文件 P75 作为默认权重，防止全部新文件进入同组。
4. 按耗时从大到小排序，每次把文件放入当前累计耗时最小的 shard（LPT greedy）。
5. 输出四个确定性的文件列表及估算总耗时。
6. 校验所有文件恰好出现一次；重复、遗漏或空列表异常均使 job 失败。

四个 pytest job 使用静态 matrix `[1, 2, 3, 4]`，各自在 runner 内调用同一脚本获取
自己的文件列表，因此不再需要独立的 `Prepare Pytest Shards` job。

#### 耗时数据更新

每个 push CI 记录按文件聚合的真实 pytest duration，四个 shard 结束后生成候选
timing 文件。为避免每次 push 自动提交产生噪声：

- 候选文件作为 artifact 保存 30 天；
- 每周或测试结构明显变化时，由维护者运行更新 workflow 创建小型 PR；
- timing 文件只影响性能，不影响测试选择，新文件永远会被执行；
- 无 timing 文件或 JSON 损坏时回退到统一权重分片，不能跳过测试。

是否限制 `pytest -n auto` 必须在确认 self-hosted runner 拓扑后决定。如果一台物理机
运行多个 runner service，应设置显式 worker 上限避免 CPU 过量订阅；如果每台机器只
运行一个 job，保留 `-n auto` 通常更合理。该决定以 queue、CPU 和 test body 指标为准，
不在设计中猜测固定数字。

### 4. 受影响镜像矩阵

Docker build 合并进主 CI 或抽成由主 CI 调用的 reusable workflow，使 change outputs、
concurrency cancellation 和最终 `CI Gate` 只有一套真值。

镜像影响规则来自 Dockerfile 的实际 `COPY` 与构建依赖：

| Image | 至少在以下路径变化时构建 |
|---|---|
| backend | `apps/backend/serving/**`、`apps/backend/routing/**`、`config/**`、`pyproject.toml`、`uv.lock`、`README.md`、`Dockerfile.backend` |
| frontend | `apps/frontend/**`、`Dockerfile.frontend`、影响其 build args 的 Compose/发行版配置 |
| oncall | `apps/backend/serving/**`、`pyproject.toml`、`uv.lock`、`README.md`、`Dockerfile.oncall` |

`.dockerignore`、Buildx 配置或通用 Compose 结构变化时保守构建全部镜像。PR 只验证受
影响镜像，不推送 PR 来源代码。

可部署的 `dev`/`main` push 使用不同规则：第一版为该环境生成一份完整 release
manifest，并构建 manifest 所需的全部应用镜像。这样不会出现「backend 更新了，但新
release manifest 没有 frontend digest」或把旧 frontend 内嵌的 build SHA 误当作新版本。
只有 push image job 具有 `packages: write`。未来若 build metadata 和公开站点配置均已
runtime 化，manifest schema 也能记录每个组件自己的 `source_sha`，才可以在不同 release
SHA 之间安全复用未变化组件的旧 digest。

继续使用 BuildKit cache，但不把 `mode=min` 改成 `mode=max` 作为默认优化。当前镜像
通常 20–60 秒完成，`mode=max` 可能增加远程 cache 上传；必须先用 cache hit 与传输耗时
证明收益。

### 5. OCI 制品与 Release Manifest

#### 镜像命名

可部署 push 为每张受影响镜像写入不可变 commit tag，并记录最终 digest，例如：

~~~text
ghcr.io/harvardmadsys/hybridinference-backend:<git-sha>
ghcr.io/harvardmadsys/hybridinference-frontend:<git-sha>-staging
ghcr.io/harvardmadsys/hybridinference-frontend:<git-sha>-production
ghcr.io/harvardmadsys/hybridinference-oncall:<git-sha>
~~~

部署只使用 `name@sha256:...`，不使用可移动 tag。若目标 registry 最终不是 GHCR，
命名可替换，但 manifest 协议不变。

#### Release Manifest

每个可部署 SHA 产生机器可读 JSON artifact：

~~~json
{
  "schema_version": 1,
  "commit_sha": "<40-char sha>",
  "source_run_id": "<github run id>",
  "images": {
    "backend": {
      "ref": "ghcr.io/.../backend@sha256:...",
      "source_sha": "<40-char sha>",
      "platform": "linux/amd64"
    },
    "frontend_staging": {
      "ref": "ghcr.io/.../frontend@sha256:...",
      "source_sha": "<40-char sha>",
      "platform": "linux/amd64"
    }
  },
  "config_revision": "<git sha>",
  "built_at": "<RFC3339 timestamp>"
}
~~~

manifest 必须经过 schema 校验，并作为 GitHub artifact 保存；目标环境要求的镜像 key
由 schema 明确定义，不能依赖「上一版可能还在」。production release 可再把 manifest
附加到 GitHub Release。CD 若找不到目标 SHA 的完整 manifest 必须 fail closed。

#### 跨 Workflow 取用与持久化

staging/production CD 由 `workflow_run` 触发，manifest 必须从**触发本次 CD 的 CI run**
读取，而不是从 CD 自己的 run 或「最新一次 CI」模糊查询：

1. deploy workflow 显式声明 `actions: read`；
2. 使用 `github.event.workflow_run.id` 定位 producer run；
3. 下载固定 artifact name，并校验 producer workflow 名称、conclusion、head branch、
   manifest `commit_sha` 与 `workflow_run.head_sha`；
4. 同一 run 出现多个同名 manifest、artifact 缺失、过期或下载失败时一律 fail closed；
5. 不允许回退为查找 branch 上「最近 manifest」，避免并发 push 部署错版本。

Actions artifact 是传递机制，不是长期发布真值：

- staging 的即时自动部署可以直接读取触发 run 的 artifact；
- main 的 production manifest 必须附加到对应 GitHub Release，或以等价的持久方式保存
  在 registry，并与 release tag/source SHA 绑定；
- manual redeploy / rollback 历史 release 不依赖可能已经过期的 Actions artifact；
- 目标机的 last-known-good manifest 是网络故障时的第一回滚来源，读取它不需要 GitHub
  artifact 服务可用；
- artifact retention 与 OCI image retention 是两个独立策略，前者到期不能导致后者被清理。

#### Frontend 环境差异

当前 frontend Dockerfile 把大量 `NEXT_PUBLIC_*` 作为 build args 烘焙到 Next.js bundle，
staging 与 production 因此不能天然共享同一 digest。迁移分两步：

1. 第一阶段由 `dev` push 构建 staging variant、由 `main` push 构建 production variant，
   对应 manifest 分别记录 digest；部署仍然不在目标机重新 build。
2. 后续把真正环境相关、可公开的站点配置迁移到 runtime `/site-config` 或启动时生成的
   config 文件。完成后两环境可以推广同一个 frontend digest。

第一阶段的等价保证边界必须明确：

- `dev` CI 验证并部署的是 staging variant 的同一 digest；
- `sync-main.yml` fast-forward 后，`main` CI 会从同一 source SHA **重新构建并验证**
  production variant，再把该 production digest 交给 production CD；
- 因 build args 不同，staging 与 production frontend digest 预期不同，不能声称
  「staging 验证过的二进制原样晋级 production」；第一阶段保证的是同 source SHA、
  同 Dockerfile 和各环境内 build-once，而不是跨环境 binary promotion；
- 只有完成 runtime config 迁移后，才可以把同一 frontend digest 从 staging 原样晋级
  production。

`NEXT_PUBLIC_BUILD_TIMESTAMP` 不得使用每次 workflow 的当前时间参与不可变产物标识，
否则同一 SHA 重跑会产生不同 digest。应使用 commit timestamp、固定
`SOURCE_DATE_EPOCH`，或把展示时间移出 bundle。

#### 平台架构

这是 Phase 0 的阻塞 gate，不只是 Phase 3 前的普通待确认问题。当前 Docker workflow
注释声称使用 ARM64 self-hosted pool，但 job label 只有 `[self-hosted, Linux]`，label 本身
不能证明实际 architecture；staging/production 架构也没有被 workflow 记录。

Phase 0 必须为 CI builder、staging 和 production 记录：runner name/labels、`uname -m`、
Docker server architecture，以及目标 image platform，并在设计附录或运维配置中确认：

- 若三者同架构，只发布 native platform，速度优先；
- 若不同，使用对应架构的 native runner 生成 multi-arch manifest；
- 不默认使用 QEMU 模拟同时构建 amd64/arm64，因为它可能显著增加构建时间。

在平台矩阵和 native builder 方案没有确认前，Phase 3/4 不得启动。若只能使用 QEMU，
必须先单独记录 build/push P50/P90、镜像启动验证和端到端交付时间，由维护者明确接受
性能成本；不能在实现中临时打开 emulation 后继续宣称满足原性能目标。

### 6. 按 Digest 部署

目标机仍 checkout 目标 SHA，以取得 Compose、host-mounted `config/`、distribution
overlay 和部署脚本；变化仅是应用容器不再现场构建。

Compose 为应用服务同时声明可覆盖的 `image:`：

~~~yaml
backend:
  image: ${BACKEND_IMAGE:-hybridinference-backend:local}
  build: ...

frontend:
  image: ${FRONTEND_IMAGE:-hybridinference-frontend:local}
  build: ...
~~~

本地开发继续 `make build`；CD 从 release manifest 注入完整 digest，并执行：

~~~text
docker compose pull backend frontend
docker compose up -d --no-build backend frontend
~~~

staging 与 production workflow 必须验证：

1. workflow_run 来源事件是 `push`，避免手动重跑普通 CI 意外触发自动部署；
2. manifest commit SHA 与 `workflow_run.head_sha` 完全一致；
3. digest 格式与允许的 registry/repository 完全匹配；
4. 镜像可拉取后才修改运行中服务；
5. 部署日志打印旧/新 digest、配置 SHA 和 run ID；
6. backend/frontend 健康检查通过后才标记 deployment success。

部署机登录 registry 所使用的 pull credential 必须是只读、短期或可轮换 credential；
manifest 下载 token 与 registry pull token 分开授权，避免为了读 Actions artifact 给远端
主机 `packages: write`。

staging 保持 `cancel-in-progress: false`，不在远程操作中途取消。GitHub concurrency
可以替换尚未开始的 pending run，但正在执行的 SSH 部署必须完成。production 继续串行，
并由 GitHub Environment 控制审批策略。

### 7. 回滚

每次成功部署在目标机保存一个小型 last-known-good manifest，只包含 SHA、digest 和
配置 revision，不包含 secret。健康检查失败时：

- staging 可以自动切回上一份 manifest，然后把本次部署标记失败；
- production 第一阶段保持手动 rollback workflow；验证成熟后再讨论自动回滚；
- rollback 只拉取历史 digest 并 `up --no-build`，不重新解析历史依赖或构建镜像；
- 若历史 digest 已被 registry retention 删除，rollback 必须明确失败，不允许静默
  source build。

Registry retention 按 release 引用的 tag/digest 集合执行，不只按镜像年龄执行：

1. 当前 production、last-known-good 与任何 GitHub Release manifest 引用的 digest
   永不进入自动清理候选；
2. 默认至少保留最近 20 个 production release，并保留所有 90 天内的 production
   release（两者取并集；维护者可以评审后调整）；
3. 未被 release 引用的 PR/cache/临时 tag 可以按更短周期清理；
4. cleanup job 运行前从 GitHub Release manifest 构建 protect set，删除后再次验证当前与
   last-known-good digest 可拉取；
5. 若 registry 原生 retention 无法理解 GitHub Release 引用关系，必须用仓库脚本实现
   allowlist，不能假设「有 SHA tag」就天然不会被删除。

production 发布与定期运维检查都必须验证回滚窗口内的受保护 digest 仍可拉取。设计的
fail-closed 语义要求 retention 本身成为受测试的发布能力，而不是运维口头约定。

## 安全与权限（Security）

1. PR（尤其 fork PR）只能 build，不具有 `packages: write`、部署 secret 或私有长期 token。
2. push image job 使用最小权限：`contents: read`、`packages: write`；部署 workflow 只需
   `contents: read`、`actions: read`（读取 manifest artifact）和 registry pull credential。
3. 私有 RouteWise token 只作为 BuildKit secret 使用，不能进入 layer、build arg、日志
   或 release manifest。
4. 所有第三方 Action 固定到已评审版本或 commit SHA；新引入 action 必须经过供应链评审。
5. manifest 中的 repository 必须 allowlist，防止把用户输入转换为任意镜像拉取。
6. 可选增加 GitHub artifact attestation / cosign；这不阻塞第一阶段提速，但 production
   后续可以要求验证 provenance。
7. Security Scan 始终运行；路径过滤不能跳过 gitleaks。pip-audit 在 root lockfile 未变化
   时仍保留运行，第一阶段不引入安全扫描缓存语义。

未来可以评估按 `uv.lock` digest 复用 pip-audit 结果，但必须保证漏洞数据库新鲜度有独立
失效上限、cache miss 时 fail closed，且 docs-only 优化不能跳过 gitleaks；这不属于
Phase 0–2 的首轮改造。

## 可观测性（Observability）

每个 workflow 在 Job Summary 输出：

- change classification 与命中文件；
- 各 job 的 queued、setup、command、post-action 耗时；
- cache restore 是否命中以及传输耗时；
- 四个 pytest shard 的文件数、估算耗时、真实耗时；
- 构建/跳过的镜像与最终 digest；
- 部署前后 SHA/digest、pull、restart、health-check 分段耗时。

每周汇总最近两周：

- PR CI P50/P90/P95；
- push CI P50/P90；
- 单 job queue P50/P95；
- runner-minute / PR；
- frontend-only、backend-only、full 的占比；
- Docker cache hit 与 image build P90；
- staging/production deploy P50/P90；
- change classifier fallback-to-full 次数；
- 因漏分类导致的补跑或回滚次数（目标为 0）。

若平均时间改善但 P90/queue 恶化，则优化不算成功。

## 失败处理与保守回退

| 故障 | 行为 |
|---|---|
| 无法计算 diff | `full=true`，运行全部检查与镜像 |
| timing JSON 缺失/损坏 | 所有测试按统一权重分片，不跳过文件 |
| shard 出现重复或遗漏 | CI 失败 |
| npm/uv lockfile 不一致 | CI 失败，不 fallback 到非锁定安装 |
| Docker cache 不可用 | 无缓存构建；不影响正确性 |
| builder platform 与目标 platform 不匹配 | 不生成 release manifest，制品发布失败；禁止留给部署机运行时才发现 |
| image push 部分成功 | 不生成完整 manifest，CI Gate 失败 |
| manifest 缺失或 SHA 不匹配 | CD 失败，不现场 build |
| 触发 CI run 的 manifest artifact 下载失败/过期/重名 | CD fail closed；不得查询其他 run 代替 |
| registry pull 失败 | 保留当前运行版本，部署失败 |
| 受 release 保护的历史 digest 不可拉取 | 阻止 cleanup 或新 production release，并告警修复 retention |
| 新版本健康检查失败 | staging 自动恢复 last-known-good；production 走 rollback workflow |
| workflow 中途取消 | 不启动新的 CD；正在执行的生产 SSH 不自动取消 |

## 迁移计划（Rollout）

### Phase 0：补正确性与采集数据

1. 修复 pytest 文件发现范围，覆盖 `tests/` 与 `distributions/`。
2. 增加 shard 完整性校验。
3. change classifier 先以 shadow mode 输出结果，不跳过任何 job。
4. 在 CI builder、staging、production 实际执行并记录 `uname -m`、Docker server
   architecture、runner labels 与 runner-per-host 拓扑，确认 native/multi-arch 构建方案。
5. 按同一个 source SHA 关联 push CI 创建时间、manifest-ready（当前阶段记为
   CI-success）、deploy workflow 开始和 staging healthy 时间，建立端到端 P50/P90。
6. 把「迁移可靠性不变量」逐项映射到当前 workflow step，形成后续 PR checklist。
7. 观察至少一周或 30 个有代表性的 PR。

**退出条件：** 分类结果人工抽查无误；测试文件无遗漏；已有可靠 queue、shard 和
push→staging healthy 数据；平台矩阵已有书面结论。Phase 1/2 的纯 CI 优化可以与平台
方案决策并行，但平台矩阵和 native builder 路径未确认时，Phase 3/4 制品晋级不得启动。

### Phase 1：收敛 job 与均衡测试

1. 合并三个 backend quality job。
2. 合并五个 frontend quality job。
3. 删除独立 Prepare Pytest Shards job，启用本地分片脚本。
4. 引入 timing manifest 与 LPT 均衡分片。
5. 增加 `CI Gate` 并迁移 branch protection。

**退出条件：** 全量 PR 的功能 gate 等价；CI P90 连续两周不高于 3 分钟；无新增 flaky。

### Phase 2：启用保守路径过滤与受影响镜像矩阵

1. 对 pure frontend、pure backend、status-monitor 三类启用条件 job。
2. unknown/shared 继续全量。
3. Docker Build 纳入主 CI 的最终 gate，只构建受影响镜像。
4. 观察 skipped job 与后续失败/补跑之间是否存在相关性。

**退出条件：** 至少 50 个 PR 无误跳；纯前端/纯后端性能目标达到；全量 fallback 可用。

### Phase 3：发布 OCI 制品并切换 Staging

1. 确认 Phase 0 平台 gate 已通过，配置 native builder 或已评审的 multi-arch builder。
2. 配置 registry、最小权限与按 release protect-set 的 retention。
3. 发布 commit-tagged images 和 release manifest。
4. 用 registry pilot 记录 build、layer upload、manifest-ready 和 push→staging healthy
   增量；不得只报告 staging deploy step。
5. Compose 增加 `image:` 覆盖，同时保留本地 build 能力。
6. staging 使用 `workflow_run.id` 下载精确 manifest，改为 pull digest + `up --no-build`。
7. 演练 artifact 下载失败、registry 故障、健康失败和 last-known-good 本地回滚。

**退出条件：** staging 连续 7 天稳定；至少完成一次前滚和一次不依赖 Actions artifact
的回滚演练；部署 P90 ≤ 60 秒；push→staging healthy P50 不高于 Phase 0 基线、P90 至少
改善 10%；新增 build/push 耗时已单独公开。

### Phase 4：切换 Production

1. production 读取 main SHA 的 release manifest。
2. 首次切换保留人工审批和人工 rollback。
3. 将 production manifest 附加到 GitHub Release，并验证 release protect-set retention。
4. 验证线上 `/health`、frontend、关键 API、SSE 与管理面。
5. 观察完整发布窗口后，移除 production 默认 source-build；应急 build 只能显式手动触发。

**退出条件：** 线上 digest 与 release manifest 一致；production P90 ≤ 90 秒；回滚演练通过。

## 验收测试（Acceptance Tests）

### CI 分类矩阵

至少构造以下 PR fixture 或临时分支：

| 改动 | 必须运行 | 必须跳过 |
|---|---|---|
| 纯 `apps/frontend/src/**` | frontend quality、security、frontend image、CI Gate | backend quality、pytest、backend/oncall image |
| 纯 backend routing | backend quality、pytest、security、backend image、CI Gate | frontend quality/frontend image |
| serving 通用代码 | backend quality、pytest、security、backend + oncall images | frontend quality（若无前端改动） |
| `pyproject.toml` | full | 无 |
| 未知新顶层目录 | full | 无 |
| docs-only | classifier、security、CI Gate | 应用检查与应用镜像 |
| classifier 故障 | full | 无 |

### 测试分片

1. 枚举集合等于四个 shard 的并集。
2. 任意两个 shard 交集为空。
3. `distributions/**/test_*.py` 被包含。
4. 新增未知 duration 的慢测试仍被执行。
5. timing JSON 损坏时回退并执行完整集合。
6. 在历史数据上估算最大 shard / 平均 shard ≤ 1.25；真实运行目标 ≤ 1.5。
7. push coverage 模式下四个 shard 继续生成唯一的 `coverage-<shard>.xml`，四个 Codecov
   upload/flag 全部成功或按现有 fail-open 策略明确报告，不能因重分片丢失 coverage。

### 平台与迁移不变量

1. CI builder、staging、production 的实际 architecture 被记录，目标 image manifest
   包含对应 platform；故意提供错误平台镜像时发布在 CD 前失败。
2. 合并 backend/frontend job 后，full checkout、隔离 uv cache、uv retry 等行为仍存在。
3. Docker reusable/matrix 化后，模拟首次 `graceful_stop` 会创建 fresh builder 并重试。
4. staging digest 部署仍执行 ownership recovery、strict host key、branch ancestry 和健康检查。
5. 顶层 workflow 继续名为 `CI`；若改名，测试必须证明 staging/production
   `workflow_run` 在同一变更后仍被受控 push 触发。

### 制品与部署

1. PR 无法推送 registry。
2. dev push 产生带正确 SHA 的 staging manifest。
3. main push 产生 production 可用 manifest。
4. CD 拒绝 tag-only、错误 SHA、错误 repository 和 digest 缺失。
5. staging 日志中的运行 digest 与 manifest 完全相同。
6. registry 暂时不可达时旧版本保持运行。
7. 新版本 health check 失败时 staging 恢复上一 digest。
8. production rollback 不触发 Docker build。
9. frontend staging/production variant 展示正确环境与 build SHA。
10. dev/staging 与 main/production frontend digest 不同被视为第一阶段预期行为；main CI
    单独验证 production variant，文档和日志不声称跨环境 binary promotion。
11. deploy workflow 只接受 `github.event.workflow_run.id` 对应的 manifest；下载失败、
    artifact 过期、同名重复或 SHA 不匹配均 fail closed。
12. 删除或屏蔽 Actions artifact 后，目标机仍可用本地 last-known-good manifest 回滚。
13. GitHub Release 引用的 digest、当前 production 和 last-known-good 不会被 cleanup；
    retention 演练后这些 digest 仍可拉取。

## 风险与缓解

### 风险 1：Builder 与部署目标架构不一致，digest 无法运行

这会直接破坏 promote-by-digest 的前提，而不只是让 CI 变慢。缓解：把实际平台盘点与
native builder 方案设为 Phase 0 硬 gate；image platform 写入 manifest；发布前做目标
架构验证。若必须 QEMU，先独立测量并由维护者接受成本，Phase 3 才能启动。

### 风险 2：路径规则漏依赖，产生 false green

缓解：shadow mode、unknown→full、单一 CI Gate、规则 fixture、对共享依赖保持全量；
任何误判后立即扩大规则，而不是继续增加例外。

### 风险 3：重构时丢失现有可靠性 workaround

合并 job 或 reusable 化很容易把 full checkout、uv cache 隔离/重试、Buildx fresh-builder
重试、staging ownership recovery 等视为样板代码删除。缓解：维护「迁移可靠性不变量」
清单，每个 PR 逐项映射并验收；移除任何 workaround 必须是有复现数据的独立决策。

### 风险 4：Push CI 指标口径掩盖新增 build/push 成本

当前 push 不构建镜像，未来 push 会新增 layer build/upload。缓解：把 PR verification、
deployable push、CD step 和 push→staging healthy 分开计时，以同 SHA 端到端指标作为最终
判断；不允许只展示变快的 deploy step。

### 风险 5：合并 job 后失去并行速度

合并会让同一语言的命令串行，但去掉重复 setup 并释放 runner slot。以整个 workflow
P90 和 queue P95 判断，而不是只看单个 job。如果独占 runner 充足且 frontend 成为关键
路径，最多拆回两个 job，不恢复五个独立安装。

### 风险 6：timing 数据陈旧

timing 只决定分配，不决定选择。新文件使用 P75，且 CI 输出估算/真实偏差；偏差超阈值
时更新 timing PR。

### 风险 7：Registry 或 Actions artifact 成为新的部署依赖

部署前完整 pull；旧容器和本地 last-known-good 保留；manifest 精确绑定 producer run；
production manifest 持久化到 Release；retention 使用 release protect-set；production 不在
artifact/pull 失败时停止旧服务，也不静默现场构建。

### 风险 8：前端 build-time 配置破坏跨环境制品等价

第一阶段显式发布环境 variant，并明确 main CI 会重新构建 production variant；长期迁移
公开配置到 runtime。禁止把不同 build args 的镜像描述为同一 binary promotion。

## 被拒绝的方案（Rejected Alternatives）

### 1. 直接减少 pytest 数量或 PR 只跑 changed tests

当前测试 body 不是唯一瓶颈，且 routing/serving 依赖交叉较多。test impact analysis 的
漏测风险高于节省的几十秒，不纳入本阶段。

### 2. 所有 job 保持不变，只增加 runner

扩容可以缓解队列，但会永久承担五次 `npm ci`、三次 Python setup 和三张无关镜像的
成本。先减少确定性浪费，再根据 queue P95 决定是否扩容。

### 3. 把 `.venv` 或 `node_modules` 在 job 间打包传递

大目录 artifact 上传/下载可能比重建更慢，还引入平台和路径兼容问题。合并同类 job、
使用 lockfile cache 更简单。

### 4. staging `cancel-in-progress: true`

取消可能发生在 SSH/Compose 中间，留下不确定远端状态。保持正在执行的部署不可取消，
只允许 GitHub concurrency 淘汰尚未开始的旧 pending run。

### 5. CD 找不到镜像时自动回退 `make build`

这会重新引入「测试的不是部署的制品」，并把 registry/manifest 故障隐藏成不可复现的
现场构建。正常 CD 必须 fail closed；应急 source-build 是单独、显式、可审计的操作。

## 必须决策与待确认事项（Required Decisions / Open Questions）

### Phase 0 必须回答

1. 当前 self-hosted runner 是一机一 runner，还是一台物理机运行多个 runner service？
2. CI builder、staging、production 的实际 CPU architecture 分别是什么？对应 native
   builder / multi-arch 方案是什么？
3. 当前同 SHA 的 push→staging healthy P50/P90 是多少，queue、CI、deploy 各占多少？

答案必须落入 Phase 0 产物。第 1 项影响 `-n auto` 和 job fan-out 调优；第 2 项是
Phase 3/4 硬阻塞；第 3 项是判断制品流水线是否真正提速的基线。

### Phase 3 前必须回答

1. OCI registry 使用 GHCR 还是现有内部 registry？其 pull 带宽、release protect-set
   retention 和只读部署 credential 如何实现？
2. production manifest 持久化在 GitHub Release、registry artifact，还是两者同时使用？
3. production 是否已通过 GitHub Environment 配置 required reviewers？
4. frontend 哪些 `NEXT_PUBLIC_*` 必须 build-time，哪些可以迁移到现有 `/site-config`？
5. branch protection 当前 require 的具体 check names 是什么，迁移窗口由谁操作？
6. oncall profile 是否要求随每次 serving 改动部署，还是只需保证镜像可构建？

## 建议实施拆分

为了让每个 PR 可独立回退，建议按以下顺序提交：

1. **CI correctness/metrics/platform inventory：** 修复 test roots、增加 shard invariant、
   同 SHA 端到端性能 summary，并记录 builder/staging/production 平台矩阵。
2. **CI fan-out：** 合并 backend/frontend quality，增加稳定 `CI Gate`；随 PR 附可靠性
   不变量 checklist，证明 checkout/uv 等自愈逻辑未丢失。
3. **Balanced shards：** 加入 timing manifest、LPT partitioner 与更新 workflow。
4. **Change classification：** shadow mode → fixture → 启用保守 skip。
5. **Affected Docker matrix：** 合并 workflow 真值，只构建受影响镜像；保留
   fresh-builder retry，保持顶层 workflow 名 `CI` 或原子更新 deploy 触发名。
6. **OCI publish：** registry、native/multi-arch digest、release manifest、Compose 双模式，
   并报告新增 layer upload 与 manifest-ready 耗时。
7. **Staging promotion：** 通过 producer run ID 精确下载 manifest、pull digest、健康检查、
   last-known-good 本地回滚。
8. **Production promotion：** Release manifest 持久化、release protect-set retention、审批、
   digest deploy、回滚演练、移除默认 source-build。

每一步都必须保留前一步的完整质量门禁；不得把路径过滤、测试分片重写和 production
部署切换塞进同一个 PR。
