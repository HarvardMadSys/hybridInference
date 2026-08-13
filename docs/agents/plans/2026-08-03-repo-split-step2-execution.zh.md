# 拆仓执行计划(Step 2 of 3):迁出 freeInference

> 状态:执行计划草案。§6 的拍板项确认后即可动工;在那之前 W0/W1/W2 不受影响,
> 可先行。
> 依据:[Step 1 执行计划](2026-07-27-same-repo-split-execution.zh.md)、
> [主设计文档](../specs/2026-07-16-hybridinference-neutral-upstream-multi-distribution-design.zh.md)、
> issue #738(生产安全不变量)、#1031、#1044(关闭注记:复活时机 = Step 2
> 开场第一项)。
> 创建:2026-08-03,基于 Murphy 与 Claude 的对话,并吸收另一 agent 会话的
> 交叉评审(symlink 挂载悬空、拉模式触发、digest 与计划文本的关系)。
> 修订:2026-08-12,状态对账——W0 已完成、W1 GitHub 侧已完成、§7 行动项已随
> H4 解决;详见 §0 与各工作项内的进展注记。同日并入六轮交叉评审:W5 删除
> 时序改判(不与搬迁同批)、W6 增补 prod 观察窗、W7 compose base 来源待拍板
> (§6-5)与 `/agents` 路由缺口(P1)、判据②改以 public-export **物化树**
> 为对象、W2 同源硬门(脏检查含 untracked + 无旁路)、W2 砍除 frontend
> 候选旁支(缺 `AGENT_*` build args 与 cloud-agent 网络,整条留待 W7)、
> W5c services 表述修正(harness 非 worker,按 §7 拆)、sweep 计数刷新为
> 18、§5 W8 计数改五项、执行原则新增"生产周边显式化"、GHCR 认证改判
> (短期 token + 临时 DOCKER_CONFIG,取消主机持久凭据)、W4 增自动发布与
> package 授权、lock 增 `source_commit`、判据"distributions/ 只剩
> example"写实为清空。

## 0. 前提:Step 1 的收官状态(2026-08-12 对账)

- 拆分机制与代码已全部上 main(2026-07-31 sync,release `20260731`);staging
  以 `DISTRIBUTION_CONFIG_MODE=active` 从 overlay 读全部真值,持续部署多日。
- **prod cutover 已完成(2026-08-07)**:当日两班生产部署(release
  `20260807.1`/`20260807.2`)即 manifest cutover 首飞,与 agent 线 H3 生产
  切换同窗完成;08-09 又一班(release `20260809`,checkout `2eb07575`)带上
  H4,四端点验收通过。原 checklist 三条与工具注记保留在 Step 1 计划中备查。
- Step 1 判据④以 `ops/admin/brand_residue_sweep.py` 台账形式存在
  (2026-08-12 复跑:0 unclaimed,**18** 个 pending work streams;08-03
  快照为 15)——它是本文 W3 归属清单的底稿。
- `HarvardMadSys/freeInference`:2026-07-27 创建,空、私有。**信任链
  GitHub 侧已于 2026-08-11 建立**(进展见 W1 注记);主机侧未动。

**W0(硬前置):已满足(2026-08-07)。**搬迁项(W4 起)仍等 §6 拍板;
W1/W2 可先行,W1 已在进行。

## 1. 终态:形态与体验

### 形态

```text
hybridInference(Step 3 后公开)          freeInference(私有)
├── apps/backend, apps/frontend         ├── distributions/freeinference/(整体平移,路径不改)
├── routing/                            │   ├── config/  models·routing·alerts.yaml
├── deploy/docker/(通用 compose)       │   ├── deploy/   backend.env·frontend.env·staging/
├── config/examples/(中立参考配置)     │   └── content/  docs·RAG·邮件模板·Terms
├── 通用 CI:测试 + 构建发布中立镜像    ├── ops/、services/(按 W3 清单裁定的部分)
└── distributions/ 清空                 ├── tests/(overlay·契约·路由快照)
                                        ├── upstream.lock   ◀── 两仓唯一耦合点
      树内无任何 FreeInference 身份     └── .github/workflows/
                                            sync-main·deploy-staging·deploy-prod·
                                            rag-index·bump-bot·…
```

`upstream.lock`(格式从第一天就同时容纳两种形态):

```yaml
upstream:
  release: 20260812            # 溯源用的上游标识
  source_commit: <sha>         # 本次 bump 的上游 SHA(provenance 锚,
                               # 2026-08-12 评审第六轮增)
  backend:  ghcr.io/harvardmadsys/hybridinference-backend@sha256:…   # digest
  frontend:                    # v1:源码 pin 用同一 source_commit;W7 后翻 digest
    source_commit: <sha>
```

`digest` 与 `source_commit` 必须在**同一个 bump PR 内原子更新**;W4 的门
测试与部署 workflow 都断言 `镜像 revision label == source_commit`——这是
W2 同源门在两仓时代的形态(W2 期间的对照物是主机 checkout 的 HEAD,W4 起
主机 HEAD 是 freeInference 的 commit,不再可比)。

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
- GHCR(2026-08-12 评审第五轮改判,取代原"主机持久只读凭据"方案):
  上游 CI 推镜像用自身 `GITHUB_TOKEN`(packages:write)。**拉取侧不设任何
  主机持久凭据**——deploy workflow 以短期 `GITHUB_TOKEN`
  (`permissions: packages: read`)经 SSH 转发,主机侧在一次性 0700 临时
  `DOCKER_CONFIG` 里 login+pull,部署毕即销毁(#1258 已按此实现),主机
  默认 Docker config 全程不被触碰。前提:把 freeInference 加进各 package
  的 Actions access(read)——package 随首个 candidate push 才存在,该授权
  排在 W4(W2 期间同仓,`GITHUB_TOKEN` 天然有权,无此前提)。镜像包
  private;Step 3 后 backend 包转 public,login 步骤整个删除。回滚兜底:
  按 digest 回滚优先命中主机本地 daemon 缓存,不依赖 registry 可达。
- Cloudflare:Pages 项目 `freeinference-doc` 的 Git 集成切到新仓
  (main=生产、dev=preview 不变;有 2026-07-27 双目录过渡的先例可循);
  workers 部署所需 CF token/OIDC 进新仓 Environments。
- 主机:`/srv/freeInference` checkout + 只读 deploy key;迁移窗口内
  `/srv/hybridInference` 保留作回滚。

**进展(2026-08-11)**:GitHub 侧已完成——`production`/`staging`
Environments 已建;secrets 已迁(production 6/6;staging 6/8,缺
`ALERT_CONTROL_PLANE_*` 两把,唯一消费者是 W5b 才搬的 lifecycle workflow,
不阻塞;值不可从旧仓读回,届时复用原值或铸新值重放 provisioning)。两个
GitHub App 均已 org 所有(production App 4436566 已自个人账户转移)且安装
范围覆盖新仓;Murphy 已获 repo admin。Cloudflare 凭据为新铸的 **Account
Token**(Workers Scripts/D1 Edit + `freeinference.org` Workers Routes;
Murphy 的 CF 角色铸不出所需权限,由 Juncheng 创建)。待办:runner 双注册;
主机 checkout 及其认证——deploy key 现计 0 把,若 org 策略不允 deploy
key,备选 = App installation token(App 安装已覆盖新仓)或 runner 侧
rsync(主机彻底免 Git 凭据,与 GHCR 短期凭据同一哲学),主机侧 W1 动工时
定。GHCR 拉取凭据一项按上条改判**取消**,代之以 W4 的 package Actions
access 授权。

### W2 同仓复活 #1044:backend digest 旁路(staging)

分支 `murphy/claude/staging-digest-deploy` 尚在。按原设计复活:上游构建
candidate 镜像推 GHCR(首个 candidate 钉 staging 当前 SHA——同代码只换包装,
即部署层的 dark),staging 以 digest 起 backend、frontend 维持源码构建,现有
源码部署链保留为回滚。验收 = smoke 通过 + 一次回滚演练。**在同仓完成,不与
搬迁混窗**(单变量原则)。

同源为硬门(2026-08-12 评审第三、四轮定稿):镜像的
`org.opencontainers.image.revision` label 必须等于主机 checkout 的 HEAD,
且 checkout 必须干净——判据是 `git status --porcelain=v1
--untracked-files=all` 为空,tracked 改动与 untracked 新文件都算脏;
ignored 的 `.env`、`var/**` 属预期主机状态,不在拒脏范围。两项均无旁路
开关(评审后移除了 `allow_sha_mismatch`);另两道与 classic 对齐的守卫:
HEAD 须为 `origin/dev` 的祖先(拒任意 ref 部署),checkout 信任/属主
自愈同款复刻(safe.directory + `sudo -n chown`)。部署日志记录 source 与
digest。复活 PR = #1258(build-candidates + digest deploy,双 workflow
均 dispatch-only,backend-only)。

### W3 归属清单重生成

按 #1043 关闭时的处方,以当前 dev 重列:`路径 | 目标仓 | 代码还是内容 |
生产影响 | 验证方式`。底稿 = sweep 台账 18 个 pending streams(2026-08-12
复跑)+ overlay AGENTS.md 的宣示。必须裁定的灰区(不在本文预判):
`ops/release/`(公开导出工具疑属上游)、`freeinference-harness`(主设计
倾向"上游 testkit + 站点 targets 拆开")、`docs/developer/`(内部站)。
(`agent-job-runner.yml` 原列灰区,已随 H4 出仓,条目作废——见 §7。)

### W4 新仓骨架:lock + bump bot + 门测试

- 目录:`distributions/freeinference/` **保持原路径整体平移**,不做扁平化
  美化(行为冻结;挂载与脚本零路径改写)。
- `upstream.lock` 落地(§1 格式,含 `source_commit` provenance 锚)。
- 上游侧自动发布(2026-08-12 评审第六轮补):dev CI 绿后自动构建并发布
  backend 镜像(带 `org.opencontainers.image.revision` label)——W2 的
  build-candidates 是 dispatch-only 的验证切片,不承担这条自动链;没有
  自动发布,bump bot 就没有新 digest 可 bump。发布 job 挂上游 ci.yml,
  digest 经 packages API / run summary 可查。
- package 授权:首个 candidate 发布后,把 freeInference 加进 backend
  package 的 Actions access(read)——W1 GHCR 改判的前提项,落在这里。
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
(c) `services/` 中 W3 判定为 FreeInference-owned 的部分——注意
`freeinference-harness` 不是 worker,按 §7 既有裁定拆开:协议一致性
testkit 留上游,站点 targets 迁私有仓;(d) `ops/` 按 W3 裁定的部分;
(e) doc 站 Pages 切仓;(f) rag-index 的跨仓依赖处理——ingest 依赖上游
`chunker.py`/`ingest.py`,v1 在 pin 的上游 checkout 里执行,终态改用带工具的
上游镜像。

**上游侧删除不与搬迁同批(2026-08-12 评审修订)**:原文"每批同时在上游侧
删除对应内容"与 W6"旧链路保留作回滚"自相矛盾——classic staging 部署跟的
是 dev,W5a 一删 overlay,旧链在 W6 之前就断了,等于边搬边拆自己的回滚梯。
改为:W5 全程旧仓内容原位保留(搬=复制+接线),classic 部署链(staging=dev、
prod=release tag)始终可用;删除推迟到 W6 观察窗结束后,按逆批次序独立 PR
执行,E 门(banned-strings、禁 import `distributions/`、中立启动)在删除批
上强制,Step 3 开源前完成即可。(备选:旧生产链钉死在迁移前 release tag、
删除照旧同批——回滚面更窄,不推荐;二选一归 Murphy。)

### W6 切换与演练

staging 先整体改由 freeInference 仓部署(旧链路保留回滚)→ staging 观察窗
→ prod 切换 → **prod 观察窗**(旧链路与旧仓内容在窗内原样保留,三项演练
在此窗内完成)→ 拆除旧链路 → 之后才执行 W5 修订所述的上游侧删除批。
必做演练:一次 bump 升级、一次 revert bump 回滚、一次旧 release dispatch
回滚。

### W7 拆后:frontend 运行时品牌化 → 翻 digest

扩 `/site-config` 承载全部品牌值(27 个 `NEXT_PUBLIC_*` 清单化)+ 静态资产
运行时注入(团队/赞助商图片:挂载目录或 URL 化)。注意
`NEXT_PUBLIC_STORAGE_KEY_PREFIX` 必须保持(变更会登出所有用户,Step 1 有
记录)。完成后 frontend 翻 digest、主机移除上游 checkout,§2-1 豁免自动失效,
主设计 Phase 4 验收全量回归。

**待设计(2026-08-12 评审指出)**:主机移除上游 checkout 后,compose base
(`deploy/docker/docker-compose.yml`)的来源悬空——v1 靠 checkout 供给,
W7 拆掉它却没安排接替。候选:freeInference vendor 一份、由 bump 门测试对照
上游 diff;上游把 compose 作为 release artifact 随镜像发布;或 compose
整体改判归 freeInference。W7 动工前拍板。

**W7 的第三类构建期身份——`/agents` 路由(2026-08-12 评审 P1)**:品牌值与
静态资产之外,`AGENT_WEB_INTERNAL_URL`/`AGENT_CONTROL_PLANE_INTERNAL_URL`
是 build 时烤进 next.config.js rewrites 的**路由行为**(不设 = `/agents`
404,见 CLAUDE.md §6.6);中立 frontend 镜像不含私有地址,`/site-config`
能补值、补不回 rewrites。翻 digest 前必须先落运行时方案:route handler
代理(console 代理 pgAdmin 的先例可循)或部署侧路由(tunnel 层)。
不阻塞 W2–W6,阻塞 W7 与 Step 2 收尾。

### W8 欠账清理(开源前必须,不阻塞搬迁)

#1078 泄漏 gateway key 吊销;staging 测试账号密码轮换;Slack 中 GitHub App
key/secret 轮换。Cloudflare 两笔(2026-08-12 增):吊销旧 user token
(2026-06-15 铸,随旧仓部署链退役);轮换 2026-08-11 新铸的 Account Token
——其值曾经 Slack 与 agent 会话两条聊天通道传递,与 pem 同性质的债。

## 4. 执行原则

- **体验守恒**:staging 自动跟、prod 手动两下,任何批次不得破坏。
- **行为冻结**:搬迁 PR 不混行为变化;判据仍是契约测试 + 生效路由快照。
- **单变量**:消费方式变更(W2)与仓库搬迁(W5)不同窗。
- **生产周边显式化**:"不碰生产"仅对网关生产部署成立;W1/W5 会触碰生产
  周边(runner、doc 站 Pages、告警 worker、secrets、旧部署入口),每批 PR
  body 必须列出自己的生产影响面。
- **staging 先行**,prod 永远最后切。
- **pin 不浮动**,无例外。

## 5. Step 2 完成判据

1. 两个手势(sync-main、Deploy Production)在 freeInference 仓完成
   staging + prod 部署,主机不再依赖 hybridInference 的部署链路;
2. hybridInference 的 **public-export 物化树**
   (`ops/release/public_export.py --materialize <tmpdir>`,连同 overlay
   replacements 在内的真实导出结果)上 brand/credential 扫描零命中——
   `--list` 只扫路径、读不到 export 新增的 replacement 内容,不作判据;
   `docs/agents/`、`docs/reviews/` 等历史文档不作为本判据对象,其去留
   (导出 manifest 排除,或随 W5 迁走)Step 3 前拍板;`distributions/`
   为空(2026-08-12 写实:中立示例已在 `config/examples/`,并无也不新造
   example overlay);上游 `make test` 全绿、无 overlay 中立启动可用
   (Step 1 判据③保持)。(门链可执行性 2026-08-12 已实测:物化 839 个
   上游文件 + 7 个 replacement,credential audit clean,物化树 strict
   brand sweep = 0 unclaimed / 8 pending streams——8 即 Step 2 未完成
   的正常现状,清零即达标;)
3. `upstream.lock` 为两仓唯一耦合点;bump 门红时 staging 停在旧 pin
   (演练证实);
4. 升级与两种回滚演练通过;
5. W8 全部清零(2026-08-12 起为五项:原三项 + Cloudflare 两笔)。

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
5. (不阻塞 W4–W6,W7 动工前必拍)W7 拆除上游 checkout 后 compose base
   的来源——候选见 W7 注记。

注(2026-08-12):执行侧对 1–3 的建议——接受豁免、裸 SHA、每次 push
(bump 频率反向调整成本低,先保"体验守恒");待 Murphy 拍板,拍板后
W4 即可动工。

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

**本线认领的行动项——已解决(2026-08-09,随 H4)**:原
`ops/release/public_export.py:36` 对 `patch_gate.py` 的硬引用已消除:
credential patterns 独立成 `ops/release/secret_patterns.py`(单一来源,
export 审计与泄漏测试共用),H4 删除 agent 代码未伤及导出工具链。顺带,
H4 之后导出树不再含 agent 代码,08-06 勘察发现的"导出会连带公开整套
agent 代码"问题自动消解。
