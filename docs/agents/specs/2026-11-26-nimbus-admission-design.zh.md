# 接入 Nimbus：把本地路由交给引擎旁的准入进程

- 日期：2026-11-26
- 状态：草案，待评审。第 1 阶段（认出退回）、第 2 阶段（路由策略和流式 chat）和第 3 阶段（非流式 chat 和 `/v1/messages`）已实现，没有模型选用新策略、也没有路由带标记时不改变任何运行行为；第 4 阶段（联调）尚未实施。
- 前序设计：[Greedy Router 设计](2026-11-23-greedy-router-design.zh.md)。那份设计的 3.4 节说 Nimbus 的接入是另一份设计，就是本文。
- 顺序：fixed → greedy → Nimbus。`FixedRouter` 和 `greedy` 都保留，本文只新增一种按模型选用的路由策略。
- 代码参照：hybridInference `2027-osdi-nimbus` @ `3921ac7d`；Nimbus `murphy/llm-nimbus-full` @ `f95fd30`（准入模式在这个分支上，还没有合并）。
- PR 目标分支：`2027-osdi-nimbus`。
- 名字 2026-11-26 定了：`router: nimbus`、`EngineDeclined`、路由标记 `admission: "nimbus"`、header `X-Nimbus-Elapsed-Ms` 和 `X-Nimbus-Request-Id`、`route_offload` 的 `reason` 值 `unsupported_local`。

## 1. 结论

给同时有自建引擎和云端路由的模型，新增一个路由策略 `router: nimbus`。去本地还是去云端，不由网关决定，由引擎旁边的 Nimbus 进程决定：

- 每台引擎旁边跑一个 `nimbus --admission-only` 进程。网关把这个模型的本地路由指向它，不再直接指向引擎。
- 网关把每个请求先发给 Nimbus，并告诉它两个数：这个请求走云端预计多久出首 token，以及请求在网关里已经花掉多久。
- Nimbus 接下请求就正常流式返回。接不了就返回 409（下文叫“退回”），网关随即把请求交给云端。
- 云端也失败时，直接把错误报给客户端，不再回本地重试。

网关这边要做四件事：认出退回并把它当成“没发出去”，新增这个路由策略，让非流式请求和 `/v1/messages` 也走得通，以及保证这台引擎只有 Nimbus 一个发送者。Nimbus 那边要补两个 header、不再丢掉 `response_format`，并把退回的格式定稿。

## 2. 背景

### 2.1 为什么是这种接法

Nimbus 按引擎的 decode step 做决定，需要精确的 token 数、引擎的缓存事件，并且要和引擎在同一台机器上。这些网关的限流器给不了（前序设计 3.4 节）。所以 Nimbus 由引擎旁边的独立进程承载，自己扣住请求，网关只负责把请求送过去、把被退回的请求送上云。

### 2.2 Nimbus 的准入模式今天是什么样

以下都对照 `nimbus/server.py` 核实过：

- **接口。** `POST /v1/chat/completions` 和 `POST /v1/completions`，另有 `GET /health`。没有 `/v1/messages`。
- **只接流式请求。** 非流式请求返回 400。
- **读一个 header。** `X-Nimbus-Remote-TTFT-Ms`：调用方的云端路由预计多少毫秒出首 token。给了它，cutoff 就按它算；不给就按 profile 里的 provider 延迟线算。不是数字时返回 400。
- **退回。** 策略把请求判给远端时，返回 409，body 是 OpenAI 风格的错误，外加一个 `nimbus` 字段：

  ```json
  {
    "error": { "message": "...", "type": "nimbus_declined", "code": "declined_at_cutoff" },
    "nimbus": { "request_id": "r17", "decision": "declined_at_cutoff", "ttft_left_ms": 812.4 }
  }
  ```

- **退回可能不是立刻来的。** 到达时就判给远端的请求立刻退回；先在 Nimbus 里排队的请求，可能等到它自己的 cutoff 才退回。cutoff 的含义是：再晚交给云端，云端就来不及在首 token 预算内出首 token。
- **期限从 Nimbus 收到请求算起。** `deadline = 收到时刻 + 首 token 预算`，`cutoff = deadline − 云端首 token 时间 − margin`，本地最晚开始时刻 `latest_local = deadline − 预计 prefill 时间`。
- **客户端断开。** Nimbus 靠连接断开来取消一个还在等的请求。
- **其他会被 400 拒绝的请求。** 非文本内容；强制调用某个 tool 的 `tool_choice`；`parallel_tool_calls` 为 false；旧的 `functions` 和 `function_call` 字段；给它不认识 tool 格式的模型带 tools（它只认 Qwen 的 XML 格式和 GPT-OSS 的 channel 格式）；以及填满模型上下文的 prompt，或超出剩余空间的输出上限。

状态码、header 名和 body 的格式在 Nimbus 那边还标着“暂定”，本文评审通过后一起定稿（第 9 节）。

### 2.3 网关今天已有的东西

以下都对照 `2027-osdi-nimbus` @ `3921ac7d` 核实过：

- **409 不进熔断。** `endpoint_health.is_client_error_status` 只把 408、429 和鉴权状态码排除在客户端错误之外，409 算客户端错误，不记到端点头上。
- **409 不算“请求被拒”。** `routers._REQUEST_REFUSAL_STATUSES` 只有 400 和 422，409 不计入“两条路由拒绝就停”的上限。
- **“没发出去”这种结果已经有了。** `pool_attempts` 的 `left_unsent` 谓词和 `AttemptLedger.unsent`：一次尝试什么都没发给引擎时，它既不是失败记录，也不算拒绝。今天只有 `UpstreamQueueWaitExpired` 走这条路。
- **云端首 token 时间的配置已经有了。** `greedy` 的 `cutoff.cloud_ttft`（`fixed_ms`、`per_input_token_ms`）。
- **非流式 chat 可以转流式再缓冲。** `completions._buffers_for_an_engine_queue`，今天只对登记了引擎队列的模型生效。
- **`/v1/messages` 能发给 OpenAI 兼容的路由。** `BaseAdapter.messages` 和 `stream_messages` 用 `anthropic_request_to_openai` 把请求转成 chat completions。
- **`/v1/messages` 有第二遍。** `anthropic_messages._SpillCarry`：请求离开引擎队列后，原样在云端路由上再跑一遍。今天它只对 `GreedyRouter` 的模型、只在 `UpstreamQueueWaitExpired` 时触发。

## 3. 设计决定

1. **Nimbus 决定，网关执行。** 网关不为这台引擎登记引擎队列，也不自己算期限。首 token 预算只放在 Nimbus 的 profile 里，网关的配置里不重复。
2. **退回是“没发出去”，不是失败。** 它不是尝试记录，不进熔断，不算拒绝，客户端看不到这个 409。
3. **每个请求最多两步：本地，然后云端。** 云端失败就把云端的错误报给客户端。（2026-11-26 定。`greedy` 的第三步“回本地再试”这里没有：Nimbus 退回的意思就是本地接不了。）
4. **网关已花掉的时间用单独的 header 带过去。** `X-Nimbus-Elapsed-Ms`，理由见 5.2 节。
5. **`/v1/messages` 走适配器现成的转换**，第二遍沿用 `_SpillCarry`。（2026-11-26 定。）
6. **这台引擎只有一个发送者。** 一个引擎前面要么是网关的引擎队列，要么是 Nimbus，两者不叠加（前序设计 3.4 节）。

### 3.1 不做

- 不把 Nimbus 的 library 嵌进网关进程。
- 不改 `FixedRouter` 和 `greedy` 的行为。
- 不在网关里重算 Nimbus 的决定，也不在网关里维护引擎状态。
- 不给 Nimbus 加 `/v1/messages`。

## 4. 一个请求的流程

1. 网关收到请求，鉴权、选模型，记下收到的时刻。
2. 策略在模型的本地路由里选一条（5.5 节），发给它指向的 Nimbus。请求带两个 header（5.2 节）。
3. Nimbus 的三种回答：
   - **流式返回。** 请求留在本地，网关照常转发。
   - **409 加 `nimbus` 字段。** 退回。网关记一条 `route_offload`，把请求交给模型的云端路由。
   - **其他错误或连不上。** 按今天的规则处理：输出开始之前失败的，去云端一次，作为普通的 fallback，并记到这个端点头上。
4. 云端路由的执行、记账和错误，都和今天一样。云端失败就是这个请求的结果。

客户端在第 3 步等待期间看到的东西，和今天在引擎队列里等待时一样：流式请求在第一帧之前什么都收不到，缓冲的非流式请求收到 keepalive。

## 5. 网关的改动

### 5.1 认出退回

- 在 OpenAI 兼容的适配器里，上游返回 409 并且 body 里有 `nimbus` 对象时，抛一个专门的异常 `EngineDeclined`，带上 `decision`、`ttft_left_ms` 和 Nimbus 的 `request_id`。
- 只有标明指向 Nimbus 的路由才做这个判断（5.4 节），别的上游返回的 409 照旧处理。
- `EngineDeclined` 是 `UpstreamSaturated` 的子类，和 `UpstreamQueueWaitExpired` 并列，同样不带 `status`、`status_code`、`code` 属性。含义也并列：那一个是请求在网关的引擎队列里等到期限、没发给引擎；这一个是请求在 Nimbus 的队列里被判给远端、没发给引擎。继承之后现成得到的：
  - 不进熔断（`endpoint_health` 对 `UpstreamSaturated` 的豁免）。
  - 记录里标成没有可归咎端点的失败（`telemetry.is_uncharged_failure`）。
  - 它在拿到上游回复之后才抛出，不经过适配器里“换一个 key 再等”的那段（那段只包着取名额的调用）；要有测试确认退回不会触发换 key。
- 这个异常接进 `pool_attempts` 的 `left_unsent`。这样它自动满足第 3 节的决定 2。
- **退回成为最终结果时。** 云端这一步没有跑（云端接不住），或者跑了却没有留下失败记录时，`AttemptLedger.raise_after` 会把 `unsent` 里的 `EngineDeclined` 抛给入口。这时客户端应当得到 503（本地接不了，云端也没有可用的路由）。继承 `UpstreamSaturated` 得不到这个状态码，各入口对它的回答并不统一：chat 入口对不带状态码的异常默认答 500，`/v1/messages` 入口把它答成 429。所以由策略自己收尾：账本里只剩退回时，抛出和 `greedy` 的 `_nothing_in_range` 同一种错误（`AllCircuitsOpenError`，chat 入口已经把它答成 503）；`/v1/messages` 入口照 `greedy` 的做法直接答 503。两个入口各有测试。
- 要专门确认的一点：云端也失败时，`_select_surfaced_error` 不能把这个 409 挑出来报给客户端。它在 `unsent` 里而不在失败记录里，按代码应当不会被挑中；要有测试守着。

### 5.2 两个 header

**`X-Nimbus-Remote-TTFT-Ms`**：这个请求走云端预计多久出首 token。

- 取值：`cloud_ttft.fixed_ms + cloud_ttft.per_input_token_ms × n`，`n` 是网关估算的 prompt token 数，和上下文窗口检查用的是同一个估算。
- 它只表示云端的首 token 时间，不掺别的量。
- 云端此刻接不住这个请求时（没有可用的 key、熔断都开着、模态不支持、上下文窗口放不下、超过 `cloud_max_input_tokens`；判断和 `greedy` 的 `_cloud_takes` 是同一个查询），传 `inf`。Nimbus 对它不设 cutoff，请求就不会因为“云端更快”被退回。这和 `greedy` 的规则“只有云端接得住才设期限”是同一个意思。
- 传了 `inf` 之后，Nimbus 仍会在三种情况下退回（对照 `llm_nimbus/router.py` 的 `_admit` 和 `policies.py` 核实过）：
  - `declined_over_context`：prompt 加输出上限超过引擎的上下文。
  - `declined_at_arrival`：即使引擎空着、单独为它 prefill，也赶不上它的首 token 预算。
  - `declined_at_latest_start`：等到本地最晚开始时刻仍然排不上，并且策略不能证明留下它是安全的。
  这三种都是“本地确实接不了”。云端又接不住时，请求以 5.1 节的 503 结束。
- 没有策略描述的派发也传 `inf`：调用方把请求 pin 到了这条路由（这类请求绕过按模型的策略，由共享的 `FixedRouter` 直接执行），或者调用方自己定了端点。这时退回之后没有人接手，Nimbus 按自己 profile 里的 provider 线算 cutoff 只会白白退回。这样的请求被退回时同样以 503 结束：策略里遇到的由策略转成 503，绕过策略的由入口转（第 3 阶段）。

**`X-Nimbus-Elapsed-Ms`**：请求从网关收到，到网关把它发给 Nimbus，已经过去多少毫秒。Nimbus 把自己的“收到时刻”往前拨这么多。

为什么不把已花掉的时间并进第一个 header：

| | 并进 `Remote-TTFT`（传 `remote + elapsed`） | 单独的 `Elapsed` header |
|---|---|---|
| cutoff | 正确：`deadline − (remote + elapsed) − margin` | 正确 |
| 首 token 的 deadline | 晚了 `elapsed` | 正确 |
| 本地最晚开始时刻 `latest_local` | 晚了 `elapsed` | 正确 |
| 策略对本地请求的准入检查 | 都按晚了 `elapsed` 的 deadline 算 | 正确 |
| 退回里的 `ttft_left_ms`、Nimbus 记录的 TTFT 是否达标 | 都偏了 `elapsed` | 正确 |
| Nimbus 的改动 | 无 | `server.py` 里约十行，不给 header 时行为不变 |

并进去只对 cutoff 这一项等价。Nimbus 的三个量都从同一个 deadline 推出来，deadline 晚了，留在本地的请求就可能恰好晚 `elapsed` 出首 token，而这正是策略本来要保证的那一项。`elapsed` 有多大也还没有量过：它包含读请求体、估算 token 和选路，长 prompt 的 coding 请求未必只有几毫秒。单独的 header 传的是一段时长，不是时刻，所以两台机器的时钟不需要对齐。

**`X-Session-Id`**：网关认定的会话 id，`/v1/messages` 从 `metadata` 里取出的那个也算。`openai_compat` 今天不向上游带任何会话 header（`_build_headers` 只有鉴权和静态的 `extra_headers`），所以 Nimbus 记录里的 `session_id` 会是空的。Nimbus 的决定不用它，但它的记录和之后按会话做的分析要用。只对带标记的路由带这个 header。要顺带确认的一点：Nimbus 在没有这个 header 时会退而取 body 里的 `session_id` 或 `user`，网关如果把 `user` 字段原样转发，它会被当成会话 id。

**`X-Nimbus-Request-Id`**：网关的 request id。Nimbus 把它记进自己的记录，两边就能逐条对上；Nimbus 内部仍用自己的 id（第 7 节）。

网关到 Nimbus 的网络时间两种做法都没有算进去。同机或同机房时可以忽略；跨机房部署时要另行处理。

### 5.3 路由策略

- 和 `greedy` 一样，是入口这一层的策略：两个池包着共享的 `FixedRouter`，一个限定在模型的本地路由，一个限定在云端路由。池内怎么选路、执行、记账，仍然是 `FixedRouter` 的。
- 每一步经过 `routing.pool_attempts`，`left_unsent` 认 `EngineDeclined`。
- 它不登记引擎队列，不给派发设期限，不读 `engine_queue_limit`。
- 和 `greedy` 重复的部分（两个池的构造、云端这一步、`route_offload` 的记录）在实现时抽成共用的，不复制。

### 5.4 配置

```json
{
  "id": "example-model",
  "router": "nimbus",
  "router_params": {
    "cloud_ttft": { "fixed_ms": 1000, "per_input_token_ms": 0.1 },
    "cloud_max_input_tokens": null
  }
}
```

| 参数 | 含义 | 取值 |
|---|---|---|
| `cloud_ttft` | 云端首 token 时间的直线，和 `greedy` 的 `cutoff.cloud_ttft` 同一个含义 | `fixed_ms`、`per_input_token_ms`，都不小于 0 |
| `cloud_max_input_tokens` | 估算的 prompt 超过这个长度的请求不上云，和 `greedy` 的同名参数同一个含义。这样的请求带 `inf` 发给 Nimbus（5.2 节），被退回就以 503 结束 | 整数，不小于 1；`null` 表示不限，默认 `null` |

- 首 token 预算和 margin 不在这里：它们在 Nimbus 的 profile 里（`slo` 和 `provider.margin_ms`）。
- 模型的本地路由把 `base_url` 指向 Nimbus 进程，并带一个标记 `admission: "nimbus"`，5.1 节的判断只对带这个标记的路由生效。
- 启动时检查，不满足就拒绝加载这个模型：
  - 选了这个策略的模型，至少有一条带标记的本地路由和一条云端路由。
  - 带标记的路由指向的地址，没有登记引擎队列，也没有不带标记的路由指向同一个地址。
  - 带标记的路由只声明文本输入。
  - 带标记的路由按网关的分类是自建的（`deployment_type: self_hosted`，或主机名被认作本地）。否则它的请求会先在网关的出站限流里排队，Nimbus 看到的到达时间就错了。
  - 模型的其余路由都是云端路由。一条不带标记的自建路由前面既没有 Nimbus 也没有网关的引擎队列，这个策略不给它发请求，加载时直接拒绝。

**部署约束，网关查不了：** 网关的配置里只有 Nimbus 的地址，看不到它背后引擎的地址，所以“没有路由绕过 Nimbus 直连引擎”没法自动检查。它写进部署文档；能做到时让引擎只监听本机地址，从网络上堵住。

### 5.5 多个本地引擎

- 一台引擎一个 Nimbus 进程，一条本地路由。
- 一个请求只发给一个 Nimbus。被它退回之后不再试另一个：退回可能发生在 cutoff，这时剩下的时间只够云端。
- 发给哪一个，沿用 `FixedRouter` 现有的选择（权重、会话亲和）。实现上是本地这一步关掉池内的 fallback（`allow_fallback=False`）：池选出一条路由、试一次，退回或失败都直接交给云端这一步。会话亲和在这里更重要：同一个会话的后续请求回到同一台引擎，才能用上它的 KV 缓存。
- 网关看不到哪个 Nimbus 更空。先按现有规则选；要不要让 Nimbus 暴露负载供网关选路，是第 9 节的问题。

### 5.6 非流式请求

- Nimbus 只接流式请求。发给带标记路由的非流式 chat 请求，在网关里转成流式再缓冲，沿用 `completions` 里引擎队列那条路径；把它的条件从“登记了引擎队列”扩成“登记了引擎队列，或路由带标记”。
- 带 `response_format` 的请求不发给 Nimbus，见 5.10 节。
- 适配器这一层另有一道：发给带标记路由的非流式调用，适配器一律改成向上游发流式请求，再把 chunk 拼回完整的回复（正文、reasoning、按 index 合并的 tool call、finish reason、usage）。所以没有走缓冲路径的非流式调用，比如被 pin 到这条路由的请求，也不会因为非流式吃到 400。

### 5.7 `/v1/messages`

- 请求体的转换是现成的：带标记的路由是 OpenAI 兼容的适配器，`BaseAdapter` 会把 Anthropic 的请求转成 chat completions 发出去。
- 要做的三件事：
  - **认出这个策略。** 今天这个入口只认 `GreedyRouter`（`_greedy_router`）。改成两种策略都认，本地优先、云端第二遍的选法共用。
  - **退回触发第二遍。** `_unless_spilled` 今天只接 `UpstreamQueueWaitExpired`，加上 `EngineDeclined`。第二遍沿用 `_SpillCarry`：同一个 request id 和起始时刻，只走云端路由。
  - **非流式的 messages。** `BaseAdapter.messages` 调的是非流式的 `chat_completion`，发给 Nimbus 会得到 400。带标记的路由改成向上游发流式请求，在适配器里拼回完整的回复（5.6 节的那一道）。
- 带图片的请求不发给带标记的路由，沿用现有的 `required_modalities`。
- 这个入口按 Anthropic 的写法判断 5.10 节的那几类：`tool_choice` 的 `any` 和 `tool` 是强制调用，`disable_parallel_tool_use` 是不许并行。结构化输出的 schema 不在判断里：这个入口本来就不把它发给 OpenAI 兼容的路由。
- 没有第二遍可走的退回（模型没有选用这个策略却有带标记的路由、auto 或改路由的请求、batch）在这个入口答成 503 `overloaded_error`，不是它对 `UpstreamSaturated` 的 429。
- Nimbus 接不了的那几类请求在选路时就跳过，见 5.10 节。

### 5.8 客户端断开

Nimbus 靠连接断开来取消请求。网关在客户端断开时必须关掉到 Nimbus 的连接，等待退回的那一段也一样。`local_deployment_proxy` 以前有不理会断开的问题，没有确认修过；这一条要有测试，并在联调时实测（第 8 节）。

### 5.9 探活

对带标记路由的探活打 Nimbus 的 `/health`，不向引擎发补全请求。

### 5.10 选路时就跳过 Nimbus 的请求

下面这些请求不发给带标记的路由，直接走云端，并记一条 `route_offload`，`reason` 是 `unsupported_local`：

| 请求 | 发给 Nimbus 会怎样 |
|---|---|
| 带 `response_format`，且不是纯文本 | **被悄悄丢掉。** `nimbus/server.py` 转发给引擎的字段（`FORWARDED`）里没有它，`unsupported()` 也不拦它，回复不受格式约束，网关无从发现 |
| `tool_choice` 不是 `auto` 或 `none` | 400 |
| `parallel_tool_calls` 为 false | 400 |
| 带 `functions` 或 `function_call` | 400 |
| 非文本内容 | 400。沿用现有的 `required_modalities`，路由只声明文本 |
| 带 tools，而 Nimbus 不认识这个模型的 tool 格式 | 400。按模型在路由上声明 |

- 第一行必须在网关拦：Nimbus 不报错，等它拒绝是等不到的。Nimbus 那边也应当改成拒绝而不是丢掉（第 7 节），两边各守一道。
- 其余几行不拦也能走通，但每个 400 都会用掉“两条路由拒绝就停”（`_REQUEST_REFUSAL_LIMIT`）的一次名额，并白发一次请求。
- 这张表是 Nimbus 的 `unsupported()` 的一份副本，Nimbus 改了规则它要跟着改。漏掉一类时的后果只是回到“吃一个 400 再走云端”，第一行除外。

## 6. 观测

- 退回记一条 `route_offload`：`router` 是这个策略的名字，`reason` 是 Nimbus 给的 `decision`（`declined_at_arrival`、`declined_at_cutoff`、`declined_at_latest_start`、`declined_over_context`），外加 `ttft_left_ms` 和 Nimbus 的 `request_id`。
- 请求日志的 `_routing["offload"]` 写同一个 `reason`。
- 管理台的 Offloaded 表已经按 `reason` 计数，不认识的 `reason` 归进 Other 一列，所以这些请求不改前端就会被算进去；要给它们单独的列是前端的改动，没有做。
- 两边的记录靠 `X-Nimbus-Request-Id` 对上（5.2 节）：Nimbus 内部用自己的计数器，把网关的 id 另记一栏。
- `api_logs.ttft_ms` 仍按网关的口径：从网关收到请求算起。

## 7. Nimbus 那边要做的

这些不在本仓库，列在这里是因为网关依赖它们：

1. **`X-Nimbus-Elapsed-Ms`。** 读这个 header，把收到时刻往前拨；不给时行为不变；不是非负数字时返回 400。
2. **不再悄悄丢掉 `response_format`。** 引擎这条路不能照办的 `response_format`，要拒绝或退回，不能当它不存在（5.10 节）。这一条和准入模式无关，任何调用方都会遇到。
3. **记下 `X-Nimbus-Request-Id`。** 写进每个请求的记录，不参与决定。
4. **定稿退回的格式。** 状态码 409、`nimbus` 字段的三个键、`decision` 的四个取值、两个 header 的名字。
5. **确认运行。** 用完整版的 router 对外服务之前，要在真引擎上跑过它自己的 correctness 检查。这一项需要 GPU，还没有做。
6. **profile。** 每台引擎要有为它校准的 profile，并填好 SLO。H200 上的 Qwen3.6 已经有。

## 8. 验证

**单元测试**（用一个假的 Nimbus：按脚本返回流、409 或错误）：

- 流式返回时，请求留在本地，日志和记账与今天的本地请求相同。
- 立刻退回和等一段时间才退回，请求都只去云端一次；没有失败记录，熔断不动，拒绝计数不动。
- 退回之后云端失败：客户端收到云端的错误，看不到 409。
- 没有标记的路由返回 409 时，行为和今天完全相同。
- 两个 header 的值：`Remote-TTFT` 等于配置的直线在估算 token 数上的值，云端接不住时是 `inf`；`Elapsed` 不小于 0，并且随网关里的耗时增长。
- 非流式 chat、流式和非流式的 `/v1/messages`，各有留在本地和被退回两种情况。
- 客户端在等待退回时断开，到 Nimbus 的连接被关掉。
- 云端接不住时 `Remote-TTFT` 是 `inf`；这时被退回的请求在 chat 和 `/v1/messages` 两个入口都得到 503，不是 500、429 或 409。
- 退回不触发适配器换 key，不进熔断。
- 5.10 节表里的每一类请求都不发给带标记的路由，各记一条 `route_offload`。
- 带标记的路由收到 `X-Session-Id` 和 `X-Nimbus-Request-Id`；不带标记的路由收不到。
- 启动检查的每一条各有一个反例。

**联调**（真引擎，H200 上的 Qwen3.6）：

- 同一批请求下，网关日志里的 `reason` 和 Nimbus 自己记录的决定逐条对得上。
- 断开客户端后，Nimbus 里对应的请求被取消，引擎不再为它生成。
- 实测 `elapsed` 的分布，写进 PR 描述。

## 9. 未决问题

1. **`cloud_ttft` 用配置的直线，还是用网关实测的云端首 token 时间。** 本文先用配置，和 `greedy` 一致。
2. **多个引擎时要不要按 Nimbus 的负载选路**（5.5 节）。
3. **跨机房部署时网络时间怎么算**（5.2 节）。
4. **`/v1/completions`（旧的补全接口）要不要走 Nimbus。** Nimbus 有这个接口；本文没有覆盖。
5. 动代码之前要和 Juncheng 对一遍；Nimbus 那边的改动（第 7 节的前四项）要和 Glen 对一遍。

## 10. 分阶段

每一阶段一个 PR，目标分支 `2027-osdi-nimbus`：

1. **认出退回。** 适配器里的判断、异常、`left_unsent`，以及它们的测试。没有模型选用新策略时，行为不变。
2. **路由策略和流式 chat。** 策略、配置、启动检查、两个 header、观测。
3. **非流式 chat 和 `/v1/messages`。**
4. **联调。** 依赖第 7 节的六项。
