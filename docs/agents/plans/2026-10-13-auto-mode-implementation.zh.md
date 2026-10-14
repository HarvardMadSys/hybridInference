# Auto 模式实施计划

- 日期：2026-10-13
- 状态：PR 1 与设计文档同一个 PR 提交；PR 2 之后尚未开始
- PR 1 分支：`murphy/claude/auto-mode-model-access`
- 当前代码参照：`70751d3c`（`dev`）
- 设计：[Auto 模式：按请求自动选择模型的设计](../specs/2026-10-13-auto-mode-model-selection-design.zh.md)

## 1. 交付边界

分两步走。第一步把"静态 auto"从头到尾做通，给内部用户真实使用：每个档位是一张有序候选表，首选不可用、没有权限或上下文放不下时用下一个，同一会话内不来回切换。第二步在真实流量上影子运行、积累数据，再升级 Scorer。

- PR 1 到 PR 3 不改变点名具体模型的请求（PR 1 修正的别名问题除外，见下文），auto 默认关闭。
- 只在两个 handler 接入：`chat_completions`（Responses API 和 `compat.py` 都委托给它）和 `anthropic_messages`（含 `count_tokens`）。admin playground 直接调用 Router，不接入。
- 设计 §6.3 列出的代码事实决定了接入方式，这里不再重复。

## 2. PR 顺序

### PR 1：合并资格检查（已完成）

- `serving/model_access.py` 新增 `model_access_denial()`：已发布、角色（含运行时可见性覆盖）、用户禁用、grant 范围，返回第一个不满足的原因。原来六处各自的实现改为调用它：completions、`/v1/messages` 的 `_resolve`、`/v1/models` 的列表函数、agent 模型目录。
- 每处的错误码、日志和是否检查 grant 范围都保持原样：未发布的路由在 completions 中仍不写请求日志；列表和 agent 目录仍不检查 grant 范围。`/v1/models` 原来的同步和异步两个列表函数除检查外完全相同，合并为一个。
- 唯一的行为变化：`/v1/messages` 和 `count_tokens` 原来用调用方写的名字做检查。可见性覆盖、用户禁用列表和 grant 都按正式 ID 记录（管理接口拒绝在别名上设置覆盖），所以 models.yaml 里的别名可以绕过前两者，grant 调用方用别名反而被拒绝。现在从路由取正式 ID，和 completions 一致。
- 测试：`tests/unit/serving/test_model_access.py` 覆盖原因、顺序和读取的 ID；`tests/servers/test_model_access_aliases.py` 在三个接口上覆盖别名。后者在修改前的代码上，Anthropic 接口的 6 个用例失败，chat completions 的通过。

### PR 2：选择核心（不接入请求路径）

- `apps/backend/routing/model_selection/`，纯计算、无 I/O：类型；目标函数 `max_quality` / `quality_band` / `min_cost` / `min_latency`；只实现 `StaticScorer`。
- 会话状态存在进程内、有容量上限、带过期时间的 LRU 缓存。规则：工具循环中锁定；新的用户轮只有好出 `switch_margin` 才换；空闲超时后重置；当前模型不可用时才强制换。
- 配置放在 `models.yaml` 新增的顶层 `auto:` 段，现有解析只读 `models:`，不受影响。启动时校验：候选存在、不嵌套 auto、effort 在模型声明的 `reasoning_efforts` 之内。校验失败只禁用 auto 并报错，不影响其他模型启动。
- 退出条件：每个目标函数和每条会话规则都有单测。

### PR 3：接入两个接口（运行时开关默认关闭）

- serving 侧新增一个模块，只做和协议格式有关的事：
  - 提取特征：prompt token 估计、模态、`max_tokens`、是否在工具循环里（Anthropic 格式看最后一条 user 消息是否只有 `tool_result`，OpenAI 格式看最后一条是否是 `role: tool`）、是否是安全检查（复用 `_is_tool_safety_check`）。
  - 过滤候选：`model_access_denial()`，再用 `router_exec.eligible_adapters()` 去掉熔断或被禁用的模型（只查询），最后检查上下文长度和模态。
  - 会话 key：调用方 + `session_identity()`；没有声明会话时用 `routing/prefill_load.py` 的 `conversation_fingerprint()`。
  - 改写请求：换模型名（`payload` 一起改）；客户端没指定 effort 时填候选的 effort（`/v1/messages` 写 `output_config.effort`，completions 写 `reasoning_effort`）；切换模型时去掉历史 thinking block。
- 接入点：两个 handler 查找模型之前、`count_tokens`、`/v1/models`（至少有一个候选可用时才列出 auto）。
- 输出：`metadata.auto`；响应里的 `model` 是实际模型，header 标出档位。
- 测试照 `tests/servers/test_anthropic_messages_reroute.py` 的写法，覆盖：改写结果、按权限过滤、首选熔断时用下一个、工具循环中不切换、`count_tokens` 和 Responses 路径、开关关闭时 auto ID 返回 404、点名模型的请求和日志不变。
- 退出条件：在 staging 上用 Claude Code（`ANTHROPIC_MODEL=auto-balance`）跑一个完整会话，OpenAI SDK 和 Responses API 各跑一次，核对 `api_logs` 里的决策和成本。

### PR 4：影子运行和任务分类

- 对点名模型的真实请求抽样，记录"如果请求的是 auto-balance 会选哪个"（`metadata.auto_shadow`），估算节省和切换率。
- 任务类型先按请求在 agent 流程里的角色划分：新的用户轮、工具循环中的后续请求、辅助小调用、安全检查。
- `TaskTableScorer`，分数先人工填写。
- 离线脚本从 `api_logs` 按"模型 × 任务类型"统计实际成本（含推理 token 和缓存）、TTFT 和隐式不满意信号。

### 之后

按用户哈希分组灰度上线并做 A/B；用 RouteWise 的 prefix cache 估计切换成本；按 9/14 设计 §8.2 增加只读估计接口；有了质量标签再试 kNN。

## 3. 开工前的默认值

PR 1 到 PR 3 只依赖设计 §10 的两个待定问题，先按默认值做：

- 命名：`auto-intelligence` / `auto-balance` / `auto-fast`，`auto` 等同 balance。
- 响应里的 `model` 返回实际模型。

"fast 指低延迟还是低成本"和"档位靠换模型还是调 effort"都由配置表达，不阻塞代码。小调用改投保持现状，auto 只处理 `auto-*`。质量标签来源、隐私和任务类型划分在 PR 4 之前确定。
