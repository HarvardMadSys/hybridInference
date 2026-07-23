# 仓库归属分类清单（Ownership Classification）

**日期：** 2026-07-17
**更新：** 2026-07-23（D-P2 Wave 0–1）
**状态：** Living document——新增顶层/二级目录时必须同步更新
**Owner：** Murphy + Juncheng
**关系：** 实现
[中立上游与发行版拆分设计](2026-07-16-hybridinference-neutral-upstream-multi-distribution-design.zh.md)
Phase 0 的第 1 项（归属标记）与第 5 项（新增内容归属规则）。迁移动作以主文档
「当前目录迁移映射」为准，本清单负责**全覆盖**与**逐目录裁定**。

## 分类定义

| 标签 | 含义 | 拆分处理 |
|---|---|---|
| `upstream` | 可被任何发行版复用的中立技术内容 | 留在上游，公开随发行物 |
| `freeinference` | 只对 freeinference.org 站点、机器或运营有意义 | 集中到 `distributions/freeinference/` overlay |
| `paper` | 论文实验 artifact（simulator、trace、绘图） | 按主文档「Paper Artifact」独立，不复制生产算法 |
| `mixed` | 同一目录内两类并存 | 先拆内部依赖再移动，本清单给出二级明细 |
| `internal` | 内部工作文档或工具，不属于任何发行物 | 不随上游公开，公开面审计逐份判断 |

风险分级沿用主文档 Tier A（真值切换）/ Tier B（内容搬运，`git revert` 即回滚）。
`unknown` 只用于评审中的临时判断，不能进入 machine-readable ownership policy。

## 顶层目录总表

以当前工作树的顶层/二级目录为覆盖基准。D-P2 Wave 1 新增 `contracts/`；当前共
13 个顶层目录与 17 个 tracked 根文件。生成目录（`.git/`、`.codegraph/`、
`.venv/`、`.pytest_cache/`、`.ruff_cache/`、`node_modules/`、`var/`）不属于清单。

| 路径 | 归属 | 迁移动作（近期） | Tier |
|---|---|---|---|
| `apps/backend/serving/` | mixed | 机制归 upstream；站点内容（RAG/on-call/邮件模板/默认值）见二级明细 | B |
| `apps/backend/routing/` | upstream | 保持；RouteWise 私有依赖是 D-P3 入口门槛 | — |
| `apps/frontend/` | **freeinference** | 当前完整 Next.js 产品前端整体迁入 `distributions/freeinference/frontend/` | B |
| `benchmark/` | mixed | 按“是否依赖生产数据”分流 upstream benchmark 与 paper | B |
| `config/` | mixed | 真实 YAML 是 FreeInference 生产真值；拆 example 与 production | **A** |
| `contracts/` | upstream | OpenAPI/error/SSE/Auth contract 与无品牌 fixture；禁止 React/Next/站点 copy | — |
| `deploy/` | mixed | 通用 backend 镜像机制留上游；site compose/systemd 移 overlay | B |
| `distributions/` | freeinference | overlay 本体；上游禁止 import，未来可见性边界 | — |
| `docs/` | mixed | developer 也是 mixed（见二级明细）；free_inference 移 overlay | B |
| `ops/` | mixed | 机器脚本/运维集中 overlay，通用机制留 upstream；deploy 脚本当前保持原位 | B |
| `services/` | mixed | worker/testkit 机制留上游；站点配置、品牌与 targets 移 overlay | B |
| `tests/` | mixed | 机制通用；站点 targets 与真实端点用例标记后移 overlay | B |
| `.codex/` | internal | agent skills，不随发行物 | — |
| `.github/` | mixed | CI 机制 upstream；production/staging workflow D-P4 才迁仓 | — |
| `.kilo/` | internal | agent skills，不随发行物 | — |
| 根文件（17 项） | mixed | 见下节逐一裁定 | B |

## 二级明细（mixed 目录）

### apps/backend/serving/（mixed）

- 机制归 upstream：协议兼容、SSE、adapters、auth/quota/storage、admin、
  RAG/on-call 的**框架代码**。
- 站点内容（拆机制与内容后随 overlay/配置走）：

  - `rag/config.py`、`rag/pipeline.py`：FreeInference RAG index 引用与品牌化 prompt；
  - `oncall/config.py`：on-call 默认 URL 等站点默认；
  - 邮件模板中的站点署名与链接；
  - `config/settings.py` 站点默认值、`adapters/openrouter.py` 归因 header；
  - schemas 中的站点示例值（`schemas_auth.py`、`schemas_admin.py`）；
  - `observability/alerts.py` 的站点告警措辞与默认接收端；
  - admin provider/usage-insights 模块中的站点 provider 清单和 API 引用；
  - `servers/routers/user_routes.py` 中的站点文案与联系邮箱；
  - `rag/prebuilt/docs_index.json`（FreeInference 文档预构建索引，数据而非机制）。

以上为类别级清单；Phase 2 拆分时以逐文件 brand/production-token gate 复核。

### apps/frontend/（freeinference）

当前 `apps/frontend/` 的完整 Next.js 应用——包括登录、Dashboard、API key、用量、
模型、Chat/Playground、Admin、landing、Team、Sponsors、Terms、Privacy、共享 chrome、
assets、lockfile、测试与构建配置——整体归 **freeinference**。

本次裁定覆盖旧清单中“通用 Console 留上游”的假设。D-P2 的边界改为**共享协议，
不共享页面**：

- `git mv apps/frontend distributions/freeinference/frontend`，保持浏览器行为等价；
- HybridInference upstream 不保留或复制 FreeInference React/Next 页面；
- 腾讯和未来发行版独立拥有自己的 frontend source、lockfile、CI 与 image；
- 可共享内容仅限 `contracts/` 中的 OpenAPI、SSE/Auth/error contract 与可选无框架 client；
- 不抽取包含 React、Next route、FreeInference branding 或产品政策的共享 UI package。

### benchmark/（mixed）

- `benchmark/local/`、`benchmark/provider/`：通用压测/对比脚本归 upstream；
  引用真实 provider 端点、生产日志的配置与结果归 paper/freeinference。
- 移动前逐文件确认是否含真实 key 引用或内部主机。

### config/（mixed，Tier A）

| 文件 | 归属 |
|---|---|
| `models.yaml` | freeinference **生产真值**——Phase 2 后段单独双读/切换 |
| `routing.yaml` | freeinference **生产真值**——与 models/alerts 分开 rollout |
| `alerts.yaml` | freeinference **生产真值**——优先独立迁移 |
| `examples/` | upstream 中立示例；禁止真实域名、Secret、模型目录和运营参数 |

### contracts/（upstream）

| 内容 | 归属 |
|---|---|
| `openapi/` | upstream：稳定 control-plane API snapshot |
| `fixtures/` | upstream：无品牌、无 Secret、可供独立 frontend 验证的协议 fixture |
| 未来 framework-neutral client | upstream：DTO/fetch/error/auth-refresh/SSE；不得依赖 React/Next |

### deploy/（mixed）

| 内容 | 归属 |
|---|---|
| `docker/Dockerfile.backend` | upstream；只能 COPY backend source 与 `config/examples/**` |
| `docker/Dockerfile.frontend` | freeinference；随完整 frontend 迁移 |
| `docker/docker-compose.yml` | mixed：当前是真实站点组合；D-P2 改为注入 upstream image |
| `systemd/` | freeinference（具体用户、路径、host、服务名） |

共享根 Docker build context 必须通过 `.dockerignore` 排除 `distributions/**` 和真实
`config/*`，只重新包含 `config/examples/**`。

### docs/（mixed）

| 内容 | 归属 |
|---|---|
| `developer/` | mixed：通用开发文档 upstream；staging/fasrc/freeinference/oncall 等站点运维文档归 freeinference |
| `free_inference/` | freeinference 用户文档（RAG corpus 来源） |
| `agents/`、`reviews/`、`superpowers/` | internal；公开面审计逐份判断 |
| `openrouter.md` | upstream（通用 adapter 文档），应归入 `developer/` |
| docs `Makefile` | 机制 upstream；internaldoc 域名和部署目标 freeinference |

### ops/（mixed，FreeInference 主导）

| 内容 | 归属 |
|---|---|
| `ci/` | upstream：change classifier、边界/ownership gate、测试分片机制 |
| `deploy/` | freeinference；当前保持原位（CI/CD 演进路径） |
| `setup/`、`admin/` | mixed：通用引导机制 upstream，站点流程 freeinference |
| `db/` | mixed：通用 DB 工具可留；备份位置、保留策略、cron 属 freeinference |
| `db/analysis/` | freeinference 受控环境——不进任何公共发行物 |
| `spark_idle_proxy/`、`h200_idle_proxy/` | freeinference（具体机器） |
| `local_deployment_proxy/` | mixed：通用进程管理可留，硬件 profile/主机配置移出 |

### services/（mixed）

| 内容 | 归属 |
|---|---|
| `alert-control-plane-worker/` | mixed：通知/事件 engine upstream；真实 sink、destination、站点部署 config 移动 |
| `status-monitor-worker/src/` | mixed：探测/存储机制 upstream；alerts/env/dashboard 品牌和站点 URL 属 freeinference |
| `status-monitor-worker/wrangler.toml` | freeinference（账号/DB/route 标识符） |
| `status-monitor-worker/test/` | mixed：机制测试 upstream；站点 fixture/断言随 branding/targets 移动 |
| `freeinference-harness/` runner/scenario/reporting | mixed：机制 upstream；产品名、CLI 默认值和示例 URL 中立化 |
| harness 真实 models/endpoints targets | freeinference |
| harness 合成 fixtures | upstream；生产形状提取数据在来源/去标识化复核前归 internal |

### tests/（mixed）

| 内容 | 归属 |
|---|---|
| `unit/`、`api/`、`servers/`、`observability/`、`utils/` | mixed：机制与多数用例 upstream；站点值断言随真值迁移 |
| 根级通用测试 | upstream；被测 machine ops 测试跟随被测内容 |
| `integration/`、`external/` 中真实 provider/站点用例 | freeinference targets |
| `e2e/` | upstream 机制；站点参数 freeinference |
| `distributions/<dist>/tests/` | 对应 distribution |

upstream tests 可以验证 distribution schema/contract，但不得 import
`distributions/<name>` 的产品源码。

### .github/（mixed）

| Workflow | 归属 |
|---|---|
| `ci.yml`、`docker-build.yml` | upstream CI；self-hosted runner 配置仍是公开前置 |
| `deploy.yml`、`deploy-staging.yml`、`deploy-rollback.yml`、`sync-main.yml` | freeinference CD |
| `rag-index.yml`、`deploy-status-monitor.yml`、`codex-oncall.yml` | freeinference |

### 根文件

| 文件 | 归属 | 说明 |
|---|---|---|
| `pyproject.toml` | upstream | authors 与 RouteWise git dependency 待处理（D-P3 门槛） |
| `Makefile`、`uv.lock`、`.gitleaks.toml` | upstream | — |
| `.pre-commit-config.yaml`、`.editorconfig`、`.dockerignore`、`.gitignore` | upstream | 通用工程配置 |
| `.gitmodules` | mixed | 机制 upstream；具体 submodule 按内容裁定 |
| `LICENSE` | upstream | MIT；Harvard 版权行保留，下游追加声明 |
| `README.md` / `README.user.md` / `README.developer.md` | mixed | 品牌与域名重写属中立清理；结构 upstream |
| `CLAUDE.md` / `AGENTS.md` | mixed | 通用开发指南 upstream；域名、staging 账号属 freeinference |
| `.env.example` | mixed | 机制 upstream；FreeInference 默认值按三步式迁移 |
| `.env.oncall.example` | freeinference | on-call 联动是站点运维配置 |

## 新增内容归属规则（Phase 0 第 5 项）

新文件/目录落位前**按序**判断——排除性检查在前，upstream 是最后的出口：

0. **含 Secret？** 一票否决：不进 git，使用 environment/Secret Manager。
1. **含站点内容吗？**（真实域名、主机、账号标识、真实模型目录、运营参数、品牌文案）

   - 全部是站点内容 → `freeinference`；
   - 机制与站点内容并存 → `mixed`，先拆；机制 upstream，内容 freeinference。

2. **论文实验专用？** → `paper`，不与生产实现混放。
3. **内部工作文档或 agent 工具？** → `internal`。
4. **以上皆否，且另一个发行版也需要它？** → `upstream`。
5. **拿不准？** → 先评审并更新本清单，禁止默认塞进 upstream。

新 workflow 一律先按 freeinference 处理，除非它不依赖站点 Secret、真实 target 或
self-hosted deployment runner。

## Machine-readable 覆盖门禁

`ops/ci/distribution_boundary_policy.json` 的 `ownership_directories` 是本清单的
machine-readable 索引。`ops/ci/check_distribution_boundaries.py` 枚举工作树实际存在的
全部顶层和二级目录：

- 新目录缺少分类 → CI 失败；
- policy 指向已删除目录 → CI 失败；
- 分类不是 `upstream|freeinference|paper|mixed|internal` → CI 失败；
- ownership 文档缺失 → CI 失败。

该索引只负责目录**覆盖**，本文件仍负责解释 mixed 子树的具体归属。目录搬迁 PR 必须在
同一变更中更新二者。

## 当前自动边界门禁

同一 CI gate 还执行：

- upstream Python/JavaScript/TypeScript 禁止 import `distributions/**`；
- 隐藏 `distributions/` 的合成文件清单与 blocked-import smoke；
- backend Dockerfile 禁止 COPY distribution、仓库根或真实 config；
- `.dockerignore` 必须排除 distribution/真实 config，保留中立 examples；
- brand/production-token 存量按 `path + rule + match_count` 精确登记，并要求
  `owner + reason + expires_on`。

allowlist 匹配数增加、减少、过期或失去 owner/reason 都会失败。清理债务时必须同步
删除或缩小条目，不允许扩大 glob 掩盖迁移。

## 覆盖核对与尚缺证据

当前 `unknown` 为 0；frontend 已按 D-P2 新决策由 `mixed` 改为完整
`freeinference`。归属表和静态门禁不代替以下运营证据：

- 现网 API/SSE/auth/quota/model/routing baseline；
- known-good image、配置 revision、DB schema 与可重复 rollback 记录；
- staging/production dark-load 结果；
- paper artifact、内部设计文档和历史仓库公开范围的法律/社区评审。

这些证据完成前，Wave 0 不能仅凭本文件宣告整体完成。
