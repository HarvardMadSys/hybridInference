# 同仓拆分执行计划(Step 1 of 3)

> 状态:执行计划(executable plan)。设计依据见
> [2026-07-16 主设计文档](../specs/2026-07-16-hybridinference-neutral-upstream-multi-distribution-design.zh.md)、
> [#953 归属分类](../specs/2026-07-17-repo-ownership-classification.zh.md)、
> issue #738(生产安全不变量)。与 issue #1031(终态 RFC)冲突之处见 §7-1,
> 以本文 + 主设计文档为准,#1031 待修订。
>
> 创建:2026-07-27,基于 Murphy 与 Claude 的对话及 Juncheng 的 Slack 意见。
>
> **2026-08-26 决策更新：** Step 3 直接公开现有 HybridInference 仓库，
> 不再采用本文早期推荐的“同名新仓 + 过滤导出”。完整门槛见
> [direct-publication readiness plan](2026-08-26-direct-publication-readiness.md)。

## 1. 总目标:三步走

1. **Step 1(本文范围):同仓拆分。** 在现有私有仓内完成"中立上游 vs
   FreeInference 专属"的边界:FreeInference 内容收进
   `distributions/freeinference/` overlay,上游侧换成中立默认值。
2. **Step 2:搬出去。** 拆好的 FreeInference 侧(overlay + 标记的
   ops/workflow)整体迁至 `HarvardMadSys/freeInference`
   (2026-07-27 已建,空、私有),并在该仓重建部署信任链。
3. **Step 3:开源。** 完成 tree/history/CI readiness 后，直接把现有
   `HarvardMadSys/hybridInference` 设为 public。

Juncheng 要求全部就绪前保持私有;三步走天然满足(直到 Step 3 前没有任何公开动作)。

### 与 #738 既有方案的对应关系

#738 正文的分阶段方案仍然有效,三步走是它的落地版:

| #738 | 三步走 | 备注 |
|---|---|---|
| Phase 0 安全基线 | 已基本完成 | 契约测试(#949)、归属分类(#953)覆盖其主要项 |
| Phase 1 仓内逻辑边界 | **Step 1(本文)** | 目录布局、"核心禁 import distributions/freeinference"、CI 独立测试、中立默认——本文 §3-E 直接实现 #738 Phase 1 的规则 |
| Phase 2 对外发布契约 | Step 1 末尾 → Step 2 前 | dark 模式即其 "shadow mode config" 的落地;digest 发布、SemVer 等在 Step 2 前完成 |
| Phase 3 抽出 operations 仓 | **Step 2** | 仓名由工作名 `freeinference-operations` 改为实际的 `HarvardMadSys/freeInference` |
| (未覆盖) | **Step 3 开源** | #738 只做边界与抽取,公开动作见主设计文档"公开与可见性策略"与 §7-1 |

与 #738 的两处刻意偏差:

1. **RouteWise 定位**:#738 称 RouteWise/Nimbus 为上游一等算法;#1031 改为
   "可选、可私有"。两文互相矛盾,即 §7-2 待拍板项,拍板后修订输掉的那份。
2. **零停机机制降级**:#738 的多实例金丝雀 / 逐实例滚动假设有实例池;
   现实是单 backend 实例 + SSH checkout 部署(#1031 也明确首发只支持单副本)。
   Step 1/2 采用"staging 先行 + env 一行回退"的轻量等价物;#738 的
   fleet 机制保留为未来多副本时的处方,不作为当前门槛。

## 2. 执行原则

- **行为冻结**:搬迁 PR 绝不混入行为变化(#738 不变量)。判据是
  #949 契约测试 + **生效路由表快照**(`iter_effective_routes`,含 DB
  覆盖层合并结果)flip 前后一致——文件 digest 相同只是手段,不是终局判据。
- **一类内容一个 PR**,每步独立可回退。
- **staging 先行**;prod 是 SSH checkout 部署,flip 跟部署节奏走。
- **搬迁方式按内容分两种**:
  - 内容类(品牌、docs、模板、Terms):**原子搬**——单 PR 完成
    移动 + manifest 指向 + flip + 删 legacy,不留双副本窗口;
  - `models/routing/alerts`:**双副本 + dark 对比**——事故处理时
    这些文件真的会被人改,dark 的逐字节对比就是双副本期间的漂移报警。
    窗口压到几天,flip active 后立即删 legacy。
- **dark 接线验证是部署 checklist 上的一项(约 30 分钟),不是里程碑**。
  digest 对比是确定性的,一次干净启动 + smoke 通过即为足够证据,
  不存在"连续观察几天"的意义。
- CI 红先对照 dev 已知的 xdist flake 基线(随机顺序跨测试状态泄漏,
  约 10 个固定失败),再判断是否与本 PR 相关。

## 3. 工作项(按序)

### A. dark 接线验证(前置,随下次 staging 部署)

- staging `.env` 加两行:
  `DISTRIBUTION_CONFIG_PATH=distributions/freeinference/distribution.yaml`、
  `DISTRIBUTION_CONFIG_MODE=dark`,重启 backend(用户零感知)。
- 跑 `distributions/freeinference/smoke_dark_load.py`,要求全部 identical。
- 目的只有一个:证明解析链和 staging 接线是通的(env 未被遮蔽、
  目录挂载可见、相对路径解析正确)。

### B. 内容搬迁(低风险)

- **B1** 前端 runtime 消费 `/site-config`(#957 只做了后端端点 +
  编译期 `branding.ts`;此项为三步式第二步)。会刻意翻转 #949 冻结的
  站点默认值断言——设计内行为。
- **B2** 内容进 `distributions/freeinference/content/`(原子搬):
  `docs/free_inference/`(公共站源码)、RAG corpus 与预建索引、
  邮件模板、Terms/Privacy、team/sponsors 数据。
- **B3** 上游侧同步:中立 README、中立品牌默认值(无 overlay 时
  显示 HybridInference 身份,绝不回落 FreeInference/Harvard)。

### C. 配置搬迁(中风险)

- **C0 前置:DB 运行时覆盖收敛。** 导出当前激活的
  `weight_overrides` / `disabled_providers` / `model_visibility` /
  `model_concurrency` / `routewise_model_settings` / `site_settings`,
  逐条裁定:事故残留清掉;长期意图固化进 `models.yaml` 后删 DB 行。
  收敛后 YAML 才逼近生产真值,搬它才有意义。导出快照存档作为验收基线。
- **C1** `config/models.yaml` / `routing.yaml` / `alerts.yaml` 真值
  复制进 overlay,manifest `paths:` 改指 overlay 内路径;dark 全
  identical 后 flip active;生效路由表快照对比一致;删 legacy。
- **C2** 上游 `config/` 换成中立 reference 配置。**此项与
  Juncheng 的 default route 意见合并实现**:openrouter reference
  registry(少量主流模型 + `${OPENROUTER_API_KEY}`)+ local-eval
  的 stub/Ollama 分层。维护责任与刷新方式在该 PR 内定义。

### D. 部署/运维(同仓阶段最小化)

- 按 #953 清单**分类标记即可,物理搬迁留给 Step 2**(终点是
  freeInference 新仓,不在同仓内搬两次)。
- 同仓阶段唯一动作:`ops/`、`deploy/` 脚本对配置的引用收敛到
  DistributionConfig 解析出的路径,不再硬编码 `config/`。
- workflow 文件受 GitHub 限制留在 `.github/workflows/`,
  FreeInference 专属的打 step-2 标记。

### E. 边界 CI 门(与 B/C 并行;同仓拆分对"物理仓界"的等价物)

- 上游代码禁止 import `distributions/`;
- banned-strings lint:`freeinference.org`、Harvard 品牌等字符串
  只允许出现在 overlay 与 #953 白名单的 mixed 文件;
- **无 overlay 中立启动测试**:不设 `DISTRIBUTION_CONFIG_PATH`,
  以中立默认启动并通过核心测试(clean-room 的同仓版)。

## 4. Step 1 完成判据

1. staging/prod 以 **active** mode 从 overlay 读全部 FreeInference 真值;
2. overlay 与白名单之外 grep 不到 FreeInference 身份(E 门持续强制);
3. 无 overlay 启动 = 可用的中立产品(有 default route,`make test` 全绿);
4. #953 清单中每个 freeinference 归属路径:已进 overlay,或带 step-2 标记。

达成后,Step 2 退化为"`distributions/freeinference/` + 标记项整体 mv 到
新仓 + 重建信任链",Step 3 按 §7-1 拍板的机制执行。

## 5. 上游产品面工作(与拆分并行,来自 Juncheng 意见)

做在上游侧、不阻塞搬迁,但决定开源产品是否可用:

1. **Default route**:并入 C2 实现。
2. **Local/hybrid 部署接口**:v1 定为静态 YAML + 明确 reload 语义;
   补一条不依赖 GPU 的 local-endpoint CI 路径(fake OpenAI server);
   compose 的 Ollama(host)/vLLM(GPU passthrough)接线写进文档;
   per-endpoint capacity(max_concurrent / in-flight)列为设计项。
3. **Router 扩展性**:policy(决策)与 engine(执行)拆分、
   按名注册的插件机制、Routing Arena replay 与线上共用同一 policy
   接口。验收 litmus test:新 router 只写一个 policy 文件 + 用
   Arena 离线评估,全程不碰 serving 代码(以 Juncheng 的
   makespan/uncertainty router 为第一个用例)。设计文档先行,
   实现不在 Step 1 关键路径上。

## 6. 已识别、低优先级(开源前修)

- **DB 运行时覆盖的可见性**:admin 界面对被覆盖值加"网页改动覆盖中"
  标记 + 一键恢复到配置文件值。外部部署者的易用性问题,与
  FreeInference 数据安全无关。

## 7. 待拍板(人工决定,不阻塞 A–C 动工)

1. **Step 3 公开机制(2026-08-26 已决定):** 翻转现有仓库；历史、issues 和
   PR 都进入公开面。公开前按 readiness plan 完成私有内容迁移、完整历史清理和
   public CI hardening。
2. **RouteWise 是否随上游公开**(owner:Murphy + Juncheng;EuroSys 在审):
   只 gate Step 3,不阻塞 Step 1 任何工作。
3. **License / Harvard 授权**(owner:Murphy):审批周期最长,建议立即启动;
   同时查 "HybridInference" 的 PyPI / npm / org 名可用性。
4. **freeInference 新仓信任链**(owner:Murphy):OIDC、Environment
   secrets、runners、deploy key,只能手工建;Step 2 前完成即可。
