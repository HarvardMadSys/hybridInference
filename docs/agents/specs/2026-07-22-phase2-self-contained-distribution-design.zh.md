# HybridInference Phase 2：自包含发行版与独立前端 — 设计

**日期：** 2026-07-22

**状态：** Draft → 待 HybridInference 维护者与 FreeInference 运营方评审

**作者：** HybridInference / FreeInference 架构讨论

**范围：** 主设计 Phase 2「运营内容集中到 FreeInference Overlay」的详细边界、
前端归属、迁移波次、验证与回滚门禁

**相关文档：**

- [HybridInference 中立上游与发行版拆分 — 设计](2026-07-16-hybridinference-neutral-upstream-multi-distribution-design.zh.md)
- [开源解耦 Epic](2026-06-18-opensource-decoupling.md)
- [CI/CD 提速与制品晋级 — 设计](2026-07-21-ci-cd-throughput-and-artifact-promotion-design.zh.md)
- [Open-source HybridInference tracking issue #738](https://github.com/HarvardMadSys/hybridInference/issues/738)

**修订关系：** 本文细化主设计的 Phase 2，并修订其「前端拆分」近期方案：
HybridInference 的强边界是 headless HTTP/SSE Control Plane；当前完整 Next.js 应用归
FreeInference 发行版，不再把“所有发行版共用同一套 React 页面、通过 `/site-config`
换皮”作为目标架构。主设计的 Phase 3（发布不可变中立制品）和 Phase 4（可选物理拆仓）
边界不变。

### Phase 编号命名空间

仓库现有三份文档都使用了 “Phase 2”，但含义不同。本文统一使用以下前缀，避免把
artifact 或 CI 工作误并入本阶段：

| 名称 | 含义 | 与本文关系 |
|---|---|---|
| `D-P2` | Distribution roadmap Phase 2：同仓 overlay 内容集中与生产真值切换 | **本文唯一范围** |
| `I738-P2` | Issue #738 Phase 2：external release contract | `D-P2` 准备 schema/conformance；`D-P3` 完成发布和 consumer lock |
| `CI-P2` | CI/CD 设计 Phase 2：路径过滤与 affected-image matrix | 可并行，但不是 `D-P2` |
| `CI-P3/P4` | OCI 发布与 staging/production digest 部署 | 支撑 `D-P3` |
| `I738-P3` | 提取 FreeInference operations repo | 对应 `D-P4` |

后文未加前缀的 “Phase 2” 均指 `D-P2`。本阶段可以更新因文件移动而失效的 CI working
directory、Docker COPY 和 Compose include，但不得改变 registry、artifact、部署单位、
promotion 或 rollback 语义。

`I738-P2` 是 issue 里的交付面，不与 distribution roadmap 的一个 phase 一一对应：
D-P2 只交付可在同仓验证的契约、schema 和 conformance source，直到 D-P3 发布版本化
artifact 并由外部 consumer 锁定后，`I738-P2` 才整体完成。

## 摘要（Executive Summary）

Phase 1 已建立 Distribution Config loader、配置路径优先级、`/site-config` 与
`distributions/freeinference/` 骨架，但当前 overlay 仍回指仓库根部的真实配置，
FreeInference 品牌、前端、Terms、文档、RAG、监控、机器运维和部署内容也仍散落在
上游目录中。

Phase 2 的成功标准不是“移动若干文件”，而是建立一个经过生产验证的、**闭合且可整体
搬走的发行版根目录**：

1. HybridInference 上游只提供 headless Gateway / Control Plane、稳定 API/SSE/Auth
   契约、通用配置 schema、迁移和测试工具。
2. 当前完整 Next.js 应用作为 FreeInference 产品前端整体归发行版；未来腾讯或其他
   发行版拥有自己的前端代码与构建产物，通过网络 API 消费同一上游。
3. 发行版内部可以持有品牌、法律文本、真实配置、用户文档、RAG 数据、监控、部署和
   machine-specific ops；所有 distribution-owned runtime/build/data/deploy input 都不得
   越出发行版根目录。
4. Phase 2 仍在当前 private monorepo 内验证，继续使用现有 source-build CD；OCI 发布、
   digest pin、自动 bump PR 和物理拆仓留给 Phase 3/4。
5. 每一类内容独立迁移，先 mirror/dark validate，再 staging active、production probe/
   可选流量 canary，
   最后 required/fail-closed；不把路径移动与路由策略、provider、数据库或产品政策调整
   合并。

一句话目标：

> Phase 2 结束时，删除或临时隐藏 `distributions/freeinference/` 后，上游仍能以中立配置
> 构建和测试；单独复制该目录后，它不需要回读 monorepo 其他路径，且保留了组成
> FreeInference 发行版所需的全部站点内容。它对 upstream 的依赖只能通过显式
> 注入的 image/package 或网络契约满足，不能通过 `../../` 源码路径满足。

## 当前基线（2026-07-22）

### 已存在的 Phase 1 接缝

- `serving.config.distribution` 已支持 manifest load、相对路径解析和
  env > manifest > legacy precedence。
- `DISTRIBUTION_CONFIG_MODE=dark|active` 已支持不生效比较与显式激活。
- `/site-config` 已暴露少量公开 identity/feature 字段。
- 前端已有 build-time branding config 与 runtime provider。
- `distributions/freeinference/` 已有 manifest、dark-load smoke 和测试骨架。

### 尚未形成发行版边界

- overlay 的 `models`、`routing`、`alerts` 仍指向 `../../config/*.yaml`。
- overlay 只有 README、manifest、smoke 和 tests，没有目标 `frontend/`、`config/`、
  `docs/`、`deploy/`、`ops/` 等内容。
- 当前 backend image 仍 `COPY config/ config/`，会把 FreeInference 真实配置带入所谓
  “中立”镜像。
- 当前 frontend image 固定从 `apps/frontend/` 构建，并在 bundle 中烘焙
  FreeInference/Harvard 域名、Terms、团队、Sponsor 和 analytics。
- `/site-config` 只覆盖少量字段，SSR metadata、首屏内容、Terms、assets 和多数 feature
  visibility 仍来自 build-time 源码。
- Compose、staging、production 和 rollback 仍以 monorepo source SHA + target-host build
  为部署单位。

因此 Phase 2 从“已有 loader 接缝”开始，而不是从“overlay 已经可搬迁”开始。

## 目标（Goals）

1. 让 `distributions/freeinference/` 成为 FreeInference 内容的唯一归属根目录。
2. 将当前完整 FreeInference 前端与上游源码边界分开；未来发行版无需 fork 或 patch
   FreeInference React/Next 页面。
3. 以稳定 HTTP/OpenAPI、SSE 和 Auth/session 行为作为前端集成契约。
4. 保留生产 API key、用户、session、quota、model name、routing 和 SSE 行为。
5. 为每类迁移提供 mirror、dark validation、production probe/可选 canary 和独立回滚。
6. 让上游在没有任何 distribution 目录时仍能构建、启动中立样例并运行 upstream tests。
7. 让发行版目录通过可机械执行的 closed-root、schema、environment-contract
   和 ownership CI gate。
8. 为 Phase 3 的中立 OCI/Wheel、distribution frontend image、digest lock 和跨仓部署提供
   清晰输入，但不在本阶段切换生产部署单位。

## 非目标（Non-goals）

- 本阶段不创建或切换 production 到独立 `freeinference` repo。
- 本阶段不把当前仓库直接改为 public，也不处理完整 Git 历史导出。
- 本阶段不发布公共 OCI/Wheel/NPM 包，不决定 registry 和 package ownership。
- 本阶段不重构 RouteWise/Nimbus，不改变任何 routing 参数或 provider 目录。
- 本阶段不创建微前端、远程模块加载或通用页面 CMS。
- 本阶段不实现腾讯前端；只证明第二个前端可以仅依赖公共网络契约。
- 本阶段不为尚不存在的下游提前实现 auth/quota/IdP 插件系统。
- 本阶段不做破坏性数据库 migration，也不改变数据库 owner。
- 本阶段不要求跨 origin 浏览器访问；默认部署仍通过同 origin reverse proxy/BFF 暴露
  frontend 与 backend。真实下游需要时再单独设计 CORS/cookie 策略。

## 不变量（Invariants）

1. **依赖单向：** distribution 可以消费 upstream 契约；upstream 不得 import、读取或
   假定某个 distribution 存在。
2. **前端经网络耦合：** 发行版前端不得从 upstream 源码相对 import React 页面、route、
   server implementation 或 private config。
3. **根目录闭合：** distribution-owned manifest 引用、symlink、构建输入、
   docs/RAG 输入和 deploy include 都不得逃逸 distribution root。upstream 必须作为
   显式 external input（Phase 2 为本地 image/package 引用）注入，不是越界路径例外。
4. **一维变更：** 文件归属移动不同时改变 routing、provider、quota、signup policy、
   DB schema 或外部域名。
5. **现网优先：** 新路径必须先证明 normalized effective config 与旧路径相同。
6. **回滚独立：** 每个迁移 PR 可以只回滚该类别；配置真值切换必须保留 N-1 路径。
7. **Secret 不入库：** 两侧仓库都只保存 Secret 引用，真实值留在 environment/Secret
   Manager。
8. **Phase 边界清晰：** Phase 2 允许 source-build；只有 Phase 3 才把发布和部署单位切为
   immutable artifact digest。

## 核心设计决策

### 决策 1：HybridInference 是 headless upstream

HybridInference 的必须交付面是：

- OpenAI/Anthropic 等协议兼容 API 与 SSE；
- auth、API key、quota、usage、admin 和 storage 能力；
- adapters、Fixed/RouteWise/Nimbus、health 与 circuit breaker；
- versioned config schema、database migrations、health/readiness；
- OpenAPI 与额外的 SSE/Auth contract tests；
- 中立 Docker/Compose 示例和 conformance testkit。

公开上游可以以后提供独立的 neutral reference console，但它不是 backend 的运行依赖，
也不是 Phase 2 的退出门槛。

### 决策 2：当前完整 Next.js 应用归 FreeInference 发行版

当前 `apps/frontend` 同时拥有 landing、Terms、团队、Sponsor、analytics、signup 产品流程、
Dashboard 与高度运营化的 Admin。继续把它切成“通用页面 + 无限增长的 site config”会把
上游变成 FreeInference 页面系统的插件宿主，也不能让腾讯自由设计自己的导航、法律流程
和视觉结构。

Phase 2 因此把**完整应用**作为一个可独立构建的 FreeInference 应用单元迁入：

~~~text
distributions/freeinference/frontend/
~~~

页面、route、layout、Tailwind theme、assets、Terms 和 SEO metadata 均随应用移动。
移动后保留现有 image/service 名称和 public behavior，先只改变源码归属与构建路径。

### 决策 3：共享契约，不共享产品页面

FreeInference、腾讯与未来前端可以共享：

- versioned OpenAPI document；
- 明确的 SSE framing/termination/error contract；
- Auth/session/cookie 行为说明；
- 可选的生成 TypeScript SDK 和类型；
- 与品牌无关的协议测试 fixture。

Phase 2 不抽取共享 React route、landing、legal page、Admin navigation 或 theme package。
只有出现两个真实消费者、且它们的实现已经稳定相同时，才从使用侧提取小型 versioned
package；不能为了避免少量重复重新制造跨仓源码耦合。

允许建立 `control-client-typescript`，但它只能包含 generated DTO、configurable fetch、统一
error codec、auth refresh singleflight 和 framework-neutral SSE decoder；不得依赖 React、
Next、React Query、浏览器 route、FreeInference branding 或页面类型。当前 FreeInference
frontend 可以渐进采用；boundary CI 必须先 `npm pack`，再在隔离 context 消费 tarball，
禁止 workspace/path import 掩盖未来拆仓问题。

### 决策 4：`/site-config` 不扩张为 CMS

`/site-config` 在兼容窗口内保留，用于当前前端平滑迁移和极少量公开 identity 信息；
它不再承担传输 landing 页面、Terms、团队、Sponsor、theme 或任意组件树的职责。

发行版前端拥有自己的内容。upstream 新增只读 `/capabilities`，返回**实际生效的公开能力**，
供任意客户端决定是否展示功能；它不能直接回显 manifest 声明或内部拓扑。

### 决策 5：Phase 2 先同仓验证，Phase 4 才物理移动

所有目标内容先进入当前 private monorepo 的 distribution root，继续走当前 staging 与
production workflow。这允许把“源码边界变化”和“跨仓发布控制面变化”分成两个独立风险。

Phase 2 结束只证明目录可移动；Phase 3 证明 artifact consumer 模式；Phase 4 才允许真正
拆仓。

### 决策 6：runtime manifest、bundle manifest 与 release lock 三分

`distribution.yaml` 只是 **backend runtime manifest**：描述 identity、runtime policy
expectation 和 backend 真正消费的 config/data path。backend 启动链只解析它，不认识
frontend、Terms、ops、deploy 或 image 发布。

`bundle.yaml` 是 **distribution-owned source inventory**：由独立 CI validator 索引
frontend、branding、legal/docs/RAG、monitoring、targets、deploy、ops 以及
`distribution.yaml`。它的失败只阻塞 distribution gate，绝不影响 backend readiness。

Phase 3 的 release/deployment manifest 再记录 backend/frontend `image@sha256`、source SHA、
config revision 和版本兼容范围。D-P2 不在前两个 manifest 中加 OCI digest、registry、
SemVer release 或 artifact promotion 语义。

### 决策 7：生产 runtime config 最终必须分资源 required/fail-closed

Phase 1 的全局 `DISTRIBUTION_CONFIG_MODE=dark|active` 只能将 models/routing/alerts 同时切换，
不能实现 Phase 2 的一次一资源。v2 runtime 增加三个显式 selector：

~~~text
DISTRIBUTION_CONFIG_PATH=/absolute/path/to/distribution.yaml
DISTRIBUTION_MODELS_MODE=shadow          # legacy | shadow | active
DISTRIBUTION_ROUTING_MODE=legacy
DISTRIBUTION_ALERTS_MODE=legacy          # dark 仅作 shadow 兼容别名
DISTRIBUTION_CONFIG_REQUIRED=1
DISTRIBUTION_EXPECTED_ID=freeinference
DISTRIBUTION_TARGET=staging              # 仅作审计/metric label；不与 manifest 比较
~~~

v1 在兼容期仍读取全局 `DISTRIBUTION_CONFIG_MODE`。v2 staging/production 必须显式给出
三个 selector；`REQUIRED=1` 时拒绝仅供应全局 mode，从机制上防止一次切换
三个真值。selector 来自 target deployment environment，不写入 runtime manifest；部署 PR
必须显示完整三元组和前后 diff。

selector 的合并语义是唯一的：v2 只读取上述三个显式 environment value，不从
manifest/target file 再合并第二份值；本地且 `REQUIRED=0` 时的缺失 selector 默认为
`legacy`。selector 为 `legacy` 时对应 runtime resource path 可缺失；`shadow|active` 时
必须存在且 closed-root。`REQUIRED=1` 保证 manifest 和所有已选候选资源 fail-closed，
但不强制尚在 rollout 中的其他 selector 提前变为 `active`。

`REQUIRED` 的软件默认值始终是 false；staging/probe/production 在各自 rollout 阶段显式
设为 true。当 `REQUIRED=1` 时，manifest 缺失/非法、必需 runtime path 缺失/越界、
environment contract 缺少 required no-default 变量、配置不可解析或 normalized hash 不可
生成，都必须在监听端口前抛出 `DistributionStartupError` 并退出进程，不得回
legacy。外部依赖在启动后变为不可用时只使 readiness 失败，不改写已选择的配置真值。

`EXPECTED_ID` 是进程外断言，必须与 runtime manifest ID 匹配。`TARGET` 没有 manifest
比较对象，只能校验枚举并写入 log/metric，不得被宣称为跨环境防错断言；真正的
target/artifact 绑定由 D-P3 release/deployment manifest 提供。`active + required` 的资源不得被
`MODELS_CONFIG_PATH` 等旧环境变量静默覆盖；break-glass 必须使用独立、可审计入口。
fail-closed 必须贯穿 models、routing、default router 和 alerts 的完整 bootstrap，不能被
记录错误后继续/默认回退的兼容 catch 吞掉。

### 决策 8：v2 runtime manifest + v1 strict bundle manifest

runtime `distribution.yaml` 的 v1 在 legacy/shadow 窗口继续可读；v2 对未知字段
`extra=forbid`，只索引 backend runtime 资源，且是 staging/production `required` 的最低版本。

source inventory 从独立的 `bundle.yaml` `bundle_schema_version: 1` 开始：

- 所有 distribution-owned 资源引用必须是相对 bundle root 的规范路径，不允许
  绝对路径、`..`、symlink escape 或本机用户目录；
- upstream backend/client 只能声明为 external input，通过 image/package 引用注入，不允许
  source path；
- `environment_contract` 只声明变量名、`secret|config` 分类、consumer、requiredness
  和是否有 default，绝不保存值；
- runtime 和 bundle schema 均不包含 OCI digest、registry 或 release SemVer；后者属于 D-P3。

## 目标架构

~~~mermaid
flowchart LR
    subgraph UP["HybridInference upstream"]
        API["HTTP · OpenAPI · SSE · Auth"]
        CORE["Gateway · Routing · Storage · Admin"]
        SCHEMA["Config schema · validator · migrations"]
        CONF["Conformance tests"]
    end

    subgraph FI["FreeInference distribution"]
        FIWEB["FreeInference frontend source/image"]
        FICFG["Real models · routing · alerts"]
        FICONT["Brand · Terms · docs · RAG"]
        FIOPS["Deploy · monitoring · machine ops"]
    end

    subgraph FUTURE["Future distribution（腾讯等）"]
        TW["Independent frontend source/image"]
        TC["Own config · policy · deployment"]
    end

    FIWEB -->|"HTTP/SSE contract"| API
    FICFG -->|"validated runtime input"| SCHEMA
    FIOPS -->|"Phase 3: pin artifact"| CORE
    TW -->|"HTTP/SSE contract"| API
    TC -->|"validated runtime input"| SCHEMA

    API --> CORE
    SCHEMA --> CORE
    CONF --> CORE
~~~

依赖关系只有 distribution → upstream；两个 distribution 之间没有依赖边。

## Phase 2 目标目录

~~~text
hybridInference/
  apps/
    backend/
      serving/                 # upstream headless control plane
      routing/                 # upstream routing algorithms

  config/
    examples/
      distribution.yaml
      models.yaml
      routing.yaml
      alerts.yaml

  contracts/
    openapi/
      control-v1.openapi.json
    streams/
      sse-v1.md
    fixtures/
      independent-frontend/    # own package/lock/build；不使用 workspace import

  packages/
    control-client-typescript/ # optional framework-neutral generated client；不发布到 registry

  deploy/
    community/                 # neutral self-host examples

  tests/
    ...                        # upstream contracts/conformance

  distributions/
    freeinference/
      distribution.yaml        # backend runtime manifest v2
      bundle.yaml              # distribution-owned source inventory v1
      bundle.lock.json          # declared-resource digests；排除自身，不含 Secret 值
      frontend/                # complete current Next.js app
        package.json
        package-lock.json
        src/
        public/
        Dockerfile
      config/
        models.yaml
        routing.yaml
        alerts.yaml
        environment-contract.yaml # secret|config 引用契约；不存值
      branding/
        site.yaml              # frontend/status 共用时的唯一品牌真值
        status.yaml
        logos/
        team.yaml
        sponsors.yaml
      content/
        legal/                 # Terms/privacy 的唯一真值；frontend build 消费
        email/
        signup/
      docs/                    # user docs / public corpus
      rag/
        rag.yaml
        docs_index.json
        docs_index.meta.json
      deploy/
        compose.yaml           # 只消费注入的 upstream image ref；不读 ../../source
        systemd/
        nginx/
        cloudflare/
      ops/
      monitoring/
      targets/
        staging.yaml
        production.yaml
        conformance/
      tests/
~~~

如果某项未来需要更严格访问控制，可以在 Phase 4 后再从 FreeInference repo 内部拆分；
Phase 2 不新增职责模糊的 `operations` repo。

## Frontend ↔ Upstream 契约

### 1. OpenAPI 是普通 HTTP 接口的真值

Phase 2 先定义 frontend 使用的 control-surface allowlist；不能把含 internal/compat/运维接口的
整个 FastAPI `/openapi.json` 都无差别承诺为永久公共 API。对 allowlist 固定并测试：

- endpoint path、method、operation ID；
- request/response schema 与 error envelope；
- auth requirement 和 admin/user role boundary；
- pagination、filter 和 enum 语义；
- deprecation 与兼容周期。

CI 从 FastAPI schema 生成 deterministic control OpenAPI snapshot；结构性变化必须在 PR 中
显式 review。Phase 2 可以生成 framework-neutral TypeScript client source 并用 isolated
`npm pack` 做边界测试，但 registry/NPM publication 属于 Phase 3。

Wave 1 必须盘点当前 frontend 的全部 rewrite，并将每个 endpoint 标为 `stable-control`、
`inference-protocol`、`compatibility`、`distribution-private` 或 `ops-only`。盘点至少覆盖
`/auth`、`/user`、`/admin`、`/v1`、`/anthropic`、`/internal/playground`、`/site-updates`、
`/site-config` 和 `/health`；不允许用“等”隐藏未分类路径。

### 2. SSE 与流式错误单独建契约

OpenAPI 不能完整描述 streaming framing。conformance tests 至少覆盖：

- 首包和终止帧；
- `[DONE]`/等价终止语义；
- disconnect、timeout、provider error 和 client cancellation；
- content-type、cache/proxy headers；
- Anthropic/OpenAI 兼容路径。

前端搬移不能改变 middleware 顺序或引入 response buffering。

### 3. Auth/session 默认同 origin

发行版 frontend 通过自己的 reverse proxy 按上述已审计 inventory 转发必需路径
到 backend，维持现有 cookie、refresh 和 CSRF/CORS 假设。独立 repo/独立 image 不等于浏览器
必须跨 origin。

如果真实腾讯部署需要不同 origin，再根据其 domain/cookie/IdP 需求独立设计；不能在 Phase 2
猜测性放宽 CORS 或 cookie scope。

### 4. `/capabilities` 返回 effective truth

初始公共响应建议：

~~~json
{
  "schema_version": 1,
  "control_api_version": "1.0",
  "capabilities": {
    "auth.password": true,
    "auth.public_signup": true,
    "auth.email_verification": true,
    "user.api_keys": true,
    "user.usage": true,
    "playground.chat": true,
    "rag.chat": true,
    "admin.core": true,
    "admin.routing.routewise": true
  }
}
~~~

约束：

- 值来自已注册 route、effective settings 和依赖 readiness，不只来自 manifest 文本。
- 不返回 provider key、内部 host、model endpoint、DB、runner 或网络拓扑。
- `schema_version` 只版本化该 response DTO；`control_api_version` 版本化 stable-control
  operation/semantics。两者由 upstream maintainer 维护，client 在构建和 conformance 中
  声明支持范围；不引入第三个含义重叠的 feature version。
- capability ID 和字段只追加、不静默改义；删除或改义需要 response schema
  或 control API major version。
- distribution 声明与 effective capability 不一致时记录结构化告警；required 模式下，声明
  为必须启用但依赖缺失的能力使 readiness 失败。
- capability 只用于 discovery/rendering，不是授权；session/me 响应应返回 backend 计算的
  permission，frontend 不应复制当前 `ROLE_RANK` 作为安全真值。
- capability ID 使用 namespace（例如 `user.api_keys`、`rag.chat`、
  `admin.routing.routewise`），未知 ID 可忽略；公共响应不暴露 unavailable 的内部原因。

### 5. Playground 从 internal path 退出

当前 FreeInference frontend 依赖 `/internal/playground/models` 和
`/internal/playground/chat`。这两个 path 不能一边叫 internal、一边被当作第二前端的
稳定契约。Wave 1 增加对等 stable-control operations（命名在 API review 中固定，
建议归入 `/control/v1/playground/*`），并将旧 path 保留为限时 compatibility alias。
`playground.chat=true` 只在 stable operation 已注册且依赖 ready 时返回。Wave 2 退出前
FreeInference frontend 必须切到 stable path，alias 的删除时点通过 deprecation policy 决定。

### 6. Control API error contract

当前 frontend API client 仍会解析英文 error message 来决定 UI 分支，这不能成为第二前端的
稳定契约。D-P2 为 frontend 使用的 auth/user/admin surface 定义统一 envelope：

~~~json
{
  "error": {
    "code": "EMAIL_NOT_VERIFIED",
    "message": "Human-readable detail",
    "details": {}
  },
  "request_id": "..."
}
~~~

- frontend 只按稳定 `code` 分支，`message` 仅展示或记录；
- 现有 message 与 status code 在兼容窗口内保留；
- 429 等标准语义继续使用 `Retry-After`；
- 新稳定 operation 必须有显式 operation ID、request/response model 和 error code 集合；
- 不要求本阶段把所有 route 迁到新 URL prefix；control surface allowlist 与 schema version
  已足以建立契约，避免把大规模 endpoint rename 混进内容搬迁。

### 7. `/site-config` 的兼容终态

Phase 2 保留现有 endpoint 与 response shape，供迁移中的 legacy frontend 使用。完整
FreeInference frontend 切到发行版路径后，可以继续使用其中 identity 字段，也可以完全由
自己的 build/runtime config 管理；upstream 不再为适配任意站点页面扩展该 endpoint。

## Distribution Config 与 closed-root validation

### Schema 策略

D-P2 的 runtime 和 source inventory 使用两个 schema，不允许 backend 启动器解析
bundle inventory。示意结构如下；实现 PR 可以调整字段名，但不能合并两个
failure domain。

`distribution.yaml` 只包含 backend runtime 输入：

~~~yaml
schema_version: 2

distribution:
  id: freeinference
  display_name: FreeInference

site:
  base_url: https://freeinference.org
  docs_url: https://doc.freeinference.org/
  status_url: https://status.freeinference.org/
  support_email: admin@freeinference.org

features:
  auth.public_signup: true
  rag.chat: true
  admin.routing.routewise: true

resources:
  gateway:
    # 下列是 D-P2 终态；rollout 中 selector=legacy 的项可暂时缺失
    models: config/models.yaml
    routing: config/routing.yaml
    alerts: config/alerts.yaml
  rag:
    settings: rag/rag.yaml
    corpus: docs/
    index: rag/docs_index.json
    metadata: rag/docs_index.meta.json

environment_contract: config/environment-contract.yaml
~~~

`site` 仅保留当前 `/site-config` 兼容所需的安全 identity；公开响应仍经过独立
DTO allowlist，且绝不返回本地路径。frontend、Terms、status config、targets、deploy 和
ops 不得出现在此 schema 中。

`bundle.yaml` 只由 CI/打包工具消费：

~~~yaml
bundle_schema_version: 1
runtime_manifest: distribution.yaml

external_inputs:
  upstream_backend_image:
    kind: image-ref
    from_env: HYBRIDINFERENCE_BACKEND_IMAGE
  control_client_package:
    kind: npm-tarball-ref
    from_env: HYBRIDINFERENCE_CONTROL_CLIENT_PACKAGE
    required: false

resources:
  frontend:
    path: frontend/
    classification: private
  branding:
    path: branding/
    classification: public
  legal:
    path: content/legal/
    classification: public
  docs:
    path: docs/
    classification: public
  rag:
    path: rag/
    classification: generated
  monitoring:
    path: monitoring/
    classification: private
  targets:
    path: targets/
    classification: private
  deployment:
    path: deploy/
    classification: private
  operations:
    path: ops/
    classification: private

environment_contract: config/environment-contract.yaml
~~~

`public` 只表示可以进入未来 public materializer 的候选内容，不表示 backend 可原样
发布。`private`、`generated` 和 environment reference 永不通过公共 endpoint 暴露。

runtime v1 在迁移期保留给 legacy/shadow；runtime v2 和 bundle v1 对所有已知
section 均 `extra=forbid`。staging/production `required` 只接受 runtime v2。

strict validator 必须：

1. 分别校验 runtime/bundle schema，拒绝未知顶层和已知 section 内字段；
2. 解析所有 distribution-owned 相对路径并验证最终路径仍位于 root；
3. 拒绝越界 symlink、NUL、目录替代文件和缺失必需文件；
4. 使用真实 models/routing/alerts/RAG/status/target parser 验证对应内容；
5. 验证 `${VAR}`/`${VAR:-default}` 引用全部在 `environment-contract.yaml` 按
   `secret|config` 分类声明，但不展开或记录值；
6. 输出 canonical normalized config、resource inventory 与 SHA-256，不输出 Secret；
7. 支持 `--root` 指向复制后的临时目录，证明验证不依赖 monorepo layout；
8. 以非零 exit code 区分 schema、path、semantic、environment-contract 和 expectation
   mismatch；
9. 生成 `bundle.lock.json`，列出 bundle 声明资源的类型与 digest，禁止未索引
   的运行/构建依赖。

lock 的 canonical 规则是：从 `bundle.yaml` 声明的目录递归枚举 regular file，路径转为
POSIX 形式并按 UTF-8 byte order 排序，对 raw bytes 做 SHA-256，同时记录 file type/mode。
`bundle.lock.json` 自身、`.git/`、build cache 和未声明的临时产物永久排除；校验不依赖
Git tracked state，因此 detached copy 没有 `.git` 也能得到同一结果。

命令名在实现 PR 中确定，语义等价于：

~~~text
hybridinference distribution runtime-validate \
  --manifest distributions/freeinference/distribution.yaml \
  --closed-root \
  --strict

hybridinference distribution bundle-validate \
  --bundle distributions/freeinference/bundle.yaml \
  --closed-root \
  --strict
~~~

CI 对 bundle 中全部 consumer 只检查 environment reference 的语法、分类和声明完整性。
backend 运行时只检查 `consumer: backend` 且被当前 `shadow|active` 资源引用的
required no-default 变量，以及与 selector 无关的必需 backend 设置；它不检查
frontend/deploy/ops consumer，也不因 `legacy` 资源的候选变量阻止启动。各独立 consumer
在自己的 preflight 检查其 required no-default 值存在且非空。shadow semantic diff 在变量展开前比较 raw
parsed/canonicalized 配置；任何 diff/log 都不得包含展开后的 key、token、
password、webhook 或普通 config value。

### Feature declaration 语义

manifest 中 `features` 表示 distribution 的**期望与政策**，不是前端 capability 的直接真值：

- `true`：发行版要求该能力可用；required 模式下，静态实现/配置缺失在 bind 前
  失败，运行期外部依赖不可用则使 readiness 失败；
- `false`：发行版明确关闭该能力；
- `null`/缺失：不作发行版承诺，由 effective runtime 决定。

`routers` 列表同样是 expectation；声明 `routewise` 但实现未注册时 strict validation 失败。

### Backend image 中立化

Phase 2 结束前，upstream backend image 不得复制 FreeInference 真实配置。允许两种中立行为：

1. 镜像只携带可启动的 `config/examples/*`；或
2. 镜像不携带 config，并要求 self-host compose 显式 mount 中立样例。

FreeInference Compose 必须显式 mount 自己的 distribution root，并设置 manifest path。
manifest required 时，mount 缺失不得回退到镜像内默认值。

## 前端迁移设计

### 为什么移动整个应用

当前 Admin、Dashboard 与 landing 在 import graph、route tree、API client 和 product copy 上高度
交织。逐组件裁定并抽成共享 React package会同时扩大重构面和生产 UI 风险；而把整个应用
归 FreeInference 只需改变路径、构建 context 和 ownership，不改变浏览器输出。

因此首次移动要求：

- `git mv apps/frontend distributions/freeinference/frontend`；
- package name、image name、service name、port、rewrite 和 env contract 暂时不变；
- 同一 commit 只修改路径引用、CI working-directory、Docker COPY 和 compose overlay；
- 不同时重做 theme、页面、auth state、query cache 或 API client；
- 通过 DOM/route snapshot、关键页面截图或浏览器 smoke 证明行为等价。

Wave 2 移动后，当前嵌在 frontend 中的 branding/Terms/team/sponsor 先随应用保持唯一
真值，`branding/` 和 `content/legal/` 不得同时出现可编辑副本。Wave 3 在同一个可回退
PR 中将需被多个 distribution consumer 使用的内容移入独立目录，更新 frontend
build/runtime consumer，然后删除旧 TSX/YAML/asset 副本。最终 Terms/privacy 以
`content/legal/` 为 canonical，共用品牌以 `branding/` 为 canonical；只被 frontend 使用的页面 copy
可继续留在 `frontend/`。`bundle.yaml` 永远只索引当前 canonical path。

### Upstream reference console

Phase 2 不通过复制当前应用制造一个“看似中立但继续携带运营假设”的 reference console。
后续若需要，可以从 OpenAPI/SSE contract 新建一个独立、最小、可选应用。它必须：

- 不被 backend import 或启动所要求；
- 不含 FreeInference/Tencent 品牌、Terms 或运营页面；
- 不成为下游必须 fork 的模板；
- 使用与其他外部前端相同的网络契约。

是否在 public beta 前提供 reference console 是待决策，但不阻塞 Phase 2 的发行版闭合。

### Phase 2 的构建方式

在 Phase 3 之前继续 source-build，但把构建所有权和 root boundary 变清楚：

- upstream backend Dockerfile 只使用 upstream source；
- FreeInference frontend Dockerfile 位于 distribution root；
- workflow 先用 upstream-only build context 构建当前 source SHA 的本地 backend image，以
  `hybridinference-backend:local-${DEPLOY_SHA}` 类型的 SHA-specific tag 和
  `org.opencontainers.image.revision=${DEPLOY_SHA}` label 标识，再通过
  `HYBRIDINFERENCE_BACKEND_IMAGE` 将本地 image ref 注入 FreeInference Compose；
- FreeInference Compose 只组合注入的 upstream backend image 与 distribution frontend，不使用
  `build.context: ../..`、root Dockerfile 或任何 upstream source path；
- staging/production workflow 文件仍原位且 checkout 同一 source SHA；它们是 Phase 4 才迁仓的
  orchestration，不是 distribution bundle 的 runtime/build input；
- Compose 前在同一 Docker context/daemon 执行 `docker image inspect`，校验注入的 tag
  存在且 revision label 等于 `DEPLOY_SHA`；拒绝 `latest` 或可能复用旧镜像的 mutable tag；
- CI 分别命名并运行 `Upstream Gate` 与 `FreeInference Distribution Gate`。

detached-copy gate 不构建 backend source：它注入一个 CI 事先构建的本地 image ref，再执行
frontend build、Compose render 和 distribution-owned checks。这保证 closed-root，又不提前要求公共
registry。Phase 3 再把两者发布成独立 digest，并由 deployment manifest 组合。

## 内容迁移映射

| 当前内容 | Phase 2 目标 | 风险等级 | 说明 |
|---|---|---:|---|
| `apps/frontend/**` | `distributions/freeinference/frontend/**` | B | 整体移动，浏览器行为不变 |
| `config/models.yaml` | `distributions/freeinference/config/models.yaml` | A | 最后切真值，normalized hash 对比 |
| `config/routing.yaml` | `distributions/freeinference/config/routing.yaml` | A | 不调整 weight/router 参数 |
| `config/alerts.yaml` | `distributions/freeinference/config/alerts.yaml` | A | 不调整 threshold/sink |
| `docs/free_inference/**` | `distributions/freeinference/docs/**` | B | RAG corpus 跟随移动 |
| prebuilt RAG index | `distributions/freeinference/rag/**` | B/A | 先复制/验证，再切 path |
| FreeInference Terms/privacy/共用品牌 | `content/legal/**` + `branding/**` | B | 更新 consumer 后同 PR 删除旧副本 |
| 只被 frontend 消费的站点 copy | `frontend/**` | B | generic mechanism 留 upstream，copy 随应用 |
| status monitor 站点 config/assets | `monitoring/**` | B | worker mechanism 可留 upstream |
| harness 真实 targets | `targets/**` | B | generic runner/scenario 留 upstream |
| host/systemd/nginx/cloudflare | `deploy/**` | B | 不改变 live target |
| spark/H200/backup/DB ops | `ops/**` | B | 通用机制先拆，具体机器全部移动 |
| `ops/db/analysis/**` | private controlled subtree | B | 不进入公开 upstream/export |
| production/staging workflows | 原位 | — | Phase 2 只改引用；Phase 4 才迁仓 |

“B”表示内容/源码归属移动，可由 revert 回滚；“A”表示生产真值切换，必须走完整
shadow/probe/active ladder（已有流量切分时可加 canary）。

## 迁移状态机

每一类内容独立通过以下状态，不允许一个全局开关同时切换全部类别：

~~~text
LEGACY
  → MIRRORED
  → DARK_VALIDATED
  → STAGING_ACTIVE
  → PRODUCTION_SHADOW
  → PRODUCTION_PROBE
  → PRODUCTION_ACTIVE_REQUIRED
  → LEGACY_REMOVED
~~~

- `LEGACY`：旧路径是唯一真值。
- `MIRRORED`：新目录存在，但没有 runtime consumer。
- `DARK_VALIDATED`：加载并比较 canonical content/hash，不改变行为。
- `STAGING_ACTIVE`：staging 从新路径读取，旧路径保留。
- `PRODUCTION_SHADOW`：现有 production 实例只加载/比较新资源。
- `PRODUCTION_PROBE`：在独立端口启动不接用户流量的并行实例，使用 production
  environment 完成 characterization/synthetic smoke 后销毁。如现有基础设施已支持
  cohort routing，可再加小流量 canary；D-P2 不为此新增流量切分控制面。
- `PRODUCTION_ACTIVE_REQUIRED`：全量使用新路径，缺失即 fail closed。
- `LEGACY_REMOVED`：经过观察窗口后删除旧副本与 fallback。

任何 mismatch 都回到上一状态；不能通过“接受差异”继续，除非差异是单独评审的产品变更。
状态机是 rollout evidence，写入 target 的 deployment record/PR，不由 backend 解析。只有
models/routing/alerts 三个 runtime 真值由显式 selector 执行；frontend、RAG、status 等
其他 consumer 各有自己的路径/部署 PR 和 rollback。紧急环境变量只能作为审计化
break-glass override，不得用一个全局 mode 同时切换多个真值。
迁移期间永远只有一个可编辑 canonical source：shadow 前旧文件可编辑、overlay 是 mirror；
active 后 overlay 可编辑、旧文件冻结为限时回滚副本。禁止双向编辑和跨 root symlink。

## Phase 2 迁移波次

### Wave 0：冻结边界与基线

1. 合入并更新全仓 ownership classification，明确完整 frontend 归 FreeInference。
2. 记录当前 frontend route/HTML smoke、backend API/SSE contract、effective config hash。
3. 记录 known-good source SHA、image、DB revision 与现有 rollback 命令。
4. 确认 staging 已显式完成 Phase 1 dark-load smoke；不能只以“服务正常启动”代替。
5. 增加 distribution closed-root 与 upstream-without-distributions CI skeleton。

**退出条件：** 基线可重复；边界无 `unknown`；不移动生产文件。

### Wave 1：契约与 strict validation

1. 定义 control-surface allowlist，增加 deterministic OpenAPI diff gate 与 SSE/Auth contract
   tests。
2. 增加 stable error code/envelope、`/capabilities` effective endpoint 与 permission contract。
3. 生成可选 framework-neutral TypeScript client，并通过 isolated `npm pack` 验证。
4. 增加 runtime v2 validator、bundle v1 validator、environment contract 与 deterministic lock。
5. 增加分资源 selector、`DISTRIBUTION_CONFIG_REQUIRED`/expected-id/target，默认关闭，
   并让 fail-closed 贯穿 bootstrap preflight。
6. 增加 canonical effective config/hash 日志与 mismatch 指标。
7. 加入一个不使用 FreeInference 商标的 minimal independent frontend fixture；它有自己的
   `package.json`、lockfile 和 build，只消费 packed client 或 OpenAPI/SSE/Auth contract，
   完成 login/session/models/API-key/streaming smoke。它不是腾讯产品，也不是第二个
   production distribution，但必须在隔离目录中真实 build。

**退出条件：** synthetic runtime/bundle fixture 及当前已迁资源可在临时 root 中通过
strict validator；minimal independent frontend 不 import FreeInference 代码并独立 build；旧
production 行为不变。真实 FreeInference `bundle-validate --all`/detached-copy 在后续资源
尚未迁入时必然不完整，只在 Wave 6/总验收变为必须 gate。

### Wave 2：完整 frontend 归属移动

1. 整体移动当前 Next.js app 到 distribution root。
2. 更新 frontend quality、Docker build、Compose 和 Makefile 路径。
3. 保持 image/service/env/rewrite/port contract 不变。
4. 将 Playground 从 `/internal/playground/*` 切到新 stable-control operation，旧 path 只作
   compatibility alias。
5. staging 验证首页、login/signup、Dashboard、API key、usage、Playground、Admin 与 Terms。
6. production 通过普通发布验证；本 wave 不启用新 backend config path。

**退出条件：** current production UI 无可观测回归；upstream source tree 不再含
FreeInference React/Next 页面或品牌资产。

### Wave 3：Docs、RAG 与站点内容

1. 移动 `docs/free_inference` 与 RAG corpus。
2. 参数化 RAG index/corpus path；index metadata 记录 corpus/chunk text digest、embedding
   model/dimension 与 chunker version。embedding float 可能不确定，不以向量文件 byte hash
   作为语义相等标准。
3. 移动 backend-consumed email/站点内容；通用模板机制留 upstream。
4. 将 Terms/privacy 和被 frontend/status 共用的 branding 切到 `content/legal/`/
   `branding/` canonical source，更新 consumer 并在同 PR 删除嵌入副本。
5. 更新 docs build、RAG rebuild 与 deploy 引用。

**退出条件：** docs/RAG build 只读取 distribution root；RAG 请求与当前结果契约兼容。

### Wave 4：Targets、monitoring 与 machine ops

1. 分离 generic status worker 与 FreeInference wrangler/dashboard/target config。
2. 分离 generic harness runner/scenario 与真实站点 targets。
3. 移动 systemd、nginx、Cloudflare、spark/H200、backup 和站点 admin ops。
4. 对 mixed 脚本先提取小型通用机制，再移动具体 host/profile。

**退出条件：** upstream 不含真实 host、account/database identifier、backup location 或站点
target；现有 deploy workflow 仍能从新位置调用相同行为。

### Wave 5：真实 config 真值切换（Tier A）

顺序固定为 alerts → models → routing，或由运营方根据 blast radius 评审后调整；三者不能在
同一 PR/rollout 同时切换。

例如 alerts 首先切换时，selector tuple 是
`models=legacy,routing=legacy,alerts=shadow|active`；runtime manifest 只要求 alerts 候选 path
closed-root，不要求 models/routing 提前 mirror。这条件必须有单元和启动测试，否则
`REQUIRED=1` 会把分项 rollout 误变成全量迁移。

每一项执行：

1. mirror 到 `distributions/freeinference/config/`；
2. strict parse + canonical hash 与旧路径比较；
3. staging dark load；
4. staging active + required；
5. production dark load；
6. production parallel probe，若现有设施支持则再加小流量 canary；
7. production active + required；
8. 观察窗口后删除旧副本。

**退出条件：** overlay 是唯一生产配置真值；API/SSE/auth/quota/routing/usage SLO 无回归。

### Wave 6：上游中立清理

1. backend image 删除 FreeInference config copy，只保留 neutral examples 或显式 mount。
2. `.env.example`、README、自部署示例与默认 policy 中立化。
3. 清除 upstream roots 中的 FreeInference/Harvard 域名、品牌、Terms、机器和备份位置。
4. 运行 public-surface、license/asset 和 Secret scan；这里只修当前树，历史导出由公开阶段处理。
5. 在不含 `distributions/` 的合成 checkout 中构建、测试并运行 neutral smoke。
6. 在 detached copy 中注入事先构建的 upstream image，对真实 FreeInference bundle
   执行完整 `bundle-validate --all`、frontend/docs/RAG/Compose/ops gate。

**退出条件：** 主设计 Phase 2 验收全部满足，可以开始 Phase 3 artifact 工作。

## CI 与边界门禁

### Upstream Gate

必须在**看不到 `distributions/`** 的工作树或构建 context 中执行：

- backend package/build/import；
- unit/API/SSE/Auth/config tests；
- neutral example config validation 与启动 smoke；
- Dockerfile 确认不 COPY production config 或 distribution；
- denylist/allowlist 审查生产域名、内部 host、品牌和站点 Terms；
- OpenAPI deterministic diff。

### FreeInference Distribution Gate

必须执行：

- runtime v2 与 bundle v1 strict/closed-root validation；
- distribution 内所有 referenced path 存在且不越界；
- frontend lint/typecheck/test/build；
- docs/RAG build 和 source/hash checks；
- status/harness target tests；
- Secret scan；
- Compose config render；
- 禁止相对 import 或 build context 越出 distribution root；upstream 只能作为 bundle
  声明的 external image/package ref 注入。
- detached-copy test：复制整个 overlay 到随机临时路径、隐藏原 repo root，注入 CI 事先
  构建的 backend image ref，再执行 validator、frontend build、distribution user-doc
  linkcheck、template render、RAG metadata/query smoke、Compose render 与 ops static checks；
- `bundle.lock.json` 二次生成 byte-identical，且在无 `.git` 的 detached copy 中校验通过。

overlay README/架构记录中指向 upstream 设计的治理链接不是 runtime/build/data/deploy input，
可使用仓库或 HTTPS 链接，并从 detached runtime linkcheck 排除。用户文档、frontend content、
RAG corpus 和 Compose/include 没有这个例外。

Phase 2 monorepo 中两个 gate 都可以使用同一 source SHA，但输出与失败归属必须分开。公共 CI
runner 与 artifact publication 属于后续公开/Phase 3 工作。

### Boundary lint

至少检查：

1. upstream Python/TypeScript 不包含 `distributions.freeinference`、相对路径或动态读取；
2. distribution manifest/path/symlink realpath 均在 root 内；
3. distribution frontend 不从 `apps/frontend` 或 upstream React source import；
4. upstream Docker build context 排除 distribution 与 production config；
5. 新增顶层/二级目录必须更新 ownership classification；
6. 新 workflow 默认视为 distribution orchestration，除非证明不需要站点 Secret/runner；
   Phase 2 可原位修改路径/注入引用，Phase 4 才迁入 distribution repo。
7. overlay 外的 FreeInference/Harvard 品牌、生产域名、host、backup path 和租户 identifier
   必须为零；迁移期 allowlist 必须有 owner、原因和 expiry，并随每个 wave 单调缩小。

## Staging、Production Probe 与回滚

### 观测维度

每次 Tier A 切换至少在 staging、production shadow/probe 和 active deployment 观察：

- request success/5xx；
- TTFT、end-to-end latency；
- SSE disconnect/abnormal termination；
- auth、refresh、signup、quota outcome；
- provider selection、fallback rate、routing decision hash；
- model catalog 与 config hash；
- frontend route error、JS exception 和关键页面 smoke；
- RAG/status/notification task success。

### 回滚原则

- Tier B path move：revert 单一 PR，恢复旧 build/input path。
- Tier A config：切回 legacy path/旧 config revision，不改 DB。
- required 模式的静态 preflight 失败：probe 进程在 bind 前退出；不得为了启动
  临时关闭 validation 后继续 rollout。
- frontend path move：保留上一 known-good image/source SHA；不同时更新 dependencies。
- 任一 rollback 后重新执行 readiness、API、SSE 与 frontend smoke。

具体 SLO 阈值、probe/active soak 时长以及可选 canary 百分比由运营基线决定，必须在
每个 Tier A rollout PR 中写明，
不能用“无明显异常”代替预声明 gate。

## 验收测试（Acceptance Tests）

### Closed-root

1. 将 distribution 复制到随机临时目录、注入外部 backend image ref 后，runtime/bundle
   strict validation 通过。
2. 删除 monorepo 根 `config/` 后 distribution validation 仍通过。
3. `../config/models.yaml` 被拒绝。
4. 指向 root 外的 symlink 被拒绝。
5. runtime 或 bundle manifest typo/未知字段在 strict gate 中失败。
6. Compose 对 `../../` backend source/Dockerfile 的引用被拒绝。
7. required 模式缺失 static manifest/config/environment requirement 时进程在 bind 前退出，
   不回 legacy；运行期外部依赖失效时 readiness 失败。

### Frontend independence

1. FreeInference frontend 从 distribution 路径独立 lint/typecheck/test/build。
2. 构建 context 不包含 upstream React source。
3. 删除 `/site-config` 的非必要 branding 字段不影响发行版自有 landing/Terms/assets。
4. minimal independent frontend 在自己的 package/lockfile/build context 中，只根据
   OpenAPI/SSE/Auth contract 完成 login、列模型、创建 API key 和一次 streaming
   smoke，不 import FreeInference 代码或 workspace source。
5. 当前关键 route、rewrite、cookie refresh 和 admin role behavior 与移动前一致。

### Upstream neutrality

1. 排除 `distributions/**` 后 backend package、tests 与 neutral Docker build 通过。
2. neutral profile 不出现 FreeInference、Harvard、官方域名、具体 host、Terms 或 analytics。
3. neutral examples 包含可解析的 models/routing/alerts，不引用不存在的文件。
4. backend image 不携带 FreeInference 真实 model/provider catalog。

### Production parity

1. legacy 与 distribution normalized config/hash 完全一致。
2. model catalog、router selection、provider weights 和 alert rules 等价。
3. API/Auth/Quota/SSE characterization tests 全通过。
4. frontend 核心页面与当前生产行为等价。
5. staging 至少完成一次前滚和一次独立回滚演练。

## 风险与缓解

### 风险 1：移动整个 frontend 影响现有 CD

**缓解：** 只改变路径引用，保留 image/service/env/port/rewrite；不同时升级 Node/Next 或依赖；
先 CI build，再 staging 全 route smoke，最后普通 production 发布。

### 风险 2：上游暂时没有官方 UI

这是 headless 边界的显式结果，而不是功能丢失：FreeInference UI 继续完整存在。若 public beta
确实需要 reference console，用公共契约另建最小应用；不从生产 UI 反向抽象一个必需框架。

### 风险 3：未来前端重复 API client 代码

**缓解：** Phase 2 固定 OpenAPI/SSE/Auth contract；Phase 3 可发布 generated TypeScript SDK。
少量客户端重复优于共享 Next route/branding/legal 源码造成的强耦合。

### 风险 4：`/capabilities` 与实际能力漂移

**缓解：** 从 route registration、effective settings 和 dependency readiness 生成；manifest 只作
expectation；增加 mismatch metric 与 required-mode gate。

### 风险 5：fail-closed 配置导致启动失败

**缓解：** required 的软件默认关闭，按 dark → staging → production shadow/probe →
active 渐进启用；旧实例在新实例通过
readiness 前不退出流量。

### 风险 6：真实 config 移动悄然改变 routing

**缓解：** canonical normalization/hash、routing replay、一次一文件、禁止同 PR 调参、保留
legacy path 和 known-good config revision。

### 风险 7：mixed 文件无法完整移动

**缓解：** 只提取被两个真实消费者需要的最小机制；站点数据直接移动；无法判断时标记
`unknown` 并阻塞该 wave，不默认留 upstream。

### 风险 8：Phase 2 与 artifact/CD 工作互相踩踏

**缓解：** Phase 2 保持 source-build，CI/CD 制品设计只消费最终路径；不在同一 PR 同时进行
frontend relocation、OCI publish 和 production digest cutover。

## 被否决的替代方案

### 1. 把 `/site-config` 扩成完整页面 CMS

这会迫使 upstream 定义所有发行版的 landing、Terms、theme、navigation 与组件 schema，
既不能保证真正独立，也形成长期兼容负担。

### 2. 抽取一个所有发行版必须使用的 React/Next Console package

当前只有一个真实消费者，边界来自单一实现，过早抽取会把 Next routing、auth state 和 Admin
页面变成难以版本化的插件协议。等两个真实前端证明稳定重合后再按需提取。

### 3. 只移动 landing/Terms，Dashboard/Admin 继续留 upstream

当前 Dashboard/Admin 已含 FreeInference provider、analytics 和运营工作流；拆到组件级会扩大
Phase 2 改动，并让下游仍需接受上游页面结构。完整应用移动更符合独立前端目标。

### 4. 现在直接创建 standalone FreeInference repo

overlay 尚未闭合，config、build、deploy 和 rollback 仍依赖 monorepo。提前拆仓只会把路径问题
变成跨仓事故，不能替代 Phase 2 验证。

### 5. 在 `distribution.yaml` 中记录 frontend image

runtime config 不应承担供应链 lock。image digest 属于 Phase 3 release/deployment manifest，
由发行版 CI/CD 消费，不由 backend loader 解释。

### 6. Phase 2 同时切到 OCI/digest deployment

同时改变源码归属、构建位置和部署单位会让故障无法归因。Phase 2 先证明 source tree boundary，
Phase 3 再证明 artifact boundary。

## Phase 2 完成标准

以下全部满足才可宣布 Phase 2 完成：

- 当前完整 FreeInference frontend 位于 distribution root，并能独立构建测试。
- FreeInference branding、Terms、assets、用户 docs、RAG、真实 targets 和 machine ops 已集中。
- models/routing/alerts 的唯一生产真值位于 distribution root。
- `distribution.yaml` 已升级 runtime v2；`bundle.yaml` v1、deterministic
  `bundle.lock.json` 与 environment contract 验证通过。
- 所有 manifest、data、docs、build 与 deploy 相对路径均 closed-root，detached-copy suite 在随机
  路径通过。
- production 使用 runtime v2，models/routing/alerts 三个 selector 均为 `active` 且
  `REQUIRED=1`；required resource preflight 在监听端口前完成，且
  已完成 staging/production shadow/probe/rollback 演练。
- upstream code/build/tests 在 distribution 不存在时通过。
- upstream backend image 不包含 FreeInference 真实配置或运营内容。
- neutral examples 完整可运行，不包含官方品牌与基础设施假设。
- API/OpenAPI/SSE/Auth contract 足以支持一个不引用 FreeInference 源码的第二前端 smoke。
- Phase 2 迁移未造成 API key、用户、session、quota、routing、usage 或 SSE 回归。

完成 Phase 2 后，仍不能直接把当前仓库改 public；Secret/history、license/asset、公共 CI 与
RouteWise 发布方式继续受公开前置清单和 Phase 3 门禁约束。

## Phase 3 交接输入

Phase 2 向 Phase 3 提供：

1. 可在没有 distribution 时构建的 neutral upstream source；
2. closed-root、strict-validated 的 FreeInference distribution source；
3. 稳定 control OpenAPI/error/SSE/Auth/capability contract；
4. 独立 backend 与 FreeInference frontend build target；
5. effective config hash 与 conformance suite；
6. source-build 路径上的 staging/production probe/rollback 证据。

Phase 3 随后负责 RouteWise 分发决策、OCI/Wheel/可选 SDK 发布、release manifest、digest pin、
自动 bump PR 和按 digest staging/production；Phase 4 再负责物理拆仓与 workflow/secrets owner
迁移。

## 待决策（Open Decisions）

1. public beta 是否必须同时提供 neutral reference console；若需要，其最小功能范围是什么？
2. `/capabilities` 初始 namespaced ID 集合；DTO/control API 版本 owner 已确定为
   upstream maintainer。
3. 分资源 selector 和 required-mode 环境变量的最终命名；静态错误在 bind 前退出、
   动态依赖进 readiness 的语义已固定。
4. config 三项切换顺序、probe/active soak 时间、可选 canary 比例与自动回滚阈值。
5. `ops/db/analysis` 留在 FreeInference private repo 的受控 subtree，还是另设更严格的私有
   数据分析仓库。
6. FreeInference frontend 的长期 package/image owner 与 release cadence。
7. OpenAPI generated TypeScript SDK 是 Phase 3 正式 artifact，还是由各下游自行生成。

这些决策中，1、5、7 不阻塞 Phase 2 Wave 0/1；2、3 必须在 Wave 1 实现前确定；4 必须在
Wave 5 前确定；6 必须在 Phase 3 artifact publication 前确定。

## 建议 PR 拆分

1. **Docs/ownership：** 评审本文，合入 ownership classification 并修订 frontend 归属。
2. **Boundary gates：** upstream-without-distributions、closed-root lint、临时目录验证。
3. **Contracts：** control OpenAPI、stable errors、SSE/Auth、permissions 与 effective
   `/capabilities`；加入 isolated independent-client fixture。
4. **Runtime v2 / bundle v1 / required config：** 双 validator、environment contract、
   deterministic bundle lock、分资源 selector、canonical hash、bootstrap preflight 与
   required/fail-closed mode。
5. **Frontend relocation：** 完整 Next app 移入 distribution，更新 CI/build/Compose 路径。
6. **Docs/RAG relocation：** corpus/index/path/build 引用迁移。
7. **Monitoring/targets relocation：** status config、harness targets、tests。
8. **Machine ops relocation：** systemd/nginx/cloudflare/spark/H200/backup。
9. **Alerts config cutover：** mirror → dark → staging → production probe → required。
10. **Models config cutover：** 同一迁移梯子，不改 catalog。
11. **Routing config cutover：** 同一迁移梯子，不调参。
12. **Neutral cleanup：** backend image、examples、README/.env 与 public-surface gate。

每个 PR 都必须独立可回退；frontend relocation、三个 config cutover 和 neutral cleanup 不能
压缩成一个大 PR。
