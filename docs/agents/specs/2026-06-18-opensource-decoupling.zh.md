# Epic:为开源部署解耦用户管理 & Harvard 专有内容

**类型:** Epic / 跟踪型 issue
**状态:** 提案中
**目标:** 让 HybridInference 能被任何人部署 —— 不只是 `freeinference.org` —— 且无需改源码。

> 英文版(用于公开 repo / 提 issue):[2026-06-18-opensource-decoupling.md](2026-06-18-opensource-decoupling.md)

> **修订记录(2026-07-16,与主设计文档对齐):** 本 Epic 的 P0–P2 是
> [中立上游与发行版拆分设计](2026-07-16-hybridinference-neutral-upstream-multi-distribution-design.zh.md)
> Phase 1 的文件级实施清单;P3–P4 不阻塞该文任何 Phase,排在其 Phase 3 之后按需执行。
> 三处处方修订(正文均已按此改写,以下为变更说明):(1)「安全关键」章节——
> `wrangler.toml` 中的 account/database ID 是标识符而非凭据(git 中无已提交凭据),
> 轮换 `CLOUDFLARE_API_TOKEN` 作为廉价保险即可,**不做 git 历史重写**;具体公开
> 机制见主文档「公开与可见性策略」及其待决策 10。Statcounter 环境变量化仍为公开
> 硬前置。(2)P1/P2 默认值与品牌内容——改为三步式:legacy FreeInference 默认值与
> 素材保留,neutral profile 提供占位或隐藏,overlay 成为生产真值后才删除 legacy。(3)P5 中
> 「解开 RouteWise 依赖」实为独立决策项:两种方案都意味着 RouteWise 代码公开,
> 且现状下 `strategies/__init__.py` 启动硬 import、`routing/routewise/` 多处 import
> `routewise.core`,实际工作量 M/L——已上移为主文档 Phase 3 的入口门槛。

---

## 动机(为什么做这件事)

HybridInference 解决的是一个有普遍价值的问题:用**一个 OpenAI 兼容的入口**,把请求
在本地推理服务(vLLM / SGLang / Ollama)和一堆远端 provider 之间路由,带健康检查、
熔断,以及成本感知的路由(RouteWise)。这个能力远不止 Harvard 用得上 —— 任何想自建
"免费/低成本统一推理网关"的实验室、公司或个人都需要它。

但**今天它只能以 `freeinference.org` 的形态运行**:身份、品牌、用户管理策略、甚至
基础设施 ID 都焊死在代码里。别人想要同样的能力,只能 fork 然后大改。这浪费了这套
系统本可以产生的影响力,也让 RouteWise 这类路由研究拿不到外部真实场景的验证和贡献者。

**核心论点:阻碍开源的是"耦合",不是"能力"。** 系统的核心(路由、adapter、流式、
存储抽象)已经足够通用;真正卡住的是两件事 ——(1)用户管理系统把**某一个组织的策略**
(审批流程、配额、域名白名单)写成了唯一选项;(2)Harvard 的身份和基础设施被硬编码。
一个全新部署者想自己决定鉴权姿态(不鉴权 / 用自己的 SSO)、自己的品牌、自己的策略,
并且**绝不应该**继承 Harvard 的基础设施或身份。

因此这个 epic 的目标是:**解耦,让默认的开源构建"中立且任何人开箱即用",而
`freeinference.org` 的那些专有设定退化成叠加在上面的一份配置。**

---

## 概述

网关的架构本身已经足够干净、可以开源:路由引擎、adapter 框架、HTTP/SSE 管道,
以及两层存储抽象(`OperationalStore` / `LogStore`)都是通用的,可以原样发布。
Harvard 耦合**不在架构层面**,而集中在四处:

1. ~30 处硬编码的 `freeinference.org` 字符串,被当成代码默认值。
2. **泄露进源码/git 历史的 Harvard 真实基础设施**(Cloudflare 账号 + D1 ID、Statcounter 账号)。
3. 前端品牌(团队 bio、赞助商 logo、`@harvard.edu` 文案)。
4. 代码里隐含"auth / quota / 审批永远开启"的假设。

所以这是**配置抽取 + 功能开关**的活,不是核心重写。

**推荐方案:** 功能开关驱动的单体 + 在现有 `Depends()` 接缝处放薄接口 ——
**不做**插件包,**不**搞 fork。代码现在就已经通过 FastAPI `Depends()` 注入
auth/quota/concurrency,也已经有 `runtime_settings` 注册表和 `USER_AUTH_ENABLED`;
我们顺着这个现成模式延伸即可。

相关:#642(decouple router from service)。

---

## ⚠️ 安全关键 —— repo 公开前必须先处理

这些会泄露 Harvard 真实基础设施,是硬阻断项:

- [ ] **`wrangler.toml` 中的 Cloudflare 账号 + D1 database ID** ——
  `services/status-monitor-worker/wrangler.toml:7,37,38`。它们是标识符而非凭据
  (文件注释本身写明 `account_id` 非机密;真正的 secret `CLOUDFLARE_API_TOKEN`
  从未提交)。从 HEAD 参数化移除,轮换 `CLOUDFLARE_API_TOKEN` 作为廉价保险;
  **不做 git 历史重写**——本私有仓的历史永远不随公开发布(公开方式见主设计文档
  「公开与可见性策略」),重写历史只会作废所有活跃 worktree 和进行中 PR。
- [ ] **Statcounter 分析代码块** —— `apps/frontend/src/app/layout.tsx:27,31,57`
  (project `13224568`,security key `2d8ab84a`)。否则每个部署者的流量都会流进
  Harvard 的分析账号。改成由 `NEXT_PUBLIC_STATCOUNTER_PROJECT_ID` 控制、默认关闭。
- [ ] 复查 `.gitleaks.toml`,确保轮换后的真实值不会被现有 allowlist 规则误屏蔽。

---

## 范围

### 在范围内
- 后端配置抽取(去掉硬编码的 `freeinference.org` 默认值)。
- 把 auth / quota / concurrency / admin / email 放到开关后面,默认是放行的 no-op。
- 前端品牌/主题改成配置驱动。
- 自托管所需的文档、license、打包。

### 不在范围内(对线上部署零行为变更)
- 路由引擎、adapter、SSE/流式热路径(原样发布)。
- 存储 schema / 迁移(已经通用、与部署无关)。
- 注册域名白名单机制本身 —— 它**已经**是数据驱动的(`signup_allowed_domains`
  表为空 = 全放行 + 自动批准);只需改前端硬编码的 `@harvard.edu` 文案和文档。

---

## 分层地图

| 层 | 内容 | 处理方式 |
|---|---|---|
| **(a) 可复用核心** | 路由引擎、adapter、HTTP/SSE、存储抽象、JWT/bcrypt、`runtime_settings` | 原样发布(只修一处:`adapters/openrouter.py:11-12`) |
| **(b) 可插拔/可选** | API-key 校验 + 配额(`servers/auth.py:88-299`)、每用户并发(`concurrency.py:203-325`)、模型门禁(`completions.py:495-552`、`model_access.py:25-30`)、admin 面、email | 放到开关后面,默认 no-op/放行 |
| **(c) 部署配置** | URL、邮箱、CORS、DB 名、角色配额/并发默认值、`NEXT_PUBLIC_*`、`models.yaml` | 移到 env / 配置文件;FreeInference 默认值保留至 overlay 成为生产真值(见 P1) |
| **(d) Harvard 专有** | 团队页、赞助 logo、Harvard SEAS 元数据、`@harvard.edu` 文案、Statcounter、Cloudflare ID、LICENSE 版权、RouteWise 锁定 | 删除或改成配置驱动 |

---

## 分阶段计划(每阶段都能独立上线)

### P0 —— 基础设施 & 密钥抽取(工作量:S)—— **最先做**
- [ ] 把 `wrangler.toml` 的 `account_id` / `database_id` / `database_name` /
      `GATEWAY_BASE_URL`(`:7,17,37,38`)参数化为 Wrangler env 变量。
- [ ] 轮换 `CLOUDFLARE_API_TOKEN` 作为保险(见安全章节;不做历史重写)。
- [ ] 把 status-monitor worker 做成**可选** add-on,而非必需依赖。

### P1 —— 后端配置抽取扫荡(工作量:S/M)
把每一处 `freeinference.org` / `admin@freeinference.org` 字面量换成 env 驱动的
`Settings` 字段。**暂不改变随代码发布的默认值**:legacy FreeInference 默认值
原样保留,**neutral profile** 提供通用占位(`example.com`、`localhost`),
overlay 成为生产真值后才删除 legacy 默认——以维持主设计文档"默认 FreeInference
行为不变"的不变量:
- [ ] `QUOTA_CONTACT_EMAIL` —— 3 处:`servers/auth.py:28`、
      `servers/routers/user_routes.py`、`schemas_auth.py`(QuotaInfo/QuotaExceeded)。
- [ ] `base_url` / `frontend_url` —— `config/settings.py:82,85`(及 `.env.example`)。
- [ ] `smtp_from_email` / `smtp_from_name` —— `config/settings.py:78,79`。
- [ ] `db_name` 默认值 `freeinference_db` —— `config/settings.py:20`。
- [ ] CORS origins —— `config/settings.py:110-116` → 解析 `CORS_ALLOWED_ORIGINS`。
- [ ] OpenRouter `HTTP-Referer` / `X-Title` → env 变量 —— `adapters/openrouter.py:11-12`。
- [ ] 角色级配额(`runtime_settings.py:107-138`)和并发(`:83-106`)默认值
      → `config/quotas.yaml` / env 覆盖。

### P2 —— 前端品牌配置化(工作量:M)
与 P1 相同的三步式:FreeInference 内容作为编译期 legacy 默认保留,neutral
profile 隐藏/省略它们,物理删除等 overlay 成为生产真值之后。
- [ ] 新建 `apps/frontend/src/config/branding.ts`(或 `branding.json`):
      `{ appName, orgName, labUrl, docsUrl, statusUrl, githubRepo, supportEmail,
      sponsors[], teamMembers[], showTeamPage }`,全部可由 `NEXT_PUBLIC_*` 覆盖。
- [ ] 改写 Hero、Header、SiteFooter、Sponsors、Team、Terms、BuildInfo、
      CodeExample、Features、`layout.tsx` 元数据、`config/env.ts` 的 apiBase。
- [ ] 团队/赞助商区块在配置为空时隐藏。
- [ ] Statcounter 由 `NEXT_PUBLIC_STATCOUNTER_PROJECT_ID` 控制(默认关)。
- [ ] 把 Harvard 素材(`public/team/murphy-tian.jpg`、
      `public/sponsors/harvard-seas.svg`)和 `next.config.js` 中的
      `junchengyang.com` 收进 branding 配置作为 FreeInference 默认值;
      物理删除等 overlay 成为生产真值之后。
- [ ] 把 `@harvard.edu` 快速通道文案(`signup/page.tsx:148-150,236`、
      `lib/schemas/auth.ts`)换成配置驱动(`NEXT_PUBLIC_FAST_TRACK_DOMAIN`、
      `NEXT_PUBLIC_SIGNUP_REQUIRES_REVIEW`);未设置时隐藏该提示。

### P3 —— auth / quota / concurrency 功能开关(工作量:M/L)—— 最大阶段
- [ ] 引入 `AuthProvider` 协议,内置三个实现:`NoAuthProvider`
      (全放行,`USER_AUTH_ENABLED=false` 时选用)、`ApiKeyAuthProvider`
      (现有逻辑,从 `servers/auth.py` 抽出)、`ExternalJwtAuthProvider`
      (自带 SSO,复用 `utils/jwt.py`)。
- [ ] 把现在内联在 `verify_api_key` 里的配额检查和邮箱验证
      (`auth.py:205-225`、`227-280`)拆成独立的 `QuotaEnforcer`
      (默认 `NoQuota` vs `DailyCostQuota`)和验证 gate,使两者能各自独立关闭。
      **配额算术逐字保留在 `DailyCostQuota` 里,另加 `NoQuota` 新类 —— 抽取时
      不要顺手重构数学逻辑。**
- [ ] `ConcurrencyLimiter`(`NoLimit` vs 现有 `concurrency.py:203-325`)。
- [ ] `ModelGate`(`AllowAll` vs 角色可见性 + 黑名单)。
- [ ] 新开关:`QUOTA_ENABLED`、`CONCURRENCY_ENABLED`、`MODEL_GATING_ENABLED`、
      `ADMIN_ENABLED`(关闭时 admin 路由返回 404)。
- [ ] **用流式补全验证 `NoAuth + NoQuota` 这条路径** —— no-op enforcer 必须同步、
      轻量、绝不缓冲 SSE 响应(中间件顺序很关键)。

### P4 —— 可插拔 email + 外部 IdP(工作量:M)
- [ ] 让 `SendEmailBackend` 可替换:SMTP / console / no-op
      (`is_email_enabled()` 已经做了一半)。
- [ ] 提供 `ExternalJwtAuthProvider`(issuer / JWKS URL 来自 env)。
- [ ] 让 Turnstile 可选(`ENABLE_TURNSTILE`,默认关)。

### P5 —— 文档、license、打包(工作量:S/M)
- [ ] 重新品牌化 README / docs / AGENTS.md;参数化 GitHub org 引用。
- [ ] 更新 LICENSE 版权行(`LICENSE:3`)。
- [ ] 按主文档 Phase 3 入口门槛处理 RouteWise 依赖(`pyproject.toml:31`):
      发布 RouteWise(wheel 或公开仓库)或 vendor 进上游。**不提供**"可选
      extra"选项——RouteWise 是上游一等算法,`strategies/__init__.py` 启动即
      硬 import,核心必须随它一起发布。
- [ ] 提供:带通用值的 `.env.example`、`config/models.example.yaml`
      (占位 endpoint、无真实别名)、以及 `examples/` 下带占位的 Docker
      Compose / systemd。

---

## 风险与坑
- **流式 / 中间件顺序(高危):** P3 把 auth/quota 决策插在路由前的热路径;
  no-op enforcer 绝不能缓冲 SSE 响应。要专门测无 auth 的流式路径。
- **已提交标识符(低危,已修订):** 见安全章节 —— 轮换 API token 作为保险并
  从 HEAD 参数化;不做历史重写,私有仓历史永远不随公开发布。
- **License(低/中):** MIT 没问题,但版权写的是 Harvard SEAS;确认 RouteWise
  本身可再分发,否则做成可选依赖。
- **D1 vs Postgres(中):** 主存储是 Postgres + 干净抽象;status-monitor 的 D1
  完全独立,应做成可选 add-on。别让"我们支持 D1"暗示网关需要 Cloudflare。
- **Admin 引导:** `ADMIN_EMAILS` 会在登录/启动时把匹配用户自动提升为 admin ——
  保留它作为文档化的"首个 admin"开通路径;它解决了空库的先有鸡还是先有蛋问题。

---

## 验收标准
- [ ] 全新 clone 在 **neutral profile** 下能端到端跑通,且该 profile 的构建与
      配置中不含任何 `freeinference.org` / Harvard 字符串(legacy FreeInference
      默认值可保留至 overlay 成为生产真值)。
- [ ] `USER_AUTH_ENABLED=false` 时能提供 chat completions(含流式),
      且无需任何 DB 用户态。
- [ ] HEAD 中不含任何 Harvard 真实凭据;已提交的标识符完成参数化,公开发布
      永远不携带本仓历史。
- [ ] 部署者无需改源码即可设置自己的品牌、支持邮箱,并(可选)接入自己的 SSO。
