"""Unit tests for Claude adapter image conversion.

Tests that OpenAI-format image_url blocks are correctly converted to
Claude's image format, including:
- Base64 data URLs
- Data URLs with extra parameters (;name=, ;charset=)
- URL-encoded (non-base64) data URLs
- Regular HTTP/HTTPS URLs
- MIME type normalization (image/jpg -> image/jpeg)
- Image conversion in tool results
"""

import base64

import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.claude import ClaudeAdapter


@pytest.fixture
def claude_adapter():
    """Create a Claude adapter instance for testing."""
    config = ModelConfig(
        id="claude-test",
        name="Claude Test",
        provider="claude",
        base_url="https://test.example.com",
        api_key="test-key",
        context_length=200000,
        max_output_length=4096,
        supports_tools=True,
    )
    return ClaudeAdapter(config)


class TestConvertContentBlock:
    """Tests for _convert_content_block method."""

    def test_string_content(self, claude_adapter):
        """Test that string content is wrapped in text block."""
        result = claude_adapter._convert_content_block("Hello world")
        assert result == {"type": "text", "text": "Hello world"}

    def test_text_block_passthrough(self, claude_adapter):
        """Test that text blocks pass through unchanged."""
        block = {"type": "text", "text": "Hello world"}
        result = claude_adapter._convert_content_block(block)
        assert result == {"type": "text", "text": "Hello world"}

    def test_image_url_with_http_url(self, claude_adapter):
        """Test conversion of HTTP URL image."""
        block = {
            "type": "image_url",
            "image_url": {"url": "https://example.com/image.jpg"},
        }
        result = claude_adapter._convert_content_block(block)

        assert result["type"] == "image"
        assert result["source"]["type"] == "url"
        assert result["source"]["url"] == "https://example.com/image.jpg"

    def test_image_url_string_format(self, claude_adapter):
        """Test conversion when image_url is a string instead of dict."""
        block = {
            "type": "image_url",
            "image_url": "https://example.com/image.png",
        }
        result = claude_adapter._convert_content_block(block)

        assert result["type"] == "image"
        assert result["source"]["type"] == "url"
        assert result["source"]["url"] == "https://example.com/image.png"

    def test_unknown_block_with_text_field(self, claude_adapter):
        """Test that unknown blocks with text field are converted to text."""
        block = {"type": "unknown", "text": "Some text"}
        result = claude_adapter._convert_content_block(block)
        assert result == {"type": "text", "text": "Some text"}

    def test_unknown_block_serialized_to_json(self, claude_adapter):
        """Test that unknown blocks without text are JSON serialized."""
        block = {"type": "custom", "data": {"key": "value"}}
        result = claude_adapter._convert_content_block(block)
        assert result["type"] == "text"
        assert "custom" in result["text"]
        assert "key" in result["text"]

    def test_image_url_missing_url(self, claude_adapter):
        """Test image_url block with missing url returns error text."""
        block = {"type": "image_url", "image_url": {}}
        result = claude_adapter._convert_content_block(block)

        assert result["type"] == "text"
        assert "Invalid image" in result["text"] or "no URL" in result["text"]

    def test_image_url_empty_url(self, claude_adapter):
        """Test image_url block with empty url returns error text."""
        block = {"type": "image_url", "image_url": {"url": ""}}
        result = claude_adapter._convert_content_block(block)

        assert result["type"] == "text"
        assert "Invalid image" in result["text"] or "no URL" in result["text"]

    def test_claude_native_image_block_passthrough(self, claude_adapter):
        """Test that Claude native image blocks are passed through."""
        # Claude native format - should ideally be passed through
        block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": "base64data",
            },
        }
        result = claude_adapter._convert_content_block(block)

        # Currently this gets serialized to JSON text since it's an unknown type
        # This test documents the current behavior
        assert result["type"] == "text"
        # The block should be JSON serialized
        assert "image" in result["text"]


class TestParseDataUrl:
    """Tests for _parse_data_url method."""

    def test_basic_base64_jpeg(self, claude_adapter):
        """Test basic base64 JPEG data URL."""
        # Create a minimal valid base64 string
        data = base64.b64encode(b"fake image data").decode("ascii")
        data_url = f"data:image/jpeg;base64,{data}"

        result = claude_adapter._parse_data_url(data_url)

        assert result["type"] == "image"
        assert result["source"]["type"] == "base64"
        assert result["source"]["media_type"] == "image/jpeg"
        assert result["source"]["data"] == data

    def test_base64_png(self, claude_adapter):
        """Test base64 PNG data URL."""
        data = base64.b64encode(b"PNG image data").decode("ascii")
        data_url = f"data:image/png;base64,{data}"

        result = claude_adapter._parse_data_url(data_url)

        assert result["type"] == "image"
        assert result["source"]["media_type"] == "image/png"

    def test_base64_gif(self, claude_adapter):
        """Test base64 GIF data URL."""
        data = base64.b64encode(b"GIF89a").decode("ascii")
        data_url = f"data:image/gif;base64,{data}"

        result = claude_adapter._parse_data_url(data_url)

        assert result["type"] == "image"
        assert result["source"]["media_type"] == "image/gif"

    def test_base64_webp(self, claude_adapter):
        """Test base64 WebP data URL."""
        data = base64.b64encode(b"RIFF....WEBP").decode("ascii")
        data_url = f"data:image/webp;base64,{data}"

        result = claude_adapter._parse_data_url(data_url)

        assert result["type"] == "image"
        assert result["source"]["media_type"] == "image/webp"

    def test_image_jpg_alias_normalized(self, claude_adapter):
        """Test that image/jpg is normalized to image/jpeg."""
        data = base64.b64encode(b"fake jpeg").decode("ascii")
        data_url = f"data:image/jpg;base64,{data}"

        result = claude_adapter._parse_data_url(data_url)

        assert result["type"] == "image"
        assert result["source"]["media_type"] == "image/jpeg"

    def test_data_url_with_name_parameter(self, claude_adapter):
        """Test data URL with ;name= parameter."""
        data = base64.b64encode(b"image with name").decode("ascii")
        data_url = f"data:image/jpeg;name=photo.jpg;base64,{data}"

        result = claude_adapter._parse_data_url(data_url)

        assert result["type"] == "image"
        assert result["source"]["type"] == "base64"
        assert result["source"]["media_type"] == "image/jpeg"
        assert result["source"]["data"] == data

    def test_data_url_with_charset_parameter(self, claude_adapter):
        """Test data URL with ;charset= parameter."""
        data = base64.b64encode(b"image with charset").decode("ascii")
        data_url = f"data:image/png;charset=utf-8;base64,{data}"

        result = claude_adapter._parse_data_url(data_url)

        assert result["type"] == "image"
        assert result["source"]["media_type"] == "image/png"

    def test_data_url_with_multiple_parameters(self, claude_adapter):
        """Test data URL with multiple parameters."""
        data = base64.b64encode(b"complex image").decode("ascii")
        data_url = f"data:image/jpeg;name=test.jpg;charset=binary;base64,{data}"

        result = claude_adapter._parse_data_url(data_url)

        assert result["type"] == "image"
        assert result["source"]["media_type"] == "image/jpeg"
        assert result["source"]["data"] == data

    def test_url_encoded_data_url(self, claude_adapter):
        """Test URL-encoded (non-base64) data URL."""
        # URL-encode some bytes
        raw_bytes = b"\x89PNG\r\n\x1a\n"
        url_encoded = "%89PNG%0D%0A%1A%0A"
        data_url = f"data:image/png,{url_encoded}"

        result = claude_adapter._parse_data_url(data_url)

        assert result["type"] == "image"
        assert result["source"]["type"] == "base64"
        assert result["source"]["media_type"] == "image/png"
        # Verify the data was re-encoded as base64
        decoded = base64.b64decode(result["source"]["data"])
        assert decoded == raw_bytes

    def test_unsupported_mime_type(self, claude_adapter):
        """Test that unsupported MIME types return error text."""
        data = base64.b64encode(b"not an image").decode("ascii")
        data_url = f"data:application/pdf;base64,{data}"

        result = claude_adapter._parse_data_url(data_url)

        assert result["type"] == "text"
        assert "Unsupported image type" in result["text"]

    def test_invalid_data_url_format(self, claude_adapter):
        """Test that invalid data URLs return error text."""
        result = claude_adapter._parse_data_url("not a data url")

        assert result["type"] == "text"
        assert "Invalid data URL format" in result["text"]

    def test_data_url_missing_comma(self, claude_adapter):
        """Test data URL without comma separator."""
        result = claude_adapter._parse_data_url("data:image/jpeg;base64")

        assert result["type"] == "text"
        assert "Invalid data URL format" in result["text"]

    def test_default_media_type(self, claude_adapter):
        """Test that missing media type defaults to image/jpeg."""
        data = base64.b64encode(b"default type").decode("ascii")
        data_url = f"data:;base64,{data}"

        result = claude_adapter._parse_data_url(data_url)

        assert result["type"] == "image"
        assert result["source"]["media_type"] == "image/jpeg"

    def test_case_insensitive_base64_marker_uppercase(self, claude_adapter):
        """Test that ;BASE64 (uppercase) is recognized."""
        data = base64.b64encode(b"uppercase base64").decode("ascii")
        # Test with uppercase ;BASE64
        data_url = f"data:image/jpeg;BASE64,{data}"

        result = claude_adapter._parse_data_url(data_url)

        assert result["type"] == "image"
        assert result["source"]["type"] == "base64"
        assert result["source"]["data"] == data

    def test_case_insensitive_base64_marker_mixed(self, claude_adapter):
        """Test that ;Base64 (mixed case) is recognized."""
        data = base64.b64encode(b"mixed case base64").decode("ascii")
        data_url = f"data:image/png;Base64,{data}"

        result = claude_adapter._parse_data_url(data_url)

        assert result["type"] == "image"
        assert result["source"]["type"] == "base64"


class TestConvertContentBlocks:
    """Tests for _convert_content_blocks method."""

    def test_string_content(self, claude_adapter):
        """Test string content conversion."""
        result = claude_adapter._convert_content_blocks("Hello")
        assert result == [{"type": "text", "text": "Hello"}]

    def test_list_with_text_and_image(self, claude_adapter):
        """Test list with mixed text and image blocks."""
        data = base64.b64encode(b"image").decode("ascii")
        content = [
            {"type": "text", "text": "Look at this:"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}},
        ]

        result = claude_adapter._convert_content_blocks(content)

        assert len(result) == 2
        assert result[0] == {"type": "text", "text": "Look at this:"}
        assert result[1]["type"] == "image"
        assert result[1]["source"]["type"] == "base64"

    def test_none_content(self, claude_adapter):
        """Test None content returns empty list."""
        result = claude_adapter._convert_content_blocks(None)
        assert result == []

    def test_multiple_images(self, claude_adapter):
        """Test multiple images in content."""
        content = [
            {"type": "image_url", "image_url": {"url": "https://example.com/1.jpg"}},
            {"type": "image_url", "image_url": {"url": "https://example.com/2.png"}},
        ]

        result = claude_adapter._convert_content_blocks(content)

        assert len(result) == 2
        assert all(r["type"] == "image" for r in result)


class TestConvertMessagesWithImages:
    """Tests for _convert_messages with image content."""

    def test_user_message_with_image(self, claude_adapter):
        """Test user message containing image is converted."""
        data = base64.b64encode(b"test image").decode("ascii")
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What's in this image?"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}},
                ],
            }
        ]

        result = claude_adapter._convert_messages(messages)

        assert len(result) == 1
        assert result[0]["role"] == "user"
        content = result[0]["content"]
        assert len(content) == 2
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "image"
        assert content[1]["source"]["type"] == "base64"

    def test_tool_result_with_image(self, claude_adapter):
        """Test tool result containing image is converted."""
        data = base64.b64encode(b"screenshot").decode("ascii")
        messages = [
            {"role": "user", "content": "Take a screenshot"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "screenshot", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": [
                    {"type": "text", "text": "Screenshot captured:"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}},
                ],
            },
        ]

        result = claude_adapter._convert_messages(messages)

        # Find the tool_result block
        tool_result = None
        for msg in result:
            if msg["role"] == "user":
                for block in msg["content"]:
                    if block.get("type") == "tool_result":
                        tool_result = block
                        break

        assert tool_result is not None, "Should have tool_result block"
        content = tool_result["content"]
        assert len(content) == 2
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "image"
        assert content[1]["source"]["type"] == "base64"

    def test_image_url_http_in_user_message(self, claude_adapter):
        """Test HTTP URL image in user message."""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this:"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/photo.jpg"}},
                ],
            }
        ]

        result = claude_adapter._convert_messages(messages)

        content = result[0]["content"]
        assert content[1]["type"] == "image"
        assert content[1]["source"]["type"] == "url"
        assert content[1]["source"]["url"] == "https://example.com/photo.jpg"

    def test_assistant_message_with_image_list(self, claude_adapter):
        """Test assistant message with list content containing image_url.

        This tests the _convert_messages handling of assistant messages
        with list content (serving/adapters/claude.py:835).
        """
        data = base64.b64encode(b"assistant image").decode("ascii")
        messages = [
            {"role": "user", "content": "Generate an image"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Here's the generated image:"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}},
                ],
            },
        ]

        result = claude_adapter._convert_messages(messages)

        # Find the assistant message
        assistant_msgs = [m for m in result if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1

        content = assistant_msgs[0]["content"]
        assert len(content) == 2
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "Here's the generated image:"
        assert content[1]["type"] == "image"
        assert content[1]["source"]["type"] == "base64"

    def test_assistant_message_with_http_image(self, claude_adapter):
        """Test assistant message with HTTP URL image."""
        messages = [
            {"role": "user", "content": "Find an image"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Found this:"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/result.png"}},
                ],
            },
        ]

        result = claude_adapter._convert_messages(messages)

        assistant_msgs = [m for m in result if m["role"] == "assistant"]
        content = assistant_msgs[0]["content"]
        assert content[1]["type"] == "image"
        assert content[1]["source"]["type"] == "url"


class TestInferMediaTypeFromUrl:
    """Tests for _infer_media_type_from_url method."""

    def test_jpg_extension(self, claude_adapter):
        """Test .jpg extension."""
        result = claude_adapter._infer_media_type_from_url("https://example.com/photo.jpg")
        assert result == "image/jpeg"

    def test_jpeg_extension(self, claude_adapter):
        """Test .jpeg extension."""
        result = claude_adapter._infer_media_type_from_url("https://example.com/photo.jpeg")
        assert result == "image/jpeg"

    def test_png_extension(self, claude_adapter):
        """Test .png extension."""
        result = claude_adapter._infer_media_type_from_url("https://example.com/image.png")
        assert result == "image/png"

    def test_gif_extension(self, claude_adapter):
        """Test .gif extension."""
        result = claude_adapter._infer_media_type_from_url("https://example.com/anim.gif")
        assert result == "image/gif"

    def test_webp_extension(self, claude_adapter):
        """Test .webp extension."""
        result = claude_adapter._infer_media_type_from_url("https://example.com/modern.webp")
        assert result == "image/webp"

    def test_url_with_query_params(self, claude_adapter):
        """Test URL with query parameters."""
        result = claude_adapter._infer_media_type_from_url(
            "https://example.com/photo.png?size=large&quality=high"
        )
        assert result == "image/png"

    def test_unknown_extension_defaults_to_jpeg(self, claude_adapter):
        """Test unknown extension defaults to image/jpeg."""
        result = claude_adapter._infer_media_type_from_url("https://example.com/image")
        assert result == "image/jpeg"

    def test_uppercase_extension(self, claude_adapter):
        """Test uppercase extension is handled."""
        result = claude_adapter._infer_media_type_from_url("https://example.com/PHOTO.JPG")
        assert result == "image/jpeg"
