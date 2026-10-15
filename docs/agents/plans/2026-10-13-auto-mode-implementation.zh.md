# Auto 模式实施计划

- 日期：2026-10-13
- 状态：PR 1 已合并（#1522）；PR 2 已实现，未接入请求路径（#1523）；PR 3 已实现，在本地网关进程上验证，staging 尚未验证；PR 4 尚未开始
- PR 2 分支：`murphy/claude/auto-mode-selection-core`
- PR 3 分支：`murphy/claude/auto-mode-wiring`，基于 PR 2 分支
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

### PR 3：接入两个接口（已实现，运行时开关默认关闭）

- 配置：启动时在路由表完整之后（YAML 模型和从数据库恢复的运行时模型都已注册）读 `models.yaml` 的 `auto:` 段，`load_auto_mode()` 报错或档位与已注册名称重名时 auto 关闭，每个问题写一条日志；候选未注册或不接受 effort 只告警。结果放在 `AppServices.auto_mode`。
- 开关：运行时设置 `auto_mode_enabled`，默认关闭。只有名字落在 `auto` / `auto-*` 且有对应档位的请求才读这个设置，点名模型的请求不读。开关存在 `site_settings`，所以没有数据库的网关打不开 auto，和 `reasoning_small_call_reroute_enabled` 一样。
- serving 侧新模块 `serving/auto_mode.py`：
  - catalog：每个候选模型要通过 `model_access_denial()`（含 grant 范围），再用 `eligible_adapters()` 查是否有 endpoint 准入（只查询）。模型的能力取各路由的最小窗口、最小输出上限、共同的模态、全部支持才算支持 tools、effort 取交集，与 `/v1/models` 对多路由模型的保守口径一致。
  - 特征：OpenAI 格式最后一条是 `role: tool` 时算工具循环；Anthropic 格式最后一条 user 消息里只要有 `tool_result` 就算工具循环。Claude Code 会在同一条消息里附加提醒文本，用户打断工具时也是 `tool_result` 加文字，宁可把新一轮当成工具循环，这样最多把切换推迟一轮，不会在循环中间换模型。图片在 `tool_result` 里也算。安全检查（`_is_tool_safety_check`）算旁路调用。
  - 会话 key：调用方（`user_id` 和 grant）+ 声明的会话 + 对话开头的指纹（`conversation_fingerprint()`，只用消息，不含 tools）。和原计划的差别是声明了会话时也带上开头指纹：Claude Code 的子 agent 和标题、安全检查等辅助调用与主对话共用一个会话 ID，只按会话 ID 记状态，子 agent 开始时会把主对话在工具循环中途换掉。不带 tools 是因为 Claude Code 中途加载工具会改变 tools 列表。
  - 改写请求：chat 同时改 `payload.model` 和 handler 的 `model`；`/v1/messages` 改 `body["model"]` 后照常走 `_resolve`。候选的 effort 只在客户端没指定时写入（`reasoning_effort` / `output_config.effort`），由各自原有的检查处理。日志里的 `request_payload` 保留客户端原样发来的请求。
  - thinking：OpenAI 翻译器本来就丢掉 thinking block，所以只在分派给 Anthropic 原生 adapter、且会话换过模型时，去掉历史里的 thinking block；会话在工具循环中沿用当前模型时，保留最后一条 assistant 消息的 thinking。
- 错误：调用方一个候选都用不了时 404（和未知模型一样，不暴露候选）；有候选能接这个请求但没有 endpoint 准入时 503；调用方能用的候选都接不了这个请求（上下文、模态、tools）时 400，消息里不出现候选名。
- 输出：日志的 `model_id` 是实际模型，成本按实际模型算；`metadata.auto` 记档位、版本、有序候选、被跳过的候选及原因、会话动作和来源、轮次类型、prompt 估计、是否写入了 effort。
- 不向用户透露实际模型（2026-10-15 决定，改掉了原计划“响应返回实际模型”的默认值）：
  - 响应：chat 非流式和流式的每个 chunk（含首个 role chunk 和 force-streaming 拼出的响应）、`/v1/messages` 非流式和流式的 `message_start`（`AnthropicStreamModelRenamer` 只改这一个事件，其余字节原样转发）、Responses API 都返回请求的虚拟 ID；去掉上游 chunk 里的 `provider` 字段（聚合商会写）。不加 `X-Auto-*` header。
  - 错误消息：选完模型后把实际模型的名字（id、别名、显示名、上游 model id；之后小调用改投换了模型的话，新模型的名字也加进去）放进请求上下文 `CONCEALED_MODEL`（每个请求清空），`safe_detail_text`、`scrub_error_for_user` 和 `_anthropic_error` 输出前按整词替换成虚拟 ID。`/v1/messages` 本来就把给用户的错误文本写进 `api_logs.error`，所以那里记的也是替换后的文本；运维看的原文在 `metadata.operator_error`（`operator_safe_error` 不替换），实际模型在 `model_id` 和 `metadata.auto`。
  - 用户能看到的日志：控制台 `/user/recent-requests` 对 auto 请求显示 `metadata.auto.requested`，provider 显示 `auto`，不返回 routewise 细节，错误换成按状态码的通用文本，按模型过滤也按显示的名字；grant 请求账本的 `model` 和 `served_model_id` 都给虚拟 ID。管理端的视图不变。
  - 用户仍可能从输出风格、延迟等推测模型，这一层不处理。
- `/v1/models`（OpenAI 和 Anthropic 两种格式）在开关打开、且调用方能用档位内至少一个模型时列出 auto（不检查 grant 范围，与其他列表一致）。窗口、模态、tools 取候选中最大的，因为选择会把请求交给有这项能力的模型；采样参数和结构化输出取交集。控制台自己的 `/user/models` 不列 auto：那个列表也用于禁用模型的设置，而禁用列表按真实模型 ID 记录。
- `count_tokens`：只检查调用方能否用档位内某个模型，计数仍在本地估计。
- admin：新建模型时拒绝 `auto` / `auto-*`（422）。检查放在 admin 请求入口，不放在启动时恢复运行时模型的共享函数里，以免已有的同名运行时模型在重启时丢失。
- 文档：`docs/developer/routing.md` 新增 “Auto models” 一节，含 zh_CN 翻译。
- 测试：`tests/unit/serving/test_auto_mode.py`（特征、会话 key、改写、thinking、加载）；`tests/servers/test_auto_mode_surfaces.py`（三个接口、开关、权限和 grant、首选不可用、上下文、模态、tools、会话规则、子 agent、按调用方隔离、Responses、`/v1/models`、Anthropic 原生 thinking、日志）；`tests/servers/test_admin_provider_routes.py`（保留名）。
- 验证：在本地起了真实网关进程（临时 Postgres、假的 OpenAI 兼容上游、三个模型），开关从环境变量导入。核对了：三种档位的选择和 effort；上下文、图片、tools 导致的跳过；400 / 404；流式和 Responses；`count_tokens`；`/v1/models` 两种格式；一个 Claude Code 格式的会话经历首选上游连续 500、熔断打开后强制切换、恢复后工具循环中不切回、下一轮切回、子 agent 按新会话、安全检查按旁路调用；`api_logs` 的 `model_id`、成本和 `metadata.auto`；运行中改 `site_settings` 关闭和重新打开开关（10 秒内生效，不用重启）。改为隐藏实际模型之后又在本地网关上查了一遍：chat 非流式和流式、`/v1/completions`、`/v1/messages` 非流式和流式、Responses API 非流式和流式、`count_tokens`、`/v1/models`，以及上游返回带模型名的 400 时四种接口的报错，响应正文和 header 里都没有出现任何候选模型名或上游 provider 字段；点名模型的请求不变；`api_logs` 的 `model_id`、`metadata.auto` 和 `metadata.operator_error` 仍是实际模型。`/user/recent-requests` 和 grant 账本的 SQL 在真实 Postgres 上跑过。
- 已知限制（按设计）：没有跨模型 fallback。会话的模型在熔断打开之前失败，客户端收到的是这个错误，熔断打开后的下一个请求才换模型（设计 §6.3 第 5 条）。
- 退出条件尚未满足：在 staging 上用 Claude Code（`ANTHROPIC_MODEL=auto-balance`）跑一个完整会话，OpenAI SDK 和 Responses API 各跑一次，核对 `api_logs` 里的决策和成本。

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
- 响应里的 `model` 返回虚拟 ID，不向用户透露实际模型（2026-10-15 决定；最初的默认值是返回实际模型）。

"fast 指低延迟还是低成本"和"档位靠换模型还是调 effort"都由配置表达，不阻塞代码。小调用改投保持现状，auto 只处理 `auto-*`。质量标签来源、隐私和任务类型划分在 PR 4 之前确定。
