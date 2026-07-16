# 仓库归属分类清单(Ownership Classification)

**日期:** 2026-07-17
**状态:** Living document——新增顶层/二级目录时必须同步更新
**关系:** 实现
[中立上游与发行版拆分设计](2026-07-16-hybridinference-neutral-upstream-multi-distribution-design.zh.md)
Phase 0 的第 1 项(归属标记)与第 5 项(新增内容归属规则);当前批次 PR 1。
迁移动作以主文档「当前目录迁移映射」为准,本清单负责**全覆盖**与**逐目录裁定**。

## 分类定义

| 标签 | 含义 | 拆分处理 |
|---|---|---|
| `upstream` | 可被任何发行版复用的中立技术内容 | 留在上游,公开随发行物 |
| `freeinference` | 只对 freeinference.org 站点/机器/运营有意义 | 逐步集中到 `distributions/freeinference/` overlay |
| `paper` | 论文实验 artifact(simulator、trace、绘图) | 按主文档「Paper Artifact」独立;不复制生产算法 |
| `mixed` | 同一目录内两类并存 | 先拆内部依赖再移动;本清单给出二级明细 |
| `internal` | 内部工作文档/工具,不属于任何发行物 | 不随上游公开;公开面审计逐份判断 |

风险分级沿用主文档 Tier A(真值切换)/ Tier B(内容搬运,`git revert` 即回滚)。

## 顶层目录总表

| 路径 | 归属 | 迁移动作(近期) | Tier |
|---|---|---|---|
| `apps/backend/serving/` | upstream | 保持;站点默认值按 P1 三步式参数化 | B |
| `apps/backend/routing/` | upstream | 保持;RouteWise 私有依赖是 Phase 3 入口门槛 | — |
| `apps/frontend/` | mixed | 通用 Console 留上游;站点内容走 6-18 P2(三步式) | B |
| `benchmark/` | mixed | 按"是否依赖生产数据"分流 upstream benchmark 与 paper | B |
| `config/` | mixed | 真实 yaml 为 FreeInference 生产真值;拆 example 与 production | **A** |
| `deploy/` | mixed | 通用镜像留上游;systemd 站点单元移 overlay | B |
| `docs/` | mixed | developer 留上游;free_inference 移 overlay;agents/reviews 为 internal | B |
| `ops/` | 大部分 freeinference | 机器脚本/运维集中 overlay;deploy 脚本当前保持原位 | B |
| `services/` | mixed | 通用 worker/testkit 留上游;站点配置与 targets 移 overlay | B |
| `tests/` | upstream 为主 | 站点 targets(external/integration 真实端点)标记后移 overlay | B |
| `.github/` | mixed | **当前零改动**;仅注释级归属标注(见主文档 CI/CD 演进路径) | — |
| `.kilo/` | internal | agent 技能,不随发行物;留原位 | — |
| 根文件 | mixed | 见下节逐一裁定 | B |

## 二级明细(mixed 目录)

### apps/backend/serving/(upstream,含待参数化站点值)

- 全部代码归 upstream:协议兼容、SSE、adapters、auth/quota/storage、admin。
- 站点值残留(6-18 P1 清单,三步式处理,不改默认):
  `config/settings.py` 的 base_url/smtp/db_name/CORS 默认值、
  `adapters/openrouter.py:11-12` 归因 header、`servers/auth.py` 配额联系邮箱。

### apps/frontend/(mixed)

| 内容 | 归属 |
|---|---|
| Console:登录、Dashboard、API key、用量、模型、Playground、Admin | upstream |
| landing(Hero/Features/CodeExample)、Team、Sponsors、Terms、Privacy | freeinference |
| `layout.tsx` 元数据 + Statcounter 代码块 | freeinference(P2 配置化,默认关) |
| `public/team/`、`public/sponsors/`、`next.config.js` 的 `junchengyang.com` | freeinference(收进 branding 配置作 legacy 默认) |
| signup 页 `@harvard.edu` 快速通道文案、`lib/schemas/auth.ts` | freeinference(配置驱动) |
| `src/config/env.ts` 默认 API base | freeinference 默认值,机制归 upstream |

### benchmark/(mixed)

- `benchmark/local/`、`benchmark/provider/`:通用压测/对比脚本归 upstream
  benchmark;引用真实 provider 端点或生产日志的配置与结果归 paper/freeinference。
  移动前逐文件确认是否含真实 key 引用或内部主机。

### config/(mixed,Tier A)

| 文件 | 归属 |
|---|---|
| `models.yaml` | freeinference **生产真值**——迁移是 Phase 2 最后一步,双读验证 |
| `routing.yaml`、`alerts.yaml` | 同上;上游只保留 example |
| (计划)`examples/` | upstream 中立示例(随 DistributionConfig PR 引入) |

### deploy/(mixed)

| 内容 | 归属 |
|---|---|
| `docker/`(Dockerfile、compose 结构) | upstream;compose 中站点 env/volume 归 freeinference |
| `systemd/` | freeinference(具体主机单元) |

### docs/(mixed)

| 内容 | 归属 |
|---|---|
| `developer/` | upstream 开发文档 |
| `free_inference/` | freeinference 用户文档(RAG corpus 来源) |
| `agents/`(specs/plans)、`reviews/` | internal——含运营细节,公开面审计逐份判断 |
| `superpowers/`(plans/specs) | internal,同上 |
| `openrouter.md` | upstream(通用 adapter 文档),应归入 `developer/` |
| `Makefile`(内部文档站构建) | 机制 upstream;部署目标(internaldoc 域名)freeinference |

### ops/(大部分 freeinference)

| 内容 | 归属 |
|---|---|
| `deploy/`(deploy_staging.sh 等) | freeinference;**当前保持原位**(CI/CD 演进路径) |
| `setup/`、`admin/`(create_admin 等) | mixed:通用引导脚本 upstream,站点流程 freeinference |
| `db/`(backup/archive/export + cron) | freeinference(备份位置、保留策略) |
| `db/analysis/` | freeinference 受控环境——**不进任何发行物**(主文档明确) |
| `spark_idle_proxy/`、`h200_idle_proxy/` | freeinference(具体机器) |
| `local_deployment_proxy/` | mixed:通用进程管理可留 upstream;硬件 profile/主机配置移出(主文档待决策 4) |

### services/(mixed)

| 内容 | 归属 |
|---|---|
| `status-monitor-worker/` 代码 | upstream 可复用 worker |
| `status-monitor-worker/wrangler.toml` | freeinference(账号/DB 标识符、GATEWAY_BASE_URL) |
| `freeinference-harness/` 通用 scenario 与 runner | upstream testkit(目录名本身待中立化) |
| harness 站点 targets(真实模型 ID/端点) | freeinference |

### tests/(upstream 为主)

| 内容 | 归属 |
|---|---|
| `unit/`、`api/`、`servers/`、fixtures | upstream |
| `integration/`、`external/` 中指向真实 provider/站点的用例与配置 | freeinference targets |
| `e2e/` | upstream 机制;站点参数 freeinference |

### .github/(mixed,当前零改动)

| Workflow | 归属 |
|---|---|
| `ci.yml`、`docker-build.yml` | upstream CI(self-hosted runner 是 freeinference 基础设施——公开前置见主文档) |
| `deploy.yml`、`deploy-staging.yml`、`deploy-rollback.yml`、`sync-main.yml` | freeinference CD |
| `rag-index.yml`、`deploy-status-monitor.yml`、codex-oncall | freeinference |

### 根文件

| 文件 | 归属 | 说明 |
|---|---|---|
| `pyproject.toml` | upstream | authors 字段与 RouteWise git 依赖待处理(P5 / Phase 3 门槛) |
| `Makefile`、`uv.lock`、`.gitleaks.toml` | upstream | — |
| `LICENSE` | upstream | MIT;Harvard 版权行保留,下游追加自己的声明 |
| `README.md` / `README.user.md` / `README.developer.md` | mixed | 品牌与域名重写属 P5;结构留 upstream |
| `CLAUDE.md` / `AGENTS.md` | internal | 含 staging 账号、域名;不随发行物 |
| `.env.example` | mixed | 机制 upstream;FreeInference 默认值按三步式 |

## 新增内容归属规则(Phase 0 第 5 项)

新文件/目录落位前按序自问:

1. **另一个发行版部署时也需要它吗?** 是 → upstream(代码进 `apps/`,
   示例进 `config/examples/`,文档进 `docs/developer/`)。
2. **它包含真实域名、主机、账号标识、真实模型目录或运营参数吗?**
   是 → freeinference(overlay 建立前:现有站点位置 + 在本清单登记;
   建立后:直接进 `distributions/freeinference/`)。
3. **它是论文实验专用(simulator/trace/绘图)吗?** 是 → paper,
   不与生产实现混放。
4. **它是内部工作文档或 agent 工具吗?** 是 → internal
   (`docs/agents/`、`.kilo/`)。
5. **拿不准** → 标 `unknown` 提交本清单 PR,评审时裁定;禁止默认塞进 upstream 目录。

一票否决项:任何含 secret 的文件不进 git(env/Secret Manager);
新 workflow 一律先按 freeinference 处理,除非它不依赖任何站点 secret。

## 覆盖核对与统计

顶层条目 13 项全覆盖:upstream 2(serving、routing)、mixed 9、
freeinference 主导 1(ops)、internal 1(.kilo)。`unknown` 当前为 0——
`benchmark/` 二级内容是最接近 unknown 的区域,移动前需逐文件确认。

本清单不移动任何文件、不改变任何行为(Phase 0 验收:不移动生产文件)。
