# Available Models

FreeInference provides access to multiple state-of-the-art LLM models for coding agents and IDEs.

## Model Overview

| Model ID | Name | Context Length | Max Output | Features |
|----------|------|----------------|------------|----------|
| `glm-5.1` | GLM-5.1 | 200K tokens | 128K tokens | Function calling, Structured output, Bilingual (Chinese/English), Thinking mode, Tool streaming |
| `glm-5.2` | GLM-5.2 | 1M tokens | 128K tokens | Function calling, Structured output, Bilingual (Chinese/English), Thinking mode, Tool streaming, Internal/staff access only |
| `glm-5-turbo` | GLM-5 Turbo | 200K tokens | 128K tokens | Function calling, Structured output, Bilingual (Chinese/English), Thinking mode, Tool streaming |
| `qwen3.6-35b` | Qwen3.6 35B | 262K tokens | 8K tokens | Function calling, Structured output |
| `minimax-m2.5` | MiniMax M2.5 | 205K tokens | 131K tokens | Function calling, Structured output, Thinking mode |
| `minimax-m2.7` | MiniMax M2.7 | 205K tokens | 131K tokens | Function calling, Structured output, Multimodal (text+image) |
| `minimax-m3` | MiniMax M3 | 1M tokens | 131K tokens | Function calling, Structured output, Thinking mode, Multimodal (text+image) |
| `kimi-k2.7-code` | Kimi K2.7 Code | 262K tokens | 131K tokens | Function calling, Structured output, Thinking mode, Coding agents only |

---

## Model Details

### GLM-5.1

**Model ID:** `glm-5.1`

**Aliases:** `freeinference-glm-5.1`

- Context length: 200,000 tokens
- Max output: 128,000 tokens
- Quantization: fp8
- Input modalities: text
- Output modalities: text
- Language support: Chinese, English
- Function calling: Yes
- Structured output: Yes
- Thinking mode: Yes
- Tool streaming: Yes

---

### GLM-5.2

**Model ID:** `glm-5.2`

**Aliases:** `freeinference-glm-5.2`

Internal/staff access only (`required_role: internal`) — a public API key gets 403.

- Context length: 1,000,000 tokens
- Max output: 128,000 tokens
- Quantization: fp8
- Input modalities: text
- Output modalities: text
- Language support: Chinese, English
- Function calling: Yes
- Structured output: Yes
- Thinking mode: Yes
- Tool streaming: Yes

---

### GLM-5 Turbo

**Model ID:** `glm-5-turbo`

**Aliases:** `freeinference-glm-5-turbo`

- Context length: 200,000 tokens
- Max output: 128,000 tokens
- Quantization: fp8
- Input modalities: text
- Output modalities: text
- Language support: Chinese, English
- Function calling: Yes
- Structured output: Yes
- Thinking mode: Yes
- Tool streaming: Yes

---

### Qwen3.6 35B

**Model ID:** `qwen3.6-35b`

- Context length: 262,144 tokens
- Max output: 8,192 tokens
- Quantization: fp8
- Input modalities: text
- Output modalities: text
- Function calling: Yes
- Structured output: Yes

---

### MiniMax M2.5

**Model ID:** `minimax-m2.5`

- Context length: 204,800 tokens
- Max output: 131,072 tokens
- Quantization: bf16
- Input modalities: text
- Output modalities: text
- Function calling: Yes
- Structured output: Yes
- Thinking mode: Yes

---

### MiniMax M2.7

**Model ID:** `minimax-m2.7`

- Context length: 204,800 tokens
- Max output: 131,072 tokens
- Quantization: bf16
- Input modalities: text, image
- Output modalities: text
- Function calling: Yes
- Structured output: Yes

---

### MiniMax M3

**Model ID:** `minimax-m3`

- Context length: 1,048,576 tokens
- Max output: 131,072 tokens
- Quantization: bf16
- Input modalities: text, image
- Output modalities: text
- Function calling: Yes
- Structured output: Yes
- Thinking mode: Yes

---

### Kimi K2.7 Code

**Model ID:** `kimi-k2.7-code`

**Aliases:** `Kimi-K2.7-Code`, `kimi-k2.7`

Served through the Kimi Code coding-plan subscription; intended for coding agents rather than general chat.

- Context length: 262,144 tokens
- Max output: 131,072 tokens
- Quantization: fp8
- Input modalities: text
- Output modalities: text
- Function calling: Yes
- Structured output: Yes
- Thinking mode: Yes

---

## Switching Models

To use different models, change the model name in your IDE configuration:

**Cursor:** Select from the dropdown in settings

**Kilo Code:** Select from the dropdown in extension settings. A good default is `glm-5.1`; switch to `glm-5-turbo` for faster iteration, `minimax-m3` for long-context and image-aware workflows, or `kimi-k2.7-code` for agentic coding.

**Roo Code:** Select from the dropdown in extension settings
