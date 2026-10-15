# Auto 模式：按请求自动选择模型的设计

- 日期：2026-10-13
- 状态：讨论稿。调研结论与抽象均为提议。[实施计划](../plans/2026-10-13-auto-mode-implementation.zh.md)的 PR 1（共享资格检查）已随本文合并（#1522）；PR 2 实现了不依赖第 10 节的选择核心；PR 3 把它接入两个接口，由运行时开关控制，默认关闭。依赖第 10 节的部分等它确定后再做
- 范围：新增"按请求选择模型"的一层，提供 intelligence / balance / fast 三个档位；不改变 Fixed / RouteWise 在单个模型内选择 endpoint 的行为
- 相关记录：[实施计划](../plans/2026-10-13-auto-mode-implementation.zh.md)、[可组合的 Hybrid Routing 设计](2026-09-14-composable-hybrid-routing-design.zh.md)（§8.2 只读估计接口）、[RouteWise 会话级 cache-aware 路由](2026-06-01-routewise-session-cache-aware-routing-design.zh.md)

本文记录三件事：业界 auto 路由的实现与抽象、我们自己的抽象，以及接入现有 inference stack 的方式。第 3 节对闭源产品的描述来自其公开文档（2026-10 查阅），可能随时变化。

## 1. 背景与目标

调用方希望不必自己挑模型：请求一个 auto 档位，由网关按请求选择模型和推理强度。三个档位：

- **intelligence**：质量优先，不计成本。
- **balance**：在质量接近最好的候选里选最省的。
- **fast**：在质量不低于下限的候选里选最快的。"fast"指低延迟还是低成本，见第 10 节。

目标：

1. 只有一个选择层，由 `/v1/chat/completions`（Responses API 也经由它）和 `/v1/messages` 共用，不在每个接口各写一份。
2. 档位是配置，三个档位共用同一条选择流水线；质量估计算法可以替换（静态 → 查表 → kNN → 学习型）。
3. 选出模型之后，执行仍交给该模型现有的 Router；点名具体模型的请求行为不变。
4. 每个决策可追溯、可度量；先以影子模式运行，再实际生效。

非目标：

- 不替代 Fixed / RouteWise 的 endpoint 选择。
- 第一版不做级联（先用便宜模型、不满意再升级），也不做跨模型 hedging。
- 第一版不做通用的布尔规则 DSL。
- 不在 gateway 前面再放一层外部路由网关。

## 2. 术语：三种"路由"

推理生态里的"路由"指三件不同的事：

| 层次 | 在什么范围里选 | 例子 | 在本项目 |
|---|---|---|---|
| 模型内部 | 同一模型内的 expert，逐 token 选 | MoE | 不归网关 |
| 同一模型的多个副本 / endpoint | 负载、健康、prefix cache 亲和、成本 | vLLM production-stack router、llm-d、SGLang router | Fixed / RouteWise |
| 不同模型之间 | 按请求选模型或推理模式 | vLLM Semantic Router、RouteLLM、OpenRouter Auto、Cursor Router | 本文的 auto |

auto 只回答"这个请求用哪个模型、什么推理强度"。"这个模型走哪个 endpoint"仍由该模型绑定的 Router 回答。

## 3. 业界实现调研

### 3.1 RouteLLM（LMSYS，开源）

Python 库加 OpenAI 兼容服务，在强、弱两个模型之间二选一。仓库 2024-08 之后没有更新。

```python
class Router(ABC):
    def calculate_strong_win_rate(self, prompt: str) -> float: ...  # 强模型胜出的概率

    def route(self, prompt, threshold, routed_pair: ModelPair) -> str:
        ...  # win_rate >= threshold 返回强模型，否则返回弱模型
```

- 实现：`mf`（矩阵分解）、`sw_ranking`（相似度加权排名）、`bert`、`causal_llm`（LLM 分类器）、`random`，用 Chatbot Arena 偏好数据训练。
- 入口：路由器和阈值编码在模型名里，如 `router-mf-0.116`。`Controller` 解析后改写 `model`，再交给 LiteLLM 调用。
- 阈值校准：在样本集上按"期望多少比例走强模型"反推阈值。线上实际比例会随请求分布漂移。
- 局限：只看最后一条消息（代码注释说训练数据是首轮对话）；只有两个候选；没有会话处理和能力过滤。
- 可借鉴：**估计与决策分离**。路由器只输出分数，阈值在外部施加，同一个估计器换个阈值就是另一个档位；阈值可以按目标比例校准。

### 3.2 vLLM Semantic Router（vllm-project，开源）

它不是 vLLM 推理引擎的一部分，而是独立的网关层：Go 实现，可以作为 Envoy ext_proc 运行，也可以用 `vllm-sr serve` 单独运行，分类模型用 Rust/Candle 推理。后端是多个推理服务或云端 API。它最早的用例只需要一个模型：判断请求要不要开推理模式（论文 "When to Reason"）。

请求链路：

```text
entrypoint（虚拟模型名，如 vllm-sr/auto）→ recipe（一整套路由配置）
  → signals → projections → decisions → algorithm → model → endpoint
```

- **signals**：启发式信号（关键词、上下文长度、语言、权限，亚毫秒级）和模型信号（embedding、领域、事实性、用户反馈、模态、复杂度、偏好，约 10–120 ms）。只计算启用中的 decision 用到的信号。
- **projections**：把信号加权成连续分数（如 `request_difficulty`），再按阈值切段（如 `support_fast` / `support_balanced` / `support_escalated`）。
- **decision**：名称、布尔规则、候选模型（`modelRefs`）、算法、插件、优先级。多个 decision 同时匹配时，按优先级（或置信度）取一个。
- **modelRef**：模型加推理设置，可带 `use_reasoning`、`reasoning_effort`、`lora_name`。
- **algorithm**：接口为 `Select(查询 embedding, 领域, 候选及成本, 参数) → (模型, 置信度)`。实现包括 static、Elo、RouterDC、AutoMix、hybrid、kNN、KMeans、SVM、MLP、Thompson sampling、latency-aware、session-aware 等。
- **plugins**（按 decision 配置）：越狱 / PII 检测、响应缓存、RAG、system prompt、header 改写、影子流量（`shadow_dispatch`）。
- **会话保护**（`learning.protection`，状态存 Redis）：

  ```yaml
  protection:
    scope: conversation
    tuning:
      idle_timeout_seconds: 300
      min_turns_before_switch: 1
      switch_margin: 0.05
      stability_weight: 1.0
  ```

  session-aware 算法在工具循环和 provider 续接状态下硬锁模型，并把 prefix cache 和切换代价计入决策（`prefix_cache_weight`、`handoff_penalty_weight`）。
- **endpoint 层**：`providers.models[].backend_refs` 按权重、健康检查、离群剔除选择后端，与模型选择分开。

可借鉴：虚拟模型名映射到一份配置；候选自带推理设置；会话保护的参数；影子流量。代价是配置面很大（官方示例配置约 3000 行），策略主要靠人写。

### 3.3 OpenRouter Auto（闭源，机制有文档）

```json
{
  "model": "openrouter/auto",
  "plugins": [
    {
      "id": "auto-router",
      "allowed_models": ["anthropic/*"],
      "excluded_models": [],
      "cost_tier": "medium"
    }
  ],
  "session_id": "可选"
}
```

- 机制：分类器把请求归到约 30 种任务类型之一（如 `code:debugging`）→ 按该类型过去 7 天的全站花费份额给模型排名 → 按 `cost_tier`、账号限制、合规策略、模态要求过滤 → 剩下的前几名作为主选加备选。分类或排名不可用时，回落到默认模型集合。
- `cost_tier`（low / medium / high / xhigh / max）是一个区间，不是上限：区间外上下两头都排除。
- 会话：按 `session_id` 或消息指纹识别。每轮都重新排名，原模型仍在前几名就继续用它；同一会话也尽量留在同一个 provider。
- 输出：响应的 `model` 是实际模型；带 `X-OpenRouter-Metadata: enabled` header 时返回任务类型。没有可选模型时返回 404。按实际模型的价格计费，不另加价。
- 早期版本由 Not Diamond 提供，后来改为基于自家的任务类型排名。

可借鉴：**按类别查表**，不需要逐条请求的质量标签；"过滤 → 排名 → 主选加备选"的输出形状；"仍在前几名就不换"的粘性规则；在响应中回显决策信息。

### 3.4 Cursor Router（闭源，2026-07 发布）

- 接口：模型选择器里的 Auto 加 `optimize_for`（cost / balanced / intelligence）；SDK 中的模型 ID 是 `auto-smart`。
- 机制：每个 agent 请求经过一个分类器，输入是查询、上下文、任务复杂度和领域；在质量相当的模型里选最省钱的。
- 训练：60 万+ 条线上请求。标签是从用户行为推断的满意度：接着做下一个功能为正，纠正 agent 为负。
- 评估：用大规模线上 A/B，而不是离线评测。指标是 keep rate（生成的代码过一段时间后仍留在代码库里的比例）和每个 commit 的成本。训练和评估都计入了切换模型导致的缓存失效成本。
- 模型池：由 Cursor 维护（文档当前列出 GPT-5.5、Claude Opus 5、Grok 4.6、Claude Fable 5），Grok 4.6 是必需的低价选项。管理员禁用某个模型时，改选允许的模型。Cost 模式沿用旧 Auto 的逻辑（按可靠性和负载选）。
- 厂商自报：相比全部用 Opus 4.8，早期客户节省 30–50%，质量不降。未经独立验证。

可借鉴：用隐式反馈当标签；把切换成本放进决策；每个档位用一个质量参照点来定义（Intelligence 对标最强的模型，Balance 对标最常用的前沿模型）。

### 3.5 其他参考

- **Azure model router**：Quality 模式选质量最高的；Balanced 模式在最佳质量 1–2% 以内的模型里选最便宜的；Cost 模式把区间放宽到 5–6%。这个"质量带"是 balance 档目标函数的直接参照。
- **LiteLLM Auto Router v2**：SIMPLE / MEDIUM / COMPLEX / REASONING 四个层级各映射到一个模型或模型池；分类可用启发式打分、LLM 分类或关键词规则；`adaptive: true` 时在层级的模型池内做 Thompson sampling；`session_affinity` 按会话固定首轮模型（默认 3600 秒）；每个请求输出一行决策日志（原因、层级、选中的模型）。
- **可以包装成 Scorer 的学习方法**：RoRF（embedding + 随机森林，两两比较）、IRT-Router（项目反应理论，可解释模型能力和问题难度）、P2L（按 prompt 输出各模型的 Bradley-Terry 系数）、Route-to-Reason（同时选模型和推理策略）。离线比较可以用 LLMRouter（多种路由算法与训练流程）和 LLMRouterBench 的数据与框架。

### 3.6 横向对比

| 组成部分 | RouteLLM | vLLM-SR | OpenRouter Auto | Cursor Router |
|---|---|---|---|---|
| 入口 | 模型名 `router-mf-0.116` | 虚拟模型名 → recipe | `openrouter/auto` + plugin | `auto-smart` + `optimize_for` |
| 候选集 | 固定两个 | decision 的 modelRefs | 允许 − 排除 − 策略过滤 | Cursor 维护的池 − 被禁用的 |
| 请求表示 | 最后一条消息 | 信号向量 | 任务类型 | 查询 + 上下文 + 复杂度 + 领域 |
| 质量估计 | 逐请求胜率 | 规则 + 可选算法 | 按任务类型查表 | 逐请求分类器 |
| 档位 | 阈值 | decision 优先级、难度分段 | 价格区间 | 三个模式 |
| 会话 | 无 | 切换门槛、最少轮数、空闲重置 | 仍在前几名就不换 | 训练中计入缓存成本 |
| 反馈来源 | 离线 Arena 数据 | 反馈信号、Elo / Thompson | 全站花费份额 | 用户行为 |
| 模型 → endpoint | LiteLLM | providers 的后端权重 | 选完模型再做 provider 路由 | 未公开 |

两点结论：

1. 能看到实现的三家（RouteLLM、vLLM-SR、OpenRouter）都把"选模型"和"选 endpoint"分成两层，和本项目的分层一致。
2. 策略来源有三种：人写规则（vLLM-SR）、从标签学习（RouteLLM、Cursor）、从群体行为统计（OpenRouter）。本项目目前没有质量标签，也没有 OpenRouter 的流量规模，第一版只能人工定策略，但抽象上要留好换成学习型策略的接口。

### 3.7 研究结论

1. **简单方法够用，候选池比算法重要。** LLMRouterBench（2026-01）统一评测后发现，多数方法表现接近，几个新方法和商业路由器都不能稳定胜过简单基线；embedding 模型的影响有限；精选候选比堆模型有效。另一项研究发现，调好参数的 kNN 经常胜过复杂的学习型路由器。
2. **按"复杂度"打分的规则基本无效。** LiteLLM 的公开 benchmark 中，规则复杂度分预测"便宜模型够不够用"的 AUC 为 0.524，接近随机；基于 embedding 和标注样例的语义路由，在约 58% 的节省下对旗舰模型的胜率为 46.8%，规则方法为 41.5%。该 benchmark 只覆盖一个模型家族、300 个 prompt、一个评审模型，结论要打折扣。
3. **成本要按实际花费估。** 同一个 benchmark 中，中档模型标价是最便宜档的 14.1 倍，效果反而更差，而且它的 thinking token 比旗舰还多，大幅抵消了标价优势。
4. **会话粘性是共识，但不等于永不切换。** OpenRouter 和 LiteLLM 固定会话的模型；vLLM-SR 在工具循环中硬锁，并设切换门槛；Cursor 允许跨轮切换，但把缓存失效成本算进决策。
5. **agent 场景要看端到端结果。** TwinRouterBench（2026-05）专门评测 agent 中间步骤的路由。它的出发点是：现有基准从不检验"换成便宜模型后，整个任务还能不能完成"。
6. **候选池小而有差异即可，并且要测稳定性。** 一项 2026-07 的研究发现，不到 10 个精选模型就能覆盖大部分多样性；kNN 路由在同义改写的 prompt 上稳定性会下降。
7. **级联不适合本项目的主要流量。** FrugalGPT、AutoMix 这类方法要等完整输出才能判断是否升级，与流式输出冲突，升级时延迟翻倍。

## 4. 设计原则

1. **估计与决策分离**（RouteLLM）：Scorer 只输出每个候选的估计，不做选择；三个档位共用一个 Scorer，只有目标函数不同。
2. **档位是配置**（vLLM-SR 的 recipe、Cursor 的模式）：一个档位就是一份带版本号的 profile，不是三套代码。
3. **两层分开**（RouteLLM、vLLM-SR、OpenRouter）：auto 选模型，模型绑定的 Router 选 endpoint。
4. **先查表，后学习**（OpenRouter、LLMRouterBench）：第一版按任务类型查表，之后在接口不变的前提下升级到逐请求估计。
5. **切换要算账**（Cursor、vLLM-SR）：会话内换模型的收益，必须超过缓存失效等切换成本。
6. **先影子、可追溯**（vLLM-SR 影子流量、LiteLLM 决策日志、Cursor 线上 A/B）：每个决策都记录原因和版本，先影子运行，再实际生效。

## 5. 抽象

### 5.1 核心对象

| 对象 | 含义 | 参照 |
|---|---|---|
| `AutoProfile` | 一个档位：候选、目标函数、会话策略、版本 | vLLM-SR recipe、Cursor 模式 |
| `Candidate` | 候选 = 模型 + 参数覆盖（如 `reasoning_effort`） | vLLM-SR modelRef |
| `RequestFeatures` | 与接口格式无关的请求特征：prompt token 数、工具、模态、`max_tokens`、轮次、agent 步骤角色、会话 ID，以及可选的 embedding / 任务类型 | vLLM-SR signals |
| 约束 | 硬过滤：权限、能力、模态、上下文长度、允许 / 排除列表、可行性 | OpenRouter 过滤 |
| `Scorer` | 给每个候选输出 `Estimate`，不做选择 | RouteLLM 胜率、OpenRouter 排名表 |
| `Estimate` | 质量、置信度、预估成本、预估延迟 | — |
| `Objective` | 档位的目标函数，把估计排成有序列表 | RouteLLM 阈值、Azure 质量带 |
| `SessionPolicy` | 会话内是否沿用原模型、何时允许切换 | vLLM-SR protection、OpenRouter 粘性 |
| `Decision` | 有序候选（主选 + 备选）、原因、版本 | OpenRouter 主选加备选 |
| `Outcome` | 请求结束后的结果和隐式反馈，按 request / session 关联到 Decision | Cursor 满意度、vLLM-SR 反馈信号 |

### 5.2 选择流水线

```text
请求 + 档位
 → ① 约束   权限、能力、模态、上下文长度、允许/排除、可行性（只查询，不占资源）
 → ② 估计   Scorer 给每个剩余候选输出 质量 q̂ / 成本 ĉ / 延迟 t̂
 → ③ 目标   Objective 按档位排序
 → ④ 会话   SessionPolicy 决定沿用原模型还是切换
 → Decision（主选 + 备选、参数覆盖、原因、版本）
 → 现有执行路径：该模型绑定的 Router / 接口自己选 endpoint
```

### 5.3 接口草图

```python
@dataclass(frozen=True)
class Candidate:
    model_id: str
    overrides: tuple[tuple[str, str | int | float | bool | None], ...]
    # 如 (("reasoning_effort", "high"),)，按参数名排序


@dataclass(frozen=True)
class Estimate:
    quality: float                        # 相对分，0–1
    confidence: float
    cost_usd: float | None                # 输出长度预测 + 价格；未知时为 None
    latency_ms: float | None              # TTFT + 预估输出耗时；未知时为 None


class Scorer(Protocol):
    """只估计，不选择。三个档位共用。"""

    version: str

    def score(
        self, req: RequestFeatures, cands: Sequence[Candidate]
    ) -> Mapping[Candidate, Estimate]: ...


class Objective(Protocol):
    """档位之间唯一不同的部分。"""

    def rank(self, est: Mapping[Candidate, Estimate]) -> list[Candidate]: ...


@dataclass(frozen=True)
class AutoProfile:
    name: str                             # intelligence / balance / fast
    candidates: tuple[Candidate, ...]
    objective: Objective
    session: SessionPolicy
    version: str


@dataclass(frozen=True)
class Decision:
    ranked: list[Candidate]               # 主选 + 备选
    reasons: list[str]                    # "task=code"、"session_keep"、"drop:ctx_too_long" 等
    profile_version: str
    scorer_version: str


class ModelSelector(Protocol):
    def select(
        self,
        profile: AutoProfile,
        req: RequestFeatures,
        eligible: Sequence[Candidate],    # serving 层已按权限过滤
        session: SessionState | None,
    ) -> Decision: ...
```

配置加载时把覆盖参数按参数名排序并转为元组，第一版只接受标量值。
这样 `Candidate` 可作为估计映射的键，同一模型的不同推理强度是不同候选，
相同参数也不会因 YAML 中的顺序不同而变成不同候选。

Scorer 的实现按阶段替换，接口不变：

- `StaticScorer`：每个候选一个固定质量分，等价于有序列表。
- `TaskTableScorer`：按任务类型查质量表（OpenRouter 的做法），可以看作把整个类别当作邻居的 kNN。
- `KnnScorer`：按 embedding 找带标签的近邻，取它们在各候选上质量的平均。
- 学习型方法或外部推荐服务（3.5 节）都可以包装成 Scorer。

### 5.4 档位的目标函数

记 Q* 为剩余候选中最高的 q̂。

| 档位 | 选择规则 | 参照 |
|---|---|---|
| intelligence | 选 q̂ 最高的；并列时选 ĉ 低的 | Azure Quality、Cursor Intelligence |
| balance | 在 q̂ ≥ Q* − ε 的候选中选 ĉ 最低的 | Azure Balanced、Cursor Balance |
| fast | 在 q̂ ≥ q_floor 的候选中选 t̂ 最低的 | vLLM-SR `mom-v1-flash` |

- 备选列表按同一规则继续排序。
- ε 和 q_floor 在样本集上校准，使档位达到目标分布或成本预算（如"balance 最多 30% 的请求走最贵的候选"），做法同 RouteLLM 的阈值校准。
- 候选是"模型 + 推理强度"的组合，同一模型的不同 effort 是不同候选。ĉ 必须按实际花费（含推理 token）估计，不能只看标价。
- 如果 fast 定义为省钱（Cursor 的 Cost），把 t̂ 换成 ĉ、放宽 q_floor 即可，结构不变。

### 5.5 会话策略

会话识别沿用 [`session_identity()`](../../../apps/backend/serving/utils/session_identity.py)，它按顺序读取 `X-Session-ID`、agent 会话 header、请求体里的声明和 Claude Code 的复合 user id。没有会话标识时，退回到首条 system 消息与首条 user 消息的指纹（OpenRouter 的做法）。

规则：

1. **工具循环中锁定。** Anthropic 格式中最后一条 user 消息只含 `tool_result`，或 OpenAI 格式中最后一条消息是 `role: tool` 时，视为同一轮 agent 执行的继续：沿用当前模型，只在该模型不可用时更换。
2. **新一轮用户输入时可以重新选择**，但必须同时满足两条才切换：新首选在目标函数上比当前模型好出 `switch_margin`；收益超过切换成本。切换成本约为"预估缓存命中 token ×（输入价 − 缓存读价）"，加上冷缓存带来的额外 TTFT。缓存命中的证据可以复用 RouteWise 的 prefix cache 估计。
3. **空闲超时后重置。** 超过 `idle_timeout` 后，下一轮按新会话处理。
4. **辅助调用不改变会话模型。** 标题生成、摘要、压缩等小调用可以选档位内更便宜的候选，但不改写会话固定的模型。安全 / 权限判定类调用不降级，使用会话当前模型或档位内质量最高的候选（与现有小调用改投的排除规则一致）。
5. **切换时清理不可移植的状态。** 去掉上一个模型的 thinking block 与签名，不把 provider 续接 ID 转发给新模型。这些属于 serving 层的请求改写。

会话状态（当前模型、profile 版本、最近一轮的时间、轮数、最近一次切换）第一版放在进程内带 TTL 的 LRU 里：目前 gateway 是单个 uvicorn 进程（systemd 单元和 Dockerfile 都没有开 `--workers`）。扩成多副本时改用共享存储。

### 5.6 不变量

1. **选择没有副作用。** 不占预留、不产生 attempt、不给 endpoint 写健康证据；真实的 attempt 和反馈仍归模型绑定的 Router。这与 [routing 子系统约定](../../../apps/backend/routing/AGENTS.md)一致：未被联系的 provider 不得收到失败证据。
2. **不扩大权限。** 只选调用方直接点名也能用的模型，即 `model_access_denial()` 放行的模型：角色与可见性、用户禁用、grant 范围都生效。
3. **按实际模型记账。** `model_id` 记实际模型，请求的虚拟 ID 和决策记在 `metadata.auto`（6.5 节），成本与用量按实际模型计算。
4. **流式提交后不跨模型。** 跨模型 fallback 只能发生在向客户端输出第一个内容之前。
5. **会话内只在安全边界切换**（5.5 节）。
6. **决策可复现。** 记录 profile 版本、Scorer 版本、关键特征和原因。
7. **点名具体模型的请求不受影响。** auto 关闭或未被使用时，行为与现在完全一致。

## 6. 接入现有 stack

### 6.1 插入点与职责划分

```text
/v1/chat/completions ─┐   （Responses API 内部调用 chat_completions）
/v1/messages ─────────┤
                      ▼
 ① 解析请求，得到 RequestFeatures；session_identity()               serving
 ② eligible = 可见 ∩ 未禁用 ∩ grant 范围内                        serving
 ③ ModelSelector.select(profile, features, eligible, session)      routing（新增）
      ← 只读估计：准入状态、RouteWise 的输出长度预测与延迟、价格
 ④ 按 Decision 改写 body.model 与参数，清理不可移植状态，记录决策  serving
 ⑤ 之后不变：get_router(model) / _pick_dispatch_adapter
```

职责按 [routing 子系统约定](../../../apps/backend/routing/AGENTS.md)划分：routing 不负责 HTTP 解析、鉴权和协议转换，所以权限过滤和请求改写留在 serving；"在合格候选中选哪个模型"属于路由决策，放在 routing（例如 `apps/backend/routing/model_selection/`）。

### 6.2 可复用的现有能力

| 能力 | 现有代码 | 在 auto 中的用途 |
|---|---|---|
| 会话识别 | [`session_identity()`](../../../apps/backend/serving/utils/session_identity.py) | 会话固定的 key |
| 准入查询 | [`eligible_adapters()`](../../../apps/backend/routing/routers.py)、[`allow_request()`](../../../apps/backend/routing/endpoint_health.py) | 可行性过滤，纯查询 |
| 输出长度预测 | [`routewise/predictor.py`](../../../apps/backend/routing/routewise/predictor.py) | ĉ、t̂ |
| endpoint 延迟画像、参考成本 | [`routewise/router.py`](../../../apps/backend/routing/routewise/router.py) 的 `_latency_estimate`、`_estimate_value`（私有） | t̂、ĉ |
| prefix cache 证据 | [`routewise/prefix_cache.py`](../../../apps/backend/routing/routewise/prefix_cache.py) 的 `estimate()` | 切换成本 |
| 访问控制 | [`model_access.py`](../../../apps/backend/serving/model_access.py) 的 `model_access_denial()`：已发布、角色（含运行时可见性覆盖）、用户禁用、grant 范围，按正式 ID 检查 | 资格过滤 |
| 运行时开关 | [`runtime_settings.py`](../../../apps/backend/serving/config/runtime_settings.py) | 总开关、影子模式 |
| 日志列 | [`log_schema.py`](../../../apps/backend/serving/storage/log_schema.py)：`served_model_id`、`num_turns`、`last_user_msg_*` | 决策记录、训练特征、重发检测 |
| 先例 | [`/v1/messages`](../../../apps/backend/serving/servers/routers/anthropic_messages.py) 的 `_maybe_reroute_small_reasoning_call` | 以后并入辅助调用规则 |

### 6.3 需要先补的缺口

1. **资格检查写了六遍（已由 PR 1 合并）。** completions、`/v1/messages` 的 `_resolve`、`/v1/models` 的三个列表函数和 agent 模型目录各写了一遍角色、可见性、禁用（和 grant）检查。auto 需要在选择之前就拿到"这个调用方能用哪些模型"，所以先合并成 [`model_access_denial()`](../../../apps/backend/serving/model_access.py)。合并时发现 `/v1/messages`（含 `count_tokens`）用调用方写的名字做检查，而可见性覆盖、用户禁用列表和 grant 都按正式 ID 记录，所以 models.yaml 里的别名可以绕过前两者，grant 调用方用别名反而被拒绝。合并后统一从路由取正式 ID。
2. **RouteWise 的估计是私有方法。** 按 [9/14 设计 §8.2](2026-09-14-composable-hybrid-routing-design.zh.md) 增加只读估计接口（可行性、成本、TTFT、不确定性，不产生预留）。Fixed 模型没有学习型估计，回落到价格表和日志统计。
3. **日志中 `model_id` 的语义。** 以下几处都按 `model_id` 分组：`/health/model-activity`（[`postgres_log.py`](../../../apps/backend/serving/storage/postgres_log.py) 的 `get_model_activity`）、按 provider 的小时汇总（[`provider_stats_rollup.py`](../../../apps/backend/serving/admin/provider_stats_rollup.py)）、后台的热门模型（[`admin/analytics.py`](../../../apps/backend/serving/servers/routers/admin/analytics.py)）。如果 `model_id` 记虚拟 ID，这些面板会多出一个叫 `auto-balance` 的"模型"，实际跑的模型反而看不到。`/v1/messages` 现在也是在 `model_id` 记解析后的模型，别名和改投放在 metadata 里。所以 auto 沿用这个做法（不变量 3）。
4. **价格查找。** [`completions_cost.py`](../../../apps/backend/serving/servers/routers/completions_cost.py) 的回退查找按 `routing.model` 遍历 `router.routes`，而 `routing.model` 取自 `payload.model`（[`routing_info.py`](../../../apps/backend/serving/servers/routers/routing_info.py) 的 `build_initial_routing_info`），用虚拟 ID 查不到价格。所以改写必须发生在进入 Router 之前，`payload` 和 `model` 一起改。
5. **`/v1/messages` 没有 fallback。** 该接口在派发前就确定唯一的 adapter，失败即返回。跨模型 fallback 要等这个接口支持重试之后才能做。
6. **`chat_completions` 被直接调用。** Responses API（[`responses.py`](../../../apps/backend/serving/servers/routers/responses.py)）和 [`compat.py`](../../../apps/backend/serving/servers/routers/compat.py) 改写缓存的请求体后直接调用 `chat_completions(...)`，依赖逐个传入；`compat.py` 的 `/v1/completions` 已经没有传 `runtime_settings`，只是靠一个 `hasattr` 检查才没出错。给 `chat_completions` 新增 `Depends` 参数，这些路径拿到的会是 `Depends` 对象本身。所以选择器从 `request.app.state.services` 取，开关用 `get_runtime_settings_instance()` 读。
7. **`count_tokens` 也解析模型。** 它和 `/v1/messages` 共用 `_resolve`，Claude Code 会调用它，所以 auto ID 在这里也要能解析，否则返回 404。
8. **并发上限在 handler 之前读模型。** 每个用户的并发上限在进入 handler 之前读 body 里的模型，判断是否为豁免模型（[`concurrency.py`](../../../apps/backend/serving/servers/concurrency.py)）。auto ID 在路由表里查不到，所以按普通额度计算。这个方向是保守的，可以接受。
9. **grant 调用方。** 用 grant 认证的调用方请求 auto 时，候选按 grant 的模型列表过滤，过滤后为空就和未知模型一样返回 404。

### 6.4 对外接口

- **模型 ID：** `auto-intelligence`、`auto-balance`、`auto-fast`，`auto` 等同默认档位（名称待定）。第三方客户端（如 Claude Code）只能填 model 字符串，所以用模型 ID 表达档位；自家 console 可以另外支持参数形式（类似 Cursor 的 `optimize_for`）。
- **`/v1/models`：** 列出 `auto-*`。能力字段取候选的并集还是交集，待定。
- **响应：** `model` 字段返回实际模型（OpenRouter 的做法）；响应 header 标出档位和决策原因（字段名待定）。
- **错误：** 过滤后没有可选候选时，与"模型不存在"一样返回 404，不泄露候选信息；请求需要的能力（如图片）在档位内没有候选支持时，返回 400。
- **配置：** 档位定义放在部署的 `models.yaml`（第 7 节），总开关和影子模式放在 runtime settings。

### 6.5 日志、计费与观测

- `api_logs`：`model_id` = 实际模型（6.3 节第 3 条）。metadata 增加一个 `auto` 块，记录请求的虚拟 ID、档位、profile 版本、Scorer 版本、有序候选、原因、会话动作（new / keep / switch）以及是否为影子决策。不需要改表。
- 计费：按实际模型和实际用量（含推理 token、缓存命中）计算。
- 观测：各档位的模型分布、切换率、fallback 率、单请求与单会话成本、TTFT、隐式不满意率。

### 6.6 不采用的方案

| 方案 | 不采用的原因 |
|---|---|
| 把 vLLM-SR 等外部路由网关放在 gateway 前面 | 鉴权、grant、计费、日志和两个 API 接口都在本项目的 gateway 里，前置网关看不到这些信息，provider 配置也要维护两份 |
| 实现成 `RouterProtocol`，把每个模型的 Router 包成 `TreeBackend` | `/v1/messages` 不走 Router 的 fallback 链，覆盖不到 agent 流量；换模型会改变请求语义（参数、thinking block、计费、权限），属于 serving 层；9/14 设计第一版只支持两层组合。需要跨模型 fallback / hedging 时再评估 |
| 直接使用 RouteLLM 的预训练路由器 | 只支持两个候选，训练数据是 2024 年的 Arena 模型对，换成本项目的候选池基本要重训 |
| 级联（先用便宜模型，不满意再升级） | 与流式输出冲突，升级时延迟翻倍 |

## 7. 配置草案

以下仅为示意，字段名待定。模型名用占位符，实际取值属于各部署的 overlay。

```yaml
# distributions/<name>/config/models.yaml
auto:
  default_profile: balance
  session:
    idle_timeout_seconds: 1800
    switch_margin: 0.05
    tool_loop_lock: true
  scorer:
    type: task_table                  # static | task_table | knn
    table:                            # 按任务类型给每个候选打分（示例按模型列出）
      code:      {<frontier-a>: 0.95, <mid-b>: 0.85, <small-c>: 0.60}
      chat:      {<frontier-a>: 0.90, <mid-b>: 0.88, <small-c>: 0.80}
      auxiliary: {<frontier-a>: 0.90, <mid-b>: 0.90, <small-c>: 0.90}
  profiles:
    intelligence:
      objective: {type: max_quality}
      candidates:
        - {model: <frontier-a>, reasoning_effort: high}
        - {model: <mid-b>, reasoning_effort: high}
    balance:
      objective: {type: quality_band, epsilon: 0.03}
      candidates:
        - {model: <frontier-a>}
        - {model: <mid-b>}
        - {model: <small-c>}
    fast:
      objective: {type: min_latency, quality_floor: 0.6}
      candidates:
        - {model: <small-c>}
        - {model: <mid-b>, reasoning_effort: low}
```

加载时校验：

- 每个候选模型都存在，且不是另一个 auto 虚拟 ID。
- 参数覆盖在模型声明的取值范围内（如 `reasoning_efforts`；有的模型没有 medium）。
- 每个档位至少有一个候选。
- Scorer 表覆盖所有候选，缺失的条目会被报告，而不是默认为 0。

PR 2 实现时做了调整：v1 没有 `scorer` / `objective` 字段，档位按候选列出的顺序排序；模型未注册、或模型不接受候选的 effort，只在启动时告警，请求时跳过该候选，不禁用 auto。原因和实际字段见[实施计划](../plans/2026-10-13-auto-mode-implementation.zh.md) PR 2。

## 8. 分期与评估

### 8.1 分期

| 阶段 | 内容 | 退出条件 |
|---|---|---|
| P0 前置 | 抽出共享的资格检查（PR 1，已完成）；`auto-*` 虚拟模型与 `/v1/models`；`metadata.auto` 决策日志 | 点名具体模型的请求行为与之前一致（现有测试通过） |
| P1 静态 + 影子 | `StaticScorer`、约束、目标函数、会话策略；影子模式只记录决策 | 影子运行一段时间，得到档位分布、切换率、预估节省 |
| P2 生效 + 查表 | 任务类型分类 + `TaskTableScorer`；按用户或比例灰度生效 | 成本、延迟、隐式不满意率都不劣于对照组 |
| P3 学习 | 收集隐式反馈与抽样标注；`KnnScorer`；离线用 LLMRouterBench / LLMRouter 比较算法 | 离线优于查表，并通过线上 A/B |
| P4 可选 | 档位内 bandit；跨模型 fallback（需 `/v1/messages` 支持）；agent 按步策略 | 按需 |

按 PR 拆分的顺序见[实施计划](../plans/2026-10-13-auto-mode-implementation.zh.md)。

### 8.2 评估口径

- **离线：** 各档位相对 intelligence 档的质量（评审模型胜率）、实际成本和延迟；同义改写下的决策稳定性；ε / q_floor 的校准。
- **线上：** 单请求与单会话成本、TTFT、错误与 fallback 率、切换率、隐式不满意率（同一消息重发、纠正语气、中途断开、会话放弃）。
- **agent：** 有条件时测端到端任务完成率；单步选对模型不代表任务能完成。
- **成本口径：** 含推理 token 和缓存命中的实际花费，失败与重试也计入。

## 9. 质量信号来源

日志里有成本和延迟，没有质量，这是本项目相对 Cursor 和 OpenRouter 最大的短板。可用来源：

- **公开数据：** Chatbot Arena、P2L 等偏好数据。与本项目候选模型的重合度有限。
- **离线回放：** 抽样请求回放给各候选，由评审模型打分。涉及把用户请求发给其他模型，需要先确定隐私政策。
- **隐式反馈：** 同一条用户消息被重发（`last_user_msg_hash`）、下一轮是纠正语气、中途断开、会话被放弃。vLLM-SR 的 `user_feedback` / `reask` 信号是同类做法。
- **显式反馈：** console 中的点赞 / 点踩（如果提供）。

## 10. 待定问题

1. **主要服务对象**是 agent（`/v1/messages`）还是 console 聊天？这决定了会话策略和任务类型划分的优先级。
2. **fast 指低延迟还是低成本？** 本地 GPU 几乎没有边际成本但容量有限，云端按量付费，"成本"本身怎么定义？
3. **档位主要靠换模型，还是靠调同一模型的推理强度？**
4. **质量标签从哪来？** 尤其是是否允许把抽样的用户请求回放给其他模型做标注。
5. **响应 `model` 字段**返回实际模型还是虚拟 ID？建议返回实际模型，用 header 标出档位。
6. **任务类型怎么分？** 按领域（代码、写作……），还是按 agent 循环中的步骤角色（主推理、处理工具结果、辅助调用）？如果主要流量是编码 agent，按领域分几乎没有区分度。
7. **第一版 Scorer 用哪种？** 静态有序列表还是任务类型查表？建议查表，分数先人工填写，参考公开榜单。
8. **现有"小调用改投"怎么处理？** 是否并入 auto 的辅助调用规则？点名具体模型的请求是否也允许改投？建议 auto 只作用于虚拟 ID，改投规则保持现状，P2 之后再评估是否合并。
9. **命名和接口细节：** 用 `auto-fast` 还是 `auto-cost`；`auto` 默认哪个档位；`/v1/models` 中能力字段的语义。建议先用 `auto-intelligence` / `auto-balance` / `auto-fast`，`auto` 等同 balance，以后改名的成本很低。

## 11. 参考资料

- RouteLLM：[仓库](https://github.com/lm-sys/RouteLLM)、[LMSYS 博客](https://lmsys.org/blog/2024-07-01-routellm/)
- vLLM Semantic Router：[仓库](https://github.com/vllm-project/semantic-router)、[示例配置](https://github.com/vllm-project/semantic-router/blob/main/config/config.yaml)、[Signal Driven Decision（arXiv 2603.04444）](https://arxiv.org/abs/2603.04444)、[v0.3 Themis](https://vllm.ai/blog/2026-06-05-v0.3-vllm-sr-themis-release)、[When to Reason（arXiv 2510.08731）](https://arxiv.org/abs/2510.08731)
- OpenRouter：[Auto Router](https://openrouter.ai/docs/guides/routing/routers/auto-router)
- Cursor：[Cursor Router 博客](https://cursor.com/blog/router)、[Cursor Router 文档](https://cursor.com/docs/cursor-router)、[可用模型](https://cursor.com/help/models-and-usage/available-models)
- Azure：[Model router](https://learn.microsoft.com/en-us/azure/ai-services/openai/concepts/model-router)
- LiteLLM：[Auto Router v2](https://docs.litellm.ai/blog/autorouter-v2)、[路由 benchmark](https://docs.litellm.ai/docs/proxy/auto_routing_benchmark)
- 学习型方法：[RoRF](https://github.com/Not-Diamond/RoRF)、[IRT-Router（arXiv 2506.01048）](https://arxiv.org/abs/2506.01048)、[Prompt-to-Leaderboard（arXiv 2502.14855）](https://arxiv.org/abs/2502.14855)、[Route to Reason（arXiv 2505.19435）](https://arxiv.org/abs/2505.19435)
- 工具与基准：[LLMRouter](https://github.com/ulab-uiuc/LLMRouter)、[LLMRouterBench（arXiv 2601.07206）](https://arxiv.org/abs/2601.07206)、[LLMRouterBench 代码](https://github.com/ynulihao/LLMRouterBench)、[TwinRouterBench（arXiv 2605.18859）](https://arxiv.org/abs/2605.18859)
- 研究：[kNN 与学习型路由器（arXiv 2505.12601）](https://arxiv.org/abs/2505.12601)、[When is Routing Meaningful?（arXiv 2607.09197）](https://arxiv.org/abs/2607.09197)、[Routing 与 Cascading 综述（arXiv 2603.04445）](https://arxiv.org/abs/2603.04445)
