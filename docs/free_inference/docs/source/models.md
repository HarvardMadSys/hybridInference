# Available Models

FreeInference provides access to multiple state-of-the-art LLM models for coding agents and IDEs.

## Model Overview

| Model ID | Name | Context Length | Max Output | Features |
|----------|------|----------------|------------|----------|
| `glm-5.1` | GLM-5.1 | 200K tokens | 128K tokens | Function calling, Structured output, Bilingual (Chinese/English), Thinking mode |
| `glm-5` | GLM-5 | 200K tokens | 128K tokens | Function calling, Structured output, Bilingual (Chinese/English), Thinking mode |
| `glm-4.7` | GLM-4.7 | 200K tokens | 128K tokens | Function calling, Structured output, Bilingual (Chinese/English), Thinking mode |
| `glm-5-turbo` | GLM-5 Turbo | 200K tokens | 128K tokens | Function calling, Structured output, Bilingual (Chinese/English), Thinking mode |
| `qwen3.6-27b` | Qwen3.6 27B | 65K tokens | 8K tokens | Function calling, Structured output |
| `qwen3.6-35b` | Qwen3.6 35B | 65K tokens | 8K tokens | Function calling, Structured output |
| `minimax-m2.7` | MiniMax M2.7 | 196K tokens | 8K tokens | Function calling, Structured output |
| `minimax-m2.5` | MiniMax M2.5 | 1M tokens | 128K tokens | Function calling, Structured output, Thinking mode, Multimodal (text+image) |

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

### GLM-5

**Model ID:** `glm-5`

**Aliases:** `freeinference-glm-5`

- Context length: 200,000 tokens
- Max output: 128,000 tokens
- Architecture: 745B MoE (44B active parameters)
- Quantization: fp8
- Input modalities: text
- Output modalities: text
- Language support: Chinese, English
- Function calling: Yes
- Structured output: Yes
- Thinking mode: Yes
- Tool streaming: Yes

---

### GLM-4.7

**Model ID:** `glm-4.7`

**Aliases:** `freeinference-glm-4.7`

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

### Qwen3.6 27B

**Model ID:** `qwen3.6-27b`

- Context length: 65,536 tokens
- Max output: 8,192 tokens
- Hosting: Self-hosted (free)
- Input modalities: text
- Output modalities: text
- Function calling: Yes
- Structured output: Yes

---

### Qwen3.6 35B

**Model ID:** `qwen3.6-35b`

- Context length: 65,536 tokens
- Max output: 8,192 tokens
- Hosting: Self-hosted (free)
- Input modalities: text
- Output modalities: text
- Function calling: Yes
- Structured output: Yes

---

### MiniMax M2.7

**Model ID:** `minimax-m2.7`

- Context length: 196,608 tokens
- Max output: 8,192 tokens
- Quantization: bf16
- Input modalities: text
- Output modalities: text
- Function calling: Yes
- Structured output: Yes

---

### MiniMax M2.5

**Model ID:** `minimax-m2.5`

- Context length: 1,000,000 tokens
- Max output: 131,072 tokens
- Architecture: 230B MoE (10B active parameters)
- Quantization: bf16
- Input modalities: text, image
- Output modalities: text
- Function calling: Yes
- Structured output: Yes
- Thinking mode: Yes

---

## Internal Models

> **Note:** The following models require internal role access and are not available to general users.

### GPT-5.4

**Model ID:** `gpt-5.4`

- Context length: 1,050,000 tokens
- Max output: 128,000 tokens
- Input modalities: text, image
- Output modalities: text
- Function calling: Yes
- Structured output: Yes

---

### Claude Sonnet 4.6

**Model ID:** `claude-sonnet-4.6`

- Context length: 200,000 tokens
- Max output: 64,000 tokens
- Input modalities: text, image
- Output modalities: text
- Function calling: Yes

---

### Claude Opus 4.6

**Model ID:** `claude-opus-4.6`

- Context length: 200,000 tokens
- Max output: 128,000 tokens
- Input modalities: text, image
- Output modalities: text
- Function calling: Yes

---

## Switching Models

To use different models, change the model name in your IDE configuration:

**Cursor:** Select from the dropdown in settings

**Codex:** Edit `~/.codex/config.toml`:
```toml
model = "glm-5"  # Change to any model ID
```

**Roo Code / Kilo Code:** Select from the dropdown in extension settings
