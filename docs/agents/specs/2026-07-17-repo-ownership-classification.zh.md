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

以 `git ls-tree --name-only HEAD` 的顶层条目为覆盖基准(当前 28 项:11 个目录
+ 17 个根文件;`.claude/` 未被 git 跟踪,不在清单范围)。

| 路径 | 归属 | 迁移动作(近期) | Tier |
|---|---|---|---|
| `apps/backend/serving/` | mixed | 机制归 upstream;站点内容(RAG/on-call/邮件模板/默认值)见二级明细 | B |
| `apps/backend/routing/` | upstream | 保持;RouteWise 私有依赖是 Phase 3 入口门槛 | — |
| `apps/frontend/` | mixed | 通用 Console 留上游;站点内容走 6-18 P2(三步式) | B |
| `benchmark/` | mixed | 按"是否依赖生产数据"分流 upstream benchmark 与 paper | B |
| `config/` | mixed | 真实 yaml 为 FreeInference 生产真值;拆 example 与 production | **A** |
| `deploy/` | mixed | 通用镜像留上游;systemd 站点单元移 overlay | B |
| `distributions/`(#954 引入) | freeinference | overlay 本体;上游禁 import,未来可见性边界 | — |
| `docs/` | mixed | developer 也是 mixed(见二级明细);free_inference 移 overlay | B |
| `ops/` | 大部分 freeinference | 机器脚本/运维集中 overlay;deploy 脚本当前保持原位 | B |
| `services/` | mixed | worker/testkit 机制留上游;站点配置、品牌与 targets 移 overlay | B |
| `tests/` | mixed | 通用为主;站点 targets 与真实端点用例标记后移 overlay | B |
| `.codex/`(skills) | internal | agent 技能,不随发行物;留原位 | — |
| `.github/` | mixed | **当前零改动**;仅注释级归属标注(见主文档 CI/CD 演进路径);`CODEOWNERS` 属 freeinference 治理 | — |
| `.kilo/` | internal | agent 技能,不随发行物;留原位 | — |
| 根文件(17 项) | mixed | 见下节逐一裁定 | B |

## 二级明细(mixed 目录)

### apps/backend/serving/(mixed)

- 机制归 upstream:协议兼容、SSE、adapters、auth/quota/storage、admin、
  RAG/on-call 的**框架代码**。
- 站点内容(拆机制与内容后随 overlay/配置走):
  - `rag/config.py`、`rag/pipeline.py`:FreeInference RAG index 引用与品牌化
    prompt;
  - `oncall/config.py`:on-call 默认 URL 等站点默认;
  - 邮件模板中的站点署名与链接;
  - `config/settings.py` 站点默认值、`adapters/openrouter.py:11-12` 归因
    header、`servers/auth.py` 配额联系邮箱(6-18 P1 清单,三步式,不改默认);
  - schemas 中的站点示例值(`schemas_auth.py`、`schemas_admin.py`)。

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
| `developer/` | mixed:通用开发文档归 upstream;`staging.md`、`fasrc.md`、`freeinference.md`、`codex-oncall.md` 等站点运维文档归 freeinference |
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
| `status-monitor-worker/src/` | mixed:探测/存储机制归 upstream;`alerts.ts`/`env.ts`/`dashboard.ts` 硬编码 dashboard 品牌、FreeInference 默认 URL 与环境识别,属站点内容 |
| `status-monitor-worker/wrangler.toml` | freeinference(账号/DB 标识符、GATEWAY_BASE_URL) |
| `freeinference-harness/` 通用 scenario 与 runner | upstream testkit(目录名本身待中立化) |
| harness 站点 targets(真实模型 ID/端点) | freeinference |

### tests/(upstream 为主)

| 内容 | 归属 |
|---|---|
| `unit/`、`api/`、`servers/`、`observability/`、`utils/`、fixtures | upstream |
| 根级测试(`test_error_scrubbing.py` 等) | upstream;其中 `test_spark_idle_proxy.py`、`test_local_deployment_proxy.py` 跟随被测 ops 内容的归属 |
| `integration/`、`external/` 中指向真实 provider/站点的用例与配置 | freeinference targets |
| `e2e/` | upstream 机制;站点参数 freeinference |
| (#954 起)发行版专属测试 | 放 `distributions/<dist>/tests/`,随 overlay 迁移 |

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
| `.pre-commit-config.yaml`、`.editorconfig`、`.dockerignore`、`.gitignore` | upstream | 通用工程配置 |
| `.gitmodules` | mixed | 机制 upstream;具体 submodule 指向按其内容裁定 |
| `LICENSE` | upstream | MIT;Harvard 版权行保留,下游追加自己的声明 |
| `README.md` / `README.user.md` / `README.developer.md` | mixed | 品牌与域名重写属 P5;结构留 upstream |
| `CLAUDE.md` / `AGENTS.md` | mixed | 主体是通用开发指南(upstream);域名、staging 账号等站点信息属 freeinference |
| `.env.example` | mixed | 机制 upstream;FreeInference 默认值按三步式 |
| `.env.oncall.example` | freeinference | on-call 联动为站点运维配置 |

## 新增内容归属规则(Phase 0 第 5 项)

新文件/目录落位前**按序**自问——排除性检查在前,upstream 是最后的出口,
不是默认值:

0. **含 secret?** 一票否决:不进 git(env/Secret Manager)。
1. **含站点内容吗?**(真实域名、主机、账号标识、真实模型目录、运营参数、
   品牌文案)
   - 全部是站点内容 → freeinference(overlay 建立前:现有站点位置 + 在本
     清单登记;建立后:直接进 `distributions/freeinference/`);
   - **机制与站点内容并存 → `mixed`:先拆——机制归 upstream,内容归
     freeinference(默认值按三步式暂留)**,并在本清单登记二级明细。
2. **论文实验专用(simulator/trace/绘图)?** 是 → paper,不与生产实现混放。
3. **内部工作文档或 agent 工具?** 是 → internal(`docs/agents/`、`.kilo/`、
   `.codex/`)。
4. **以上皆否,且另一个发行版部署时也需要它** → upstream(代码进 `apps/`,
   示例进 `config/examples/`,文档进 `docs/developer/`)。
5. **拿不准** → 标 `unknown` 提交本清单 PR,评审时裁定;禁止默认塞进
   upstream 目录。

新 workflow 一律先按 freeinference 处理,除非它不依赖任何站点 secret 与
self-hosted runner。

## 覆盖核对与统计

覆盖基准 = `git ls-tree --name-only HEAD` 的 28 个顶层条目(11 目录 + 17
根文件),外加 #954 引入的 `distributions/`,共 29 项,全部在上表或根文件
表中出现。粗分:纯 upstream 1(routing)、internal 2(.kilo、.codex)、
freeinference 主导 2(ops、distributions)、其余为 mixed 或按表逐项裁定的
根文件。`unknown` 当前为 0——`benchmark/` 二级内容是最接近 unknown 的区域,
移动前需逐文件确认。

**维护义务:** 任何新增/删除顶层条目的 PR 必须同步更新本清单(决策树第 5 条
的登记出口即为此)。

本次随清单一并修正了 `CLAUDE.md` / `AGENTS.md` 中两处过时事实
(`benchmark/` 位于顶层;用户文档在 `docs/free_inference`)。除此之外本清单
不移动任何文件、不改变任何行为(Phase 0 验收:不移动生产文件)。
