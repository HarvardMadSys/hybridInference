# 拆仓执行计划(Step 2 of 3):迁出 freeInference

> 状态:执行计划草案。§6 的拍板项确认后即可动工;在那之前 W0/W1/W2 不受影响,
> 可先行。
> 依据:[Step 1 执行计划](2026-07-27-same-repo-split-execution.zh.md)、
> [主设计文档](../specs/2026-07-16-hybridinference-neutral-upstream-multi-distribution-design.zh.md)、
> issue #738(生产安全不变量)、#1031、#1044(关闭注记:复活时机 = Step 2
> 开场第一项)。
> 创建:2026-08-03,基于 Murphy 与 Claude 的对话,并吸收另一 agent 会话的
> 交叉评审(symlink 挂载悬空、拉模式触发、digest 与计划文本的关系)。

## 0. 前提:Step 1 的收官状态(2026-08-03 快照)

- 拆分机制与代码已全部上 main(2026-07-31 sync,release `20260731`);staging
  以 `DISTRIBUTION_CONFIG_MODE=active` 从 overlay 读全部真值,持续部署多日。
- **prod cutover 尚未发生**:prod 仍跑 `574bd272`。下一次手动 Deploy
  Production 即 cutover,部署前 checklist 三条:`.env` 残留 grep(三个
  `*_CONFIG_PATH` / `DISTRIBUTION_*`)、清零 open incident(告警标签
  local→production 必翻)、可选补 `ZAI_API_KEY2`。工具:
  `ops/release/check_production_env.sh`、`distributions/freeinference/smoke_dark_load.py`。
- Step 1 判据④以 `ops/admin/brand_residue_sweep.py` 台账形式存在
  (0 unclaimed,15 个 pending work streams)——它是本文 W3 归属清单的底稿。
- `HarvardMadSys/freeInference`:2026-07-27 创建,空、私有,信任链未起。

**W0(硬前置):先完成 prod cutover、收官 Step 1,再执行本文任何搬迁项。**
(W1 信任链、W2 digest 旁路与 cutover 无冲突,可并行。)

## 1. 终态:形态与体验

### 形态

```text
hybridInference(Step 3 后公开)          freeInference(私有)
├── apps/backend, apps/frontend         ├── distributions/freeinference/(整体平移,路径不改)
├── routing/                            │   ├── config/  models·routing·alerts.yaml
├── deploy/docker/(通用 compose)       │   ├── deploy/   backend.env·frontend.env·staging/
├── config/examples/(中立参考配置)     │   └── content/  docs·RAG·邮件模板·Terms
├── 通用 CI:测试 + 构建发布中立镜像    ├── ops/、services/(按 W3 清单裁定的部分)
└── distributions/ 只剩 example         ├── tests/(overlay·契约·路由快照)
                                        ├── upstream.lock   ◀── 两仓唯一耦合点
      树内无任何 FreeInference 身份     └── .github/workflows/
                                            sync-main·deploy-staging·deploy-prod·
                                            rag-index·bump-bot·…
```

`upstream.lock`(格式从第一天就同时容纳两种形态):

```yaml
upstream:
  release: 20260812            # 溯源用的上游标识
  backend:  ghcr.io/harvardmadsys/hybridinference-backend@sha256:…   # digest
  frontend:                    # v1:源码 pin;W7 完成后翻 digest
    source_commit: <sha>
```

部署主机(终态):只有 `/srv/freeInference` 一个 checkout,不 checkout 上游
源码、不现场编译;部署 = 按 digest 拉镜像 + 挂配置 + 重启。`.env` 与密钥照旧
只在主机上。v1 过渡期主机另有一个钉在 pin 的上游 checkout,仅用于现场 build
frontend;W7 完成后移除。

### 体验(两个手势不变,中间多一个不用管的机器人)

```text
上游代码更新:
  hybridInference dev merge → CI 绿 → 发布镜像
    → [bump bot] freeInference dev 收到 upstream.lock 升级 PR,跑门测试
    → 绿:自动合入 → staging 自动部署(比今天多几分钟)
    → 红:staging 停在上一个好 pin,红 PR 等人补适配(今天同类情形是 staging 直接坏)

FreeInference 自身变更(配置/权重/品牌/文档):
  PR 直进 freeInference dev → 自己的轻量 CI → staging 自动部署
  (比今天快:不再陪上游全量 CI;事故时改 models.yaml 尤其受益)

上生产(与今天完全相同的两下,只是搬了家):
  freeInference 仓 dispatch sync-main(dev→main + release tag)
  freeInference 仓 dispatch Deploy Production
```

不变的东西:主机与 compose 栈形态、主机 `.env`、admin 页 DB 运行时覆盖、
staging=dev / prod=main 映射、release tag 节奏、回滚入口。

## 2. 决策记录(对既有计划文本的三处显式修订)

1. **消费方式(A 决策):digest 为终态;v1 = backend digest + frontend
   源码 pin。**
   - Step 1 执行计划 §1 对应表写"digest 发布、SemVer 等在 Step 2 前完成":
     backend 按原文满足(W2 在同仓先行验证);**frontend 豁免**,退出条件 =
     W7 运行时品牌化完成。豁免理由:`/site-config` 运行时目前只覆盖
     display_name/base_url/support_email/两个 feature flag,其余 27 个
     `NEXT_PUBLIC_*` 与 `public/` 静态资产(团队照片、赞助商 logo)均为
     编译期注入——中立 frontend 镜像今天无法承载 FreeInference 身份,
     而这项工程不应把整个拆仓押后数周。
   - 主设计 Phase 4 验收"发行版不依赖未发布 branch/SHA":v1 仅 frontend
     暂时豁免,终态回归。
   - **任何形态、任何阶段禁止浮动 dev**:开源后浮动 = 上游任何被 merge 的
     PR 直达生产。`upstream.lock` 是安全边界。
2. **跨仓触发 = 拉模式**(freeInference 侧 cron 轮询或 Renovate)。主设计
   原文即如此推荐;`repository_dispatch` 要求上游持有发行版仓
   `contents: write`,不给。`workflow_run` 触发链不能跨仓,bump PR 即其替代。
3. **RouteWise 不再是任何门槛**:仓已 public(2026-07-27 验证)、上游已容忍
   RouteWise 未安装(#1063)。主设计 Phase 3 入口门槛中该条目按过时处理;
   顺带核实 `ROUTEWISE_GITHUB_TOKEN` 是否已可从部署链中删除。

## 3. 工作项(按序)

### W1 信任链(Murphy 手工;周期最长,立即开工,与 W0 并行)

- GitHub:freeInference 建 `production`/`staging` Environments,迁移
  `PROD_*` / `STAGING_*` 五件套 secrets;org App(4436561)安装覆盖新仓
  (bump bot 与 agents 签 token 用);三组 self-hosted runner 标签
  (`deploy-production`/`deploy-staging`/`deploy-edge`)在新仓注册——runner
  可同机双注册,迁移窗口内两仓并行。
- GHCR:上游 CI 推镜像用自身 `GITHUB_TOKEN`(packages:write);**部署主机
  拉私有镜像的只读凭据是今天不存在的新信任链项**(deploy token 或 App 安装
  令牌,主机 `docker login ghcr.io`)。镜像包 private;Step 3 后 backend 包
  可转 public。
- Cloudflare:Pages 项目 `freeinference-doc` 的 Git 集成切到新仓
  (main=生产、dev=preview 不变;有 2026-07-27 双目录过渡的先例可循);
  workers 部署所需 CF token/OIDC 进新仓 Environments。
- 主机:`/srv/freeInference` checkout + 只读 deploy key;迁移窗口内
  `/srv/hybridInference` 保留作回滚。

### W2 同仓复活 #1044:backend digest 旁路(staging)

分支 `murphy/claude/staging-digest-deploy` 尚在。按原设计复活:上游构建
candidate 镜像推 GHCR(首个 candidate 钉 staging 当前 SHA——同代码只换包装,
即部署层的 dark),staging 以 digest 起 backend、frontend 维持源码构建,现有
源码部署链保留为回滚。验收 = smoke 通过 + 一次回滚演练。**在同仓完成,不与
搬迁混窗**(单变量原则)。

### W3 归属清单重生成

按 #1043 关闭时的处方,以当前 dev 重列:`路径 | 目标仓 | 代码还是内容 |
生产影响 | 验证方式`。底稿 = sweep 台账 15 个 pending streams + overlay
AGENTS.md 的宣示。必须裁定的灰区(不在本文预判):`ops/release/`(公开导出
工具疑属上游)、`agent-job-runner.yml`、`freeinference-harness`(主设计倾向
"上游 testkit + 站点 targets 拆开")、`docs/developer/`(内部站)。

### W4 新仓骨架:lock + bump bot + 门测试

- 目录:`distributions/freeinference/` **保持原路径整体平移**,不做扁平化
  美化(行为冻结;挂载与脚本零路径改写)。
- `upstream.lock` 落地(§1 格式)。
- bump workflow:cron 轮询上游 dev(节奏见 §6),生成 bump PR;门测试 =
  checkout freeInference → backend 按 digest 拉 / frontend 按 pin checkout
  上游 → 起栈 → overlay 测试 + 契约测试 + `iter_effective_routes` 生效路由
  快照对比 + smoke;绿则 auto-merge(留痕优先于直接 push),红则停在旧 pin。
- 部署 workflow 改造:compose 用上游仓的 base 文件 + freeInference 的
  override **显式挂载**本仓路径(`/srv/freeInference/distributions/freeinference:/app/distributions/freeinference:ro`)。
  **不用 symlink**——挂载目录内的软链接在容器命名空间悬空。

### W5 物理搬迁(一类一个 PR,批间 staging 验证)

沿用 Step 1 原则:内容类原子搬,每步独立可回退。批次:
(a) distribution 内容与配置;(b) workflows(sync-main、deploy-*、
rag-index、codex-oncall、alert-control-plane-*;按 W3 裁定);
(c) `services/` 三个 worker;(d) `ops/` 按 W3 裁定的部分;
(e) doc 站 Pages 切仓;(f) rag-index 的跨仓依赖处理——ingest 依赖上游
`chunker.py`/`ingest.py`,v1 在 pin 的上游 checkout 里执行,终态改用带工具的
上游镜像。每批同时在上游侧删除对应内容,E 门(banned-strings、禁 import
`distributions/`、中立启动)持续强制。

### W6 切换与演练

staging 先整体改由 freeInference 仓部署(旧链路保留回滚)→ 观察窗口 →
prod 切换 → 拆除旧链路。必做演练:一次 bump 升级、一次 revert bump 回滚、
一次旧 release dispatch 回滚。

### W7 拆后:frontend 运行时品牌化 → 翻 digest

扩 `/site-config` 承载全部品牌值(27 个 `NEXT_PUBLIC_*` 清单化)+ 静态资产
运行时注入(团队/赞助商图片:挂载目录或 URL 化)。注意
`NEXT_PUBLIC_STORAGE_KEY_PREFIX` 必须保持(变更会登出所有用户,Step 1 有
记录)。完成后 frontend 翻 digest、主机移除上游 checkout,§2-1 豁免自动失效,
主设计 Phase 4 验收全量回归。

### W8 欠账清理(开源前必须,不阻塞搬迁)

#1078 泄漏 gateway key 吊销;staging 测试账号密码轮换;Slack 中 GitHub App
key/secret 轮换。

## 4. 执行原则

- **体验守恒**:staging 自动跟、prod 手动两下,任何批次不得破坏。
- **行为冻结**:搬迁 PR 不混行为变化;判据仍是契约测试 + 生效路由快照。
- **单变量**:消费方式变更(W2)与仓库搬迁(W5)不同窗。
- **staging 先行**,prod 永远最后切。
- **pin 不浮动**,无例外。

## 5. Step 2 完成判据

1. 两个手势(sync-main、Deploy Production)在 freeInference 仓完成
   staging + prod 部署,主机不再依赖 hybridInference 的部署链路;
2. hybridInference 树内 grep 不到 FreeInference 身份(E 门),
  `distributions/` 只剩 example;上游 `make test` 全绿、无 overlay 中立启动
   可用(Step 1 判据③保持);
3. `upstream.lock` 为两仓唯一耦合点;bump 门红时 staging 停在旧 pin
   (演练证实);
4. 升级与两种回滚演练通过;
5. W8 三项清零。

达成后,Step 3 只剩:公开机制拍板(Juncheng)→ 过滤导出
(`ops/release/public_export.py` 工具链已就绪)→ hybridInference 公开。

## 6. 待拍板(人工决定)

1. **frontend 源码 pin 豁免**(owner:Murphy)——本文默认"豁免"编写;
   若否决,W7 提前为 W5 的前置,拆仓整体押后。
2. **pin 对象**:上游裸 SHA(建议:staging 要跟 dev,dev 上没有 release)
   vs 上游 release tag(更符合"已发布"字义,但把 staging 锁死在上游 main
   节奏)。
3. **bump 节奏**:每次上游 push(体验最接近今天)vs 每日汇总(噪音更小)。
4. (不阻塞)Step 3 公开机制,owner:Juncheng。

## 7. 与 cloud-agent 拆仓的协调(2026-08-03 增补)

cloud agent 同日启动了自己的拆仓(计划:
[2026-08-03-cloud-agent-repo-split.md](2026-08-03-cloud-agent-repo-split.md),
冻结点 `764a6f97`,目标仓 `freeinference-cloud-agent`)。两条拆分线并行,
其计划的 Coordination rules 对本文的落点:

1. **部署事件隔离 → W0/W6**:本线的 prod cutover(W0,manifest 首飞 +
   告警标签翻转)与 agent 线的 H3 生产切换**不得共享同一次 deploy**,各自
   单独 ride,排期由 Murphy 统一拍。注意 agent 的 identity 代码
   (C1–C4,#1174/#1177/#1179)已进 dev,下次 sync-main 随行上 main——
   对 prod 行为中性(`IDENTITY_*` 未配 → 端点 404;`AGENT_RUNNER` 未设 →
   runner 不起),W0 checklist 三条不变,但 W0 越晚执行,首飞携带的 delta
   越大,归因越难。
2. **冻结纪律 → W3/W5**:agent 冻结期(A2 → agent E10)内,本线在旧仓的
   repo-wide sweep(归属清单重生成、brand sweep、E 门 grep、批量搬迁)
   **必须排除 agent 路径**(清单见旧仓 CLAUDE.md §6.6,随 #1175 合入)。
   W3 的目的地从二分变**三分**:upstream / freeInference /
   freeinference-cloud-agent;agent 形状路径以
   [cloud-agent-split-manifest.md](cloud-agent-split-manifest.md) 为权威,
   本线清单不再裁定它们。已被对方裁定、本文 W3 灰区因此关闭的:
   `agent-job-runner.yml` → cloud-agent 仓(D7);
   `docs/developer/agent-sandbox-operations.md` → cloud-agent 仓(H5);
   `services/freeinference-harness/` 的 agent-loop 文件**留上游**
   (协议一致性测试);`terminal_coordination.py` 属 control plane
   (agent Phase E)。
3. **auth 单写者**:agent C 阶段与本线的 pluggable-IdP(主设计 P3/P4,
   目前休眠)共用 auth 层——同一时间只允许一个 PR 动 `auth.py`,顺序:
   agent 契约先,IdP 抽象后。本文 W0–W6 均不触碰 auth.py,无冲突。

**本线认领的行动项**:`ops/release/public_export.py:36` 硬引用
`apps/backend/serving/agent_jobs/patch_gate.py`;agent 线 H4 删除该文件前,
导出 manifest 必须先摘除或改指向该引用(export 工具链归本线/Step 3 资产)。
H4 排在 agent H3 生产切换之后,不紧急,但列入 W8 同批清账。
