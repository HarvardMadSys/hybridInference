# W3:仓库归属清单与依赖矩阵(Step 2)

> 状态:裁定稿,待 Murphy review。生成:2026-08-19,基于 dev `67452e63` 的
> 实测采集(`brand_residue_sweep.py` = 0 unclaimed / **20** work streams;
> 13 个 workflow 的逐文件抽取)。既有裁定沿用不重裁:Step 2 计划 §7
> (agent 路径、harness 拆分)、主设计三分表。
> 用法:W4 骨架与 W5 每一批搬迁,以本文 §1 定目的地、§2 做 preflight;
> 发现与本文不符,先改本文再动手。
> v2(2026-08-19,评审第一轮):批次按依赖闭合重组(worker 的 3 个
> workflow 移入 W5c 与源码同批;codex-oncall 依赖上游 `serving.oncall`,
> 移入 W5f 经 pin 供码);§2 变量/密钥改全名并新增"源码依赖"列。
>
> **2026-08-26 决策更新：** 本文的“导出 manifest / 物化树”归属判断已被
> [direct-publication readiness plan](2026-08-26-direct-publication-readiness.md)
> 取代。现有 HybridInference 仓库本身将公开；不能公开的内容必须迁出或从
> 保留历史中清理，不能依赖导出规则隐藏。

## 0. 裁定原则

1. **身份与运维随部署走,机制与守卫留上游**——sweep 的 reason 字段是初判,
   本文只在其上细化到可执行粒度。
2. **"上游中立化"不是搬迁**:冻结的 FreeInference 契约值、compose 默认值
   属于上游代码的中立化批(随 W5a 完成后翻转),不进任何搬迁批次。
3. 历史文档同样属于公开面；Step 3 前逐项判断保留、迁出或清理历史。

## 1. 归属总表

### 1.1 迁 freeInference(物理搬迁,按 W5 批次)

**批次闭合原则(v2 修订)**:workflow 与它 `working-directory` /
`PYTHONPATH` / 脚本调用所指向的源码树**必须同批**(或该树已在先前批次落位/
留在上游经 pin 供给);任何批完成后,新仓内已迁的 workflow 都必须是可
dispatch 的。

| 路径 | 类型 | 批次 | 生产影响 | 验证方式 |
|---|---|---|---|---|
| `distributions/freeinference/`(26 hits) | 内容+配置 | **W5a** | 高:models/routing/alerts 真值 | 契约测试 + `iter_effective_routes` 路由快照 diff + smoke |
| 网关部署族 5 个 workflow:`deploy.yml`、`deploy-staging.yml`、`deploy-staging-digest.yml`、`deploy-rollback.yml`、`sync-main.yml` + `ops/deploy/`(它们调用的脚本,同批) | 代码 | **W5b** | 高:部署链路 | §2 矩阵逐行 preflight;staging 先行部署一次 |
| `services/status-monitor-worker/`(12 hits)+ `deploy-status-monitor.yml`(`working-directory` 指向该树,同批) | 代码 | **W5c** | 中:状态页/告警探测 | worker 部署 + 探测记录出现 |
| `services/alert-control-plane-worker/`(10 hits)+ `alert-control-plane-staging-lifecycle.yml` + `slack-readback-gate.yml`(两者 `working-directory` 均指向该树,同批) | 代码 | **W5c** | 中:告警链路 | lifecycle workflow 全流程 |
| `services/freeinference-harness/` 站点 targets | 代码 | **W5c** | 低 | harness 对 staging 跑通 |
| `ops/db/`(20 hits:api_logs 导出、分析、runtime overrides 导出) | 代码 | **W5d** | 低(离线工具) | 在新仓对 staging DB 跑一次代表性脚本 |
| `ops/h200_idle_proxy/`、`ops/spark_idle_proxy/`、`ops/local_deployment_proxy/`(14 hits) | 代码 | **W5d** | 中:机队闲置代理 | 对应主机 systemd 单元指向新路径后重启验证 |
| `ops/setup/`(3 hits) | 代码 | **W5d** | 低 | 文档内命令走查 |
| `benchmark/`(paper artifacts) | 内容 | **W5d**(或未来 paper 仓,不阻塞) | 无 | n/a |
| `docs/developer/`(内部站,5+ hits) | 内容 | **W5e** | 中:internaldoc 站 | 站点构建绿 + 抽查页面 |
| doc 站 Pages Git 集成(freeinference-doc) | 设置 | **W5e**(四步硬序,见计划) | 高:生产 doc 站 | production/preview 双构建验证 |
| 跨上游代码族:`rag-index.yml`(chunker/ingest)+ `codex-oncall.yml`(`PYTHONPATH=apps/backend` 运行 `serving.oncall.gha`,workflow 行 59/116/127)+ `.env.oncall.example` | 代码+内容 | **W5f** | 中:RAG 索引 + on-call | 两个 workflow 都以 **pin 的上游 checkout** 供代码(v1;终态改带工具的上游镜像),新仓各跑通一次 |

### 1.2 留上游(不动)

| 路径 | 理由 |
|---|---|
| `ops/release/` 中的中立 release 工具 | 上游；filtered-export 工具链已退役 |
| `ops/ci/`、`ops/admin/brand_residue_sweep.py`、`ops/lib/`* | CI 与中立性守卫(*lib 按消费方跟随,搬迁批 preflight 逐个核) |
| `.github/workflows/` 三个:`ci.yml`、`ci-observability.yml`、`build-candidates.yml` | 上游 CI 与 release engineering(candidates 是 W4 自动发布的前身) |
| 守卫测试 6 个(test_neutral_startup、contract_settings_defaults、site_identity、compose_identity、brand_residue_sweep、no_personal_data) | 刻意携带 marker 的中立性断言 |
| `LICENSE`、`README*` 三份、`branding.ts` 注释、compose `NEXT_PUBLIC_GITHUB_URL` 默认、`pyproject/uv.lock` RouteWise URL、`docs/developer` 内 5 个单点提及 | sweep 判定的事实性提及(worked example / 版权归属 / 包源),非品牌残留 |
| `services/freeinference-harness/` 协议一致性 testkit | §7 既有裁定 |

### 1.3 上游中立化批(代码工作,非搬迁;W5a 之后执行)

`tests/` 冻结契约值(14)、`deploy/` compose 默认(11)、`apps/frontend`(4)、
`apps/backend`(2)、`.env.example`(1)、`AGENTS.md`/`CLAUDE.md` 站点行、
`Makefile` 的 docs 指针——**随 W5a 真值落新仓后统一翻中立**,判据 =
契约测试刻意更新 + `make test` 全绿 + 中立启动保持。

### 1.4 历史文档(公开前复核)

`docs/agents/`、`docs/reviews/`、`docs/superpowers/` 不再由 manifest 排除。
保留即公开；含私有内容的文件必须迁出，并在需要时清理所有保留 refs。

## 2. workflow → runner → environment → secrets/variables 依赖矩阵

变量与密钥名一律**全名**,禁止缩写——本表是 preflight 的对照原文。

| workflow | runner | env | secrets | variables | 源码依赖(checkout 后实际执行) | 目的地/批次 |
|---|---|---|---|---|---|---|
| Deploy Production | `deploy-production` | production | PROD_HOST, PROD_HOST_KEY, PROD_PORT, PROD_SSH_KEY, PROD_USER, ROUTEWISE_GITHUB_TOKEN | — | `ops/deploy/deploy_production.sh` | freeInference / W5b |
| Rollback Production | `deploy-production` | production | 同上一行 | — | `ops/deploy/deploy_production.sh` | freeInference / W5b |
| Deploy Staging | `deploy-staging` | staging | STAGING_HOST, STAGING_HOST_KEY, STAGING_PORT, STAGING_SSH_KEY, STAGING_USER, ROUTEWISE_GITHUB_TOKEN | — | `ops/deploy/deploy_staging.sh` | freeInference / W5b |
| Deploy Staging by Digest | `deploy-staging` | staging | STAGING_HOST, STAGING_HOST_KEY, STAGING_PORT, STAGING_SSH_KEY, STAGING_USER, GITHUB_TOKEN | — | 无(内联远端脚本) | freeInference / W5b(W4 改造:同源门改断言 `upstream.lock.source_commit`) |
| Sync dev to main | `trusted-automation` | — | GITHUB_TOKEN | — | 无(纯 git) | freeInference / W5b |
| Deploy Status Monitor | `deploy-edge` | staging + process | CLOUDFLARE_API_TOKEN | ALERT_CONTROL_PLANE_STAGING_URL | `working-directory: services/status-monitor-worker`(wrangler deploy/d1 migrations) | freeInference / **W5c(与 worker 同批)** |
| Alert CP Staging Lifecycle | **ubuntu-22.04(hosted!)** | staging | ALERT_CONTROL_PLANE_PRODUCER_SIGNING_KEY_V1, ALERT_CONTROL_PLANE_ROUTE_KEY_V1, CLOUDFLARE_API_TOKEN, CODEX_ONCALL_SLACK_BOT_TOKEN | ALERT_CONTROL_PLANE_STAGING_URL, ALERT_CONTROL_PLANE_SLACK_CHANNEL_ID | `working-directory: services/alert-control-plane-worker` | freeInference / **W5c(与 worker 同批)** |
| Slack Readback Gate | **ubuntu-22.04(hosted!)** | — | CODEX_ONCALL_SLACK_BOT_TOKEN | — | `working-directory: services/alert-control-plane-worker` | freeInference / **W5c(与 worker 同批)** |
| Codex On-Call | **ubuntu-22.04(hosted!)** | — | CODEX_ONCALL_MODEL_API_KEY, CODEX_ONCALL_SLACK_BOT_TOKEN | — | **`PYTHONPATH=apps/backend` + `python -m serving.oncall.gha`(上游代码,行 59/116/127)** | freeInference / **W5f(经 pin 的上游 checkout 供码)** |
| RAG Index | `trusted-automation` | — | RAG_GATEWAY_API_KEY, GITHUB_TOKEN | — | 上游 chunker/ingest(计划既定) | freeInference / W5f |
| CI | `ci-general`, `image-verify-arm64` | — | DEEPSEEK_API_KEY, GEMINI_API_KEY, ZAI_API_KEY, ROUTEWISE_GITHUB_TOKEN | — | 全树 | 上游 |
| CI Observability | **ubuntu-latest(hosted!)** | — | — | — | 无 | 上游 |
| Build Candidate Images | `arm64-docker` | — | GITHUB_TOKEN, ROUTEWISE_GITHUB_TOKEN | — | `deploy/docker/Dockerfile.backend` + 全树构建上下文 | 上游(W4 演化为自动发布) |

## 3. 新仓资产缺口清单(对照 08-11 已迁项)

- **runner 标签 6 组**:`deploy-production` / `deploy-staging` / `deploy-edge` /
  `trusted-automation`(计划已列)+ 迁移后若 oncall 族转自建还需规划(见 §4-1)。
- **staging 环境缺 2 secrets**:`ALERT_CONTROL_PLANE_PRODUCER_SIGNING_KEY_V1`、
  `ALERT_CONTROL_PLANE_ROUTE_KEY_V1`(已知,W5b 前补)。
- **⚠ 新发现:2 个 variables 从未在迁移清单上**——
  `ALERT_CONTROL_PLANE_STAGING_URL`、`ALERT_CONTROL_PLANE_SLACK_CHANNEL_ID`
  是 **environment/repo variables 不是 secrets**(08-11 只迁了 secrets,
  variables 走 `gh variable set`,值可从旧仓直接读出:`gh variable list`)。
- **repo 级 secrets 4 项待迁**:`RAG_GATEWAY_API_KEY`、
  `CODEX_ONCALL_MODEL_API_KEY`、`CODEX_ONCALL_SLACK_BOT_TOKEN`、
  `ROUTEWISE_GITHUB_TOKEN`(最后一项先做 §4-2 核实,能删则不迁)。
- **package Actions access**:backend 包已诞生(2026-08-19),给
  freeInference 授 read 现在即可执行(原 W4 项,可提前)。

## 4. 风险注记

1. **hosted-runner 账单面(现行风险,与迁移无关也会咬)**:矩阵中 4 个
   workflow 跑在 GitHub-hosted runner 上,当前 org 账单状态下**下次触发即
   失败**(Alert CP Lifecycle、Codex On-Call、Slack Readback Gate、
   CI Observability)。选项:修账单,或迁去自建标签(oncall 族低负载,
   `trusted-automation` 可承接)。搬迁时必须显式选择,不能默认照抄
   `ubuntu-22.04`。
2. **`ROUTEWISE_GITHUB_TOKEN` 核实项**(计划 §2-3):RouteWise 仓已公开,
   该 token 理论上可从全链路删除(CI/deploy/build-candidates 均有
   `|| github.token` 类回退或可加)。在 W5b 前做一次实测(移除后跑通
   CI + staging 部署),能删则新仓少迁一个 secret。
3. **`deploy-staging-digest` 不是照抄搬迁**:W4 要把同源门从"对齐主机
   HEAD"改写为"断言 `upstream.lock.source_commit`"(计划 §1 已定),
   搬的是改造后的版本;上游副本在删除批清除。
4. **`ops/lib` 与零散共享件**:按消费方跟随;每个搬迁批的 preflight 里
   跑一次"谁 import 它"检查(删代码前先问谁按路径加载它)。
5. **审计方法(v2 教训)**:只 grep `secrets./vars.` 不构成依赖审计——
   必须同时枚举 checkout 后**实际执行的树内路径**
   (`working-directory:` / `PYTHONPATH=` / `python -m` / `bash <path>` /
   构建上下文)。本表 §2 的"源码依赖"列即该审计的固化;新增 workflow
   入表时两类都要采。

## 5. 执行序摘要

W5a(distributions)→ W5b(网关部署族 5 workflow + ops/deploy + sync-main)
→ W5c(三件 services **连同各自的 3 个 workflow**)→ W5d(ops 数据/代理/
setup + benchmark)→ W5e(docs/developer + Pages 四步硬序)→
W5f(跨上游代码族:rag-index + codex-oncall + .env.oncall.example,
经 pin 的上游 checkout 供码)。
每批:复制+接线 → §2 矩阵 preflight → staging 验证 → 下一批;
上游删除全部推迟到 W6 观察窗后(计划既定)。
