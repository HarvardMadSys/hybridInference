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
> **2026-08-26 决策更新：** 本文的“导出 manifest / 物化树”归属判断已失效。
> 现有 HybridInference 仓库本身将公开；不能公开的内容必须迁出或从保留历史
> 中清理，不能依赖导出规则隐藏。
> v3(2026-08-26,W6 收口):修正 W5b workflow 身份——四个上游 deploy
> workflow 是迁移期旧入口,不是 freeInference 的长期归属资产;
> `sync-main.yml` 留上游。freeInference 的部署与同名 promotion 手势均为
> 新仓自有实现,不是搬走同一个上游文件。

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
| `ops/deploy/`(由 freeInference 自有 workflow 接线) | 代码 | **W5b** | 高:部署链路 | §2 矩阵逐行 preflight;staging 先行部署一次 |
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

**W5c 状态(2026-08-27):已关闭。** worker 源码及其部署、lifecycle、readback
workflow 权威已由
[freeInference #22](https://github.com/HarvardMadSys/freeInference/pull/22)
接管，站点 harness target 由
[#26](https://github.com/HarvardMadSys/freeInference/pull/26)接管。成功证据：
[lifecycle 32495280162](https://github.com/HarvardMadSys/freeInference/actions/runs/32495280162)、
[status-monitor dispatch 32495471487](https://github.com/HarvardMadSys/freeInference/actions/runs/32495471487)、
[push deploy 32496012192](https://github.com/HarvardMadSys/freeInference/actions/runs/32496012192)、
[Slack readback 32496095487](https://github.com/HarvardMadSys/freeInference/actions/runs/32496095487)。
上游副本与专属 CI 接线已退役；协议一致性 testkit 和
`agent-loop-local.yaml` 仍按 §1.2 留上游。

**W5d 状态(2026-08-27):已关闭,边界经实证修订。** freeInference 已实际
接管运营面:production `/etc/cron.d/freeinference-*` 三条 cron 从
`/srv/freeInference/ops/db/` 执行,spark2 `spark_idle_proxy.service` 指向
`/srv/freeInference`;两仓逐 blob 比对,工具本体一致、cron/安装器为 fi
适配版。上游已删除 `ops/db` 运营工具链、`ops/lib`、三个 idle proxy、
`deploy/systemd` 10 个 proxy/tunnel 模板与配套测试(上游独有的三个测试先由
[freeInference #53](https://github.com/HarvardMadSys/freeInference/pull/53)
verbatim 移植)。修订:(a) 顶层 import `serving.*` 的四个分析工具
(automation score、prompt sample、geo exporter+globe、num_user_turns
backfill)离开 `apps/backend` 即不可运行,改判**中立后端耦合工具留上游**
(freeInference 侧同名副本为死代码,已在 #53 注明待清理);
(b) 行 47 `ops/setup/` 改随 **W5b**(被 `ops/deploy` 脚本调用、
`test_geoip_deployment` 断言钉住,freeInference 缺 `test_setup_claude_code`
移植);(c) 行 46 的"主机 systemd 单元指向新路径"验证:spark2 已验,
h200/rtx6000 无 shell 待 W7 清点(上游 git 删除不影响在跑主机)。

**W5f-RAG 状态(2026-08-27):已关闭。** freeInference current-dev
验证成功([run 33037428965](https://github.com/HarvardMadSys/freeInference/actions/runs/33037428965)):
`dev` 为 `e002101f`,pin 的 HybridInference 源为 `bf84c900`;经 `bge-m3`
从 6 个文件重建 91 chunks,并复现可检索内容,已满足上游 `rag-index.yml`
退役条件。按 operator 指示,`codex-oncall.yml`、`.env.oncall.example` 与
cloud-agent/on-call 工作延期,保持未动且未完成。

### 1.2 留上游(不动)

| 路径 | 理由 |
|---|---|
| `ops/release/` 中的中立 release 工具 | 上游；filtered-export 工具链已退役 |
| `ops/ci/`、`ops/admin/brand_residue_sweep.py`、`ops/lib/`* | CI 与中立性守卫(*lib 按消费方跟随,搬迁批 preflight 逐个核) |
| `.github/workflows/` 三个:`ci.yml`、`ci-observability.yml`、`build-candidates.yml` | 上游 CI 与 release engineering(candidates 是 W4 自动发布的前身) |
| `.github/workflows/sync-main.yml` | HybridInference 中立的 dev→main/release promotion;freeInference production ancestry gate 仍依赖该上游 promotion。freeInference 的同名手势是新仓自有实现,不是此文件的搬迁副本 |
| 守卫测试 6 个(test_neutral_startup、contract_settings_defaults、site_identity、compose_identity、brand_residue_sweep、no_personal_data) | 刻意携带 marker 的中立性断言 |
| `LICENSE`、`README*` 三份、`branding.ts` 注释、compose `NEXT_PUBLIC_GITHUB_URL` 默认、`pyproject.toml` 的 `authors`、`docs/developer` 内 5 个单点提及 | sweep 判定的事实性提及(worked example / 版权归属 / 包源),非品牌残留 |
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
| Deploy Production(`deploy.yml`,legacy) | `deploy-production` | production | PROD_HOST, PROD_HOST_KEY, PROD_PORT, PROD_SSH_KEY, PROD_USER | — | `ops/deploy/deploy_production.sh` | HybridInference / W6 收口已退役;freeInference 使用新仓自有 workflow |
| Rollback Production(`deploy-rollback.yml`,legacy) | `deploy-production` | production | 同上一行 | — | `ops/deploy/deploy_production.sh` | HybridInference / W6 收口已退役;freeInference 使用新仓自有 workflow |
| Deploy Staging(`deploy-staging.yml`,legacy) | `deploy-staging` | staging | STAGING_HOST, STAGING_HOST_KEY, STAGING_PORT, STAGING_SSH_KEY, STAGING_USER | — | `ops/deploy/deploy_staging.sh` | HybridInference / W6 收口已退役;freeInference 使用新仓自有 workflow |
| Deploy Staging by Digest(`deploy-staging-digest.yml`,legacy) | `deploy-staging` | staging | STAGING_HOST, STAGING_HOST_KEY, STAGING_PORT, STAGING_SSH_KEY, STAGING_USER, GITHUB_TOKEN | — | 无(内联远端脚本) | HybridInference / W6 收口已退役;freeInference 使用新仓自有 workflow |
| Sync dev to main(`sync-main.yml`,upstream) | `trusted-automation` | — | GITHUB_TOKEN | — | 无(纯 git) | HybridInference / 保留(中立 dev→main/release);freeInference 同名手势非此文件搬迁 |
| Deploy Status Monitor | `deploy-edge` | staging + process | CLOUDFLARE_API_TOKEN | ALERT_CONTROL_PLANE_STAGING_URL | `working-directory: services/status-monitor-worker`(wrangler deploy/d1 migrations) | freeInference / **W5c(与 worker 同批)** |
| Alert CP Staging Lifecycle | **ubuntu-22.04(hosted!)** | staging | ALERT_CONTROL_PLANE_PRODUCER_SIGNING_KEY_V1, ALERT_CONTROL_PLANE_ROUTE_KEY_V1, CLOUDFLARE_API_TOKEN, CODEX_ONCALL_SLACK_BOT_TOKEN | ALERT_CONTROL_PLANE_STAGING_URL, ALERT_CONTROL_PLANE_SLACK_CHANNEL_ID | `working-directory: services/alert-control-plane-worker` | freeInference / **W5c(与 worker 同批)** |
| Slack Readback Gate | **ubuntu-22.04(hosted!)** | — | CODEX_ONCALL_SLACK_BOT_TOKEN | — | `working-directory: services/alert-control-plane-worker` | freeInference / **W5c(与 worker 同批)** |
| Codex On-Call | **ubuntu-22.04(hosted!)** | — | CODEX_ONCALL_MODEL_API_KEY, CODEX_ONCALL_SLACK_BOT_TOKEN | — | **`PYTHONPATH=apps/backend` + `python -m serving.oncall.gha`(上游代码,行 59/116/127)** | freeInference / **W5f(经 pin 的上游 checkout 供码)** |
| RAG Index | `trusted-automation` | — | RAG_GATEWAY_API_KEY, GITHUB_TOKEN | — | 上游 chunker/ingest(计划既定) | freeInference / W5f |
| CI | `ci-general`, `image-verify-arm64` | — | DEEPSEEK_API_KEY, GEMINI_API_KEY, ZAI_API_KEY | — | 全树 | 上游 |
| CI Observability | **ubuntu-latest(hosted!)** | — | — | — | 无 | 上游 |
| Build Candidate Images | `arm64-docker` | — | GITHUB_TOKEN | — | `deploy/docker/Dockerfile.backend` + 全树构建上下文 | 上游(W4 演化为自动发布) |

## 3. 新仓资产缺口清单(对照 08-11 已迁项)

- **runner 标签 6 组**:`deploy-production` / `deploy-staging` / `deploy-edge` /
  `trusted-automation`(计划已列)+ 迁移后若 oncall 族转自建还需规划(见 §4-1)。
- **staging 环境缺 2 secrets**:`ALERT_CONTROL_PLANE_PRODUCER_SIGNING_KEY_V1`、
  `ALERT_CONTROL_PLANE_ROUTE_KEY_V1`(已知,W5b 前补)。
- **⚠ 新发现:2 个 variables 从未在迁移清单上**——
  `ALERT_CONTROL_PLANE_STAGING_URL`、`ALERT_CONTROL_PLANE_SLACK_CHANNEL_ID`
  是 **environment/repo variables 不是 secrets**(08-11 只迁了 secrets,
  variables 走 `gh variable set`,值可从旧仓直接读出:`gh variable list`)。
- **repo 级 secrets 3 项待迁**:`RAG_GATEWAY_API_KEY`、
  `CODEX_ONCALL_MODEL_API_KEY`、`CODEX_ONCALL_SLACK_BOT_TOKEN`。
  `ROUTEWISE_GITHUB_TOKEN` 的 §4-2 核实已完成:依赖链已删除,不迁;
  旧仓的 GitHub secret 本体尚未回收,见 §4-2。
- **package Actions access**:backend 包已诞生(2026-08-19),给
  freeInference 授 read 现在即可执行(原 W4 项,可提前)。

## 4. 风险注记

1. **hosted-runner 账单面(现行风险,与迁移无关也会咬)**:矩阵中 4 个
   workflow 跑在 GitHub-hosted runner 上,当前 org 账单状态下**下次触发即
   失败**(Alert CP Lifecycle、Codex On-Call、Slack Readback Gate、
   CI Observability)。选项:修账单,或迁去自建标签(oncall 族低负载,
   `trusted-automation` 可承接)。搬迁时必须显式选择,不能默认照抄
   `ubuntu-22.04`。
2. ~~**`ROUTEWISE_GITHUB_TOKEN` 核实项**(计划 §2-3)~~ **已收口**:该
   token 存在只为拉取 `routewise` 这条 git 依赖。依赖改从 PyPI 安装
   (`llm-routewise`)后,`uv.lock` 再无任何 git source,构建也就不再需要
   VCS 凭据。`Dockerfile.backend` 的 secret mount 与 `url.insteadOf` 改写、
   `docker-compose.yml` 的 secret 定义、`ci.yml`(6 处)与
   `build-candidates.yml`(1 处)的 build secret 已全部删除;后端镜像已实测
   可在无 git、无 token 的情况下构建。

   **状态区分**:代码侧引用已全部删除,该 secret 不必迁往新仓。GitHub 上
   旧仓的 secret 本体**尚未删除**——本 PR 不碰仓库设置。合并且确认没有
   workflow 再引用它之后,在旧仓 Settings → Secrets 手工删除,该项才算完全
   收口。
3. **`deploy-staging-digest.yml` 身份修正**:上游文件是 W6 回滚窗保留的
   legacy 入口,在 W6 收口退役,不作为同一个文件搬迁。freeInference 的
   digest 部署 workflow 是新仓自有实现,同源门断言
   `upstream.lock.source_commit`。
4. **`ops/lib` 与零散共享件**:按消费方跟随;每个搬迁批的 preflight 里
   跑一次"谁 import 它"检查(删代码前先问谁按路径加载它)。
5. **审计方法(v2 教训)**:只 grep `secrets./vars.` 不构成依赖审计——
   必须同时枚举 checkout 后**实际执行的树内路径**
   (`working-directory:` / `PYTHONPATH=` / `python -m` / `bash <path>` /
   构建上下文)。本表 §2 的"源码依赖"列即该审计的固化;新增 workflow
   入表时两类都要采。

## 5. 执行序摘要

W5a(distributions)→ W5b(`ops/deploy/` + freeInference 新仓自有 deploy/sync
手势的接线;不是搬迁上游 5 个 workflow 文件)
→ W5c(三件 services **连同各自的 3 个 workflow**)→ W5d(ops 数据/代理/
setup + benchmark)→ W5e(docs/developer + Pages 四步硬序)→
W5f(跨上游代码族:rag-index + codex-oncall + .env.oncall.example,
经 pin 的上游 checkout 供码)。
每批:复制+接线 → §2 矩阵 preflight → staging 验证 → 下一批;
W6 观察窗后退役上游四个 legacy deploy workflow;上游 `sync-main.yml`
继续保留。其余上游删除按计划进入独立删除批。
