# Refactor: claude.py (Vertex AI) Streaming → 使用 claude_format 共享模块

**Status**: Proposed
**Author**: Claude Code
**Date**: 2026-03-29
**Estimated effort**: 2-3 hours
**Net code change**: ~-120 lines

---

## 1. Background

`claude_sub.py`（Subscription adapter）已经成功地使用了 `claude_format.py` 中的共享流式处理模块：

- `handle_stream_event()` — 统一的 Claude SSE 事件分发
- `ToolCallAccumulator` — 流式 tool_use 块的状态机
- `StreamEventResult` — 事件处理结果的类型化封装
- `build_final_usage()` — 构建最终 usage 字典

但 `claude.py`（Vertex AI adapter）的 `stream_chat_completion` 仍然手写了一整套
`content_block_start` / `content_block_delta` / `content_block_stop` /
`message_delta` / `message_start` / `message_stop` 的 if-elif 分发链（约150行），
与共享模块的逻辑完全重复。

## 2. Goal

让 `claude.py` 的正常流式路径也使用 `handle_stream_event` + `ToolCallAccumulator`，
**在不改变任何外部行为的前提下**，减少约 120 行重复代码。

## 3. Key Differences Between claude.py and claude_sub.py

重构前需要理解两个 adapter 的差异，确保不丢功能：

| 差异点 | claude.py (Vertex) | claude_sub.py (Subscription) |
|--------|-------------------|------------------------------|
| **`"message"` 一次性响应** | ✅ 需处理 — Vertex 有时不走流式，直接返回完整 `type: "message"` | ❌ 不需要 — Anthropic API 不会这样 |
| **stream_post 模式** | `mode="auto"` (NDJSON)，直接 `json.loads(line)` | 默认 SSE 模式，需剥离 `"data: "` 前缀 |
| **Cache tokens 提取** | `message_start` 中只取了 `input_tokens`，没取 cache tokens；`"message"` 完整响应中取了全部 | `handle_stream_event` 外手动从 raw event 提取 cache tokens |
| **Thinking tokens** | 仅从 `"message"` 类型的 usage 中取 `thinking_tokens`，正常流式路径不提取 | 不提取（默认 0） |
| **Error handling** | fail-fast，广播异常 | retry + fallback 到付费 API |
| **`_routing` 元数据** | `{provider, base_url}` | `{provider, base_url, pricing, account_id}` |
| **Upstream error 检测** | 检查 `"Code"` + `"Error"` 字段（Vertex 特有） | 无（标准 API 不返回这种格式） |
| **Debug logging** | 前10行逐行 debug log（`line_count`） | 无逐行 debug |

## 4. Implementation Plan

### Step 1: 增强 `claude_format.py` 的 `StreamEventResult`

**文件**: `serving/adapters/claude_format.py`

**改动**: 给 `StreamEventResult` 增加三个字段，让 `handle_stream_event` 自动提取
cache/thinking tokens，避免调用方重复解析 raw event。

```python
# StreamEventResult.__slots__ 增加:
"cache_read_tokens"       # from message_start usage
"cache_write_tokens"      # from message_start usage  (cache_creation_input_tokens)
```

修改 `handle_stream_event` 的 `message_start` 分支：

```python
# 当前:
if event_type == "message_start":
    message = event.get("message", {})
    usage_data = message.get("usage", {})
    input_tokens = int(usage_data.get("input_tokens", 0) or 0)
    return StreamEventResult(input_tokens=input_tokens)

# 改为:
if event_type == "message_start":
    message = event.get("message", {})
    usage_data = message.get("usage", {})
    input_tokens = int(usage_data.get("input_tokens", 0) or 0)
    cache_read = int(usage_data.get("cache_read_input_tokens", 0) or 0)
    cache_write = int(usage_data.get("cache_creation_input_tokens", 0) or 0)
    return StreamEventResult(
        input_tokens=input_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )
```

**注意**: 不在 `StreamEventResult` 中加 `reasoning_tokens`。原因：
- `claude.py` 的正常流式路径（`message_start` / `message_delta`）中 Anthropic API 不返回 `thinking_tokens`
- `thinking_tokens` 只出现在 Vertex 的 `type: "message"` 完整响应中，而那段逻辑保留不动
- 加了也没有调用方会用到，违反 YAGNI

**向后兼容**: 新字段默认值为 0，`claude_sub.py` 不需要改任何一行即可正常工作。

### Step 2: 重构 `claude.py` 的流式事件循环

**文件**: `serving/adapters/claude.py`

**保留不变的部分**:
- 第146-225行: payload 构建、endpoint URL、headers、局部变量初始化
- 第226-258行: `stream_post` 调用、line parsing、upstream error 检测
- 第264-382行: **`chunk_type == "message"` 完整响应处理** (Vertex 特有，原样保留)
- 第534-548行: 异常处理
- 第550-586行: helper 方法

**替换的部分** (第384-533行 → 约40行):

把手写的 6 个 if-elif 分支替换为：

```python
# 新增 import (文件顶部)
from .claude_format import (
    ToolCallAccumulator,       # 新增
    build_final_usage,         # 新增
    handle_stream_event,       # 新增
    ...existing imports...
)

# stream_chat_completion 内部:
# 初始化 (替换 current_tool_use, tool_input_buffer, current_tool_index, completed_tool_calls)
accumulator = ToolCallAccumulator()

# 事件循环内 (替换第384-533行):
# ---- "message" 类型处理保留在上面 (264-382行) ----

# 正常流式事件
result = handle_stream_event(chunk_data, accumulator)

# 跟踪 usage
if result.input_tokens:
    input_tokens = result.input_tokens
if result.output_tokens:
    output_tokens = result.output_tokens
if result.cache_read_tokens:
    cache_read_input_tokens = result.cache_read_tokens
if result.cache_write_tokens:
    cache_creation_input_tokens = result.cache_write_tokens

# 发送 text delta
if result.text_delta:
    total_content += result.text_delta
    yield self.format_stream_chunk(result.text_delta, self.config.id)

# 跟踪 finish_reason
if result.finish_reason:
    finish_reason = result.finish_reason

# message_stop: 发送 tool calls + final chunk
if result.is_done:
    completed_tools = accumulator.get_completed()
    if completed_tools:
        yield self.format_tool_chunk(completed_tools, self.config.id)
        finish_reason = "tool_calls"

    usage_obj = build_final_usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
    )
    final_chunk = {
        "id": f"chatcmpl-{int(time.time() * 1000)}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": self.config.id,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        "usage": usage_obj,
        "_routing": {
            "provider": self.config.provider,
            "base_url": self.config.base_url,
        },
    }
    yield f"data: {json.dumps(final_chunk)}\n\n"
    yield done_sentinel()
    break
```

**删除的局部变量**:
- `current_tool_use` → `ToolCallAccumulator` 内部管理
- `tool_input_buffer` → `ToolCallAccumulator` 内部管理
- `current_tool_index` → `ToolCallAccumulator` 内部管理
- `completed_tool_calls` → `accumulator.get_completed()`
- `line_count` → 删除逐行 debug logging（保留 stream start/payload summary log）

**行为差异说明（当前 claude.py 正常流式路径的一个疏漏）**:

当前 `claude.py` 的 `message_start` 分支（第460-465行）只提取了 `input_tokens`，
**没有提取 `cache_read_input_tokens` 和 `cache_creation_input_tokens`**。
这意味着在正常流式路径（非 `"message"` 一次性响应）下，cache token 计费数据会丢失。
重构后通过增强的 `handle_stream_event` 自动提取，**顺便修复了这个 bug**。

### Step 2b (Optional): 简化 claude_sub.py 的 cache token 提取

**文件**: `serving/adapters/claude_sub.py`

Step 1 增强后，`claude_sub.py` 中手动提取 cache tokens 的代码可以简化：

```python
# 当前 (_subscription_stream 第442-453行):
if result.input_tokens:
    input_tokens = result.input_tokens
if result.output_tokens:
    output_tokens = result.output_tokens
# 还需要额外从 raw event 提取 cache:
if event.get("type") == "message_start":
    msg_usage = event.get("message", {}).get("usage", {})
    cache_read_input_tokens = int(msg_usage.get("cache_read_input_tokens", 0) or 0)
    cache_creation_input_tokens = int(
        msg_usage.get("cache_creation_input_tokens", 0) or 0
    )

# 简化后:
if result.input_tokens:
    input_tokens = result.input_tokens
if result.output_tokens:
    output_tokens = result.output_tokens
if result.cache_read_tokens:
    cache_read_input_tokens = result.cache_read_tokens
if result.cache_write_tokens:
    cache_creation_input_tokens = result.cache_write_tokens
```

同样适用于 `_fallback_stream`（第556-566行）。

**这步是可选的** — 不做也不影响正确性，只是让代码更一致。但既然我们动了
`claude_format.py`，一并清理可以减少未来维护的认知负担。

## 5. Files Changed

| 文件 | 改动 | 行数变化 |
|------|------|---------|
| `serving/adapters/claude_format.py` | StreamEventResult +2 fields, handle_stream_event message_start 增强 | **+10** |
| `serving/adapters/claude.py` | 事件循环替换为 handle_stream_event；顺便修复 cache token 丢失 bug | **-120** |
| `serving/adapters/claude_sub.py` | (可选) _subscription_stream 和 _fallback_stream 的 cache token 提取简化 | **-10** |

## 6. What Does NOT Change

- ✅ Vertex `"message"` 一次性响应处理 — 原样保留（包括 thinking_tokens 提取）
- ✅ `_routing` 元数据格式 — 各 adapter 保持各自格式
- ✅ Error handling 策略 — claude.py fail-fast, claude_sub.py retry
- ✅ `stream_post` 调用方式 (`mode="auto"`) — 不变
- ✅ Upstream error 检测（`"Code"` + `"Error"` 字段）— 不变
- ✅ Final chunk 的 usage 数值 — `build_final_usage()` 计算结果与手写一致
- ✅ `claude_sub.py` 行为 — 新字段默认 0，完全向后兼容

## 7. Risks & Mitigations

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| handle_stream_event 漏处理某个 Vertex 特有事件类型 | 低 | 该事件被忽略（返回空 StreamEventResult） | Vertex 正常流式用的是标准 Anthropic SSE 协议，已逐一比对 event types；未知事件类型返回空 result 是安全的降级 |
| ToolCallAccumulator 行为与手写逻辑不一致 | 低 | Tool calls 格式错误 | 逐行比对过: `start_tool` / `accumulate_json` / `finish_tool` / `get_completed` 与手写逻辑语义完全一致，输出格式（含 index 字段）也一致 |
| cache tokens 提取时机差异 | 无 | N/A | `handle_stream_event` 在 `message_start` 时提取，与 `claude_sub.py` 当前提取位置相同 |
| 删除 line_count debug 日志影响排查 | 低 | Vertex 问题排查变难 | 保留 stream start 和 payload summary 日志；逐行 debug 只在前 10 行触发，实际价值有限，出问题可临时恢复 |

## 8. Verification

1. **单元测试**: `make test` — 确保所有现有测试通过
2. **Vertex 流式文本**: 发请求到 Claude Vertex 模型，确认文本内容逐 chunk 返回正确
3. **Vertex 流式 tool calls**: 发带 tools 的请求，确认 tool_calls 格式正确（含 index 字段）
4. **Vertex "message" 回退**: 确认完整响应场景仍然工作（日志中有 `chunk_type: message`）
5. **Vertex usage 准确性**: 对比重构前后的 usage 数值（prompt_tokens, completion_tokens, cache 相关）
6. **claude_sub 无影响**: 发请求到 Claude subscription 模型，确认行为不变

## 9. Review Changelog

- **v2 (2026-03-29)**: Review 后修正以下问题:
  - 去掉 `reasoning_tokens` 字段提案 — YAGNI，正常流式路径不返回 thinking_tokens
  - 修正 "Cache tokens 提取" 差异描述 — 原 claude.py `message_start` 根本没提取 cache tokens（不是"从 message_start 取"），这是一个现有 bug
  - 新增 "行为差异说明" 段落，标注重构顺便修复 cache token 丢失 bug
  - 差异表增加 "Debug logging" 行 — line_count 逐行 debug 是 claude.py 特有的
  - 风险表增加 "删除 line_count debug 日志" 条目
  - Step 2b 改为推荐一并执行而非纯可选 — 减少认知负担
