"""Language detection used by the public usage stats."""

from __future__ import annotations

from serving.analytics.language_id import Detection, detect_message, user_languages


def test_detects_plain_prose():
    found = detect_message(
        "Could you explain how the routing layer picks a provider for each request?"
    )
    assert found is not None
    assert found.language == "en"
    assert found.share == 1.0


def test_detects_cjk_and_cyrillic():
    zh = detect_message("请帮我检查一下这个函数为什么在高并发的时候会返回错误的结果")
    ru = detect_message(
        "Пожалуйста, проверь почему этот сервис падает после перезапуска контейнера."
    )
    assert zh is not None and zh.language == "zh"
    assert ru is not None and ru.language == "ru"


def test_code_and_markup_do_not_vote():
    text = (
        "<system-reminder>You are in auto mode. Follow the repository rules.</system-reminder>\n"
        "```python\ndef handler(request):\n    return request.json()\n```\n"
        "Por favor, revisa por qué esta función devuelve un error cuando la lista está vacía."
    )
    found = detect_message(text)
    assert found is not None
    assert found.language == "es"


def test_majority_by_length_in_a_mixed_message():
    text = (
        "Summarize the conversation.\n"
        "Vấn đề rõ ràng là máy chủ không có quyền ghi vào thư mục này, nên cần cấp quyền trước. "
        "Tôi sẽ tạo một bản vá và một tập lệnh cài đặt để anh chạy một lần là xong."
    )
    found = detect_message(text)
    assert found is not None
    assert found.language == "vi"


def test_too_little_prose_is_unclassified():
    assert detect_message("ok") is None
    assert detect_message("`npm test` ./src/index.ts") is None
    assert detect_message(None) is None


def test_a_language_needs_two_messages():
    one = Detection("de", 1.0, 30)
    assert user_languages([Detection("en", 1.0, 30), Detection("en", 0.9, 30), one]) == {"en"}
    assert user_languages([one, Detection("de", 0.8, 50)]) == {"de"}


def test_a_lone_long_unambiguous_message_counts():
    assert user_languages([Detection("lt", 0.95, 60), None]) == {"lt"}
    assert user_languages([Detection("lt", 0.95, 30)]) == set()
    assert user_languages([Detection("lt", 0.8, 60)]) == set()


def test_ambiguous_messages_are_ignored():
    assert user_languages([Detection("pl", 0.6, 80), Detection("pl", 0.65, 80)]) == set()
