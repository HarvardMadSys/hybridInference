# Auto 模式实施计划

- 日期：2026-10-13
- 状态：PR 1 已合并（#1522）；PR 2 已实现，未接入请求路径；PR 3 之后尚未开始
- PR 2 分支：`murphy/claude/auto-mode-selection-core`
- 当前代码参照：`bbf1bbe8`（`dev`）
- 设计：[Auto 模式：按请求自动选择模型的设计](../specs/2026-10-13-auto-mode-model-selection-design.zh.md)

## 1. 交付边界

分两步走。第一步把"静态 auto"从头到尾做通，给内部用户真实使用：每个档位是一张有序候选表，首选不可用、没有权限或上下文放不下时用下一个，同一会话内不来回切换。第二步在真实流量上影子运行、积累数据，再升级 Scorer。

- PR 1 到 PR 3 不改变点名具体模型的请求（PR 1 修正的别名问题除外，见下文），auto 默认关闭。
- 只在两个 handler 接入：`chat_completions`（Responses API 和 `compat.py` 都委托给它）和 `anthropic_messages`（含 `count_tokens`）。admin playground 直接调用 Router，不接入。
- 设计 §6.3 列出的代码事实决定了接入方式，这里不再重复。

## 2. PR 顺序

### PR 1：合并资格检查（已合并，#1522）

- `serving/model_access.py` 新增 `model_access_denial()`：已发布、角色（含运行时可见性覆盖）、用户禁用、grant 范围，返回第一个不满足的原因。原来六处各自的实现改为调用它：completions、`/v1/messages` 的 `_resolve`、`/v1/models` 的列表函数、agent 模型目录。
- 每处的错误码、日志和是否检查 grant 范围都保持原样：未发布的路由在 completions 中仍不写请求日志；列表和 agent 目录仍不检查 grant 范围。`/v1/models` 原来的同步和异步两个列表函数除检查外完全相同，合并为一个。
- 唯一的行为变化：`/v1/messages` 和 `count_tokens` 原来用调用方写的名字做检查。可见性覆盖、用户禁用列表和 grant 都按正式 ID 记录（管理接口拒绝在别名上设置覆盖），所以 models.yaml 里的别名可以绕过前两者，grant 调用方用别名反而被拒绝。现在从路由取正式 ID，和 completions 一致。
- 测试：`tests/unit/serving/test_model_access.py` 覆盖原因、顺序和读取的 ID；`tests/servers/test_model_access_aliases.py` 在三个接口上覆盖别名。后者在修改前的代码上，Anthropic 接口的 6 个用例失败，chat completions 的通过。

### PR 2：选择核心（已实现，不接入请求路径）

`apps/backend/routing/model_selection/`，纯计算、无 I/O，没有请求路径 import 它：

- `types.py`：`Candidate`（模型 + 可选 effort）、`CandidateInfo`（serving 从路由表算出的能力）、`RequestFeatures`、`TurnKind`（新用户轮 / 工具循环 / 旁路调用）、`SessionState`、`Decision`。
- `constraints.py`：硬过滤，每个被去掉的候选带 `DropReason`：不在 catalog（未注册、无权限、无可用 endpoint）、effort 不被接受、请求带 tools 而模型不支持（adapter 会静默丢掉 tools）、模态、上下文。上下文检查把 prompt 估计放大 1.25 倍（`estimate_prefill_tokens` 按 4 字节一个 token 估，可能偏低），再预留 `min(max_tokens, 输出上限)`。
- `scoring.py`：`Scorer` / `Objective` 接口、`StaticScorer`（按候选在列表中的位置给分）、`MaxQuality`。
- `session.py`：会话规则 `continue_session()` 和进程内 `SessionStore`（有序字典加锁、按空闲超时过期、容量上限、时钟可注入，结构同 `SessionProviderPrefixMemory`）。
- `selector.py`：`select()`，过滤 → 打分 → 排序 → 会话规则，返回 `Decision`；会话状态由调用方按 `Decision.next_state` 写回。
- `config.py`：`auto:` 段的 schema 和两步校验。`parse_auto_config()` 发现的问题（未知字段、空档位、重复候选、`default_profile` 不存在、档位名不合法、候选是 auto ID）一次全部报出，调用方据此禁用 auto；`check_against_catalog()` 中档位 ID 与已有模型或别名重名也禁用 auto，候选模型未注册或不接受该 effort 只告警，请求时跳过。每个档位有 `version`（决定排序的配置的哈希），记进决策；版本不同的会话状态不沿用。

会话规则：工具循环中沿用当前模型，它不能服务时（熔断、失去权限、上下文涨破窗口）才强制换；新用户轮重新排序，首选恢复后在这里切回；旁路调用（v1 只有安全检查）用会话当前模型，不写会话状态；没有状态或版本不同时按新会话处理。

和最初计划相比的调整：

- 只实现按配置顺序排序。`quality_band` / `min_cost` / `min_latency` 移到 PR 4：分数固定时它们对每个档位只排出一个固定顺序；所需输入也还没有（只有 RouteWise 模型记录实时首 token 延迟，成本要先回答设计 §10 Q2）。
- `switch_margin` 和切换成本移到 PR 4：只有顺序时"好多少"没有意义。
- 不因 `max_tokens` 超过输出上限去掉候选：adapter 会把 `max_tokens` 截到上限（`serving/adapters/base.py`），Claude Code 发的 `max_tokens` 很大，按它过滤会去掉所有输出上限小的模型。截断后的值计入上下文预留。
- 启动时加载 `auto:` 段移到 PR 3，和运行时开关一起。

PR 4 新增的字段默认值都等于 v1 的行为，v1 的配置不用改。

测试：`tests/unit/routing/test_model_selection_*.py`，覆盖每个过滤原因、每条会话规则、存储的过期 / 淘汰 / key 隔离、两步校验，以及一个跨故障和上下文增长的完整会话。

### PR 3：接入两个接口（运行时开关默认关闭）

- 启动时读 `models.yaml` 的 `auto:` 段，`parse_auto_config()` 和 `check_against_catalog()` 报错时禁用 auto，告警写日志；admin 新建模型时保留 `auto` / `auto-*`。
- serving 侧新增一个模块，只做和协议格式有关的事：
  - 提取特征：prompt token 估计、模态、`max_tokens`、是否在工具循环里（Anthropic 格式看最后一条 user 消息是否只有 `tool_result`，OpenAI 格式看最后一条是否是 `role: tool`）、是否是安全检查（复用 `_is_tool_safety_check`）。
  - 构造 catalog：只放 `model_access_denial()` 放行、且 `router_exec.eligible_adapters()` 非空的模型（只查询）；上下文、模态等由 `select()` 检查。
  - 会话 key：调用方 + `session_identity()`；没有声明会话时用 `routing/prefill_load.py` 的 `conversation_fingerprint()`。会话 ID 是客户端自报的，key 必须带调用方。
  - 改写请求：换模型名（`payload` 一起改）；客户端没指定 effort 时填候选的 effort（`/v1/messages` 写 `output_config.effort`，completions 写 `reasoning_effort`）；切换模型时去掉历史 thinking block。
- 接入点：两个 handler 查找模型之前、`count_tokens`、`/v1/models`（至少有一个候选可用时才列出 auto）。
- 输出：`metadata.auto`；响应里的 `model` 是实际模型，header 标出档位。
- 测试照 `tests/servers/test_anthropic_messages_reroute.py` 的写法，覆盖：改写结果、按权限过滤、首选熔断时用下一个、工具循环中不切换、`count_tokens` 和 Responses 路径、开关关闭时 auto ID 返回 404、点名模型的请求和日志不变。
- 退出条件：在 staging 上用 Claude Code（`ANTHROPIC_MODEL=auto-balance`）跑一个完整会话，OpenAI SDK 和 Responses API 各跑一次，核对 `api_logs` 里的决策和成本。

### PR 4：影子运行和任务分类

- 对点名模型的真实请求抽样，记录"如果请求的是 auto-balance 会选哪个"（`metadata.auto_shadow`），估算节省和切换率。
- 任务类型先按请求在 agent 流程里的角色划分：新的用户轮、工具循环中的后续请求、辅助小调用、安全检查。
- `TaskTableScorer`，分数先人工填写；`quality_band` / `min_cost` / `min_latency` 目标函数；新用户轮的 `switch_margin` 和切换成本。
- 离线脚本从 `api_logs` 按"模型 × 任务类型"统计实际成本（含推理 token 和缓存）、TTFT 和隐式不满意信号。

### 之后

按用户哈希分组灰度上线并做 A/B；用 RouteWise 的 prefix cache 估计切换成本；按 9/14 设计 §8.2 增加只读估计接口；有了质量标签再试 kNN。

## 3. 开工前的默认值

PR 1 到 PR 3 只依赖设计 §10 的两个待定问题，先按默认值做：

- 命名：`auto-intelligence` / `auto-balance` / `auto-fast`，`auto` 等同 balance。
- 响应里的 `model` 返回实际模型。

"fast 指低延迟还是低成本"和"档位靠换模型还是调 effort"都由配置表达，不阻塞代码。小调用改投保持现状，auto 只处理 `auto-*`。质量标签来源、隐私和任务类型划分在 PR 4 之前确定。
