# Multimodal Support — End-to-End Audit & Design

**Date:** 2026-06-17
**Status:** Audit + incremental implementation (token counting + audio modality)
**Scope:** Image and audio input handling across the serving gateway and adapters.

## 1. Summary

Multimodal **image** input was already substantially implemented before this
audit. The request schema accepts structured content, model configs declare
`input_modalities`, the router rejects media a model can't accept, and the
OpenAI-compatible and Claude adapters forward/convert images. The two real gaps
were:

1. **Token estimation corrupted multimodal requests** — `estimate_prompt_tokens`
   ran the entire `content` field (including base64 image/audio blobs) through
   `str()` and the text tokenizer, producing wildly inflated counts that feed
   rate limiting, quota, RouteWise routing hints, and usage fallbacks.
2. **Audio was not a first-class modality** — only `image` was recognized by the
   router pre-flight and the OpenAI-compat passthrough decision.

This document records the full coverage map and the changes made.

## 2. Coverage map (as audited)

| Layer | File | Image | Audio (before) | Notes |
|---|---|---|---|---|
| Request schema | `serving/schemas.py` | ✅ `content: Any` | ✅ `content: Any` | Accepts any block shape; no change needed. |
| Model capability | `serving/adapters/base.py` (`input_modalities`) | ✅ | ✅ (string list) | `["text"]` default; models opt into `["text","image"]` etc. |
| Router pre-flight | `serving/servers/routers/completions.py` | ✅ reject on text-only | ❌ ignored | Now generalized to any modality. |
| OpenAI-compat adapter | `serving/adapters/openai_compat.py` | ✅ passthrough / strip | ❌ stripped | Now passes through for any non-text modality. |
| Claude adapter | `serving/adapters/claude_format.py` | ✅ full conversion | ➖ n/a | base64/URL → Anthropic blocks. |
| Gemini adapter | `serving/adapters/gemini.py` | ⚠️ drops images | ⚠️ drops audio | Text-only extraction; out of scope here (see §5). |
| Anthropic translator | `serving/adapters/anthropic_translator.py` | ✅ reverse conversion | ➖ n/a | Anthropic → OpenAI. |
| Token estimation | `serving/utils/tokens.py` | ❌ stringified blob | ❌ stringified blob | **Fixed.** |

## 3. Changes made

### 3.1 Token estimation (`serving/utils/tokens.py`)

`estimate_prompt_tokens` now walks `content` structurally:

- string content → tokenized as before (text-only behavior is **unchanged**);
- list content → each block estimated by type:
  - text blocks → tokenized,
  - `image_url` / `image` / `input_image` → flat `IMAGE_TOKEN_ESTIMATE` (85,
    matching OpenAI's low-detail base),
  - `input_audio` / `audio` → flat `AUDIO_TOKEN_ESTIMATE` (200, coarse
    placeholder),
  - unknown blocks → only embedded `text` is counted, never the raw payload.

These constants are deliberately coarse: precise image/audio token costs are
provider-specific and arrive in the upstream `usage` payload. The local
estimate only matters for pre-checks, routing size hints, and the fallback when
a provider omits `usage`. The critical property is that a multi-hundred-KB
inline data URL no longer counts as tens of thousands of phantom text tokens.

### 3.2 Audio modality, router pre-flight (`completions.py`)

The image-only rejection loop is replaced by `_find_unsupported_modality`, which
maps content block types to required modalities via `_CONTENT_BLOCK_MODALITY`
(`image_url`/`image`/`input_image` → `image`, `input_audio`/`audio` → `audio`)
and returns the first modality a message requires but the model lacks. The 400
error message is unchanged for images (`"... does not support image input"`) and
mirrors for audio (`"... does not support audio input"`).

### 3.3 Audio passthrough (`openai_compat.py`)

`_clean_message` previously keyed only on `"image"` to decide whether to forward
structured content or flatten it to text. It now forwards structured content
when the model declares **any** non-text input modality (image *or* audio), and
flattens to plain text only for text-only models. Disallowed block types never
reach the adapter because the router pre-flight rejects them first.

## 4. Tests

- `tests/unit/utils/test_tokens.py` (new): text-only parity, structural text
  block counting, flat image/audio estimates, and a regression asserting a
  200k-char inline data URL does not inflate the count.
- `tests/unit/adapters/test_openai_compat_image.py`: added audio passthrough
  (audio-capable model) and audio-strip (text-only model) cases.
- `tests/servers/test_completions.py`: added an `audio-model` route to the
  modality-gate fixture and tests for audio accept, audio reject on text-only,
  and image reject on an audio-only model.

## 5. Recommended next steps (not in this change)

- **Gemini multimodal forwarding** — `gemini.py` extracts text only. To support
  vision/audio it must emit `inlineData` (base64) / `fileData` parts instead of
  dropping `image_url` / `input_audio` blocks.
- **Output modalities** — image/audio *generation* (`output_modalities`) is not
  wired through response handling; only input is covered here.
- **Per-image cost** — billing does not yet apply a `pricing["image"]` term; the
  token estimate is a count, not a price. Wire image/audio pricing into
  `completions_cost.py` when those models go live.
- **Model registry** — no model currently declares `["text","audio"]`; the audio
  path is implemented and tested but inert until a config entry opts in.
