# HybridInference 中立上游与发行版拆分 — 设计

**日期：** 2026-07-16
**状态：** Draft → 待项目维护者与 FreeInference 运营方评审
**作者：** HybridInference / FreeInference 架构讨论
**范围：** 开源上游边界、FreeInference 运营内容拆分、未来发行版扩展方式
**说明：** 本文聚焦如何安全整理和拆分当前仓库，不改变 RouteWise、Nimbus 的算法语义，也不设计尚未确定的资源系统。

**相关文档：**
[2026-06-18-opensource-decoupling.zh.md](2026-06-18-opensource-decoupling.zh.md)
（英文版 [2026-06-18-opensource-decoupling.md](2026-06-18-opensource-decoupling.md)）
是用户管理与站点专属内容解耦的 Epic。两文关系：该 Epic 的 P0–P2 是本文
Phase 1「配置与品牌中性化」的文件级实施清单；其 P3–P4（可插拔
auth/quota、外部 IdP）不阻塞本文任何 Phase，排在 Phase 3 之后按真实需求执行；
其「安全关键」章节的处方以本文「公开与可见性策略」一节为准（不做 git 历史重写）。

[2026-07-22-phase2-self-contained-distribution-design.zh.md](2026-07-22-phase2-self-contained-distribution-design.zh.md)
是本文 Distribution roadmap Phase 2（简称 `D-P2`）的详细设计，并修订本文早期的前端
边界：当前完整 Next.js 产品前端归 FreeInference 发行版；上游以 headless API/SSE/Auth
契约为强边界，可选 neutral reference console 不是运行依赖。该文同时区分 `D-P2`、
Issue #738 的 `I738-P2` 和 CI/CD 设计的 `CI-P2`，避免 phase 编号混用。

## 摘要（Executive Summary）

当前目标是：

> **把现有仓库整理成“中立技术上游 + FreeInference 运营发行版”的清晰结构，使品牌、配置、文档、部署和特定运维内容可以安全集中和移动，同时保留未来其他组织基于同一上游构建独立发行版的能力。**

本设计恢复并确认以下方向：

1. RouteWise、Nimbus、FixedRouter、Gateway、Adapters 和通用平台能力继续属于 HybridInference 上游。
2. FreeInference 的品牌、域名、真实模型目录、Terms、用户内容、生产部署和特定机器运维属于 FreeInference 发行版。
3. 当前先在同一仓库建立 `distributions/freeinference/`，形成逻辑边界，不立即物理拆仓。
4. 独立 `freeinference-deployment` 仓库是边界稳定后的可选动作，不是当前 Phase 1 的强制终态。
5. 当前不创建职责模糊的 operations repo。
6. 未来其他组织如何消费或 fork 发行版，等边界和发布物稳定后再决定；当前不强制一种下游模型。
7. FreeInference 已有大量真实用户和流量，任何拆分必须保持现网行为不变，并支持
   staging、production probe（已有设施时可加流量 canary）和快速回滚。

## 背景（Context）

当前仓库同时包含：

- 通用推理网关和协议兼容；
- Provider Adapters；
- Fixed、RouteWise 等路由算法；
- 用户注册、API key、角色、额度和模型权限；
- Postgres、日志、成本和运营查询；
- Admin API、Dashboard 和 Playground；
- FreeInference 品牌、Terms、Sponsor、用户文档和 RAG；
- 线上真实模型目录、Provider 配置和告警；
- staging/production 部署、特定主机脚本、监控和备份；
- benchmark、论文 artifact 和运营分析脚本。

这些内容在一个仓库里帮助 FreeInference 快速发展，但归属和依赖方向逐渐不清：

- 外部使用者难以判断哪些能力可复用，哪些只服务 freeinference.org。
- FreeInference 特定内容继续进入通用模块后，未来拆分成本会增加。
- 如果现在直接复制或拆仓，又会把一个代码整理问题变成跨仓发布和生产迁移问题。
- 为追求目录纯净进行大规模移动，可能影响已有用户和持续流量。

因此，近期重点是建立清晰、可验证的边界，而不是立即改变仓库数量。

## 最高优先级约束：不破坏现有 FreeInference

FreeInference 是有真实用户、真实 API key 和持续请求流量的生产服务，不是待重构原型。

任何拆分必须满足：

1. 现有 API key、用户、角色、额度和登录 session 无需迁移即可继续工作。
2. OpenAI/Anthropic API、SSE framing、错误格式、usage、模型 ID 和 alias 保持兼容。
3. Fixed、RouteWise、fallback、health 和 circuit breaker 默认行为不变。
4. 当前数据库和 `models.yaml` 在新路径验证完成前继续作为生产真值。
5. 数据库变更允许 N 与 N-1 应用并行；应用回滚不依赖 down migration。
6. 每次生产发布保留 known-good image digest、配置 revision 和回滚入口。
7. 新配置或发行版 loader 故障时可以退回现有路径。
8. 站点拆分、Router 行为变更、数据库真值切换和模型目录变更不得同窗进行。
9. 拆分时间表服从稳定性；未满足门禁时可以长期停留在 staging 或 production probe。

## 目标（Goals）

1. 明确上游、FreeInference 发行版、研究 artifact 和未来下游的边界。
2. 把散落的 FreeInference 专属内容逐步参数化并集中管理。
3. 让同一份上游代码能够通过不同 Distribution Config 构建不同站点。
4. 保留 RouteWise、Nimbus 作为上游一等算法，不产生组织级分叉。
5. 建立不包含 FreeInference 私有假设的中立构建和自部署示例。
6. 让发行版通过配置、内容和少量稳定扩展点组合上游。
7. 让 FreeInference 运营内容未来具备整体迁移到独立仓库的条件。
8. 使用兼容测试、staging、production probe/可选 canary 和回滚保护现网。

## 非目标（Non-goals）

- 不在当前阶段重写 RouteWise、Nimbus 或改变论文实验。
- 不为了拆分立即拆成多个 Python package、微服务或 Git 仓库。
- 不把通用用户、鉴权、额度、Storage 和 Admin 能力全部移出上游。
- 不要求立即建立腾讯公益或其他生产发行版。
- 不规定未来公司必须 fork 哪个仓库。
- 不采用 Git submodule 作为当前生产依赖。
- 不默认开放 FreeInference 用户数据或运营数据库。
- 不在同一个发布中引入替代路径并删除旧路径。
- 不建设与仓库拆分无关、需求尚未落实的系统。

## 术语（Terminology）

- **HybridInference 上游：** 可被不同运营方共同复用的技术代码和发布物。
- **发行版（Distribution）：** 对上游进行品牌、配置、政策、内容和部署组合的可运营产品。
- **FreeInference 发行版：** 当前实际存在并运行的官方发行版。
- **运营内容：** 只对特定站点、域名、机器、组织或用户政策有意义的内容。
- **Distribution Overlay：** 发行版在仓库中的隔离目录。
- **未来下游：** 未来可能使用上游构建其他平台的组织；当前不规定其仓库关系。
- **Paper Artifact：** simulator、trace、实验配置、绘图和复现代码。
- **扩展缝（Extension Seam）：** 为真实下游需求预留的小型稳定接口，不代表现在建设对应系统。

## 设计原则（Design Principles）

### 1. 按职责和发布周期拆分，不按机构名字拆分

RouteWise/Nimbus 属于算法层；用户、鉴权和 Admin 可以是通用平台能力；FreeInference 品牌、真实配置和具体部署属于发行版。

### 2. 依赖只能从发行版指向上游

~~~text
FreeInference distribution ──depends on──> HybridInference upstream
Future distribution       ──depends on──> HybridInference upstream
HybridInference upstream  ──must not import──> any distribution
~~~

上游不能读取 `distributions/freeinference` 中的实现，也不能硬编码官方域名、具体主机或备份位置。

### 3. 先集中，再验证，最后决定是否移动仓库

第一步将站点内容集中到 Distribution Config 和 overlay；第二步让 staging/production 使用并验证；最后才判断独立仓库是否有净收益。

### 4. 兼容包装优先于内部重构

稳定运行的功能先通过配置或薄 Adapter 建立边界，不为抽象纯度重写现有逻辑。

### 5. 当前目标是可拆分，不是必须拆仓

目录移动和仓库数量不是成功标准。成功标准是发行版内容不再污染上游，并且可以整体移动而不改上游代码。

### 6. 现网是 reference implementation

新路径必须证明与当前路径兼容；旧路径跨过完整观察窗口后才能通过独立变更删除。

## 迁移总览（Before → After）

同样的内容一件不少——变化的是**所有权边界**。颜色语义全文一致：青绿 = 中立上游，
绯红 = FreeInference 发行版，琥珀虚线 = 未来发行版（未启动），灰 = 现状。

~~~mermaid
flowchart LR
    R["<b>现状：单仓四类混装</b><br/>网关 · SSE ⊕ RouteWise / Nimbus<br/>⊕ 品牌 · Terms · 真实 models.yaml<br/>⊕ 机器脚本 · 部署 · 备份"]

    subgraph AFTER["目标：同一仓库内三条泳道"]
        UP["<b>HybridInference 上游</b><br/>网关 · 一等算法 · Adapters<br/>控制面 · DistributionConfig 扩展缝"]
        FI["<b>distributions/freeinference</b><br/>品牌 · Terms · 真实配置 · 机器 ops"]
        FU["<b>distributions/&lt;future&gt;</b><br/>腾讯公益等 · 从空骨架开始"]
    end

    R == "先集中 · 再验证 · 最后移动" ==> UP
    R ==> FI
    R -.-> FU
    FI -- "depends on" --> UP
    FU -. "depends on" .-> UP

    classDef legacy fill:#EFEDE8,stroke:#7A756B,color:#3F3B33
    classDef up fill:#E3F1EE,stroke:#0E6E63,color:#0A4F47
    classDef fi fill:#F8E9EB,stroke:#A62639,color:#7E1D2C
    classDef fu fill:#F6EEDC,stroke:#9A6B00,color:#6F4E02,stroke-dasharray:6 4
    class R legacy
    class UP up
    class FI fi
    class FU fu
~~~

依赖只有一个方向：发行版 depends on 上游；上游永不 import 任何发行版。

## 目标边界（Target Boundaries）

~~~mermaid
flowchart TB
    Client["用户 / Agent"]

    subgraph Distribution["FreeInference 发行版"]
        Brand["完整产品前端、品牌、域名、Terms"]
        Catalog["真实模型目录与运营策略"]
        Ops["部署、监控、备份、特定运维"]
    end

    subgraph Upstream["HybridInference 中立上游"]
        Gateway["Gateway 与协议兼容"]
        Control["通用鉴权、额度、Storage、Admin"]
        Runtime["Routing Runtime 与执行"]
        Algorithms["Fixed / RouteWise / Nimbus"]
        Adapters["Provider Adapters"]
        Contract["OpenAPI / SSE / Auth / Capabilities"]
        Seams["稳定扩展点"]
    end

    subgraph Providers["推理供应"]
        Local["本地推理 Endpoint"]
        Remote["远程 API"]
    end

    Client --> Brand
    Brand --> Contract
    Contract --> Gateway
    Catalog --> Gateway
    Gateway --> Control
    Gateway --> Runtime
    Runtime --> Algorithms
    Runtime --> Adapters
    Adapters --> Local
    Adapters --> Remote
    Ops --> Gateway
~~~

## 内容归属

### HybridInference 上游

应保留：

- OpenAI、Anthropic、Responses 等协议兼容；
- SSE、错误处理、middleware 和请求生命周期；
- 通用 Provider Adapter 与注册机制；
- Fixed、RouteWise、Nimbus 及其生产集成；
- health、fallback、circuit breaker 和 routing observability；
- 通用用户、API key、JWT、角色、额度和模型可见性机制；
- 通用 Postgres storage 和 migration；
- 通用 Admin API、versioned OpenAPI、SSE/Auth/capability contract；
- 通用 Docker 镜像、自部署示例和开发文档；
- black-box API conformance testkit；
- Distribution、Adapter、Identity/Policy 和 Provider 注册等小型扩展接口。

上游未来可以提供可选 neutral reference console，但它不是 backend 的运行依赖，也不是
发行版必须复用或 fork 的页面框架。

### FreeInference 发行版

应逐步集中：

- `freeinference.org` 及 staging/status/docs 域名；
- 当前完整 Next.js 产品前端（landing、auth、Dashboard、Playground、Admin、Terms）；
- Harvard/FreeInference Logo、颜色、团队、Sponsor 和 landing page；
- Terms、Privacy、联系邮箱、signup 文案和邮件模板；
- 真实 `models.yaml`、Provider 目录和线上 routing/alert 参数；
- FreeInference 用户文档、安装脚本和 RAG corpus/index；
- production/staging workflows 和 release promotion；
- Cloudflare、systemd、Nginx 站点配置；
- 特定主机、SSH tunnel 和服务脚本；
- 备份位置、数据库导出和受控运营分析；
- status monitor 的官方站点配置；
- 仅对 FreeInference 有意义的用户政策和影响力报表。

### Paper Artifact

可以独立，但不复制生产算法：

- RouteWise/Nimbus simulator；
- experiment configs；
- workload traces；
- figure/table generation；
- paper-specific tag 和复现说明。

### 未来发行版

当前只定义边界，不要求实现。未来启动时应优先通过：

- 自己的 Distribution Config；
- 自己的完整前端 source/image；
- 品牌和内容；
- 模型与 Provider 配置；
- 身份和政策扩展；
- 部署与监控配置；

来组合同一上游。是否 fork、独立建仓或消费发布 artifact，由边界成熟后的产品和治理需求决定。

## 仓库策略

### 当前：Monorepo 内建立 FreeInference Overlay

~~~text
hybridInference/
  apps/
    backend/
      serving/                 # 通用 Gateway / Control Plane
      routing/                 # Fixed / RouteWise / Nimbus

  config/
    examples/                  # 中立自部署示例

  distributions/
    freeinference/
      distribution.yaml
      frontend/                # 完整 FreeInference Next.js 产品前端
      config/
        models.yaml
        routing.yaml
        alerts.yaml
      branding/
      content/
        terms.md
        privacy.md
        emails/
      docs/
      deploy/
      ops/
      monitoring/
      targets/

  deploy/
    community/

  services/
    conformance-harness/

  docs/
    developer/
~~~

这是一张目标地图，不要求一次性移动所有文件。现有路径在兼容期继续存在，由新 loader 双读并比较。

### 为什么不现在创建 operations repo

需要抽离的不只是运维脚本，还包括品牌、内容、真实模型目录、用户政策、文档和部署。`operations` 会错误缩窄职责，并在边界未验证前引入跨仓同步问题。

`ops` 只是 `distributions/freeinference/` 内部的一个子目录，与 branding、content、config、docs、deploy 和 tests 并列。

### 未来可选：freeinference-deployment

当 overlay 已稳定成为生产真值，且上游发布物、跨仓 staging 和回滚均成熟后，可以将整个 overlay 抽成：

~~~text
freeinference-deployment/
  distribution.yaml
  branding/
  content/
  config/
  docs/
  deploy/
  ops/
  monitoring/
  targets/
~~~

这不是当前 Phase 的强制终态。即使具备拆仓条件，若 monorepo 更简单，也可以继续保留同仓。

### 可选私有基础设施仓库

只有未来确实出现需要独立访问控制的敏感基础设施，才考虑：

~~~text
freeinference-infra-private/
  terraform/
  inventory/
  private-network/
  certificates/
  disaster-recovery/
~~~

当前不创建该仓库。它也不保存应用源码、RouteWise/Nimbus 或公开发行版内容。

## 公开与可见性策略（Publication & Visibility）

当前仓库为 private。Phase 3 要求公共 CI 和外部可自部署，这隐含"上游必须存在一个
公开形态"。为避免"归拢运营内容"与"公开仓库"互相冲突，确立以下规则：

### 1. Overlay 目录即未来的可见性边界

`distributions/freeinference/` 按"未来独立仓库 + 独立可见性等级"设计。归拢做得越
彻底，这个目录越集中了不适合公开的内容（真实模型目录、告警参数、机器脚本、RAG
语料、备份位置）。因此 monorepo 在 overlay 存续期间保持 private。

### 2. 已定的硬约束与待定的公开机制

已定（硬约束）：**不重写本仓 git 历史**——大量活跃 worktree 与进行中分支会被
全部作废，且历史中的运营细节（内部拓扑注释、真实配置演变）本就无需随上游公开；
overlay 存续期间本仓保持 private。

待定（见待决策 10）：具体公开机制。当前**推荐方向**是"新建公共仓库 + 过滤导出"
（排除 `distributions/`、`ops/db/analysis` 及公开面审计标记的内容，同步方向为
"本仓 → 导出仓"），但它引入第三套仓库状态与同步治理，在 overlay 验证稳定前
不定稿；"翻转本仓为 public + 历史清理"的路线因违反上述硬约束被排除。

### 3. 公开前置清单

上游以任何形式公开前必须完成：

1. [2026-06-18-opensource-decoupling.zh.md](2026-06-18-opensource-decoupling.zh.md)
   的 P0–P2，其中安全项按修订版处方执行：Statcounter 改 `NEXT_PUBLIC_*` 环境变量
   且默认关闭；`wrangler.toml` 中的 account/database ID 属标识符而非凭据，轮换
   `CLOUDFLARE_API_TOKEN` 作为廉价保险即可，**不做 git 历史重写**。
2. 全仓公开面审计：内部域名、主机拓扑注释、真实邮箱、未跟踪杂项文件。
3. 公共 CI **必须**使用 GitHub-hosted runner（或一次性、隔离的 ephemeral
   runner）——持久 self-hosted runner 暴露给公开仓库的 fork PR，等于允许任意人
   在自有服务器上执行代码；对 fork PR 的人工审批只能作为额外防线，**不是**
   runner 隔离的替代品（审批疲劳与恶意-但-看似无害的 PR 都能穿透它）。
4. RouteWise 依赖方式已决定（见 Phase 3 入口门槛）——私有 git 依赖存在时无法构建
   可公开的 artifact。

### 4. 时点

任何公开形态最早在 Phase 2 完成（上游目录已不含运营内容）后启动；公开机制、
时点与同步治理统一列入待决策 10，在 overlay 验证稳定前不定稿。

## 当前目录迁移映射

| 当前内容 | 目标归属 | 近期动作 |
|---|---|---|
| `apps/backend/routing/**` | 上游 | 保持位置和行为 |
| `apps/backend/serving/adapters/**` | 上游 | 后续减少 registry 硬编码 |
| API/SSE/auth/quota/storage | 上游 | 保持能力与数据库兼容 |
| RouteWise/Admin backend API | 上游官方平台能力 | 保留，不降级成外部插件 |
| `config/models.yaml` | FreeInference 发行版 | 先支持可配置路径，再迁移 |
| `config/routing.yaml`、`alerts.yaml` | 示例上游、真实值发行版 | 区分 example 与 production |
| `apps/frontend/**` | FreeInference 发行版 | 完整应用迁入 overlay；上游共享网络契约而非页面源码 |
| `docs/free_inference` | FreeInference 发行版 | 与开发文档分开 |
| FreeInference RAG index | FreeInference 发行版 | 构建跟随发行版内容 |
| `ops/local_deployment_proxy` | Mixed | 通用逻辑保留；具体主机配置移出 |
| 特定主机/tunnel/systemd | FreeInference 发行版 | 集中到 distribution ops |
| production/staging workflows | FreeInference 发行版 | D-P2 保持原位和 source-build 语义，但允许修改移动后的路径与本地 image 注入；下沉/迁仓属 Phase 4 |
| Dockerfile/Compose | Mixed | 通用镜像上游，站点 overlay 进发行版 |
| status monitor | Mixed | 通用 worker 可留，站点配置移出 |
| `ops/db/analysis` | FreeInference 受控环境 | 不进入通用发行物 |
| benchmark/paper scripts | Paper Artifact 或上游 benchmark | 按用途分类 |
| `freeinference-harness` | 上游 testkit + 发行版 targets | 分开 scenarios 和站点 target |

## Distribution Config

以下 v1 是 Phase 1 兼容形态。D-P2 的最终 backend runtime manifest v2、独立
bundle manifest v1、strict validation、environment contract 与 required/fail-closed 语义由
[D-P2 详细设计](2026-07-22-phase2-self-contained-distribution-design.zh.md)定义；
staging/production 在 D-P2 退出前升级到 runtime v2。frontend/legal/ops/deploy 等 source
inventory 不进入 backend runtime schema。

~~~yaml
schema_version: 1

distribution:
  id: freeinference
  display_name: FreeInference
  release: 2026.07.1

site:
  public_base_url: https://freeinference.org
  support_email: admin@freeinference.org
  terms_document: ./content/terms.md
  privacy_document: ./content/privacy.md
  branding: ./branding/site.yaml

features:
  routers: [fixed, routewise]
  public_signup: true
  rag: true

paths:
  models: ./config/models.yaml
  routing: ./config/routing.yaml
  alerts: ./config/alerts.yaml

deployment:
  target: production
~~~

要求：

- 路径由启动入口从 distribution root 解析。
- 上游提供 schema 和 validation，不提供 FreeInference 默认值。
- 缺失可选内容时使用中立 fallback。
- Secret 通过 environment/Secret Manager 注入。
- Manifest 有 `schema_version` 和兼容测试。
- 当前环境变量优先级在兼容窗口内保持不变。

## 前端拆分

Phase 1 已用 build-time branding 与 `/site-config` 建立兼容接缝；它解决安全引入 overlay 的
问题，但不能成为多发行版的最终 UI 架构。

D-P2 采用“共享协议，不共享页面”：

- HybridInference 是 headless Gateway / Control Plane；
- 当前完整 Next.js 应用（包括 landing、auth、Dashboard、Playground、Admin、Terms、assets）
  归 FreeInference distribution；
- FreeInference、腾讯和未来发行版各自拥有 frontend source、lockfile、CI、image 和发布节奏；
- 共享面仅限 versioned OpenAPI、stable error、SSE、Auth/session、capabilities 和可选的
  framework-neutral TypeScript client；
- `/site-config` 保留为 legacy compatibility endpoint，但不扩张为页面 CMS；
- 当前不创建微前端，也不抽取所有发行版必须使用的 React/Next Console package；
- 可选 neutral reference console 以后可以基于同一网络契约独立创建，不是 backend 依赖。

完整目录、迁移波次和验收见
[D-P2 自包含发行版与独立前端设计](2026-07-22-phase2-self-contained-distribution-design.zh.md)。

## 后端拆分

### 配置路径

将硬编码路径逐步改为：

~~~text
MODELS_CONFIG_PATH
ROUTING_CONFIG_PATH
ALERTS_CONFIG_PATH
DISTRIBUTION_CONFIG_PATH
~~~

兼容规则：

1. 显式环境变量优先；
2. Distribution Config 次之；
3. 现有 `config/*.yaml` 兜底；
4. 启动日志打印来源和内容 hash，不打印 Secret；
5. staging 双读比较；
6. production 未启用新 loader 时继续使用旧路径。

~~~mermaid
flowchart LR
    ENV["① 显式环境变量<br/>MODELS_CONFIG_PATH / ROUTING_CONFIG_PATH / …<br/>（任意大小写 · .env · 构造器均计入）"]
    MAN["② 发行版 manifest 的 paths:<br/>distribution.yaml（相对路径锚定 manifest 目录）"]
    LEG["③ legacy 兜底<br/>config/*.yaml —— 现网真值"]

    ENV -- "未显式设置" --> MAN
    MAN -- "未声明该项 / 加载失败(fail-open)" --> LEG
    MAN -. "mode=dark（默认）：只对比、不生效" .-> LEG

    classDef p1 fill:#ECEFEE,stroke:#17211F,color:#17211F
    classDef p2 fill:#F8E9EB,stroke:#A62639,color:#7E1D2C
    classDef p3 fill:#EFEDE8,stroke:#7A756B,color:#3F3B33
    class ENV p1
    class MAN p2
    class LEG p3
~~~

激活 manifest 路径必须显式 `DISTRIBUTION_CONFIG_MODE=active`；非法 / 缺失的 mode
一律降级为 dark——任何失误只可能压住激活，永远不可能触发激活。

上述优先级和全局 mode 是 Phase 1/v1 兼容语义。D-P2 runtime v2 在 staging/production
使用 models/routing/alerts 分资源 selector；`required` 时不允许旧环境变量覆盖已激活
资源，也不允许 fail-open 回 legacy。具体语义以 D-P2 详细设计为准。

### Adapter、Router 与供应接入

为了拆发行版，不要求先重写 RouteWise 或抽取新的执行框架。只建立以下纪律：

- Adapter/Router 不读取发行版目录；
- 发行版只提供配置和注册信息；
- 标准本地 endpoint 和远程 API 优先通过配置接入；
- 特殊协议只有在真实需求出现时才提取 Connector；
- Router metadata 使用 namespace；
- RouteWise/Nimbus 的生产集成仍由上游维护。

更深的路由架构调整不是本次发行版拆分的前置条件。

### Identity 与 Policy

当前不重写 FreeInference auth。只有真实下游需要不同身份和政策时，才从现有实现中提取小型接口；不提前实现不存在的集成。

## 生产稳定性与在线迁移

### 兼容性 Contract

| 契约 | 验证内容 |
|---|---|
| API | OpenAI/Anthropic path、header、status、error、stream event |
| 模型 | ID、alias、capability、context/output limit、visibility |
| Auth | API key、JWT/session、role、quota、concurrency |
| Routing | Fixed/RouteWise 参数、fallback、pin、health、circuit |
| Usage | prompt/completion/cache/tool token、cost、成功/失败 |
| Storage | N/N-1 兼容、旧日志可读 |
| Admin | 用户审批、Provider 操作、告警、导出和 dashboard |
| Deploy | readiness、SSE drain、配置和镜像回滚 |

### 迁移状态

~~~text
LEGACY_ONLY
  → DARK_LOADED
  → SHADOW_COMPARE
  → INTERNAL_CANARY
  → SCOPED_CANARY
  → FULL
  → LEGACY_REMOVAL_ELIGIBLE
~~~

- `DARK_LOADED`：加载新 manifest/loader，仍使用旧值。
- `SHADOW_COMPARE`：双读配置并比较，不双发用户请求。
- `INTERNAL_CANARY`：内部账号或 staging 使用新路径。
- `SCOPED_CANARY`：按稳定 hash 的 model/user/session 分桶。
- `FULL`：新路径成为真值，旧路径继续保留。
- `LEGACY_REMOVAL_ELIGIBLE`：完整观察窗口后，另开 PR 删除兼容路径。

### Feature Flags 与回滚

每项拆分至少有：

~~~text
global kill switch
distribution
config loader
frontend site config
model/provider
user/account allowlist
percentage bucket
~~~

生产 PR 按风险分两档，避免流程重量压垮小团队：

- **Tier A（真值切换类）：** config loader 切换、真实 `models.yaml`/routing 真值
  迁移、部署机制改造、数据库 schema 变更——适用下述全部要求。
- **Tier B（内容搬运类）：** branding、content、docs、RAG、examples 等
  `git revert` 即可回滚的改动——普通 PR + staging 冒烟即可，不要求完整 runbook。

每个 Tier A 生产 PR 必须写明：

- owner/on-call；
- 影响范围；
- 旧行为 reference；
- feature flag/kill switch；
- staging 和 canary；
- baseline 与 abort gate；
- known-good image；
- previous config；
- DB compatibility；
- rollback runbook 和 RTO。

### 数据库与部署

采用 Expand–Migrate–Contract：

1. Expand：只新增兼容 schema；
2. Migrate：幂等、分批、可暂停；
3. Switch：feature flag 切读路径；
4. Contract：跨稳定版本和观察窗口后删除旧 schema。

同时：

- 新旧实例允许并行；
- 旧实例停止新请求并 drain SSE；
- 上游/发行版拆分本身不要求数据库结构变化；
- 新路径故障时回滚 known-good image/config；
- 拆分不与 Router 或真实模型目录变更同窗。

### CI/CD 演进路径

当前 CD 的真实依赖不是"workflow 住在哪个仓库"，而是"部署单位是本仓源码 SHA"：
deploy workflow ssh 到主机后 `git reset --hard` 并在主机上现场构建镜像，回滚也按
旧 release tag 重新构建。演进分三个阶段：

1. **当前阶段（Phase 0–2）：不改部署单位与语义。** workflow 文件保持原位，
   继续 checkout 同一 source SHA 并现场构建；D-P2 允许修改因 frontend/config/ops 移动而
   失效的 working directory、Docker COPY、Compose include 和脚本引用。为满足 closed-root，
   orchestration 先用 upstream-only context 构建本地 backend image，再将 image ref 注入
   distribution Compose；不推 registry、不改 promotion/rollback 单位。workflow/脚本的物理
   归属和迁仓仍保留到 Phase 4。
2. **未来独立阶段（时点待定，单独立项）：部署单位换成 image digest。** 与
   Phase 3 的中立 artifact 配套但独立启动，不并入当前重构：`docker-build.yml`
   从 `push: false` 改为推送 registry；deploy 脚本从"源码同步 + 现场构建"改为
   `compose pull image@digest`；回滚从"按旧 tag 重建"变为切回上一个 known-good
   digest；`ROUTEWISE_GITHUB_TOKEN` 从生产主机退回 CI 构建环节。该阶段内的解析
   策略：staging 部署"当前 commit 构建出的镜像"保持迭代节奏，production 只读
   manifest 中显式钉住的 digest，晋升生产 = 一个修改 manifest 的 PR。
3. **拆仓后目标态（Phase 4，保留为目标设计）：** CD workflow 迁入发行版仓库。
   上游发版后，发行版仓自动收到 bump PR（更新 manifest 中的 version 与 digest；
   推荐拉模式——发行版侧定时任务或 Renovate，不要求上游持有发行版仓写权限），
   合并即部署；回滚 = revert bump PR，或部署发行版仓旧 commit（镜像与配置原子地
   一起回退）。注意 `workflow_run` 触发链不能跨仓库，bump PR 即其替代。

## 分阶段迁移计划

~~~mermaid
flowchart LR
    P0["<b>Phase 0</b><br/>归属基线 + 契约冻结"]
    P1["<b>Phase 1</b><br/>配置与品牌中性化"]
    P2["<b>Phase 2</b><br/>运营内容集中 overlay"]
    P3["<b>Phase 3</b><br/>发布中立 Artifact"]
    P4["<b>Phase 4</b><br/>物理拆仓（可选）"]

    P0 --> P1 --> P2 --> P3 --> P4

    classDef active fill:#E3F1EE,stroke:#0E6E63,color:#0A4F47
    classDef next fill:#F8E9EB,stroke:#A62639,color:#7E1D2C
    classDef gated fill:#F6EEDC,stroke:#9A6B00,color:#6F4E02
    classDef opt fill:#F6EEDC,stroke:#9A6B00,color:#6F4E02,stroke-dasharray:6 4
    class P0 active
    class P1 active
    class P2 next
    class P3 gated
    class P4 opt
~~~

状态（截至 2026-07-22）：Phase 1 的 config path、Distribution loader、`/site-config`、
branding config、contract tests 与 overlay skeleton 已合入 `dev`；ownership classification
仍需落入 `dev`，staging/production dark-load、baseline 与回滚验收也没有仓库内完成证据，
因此 Phase 0–1 尚不能标为完成。D-P2 已进入设计评审，但任何 production 真值切换仍受上述
验收门控。Phase 3 被 RouteWise 依赖发布方式的决策门控；Phase 4 非成功条件。

### Phase 0：建立归属清单与稳定基线

1. 标记 upstream、FreeInference、paper artifact、mixed、unknown。
2. 固化 API/SSE/auth/quota/model/routing characterization tests。
3. 收集现网按 model/provider/client 分桶的稳定性和成本 baseline。
4. 记录 known-good image、配置 revision、DB schema 和回滚流程。
5. 确认核心目录新增内容的归属规则。

验收：

- 不移动生产文件；
- 不改变 RouteWise/Nimbus；
- 归属覆盖全部顶层目录；
- 回滚和兼容测试可重复执行。

### Phase 1：配置与品牌中性化

1. 增加 Distribution Config schema 和 loader。
2. 路径支持 env/manifest/legacy fallback。
3. 增加 `/site-config` 和中立前端 fallback。
4. 参数化域名、邮箱、品牌、Terms 和 Sponsor。
5. 建立 FreeInference overlay，但旧路径仍是生产真值。
6. staging 双读，production dark load。

验收：

- 默认 FreeInference 行为不变；
- 关闭新 loader 可完全回旧路径；
- 中立 profile 不出现官方品牌；
- 不需要破坏性数据库 migration。

### Phase 2：运营内容集中到 FreeInference Overlay

按低风险到高风险迁移：

1. 完整 FreeInference frontend、branding/content；
2. docs 和 RAG；
3. test targets；
4. status monitor 站点配置（GitHub workflow 保持原位，但可更新输入路径）；
5. machine-specific ops 与站点专属 deploy 脚本；workflow 继续在原位调用新路径；
6. 真实 model/routing/alert config。

每一类独立 PR，并保留路径回退。首先在同一仓库移动。

验收：

- 上游目录不含生产域名、具体主机、备份位置和官方 Terms；
- 上游不依赖 distribution，也不拥有 FreeInference React/Next 页面；
- overlay 通过 runtime v2 + bundle v1 strict/closed-root 与 detached-copy validation；
- staging 完整部署和回滚成功；
- 生产 API、SSE、auth、quota、routing、usage 无回归；
- 旧路径在观察窗口内可用。

### Phase 3：发布中立 Artifact

**入口门槛（阻塞项）：** RouteWise 依赖方式必须先决定（对应待决策 5，owner：
Murphy + Juncheng）。`pyproject.toml` 当前固定依赖私有 RouteWise 仓库的 commit，
且 strategies 注册在启动时硬 import 该包——私有依赖存在时可以构建 FreeInference
私有镜像（构建时注入 token），但**无法构建可公开的中立 Wheel/OCI image**。两个
选项的真实含义：workspace package = RouteWise 源码进入公开上游（事实开源）；
发布正式 Wheel = RouteWise 包/仓库公开。该决定涉及在审论文的披露考量，需在
Phase 3 开工前完成；在此之前 Phase 3 其余条目可先以私有 artifact 演练。

1. 统一 package/API/image 版本；
2. 发布 Wheel/sdist 和 neutral OCI image；
3. 发布中立 example config；
4. 公共 CI 不依赖 FreeInference Secret；
5. FreeInference manifest 锁定 version/digest；
6. 自动创建上游升级 PR，独立 staging/canary/promotion。

验收：

- 外部用户可无 Secret 自部署；
- FreeInference 使用不可变 artifact；
- 发行版不依赖未发布 branch/SHA；
- previous digest 可回滚。

### Phase 4：可选物理拆仓

只有以下条件满足后再决定：

- overlay 已成为唯一生产配置来源；
- 中立 artifact 连续稳定发布；
- overlay 不通过相对 import 或源码路径依赖上游；
- staging 完成跨仓升级、部署和回滚演练；
- 仅修改旧 digest 即可回滚；
- production workflow 与上游 CI 已分离；
- Secrets、webhooks、deploy keys、package ownership 已盘点；
- 文件公开性、版权、商标和 owner 已确认。

届时可选择：

- 保持 monorepo；
- 抽出 `freeinference-deployment`；
- 迁移上游到中立 Organization。

物理拆仓不是当前 roadmap 的完成条件。

## 第一批建议 PR

当前批次（不触碰现有 CI/CD）：

1. `docs: classify upstream and FreeInference-owned paths`
2. `test: freeze production API/SSE/auth/quota/model/routing contracts`
3. `feat(config): add DistributionConfig with legacy fallback`
4. `feat(frontend): load runtime site config with neutral fallback`
5. `refactor(site): move branding, terms, sponsors, and contact data behind config`
6. `refactor(config): support configurable model, routing, and alert paths`
7. `deploy: create in-repo FreeInference distribution overlay`

这些 PR 不修改 RouteWise/Nimbus，不改变数据库真值，不创建新生产仓库，也不改动
GitHub workflow 与部署脚本。交付顺序建议：2 → 1 → 6 → 3 → 7 → 4/5。

以下两个 PR 属于「CI/CD 演进路径」中的**未来独立阶段**，时点待定，不在当前批次，
仅预先登记：

- `build: publish immutable neutral and FreeInference images`
- `deploy: stage and rollback by immutable artifact`

届时注意：registry 权限、Environment secrets 与 runner 配置需要维护者手工操作，
agent 只能改 workflow 文件本身；neutral image 被 RouteWise 依赖决定阻塞
（见 Phase 3 入口门槛），在此之前只能发布 FreeInference 私有镜像。

## CI 与验收

### 上游 CI

- lint、format、unit；
- Wheel/sdist 和 neutral image build；
- 中立 example config 启动；
- API/SSE/auth/quota/routing contract；
- 禁止 core 引入 distribution import；
- 扫描上游目录中的官方域名、主机和 Secret pattern；
- config schema backward compatibility。

### FreeInference 发行版 CI

- runtime v2 + bundle v1 strict/closed-root 与 detached-copy validation；
- 真实模型目录静态检查；
- 完整 FreeInference frontend build；
- staging deployment；
- black-box API/streaming；
- DB N/N-1；
- known-good digest rollback；
- production config 与 Secret 引用检查。

### 完成标准

- FreeInference 线上行为和关键 SLO 不下降；
- RouteWise/Nimbus 仍是唯一上游生产实现；
- 上游可用中立默认值独立启动；
- FreeInference 专属内容集中在 overlay；
- 上游不含生产域名、具体主机、备份位置或官方 Terms；
- 未来发行版可以用独立 frontend 复用上游网络契约，而不修改 FreeInference 或上游页面源码；
- 没有新增仓库也能完成近期目标。

## 风险与缓解

### 风险 1：为了抽象扩大改动

**缓解：** 本设计只要求配置、品牌和运营边界；更深重构另行设计。

### 风险 2：移动真实配置破坏启动

**缓解：** 可配置路径、legacy fallback、双读、dark load、staging 后 production probe；
已有流量切分设施时再加 canary。

### 风险 3：前端 site config 影响 Dashboard

**缓解：** 编译期中立 fallback；登录和 Dashboard 不依赖远端品牌配置才能启动。

### 风险 4：过早拆仓破坏发布链路

**缓解：** 先同仓 overlay 和不可变 artifact；跨仓演练完成前不迁移 production。

### 风险 5：下游模式过早固化

**缓解：** 当前只保证 overlay 与上游边界；未来可以选择 fork、独立 repo 或 artifact consumer。

### 风险 6：真实流量放大迁移错误

**缓解：** 现网作为 reference；兼容测试、shadow、production probe/可选 canary、
kill switch、SSE drain 和 N/N-1 DB。

### 风险 7：混合文件难以一次归类

**缓解：** 允许 Mixed；先拆内部依赖再移动，不复制生产逻辑。

## 被否决的替代方案

### 现在强制拆出 standalone FreeInference repo

边界尚未经过 staging 和 artifact 验证，会提前引入跨仓发布风险。独立仓库保持为后续选项。

### 现在创建 operations repo

抽离对象不只是运维脚本；职责会过窄并再次分散发行版内容。

### 把 RouteWise/Nimbus 移出上游

它们是共享算法资产，不属于 FreeInference 站点杂项。

### 下游现在直接 fork 完整仓库

会同时继承上游与官方发行版的未整理内容，产生高冲突。

### 只替换品牌字符串

无法处理真实模型目录、部署 workflow、机器配置、Terms、RAG、备份和运营分析。

## 待决策（Open Decisions）

1. 中立上游长期使用 HybridInference 还是 FreeInference 品牌。
2. Overlay 稳定多久后才评估独立仓库。
3. public beta 是否需要独立 neutral reference console；若需要，其最小功能范围。
4. `ops/local_deployment_proxy` 的通用与站点边界。
5. RouteWise Git dependency 是 workspace 还是正式 Wheel（**阻塞 Phase 3 中立
   artifact**；两个选项都意味着 RouteWise 代码公开，需结合在审论文情况决定）。
6. 是否采用 DCO，以及中立 Organization 的治理时间。
7. 各模型/客户端 production probe/active soak window、可选 canary 比例和 rollback RTO。
8. 未来下游更适合 fork、独立发行版仓库还是纯 artifact consumer。
9. 独立 `freeinference-deployment` 的公开范围与可选私有 infra 边界。
10. 上游公开机制(推荐方向:过滤导出公共仓;硬约束:不重写本仓历史)、创建
    时点与同步治理;在 overlay 验证稳定前不定稿。

## 最终决策摘要（Decision Summary）

1. 当前工作聚焦于拆分 FreeInference 运营内容。
2. HybridInference 是共享上游；RouteWise、Nimbus 是上游一等算法。
3. FreeInference 是当前唯一需要保护的生产发行版。
4. 先在同一仓库建立 Distribution Config 和 `distributions/freeinference/`。
5. 上游不得依赖发行版；发行版通过配置、内容和稳定扩展缝组合上游。
6. 当前完整产品前端归 FreeInference；未来发行版通过网络契约拥有自己的独立前端，不共享
   React/Next 页面源码。
7. 当前不强制建立 standalone FreeInference repo，也不强制下游 fork 模型。
8. 未来是否抽出 `freeinference-deployment`，由 overlay、artifact、staging 和回滚成熟度决定。
9. 所有迁移以现网兼容、staging/production probe（已有设施时可加流量
   canary）、快速回滚和 N/N-1 数据库兼容为前提。
10. 近期不修改 RouteWise/Nimbus 算法，不改变线上路由真值。
11. “运营内容可整体移动且不污染上游”是当前成功标准，物理拆仓不是。

推荐统一表述：

> 我们先在当前仓库中把 HybridInference 上游与 FreeInference 运营发行版的边界整理清楚，并通过同仓 overlay 验证生产稳定性。边界和发布物成熟后，再决定是否独立拆仓以及未来下游采用何种同步方式。
