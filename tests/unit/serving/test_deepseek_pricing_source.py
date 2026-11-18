"""Small synthetic fixtures for fail-closed external pricing parsing."""

from __future__ import annotations

import copy
import re

import pytest

from serving.pricing_sources.deepseek import (
    MissingModelError,
    parse_deepseek_page,
    parse_holiday_calendar,
)

pytestmark = pytest.mark.unit

_RULE = (
    "(2) Off-peak rates are half of the peak rates. "
    "Peak hours are 02:00 - 03:00 and 07:00 - 09:00 UTC, Monday through Friday, "
    "excluding Chinese public holidays. All other hours are off-peak, "
    "including weekends and Chinese public holidays in full."
)


def _page(*, flash_first: bool = True) -> str:
    """An authored fixture with different prices and times from the real page."""

    def columns(flash: str, pro: str) -> str:
        values = [flash, pro] if flash_first else [pro, flash]
        return "".join(f"<td>{value}</td>" for value in values)

    rows = [
        '<tr><td colspan="3">MODEL</td>'
        + columns("deepseek-flash<sup>(1)</sup>", "deepseek-v4-pro")
        + "</tr>",
        '<tr><td colspan="3">MODEL VERSION</td>'
        + columns("DeepSeek-V9-Flash-0101", "DeepSeek-V9-Pro-0101")
        + "</tr>",
        '<tr><td colspan="3">CONTEXT LENGTH</td><td colspan="2">1M</td></tr>',
    ]
    for index, (label, low, high, pro_low, pro_high) in enumerate(
        [
            ("1M INPUT TOKENS<br>(CACHE HIT)", "0.004", "0.008", "0.04", "0.08"),
            ("1M INPUT TOKENS<br>(CACHE MISS)", "0.20", "0.40", "2.00", "4.00"),
            ("1M OUTPUT TOKENS", "0.80", "1.60", "8.00", "16.00"),
        ]
    ):
        outer = '<td rowspan="6">PRICING<sup>(2)</sup></td>' if index == 0 else ""
        rows.append(
            f'<tr>{outer}<td rowspan="2">{label}</td><td>OFF-PEAK</td>'
            + columns(f"${low}", f"${pro_low}")
            + "</tr>"
        )
        rows.append("<tr><td>PEAK</td>" + columns(f"${high}", f"${pro_high}") + "</tr>")
    return '<html lang="en"><body><table>' + "".join(rows) + f"</table><p>{_RULE}</p></body></html>"


def _cny_page() -> str:
    html = _page()
    replacements = {
        'lang="en"': 'lang="zh-cn"',
        "MODEL VERSION": "模型版本",
        "MODEL": "模型",
        "PRICING": "价格",
        "1M INPUT TOKENS<br>(CACHE HIT)": "百万tokens输入<br>\uff08缓存命中\uff09",
        "1M INPUT TOKENS<br>(CACHE MISS)": "百万tokens输入<br>\uff08缓存未命中\uff09",
        "1M OUTPUT TOKENS": "百万tokens输出",
        "OFF-PEAK": "空闲时段",
        "PEAK": "高峰时段",
        _RULE: "(2) 空闲时段价格为高峰时段价格的一半。北京时间周一至周五\uff08不含中国法定节假日\uff09"
        "10:00 - 11:00、15:00 - 17:00 为高峰时段\uff1b其余时段\uff0c包括周末及中国法定节假日全天均为空闲时段。",
    }
    for before, after in replacements.items():
        html = html.replace(before, after)
    return re.sub(r"\$([0-9.]+)", r"\1元", html)


def test_cny_uses_published_amounts_and_converts_beijing_windows_to_utc() -> None:
    result = parse_deepseek_page(_cny_page(), "deepseek-flash", currency="CNY")
    assert result["default"] == {
        "prompt": "0.20",
        "completion": "0.80",
        "input_cache_reads": "0.004",
    }
    assert [(window["start"], window["end"]) for window in result["windows"]] == [
        ("02:00", "03:00"),
        ("07:00", "09:00"),
    ]


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ('lang="zh-cn"', 'lang="en"'),
        ("0.20元", "$0.20"),
        ("0.20元", "¥0.20"),
        ("百万tokens输入", "千tokens输入"),
        ("缓存命中", "缓存写入"),
        ("北京时间", "UTC时间"),
        ("周一至周五", "周一至周六"),
        ("不含中国法定节假日", "含中国法定节假日"),
        ("一半", "三分之一"),
        ("0.40元", "0.30元"),
        ("10:00 - 11:00", "7:00 - 8:00"),
        ("15:00 - 17:00", "10:00 - 12:00"),
    ],
)
def test_cny_rejects_wrong_currency_units_and_calendar_semantics(before: str, after: str) -> None:
    with pytest.raises(ValueError):
        parse_deepseek_page(_cny_page().replace(before, after), "deepseek-flash", currency="CNY")


def test_currencies_cannot_use_each_others_price_pages() -> None:
    with pytest.raises(ValueError):
        parse_deepseek_page(_cny_page(), "deepseek-flash")
    with pytest.raises(ValueError):
        parse_deepseek_page(_page(), "deepseek-flash", currency="CNY")
    with pytest.raises(ValueError):
        parse_deepseek_page(_page(), "deepseek-flash", currency="EUR")


@pytest.mark.parametrize("flash_first", [True, False])
def test_exact_model_column_survives_reordering_and_table_spans(flash_first: bool) -> None:
    result = parse_deepseek_page(_page(flash_first=flash_first), "deepseek-flash")

    assert result == {
        "model_version": "DeepSeek-V9-Flash-0101",
        "default": {"prompt": "0.20", "completion": "0.80", "input_cache_reads": "0.004"},
        "windows": [
            {
                "start": start,
                "end": end,
                "pricing": {
                    "prompt": "0.40",
                    "completion": "1.60",
                    "input_cache_reads": "0.008",
                },
            }
            for start, end in [("02:00", "03:00"), ("07:00", "09:00")]
        ],
        "weekdays": [1, 2, 3, 4, 5],
    }


def test_different_exact_model_uses_its_own_prices() -> None:
    result = parse_deepseek_page(_page(), "deepseek-v4-pro")

    assert result["model_version"] == "DeepSeek-V9-Pro-0101"
    assert result["default"]["prompt"] == "2.00"
    assert result["windows"][0]["pricing"]["input_cache_reads"] == "0.08"


@pytest.mark.parametrize("model_id", ["flash", "deepseek-v4-flash", "deepseek-flash-extra", ""])
def test_never_guesses_model_aliases_or_substrings(model_id: str) -> None:
    with pytest.raises(ValueError):
        parse_deepseek_page(_page(), model_id)


def test_rejects_missing_flash_even_when_pro_prices_are_present() -> None:
    html = _page().replace("deepseek-flash", "deepseek-other")
    with pytest.raises(MissingModelError, match="matching model column"):
        parse_deepseek_page(html, "deepseek-flash")


@pytest.mark.parametrize(
    ("before", "after"),
    [("$2.00", "$0"), ("Monday through Friday", "Monday through Sunday"), ("MODEL", "FIELD")],
)
def test_malformed_pages_do_not_report_safe_model_absence(before: str, after: str) -> None:
    html = _page().replace("deepseek-flash", "deepseek-other").replace(before, after)
    with pytest.raises(ValueError) as error:
        parse_deepseek_page(html, "deepseek-flash")
    assert not isinstance(error.value, MissingModelError)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ('lang="en"', 'lang="zh-cn"'),
        ("$0.20", "¥0.20"),
        ("$0.20", "USD 0.20"),
        ("1M INPUT TOKENS", "1K INPUT TOKENS"),
        ("1M OUTPUT TOKENS", "1M IMAGES"),
        ("CACHE HIT", "CACHE WRITE"),
        ("OFF-PEAK", "DISCOUNT"),
        ("$0.20", "$-0.20"),
        ("$0.20", "$0"),
        ("$0.004", "$0.00"),
        ("$0.20", "$NaN"),
        ("$0.20", "$Infinity"),
        ("$0.20", "$1e-2"),
        ("$0.20", "$0.20 / $0.30"),
        ("$0.20", ""),
        ("$0.40", "$0.30"),
        ("$0.40", "$0.10"),
        ("MODEL VERSION", "RELEASE"),
        ("DeepSeek-V9-Flash-0101", ""),
        ('rowspan="6"', 'rowspan="7"'),
        ('rowspan="6"', 'rowspan="0"'),
        ('colspan="3"', 'colspan="65"'),
        ("deepseek-v4-pro", "deepseek-flash"),
        ("(CACHE MISS)", "(CACHE HIT)"),
    ],
)
def test_rejects_ambiguous_changed_or_incomplete_price_tables(before: str, after: str) -> None:
    with pytest.raises(ValueError):
        parse_deepseek_page(_page().replace(before, after), "deepseek-flash")


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (" UTC,", " GMT+8,"),
        ("Monday through Friday", "Monday through Saturday"),
        ("excluding Chinese public holidays", "including Chinese public holidays"),
        ("excluding Chinese public holidays", "excluding public holidays"),
        ("half of", "one third of"),
        ("All other hours are off-peak", "Most other hours are off-peak"),
        ("02:00 - 03:00", "02:00 - 02:00"),
        ("02:00 - 03:00", "25:00 - 26:00"),
        ("02:00 - 03:00", "02:60 - 03:00"),
        ("07:00 - 09:00", "02:30 - 04:00"),
        ("07:00 - 09:00", "02:00 - 03:00"),
    ],
)
def test_rejects_unsupported_or_contradictory_peak_rules(before: str, after: str) -> None:
    with pytest.raises(ValueError):
        parse_deepseek_page(_page().replace(before, after), "deepseek-flash")


@pytest.mark.parametrize("window", ["15:00 - 17:00", "18:00 - 19:00", "23:00 - 02:00"])
def test_rejects_windows_where_china_holidays_do_not_match_utc_dates(window: str) -> None:
    with pytest.raises(ValueError, match="China calendar date"):
        parse_deepseek_page(_page().replace("07:00 - 09:00", window), "deepseek-flash")


def test_half_open_window_can_end_at_china_midnight() -> None:
    result = parse_deepseek_page(
        _page().replace("07:00 - 09:00", "14:00 - 16:00"), "deepseek-flash"
    )
    assert result["windows"][1]["end"] == "16:00"


def test_window_count_is_read_from_the_rule() -> None:
    result = parse_deepseek_page(
        _page().replace(
            "02:00 - 03:00 and 07:00 - 09:00", "00:00 - 01:00, 05:00 - 06:00 and 12:00 - 13:00"
        ),
        "deepseek-flash",
    )
    assert [(window["start"], window["end"]) for window in result["windows"]] == [
        ("00:00", "01:00"),
        ("05:00", "06:00"),
        ("12:00", "13:00"),
    ]


def test_accepts_semantic_whitespace_and_nested_inline_markup() -> None:
    html = _page().replace("$0.20", "<strong>$0.20</strong>")
    html = html.replace("02:00 - 03:00", "02:00 &ndash; 03:00")
    html = html.replace("Monday through Friday", "Monday\n through&nbsp;Friday")
    assert parse_deepseek_page(html, "deepseek-flash")["default"]["prompt"] == "0.20"


def test_ignores_script_text_and_unrelated_request_price_table() -> None:
    html = _page().replace(
        "</body>",
        '<script>"Peak hours are untrusted script text"</script>'
        "<table><tr><th>API</th><th>Price</th></tr>"
        "<tr><td>Search</td><td>$1 per request</td></tr></table></body>",
    )
    assert parse_deepseek_page(html, "deepseek-flash")["default"]["prompt"] == "0.20"


def test_duplicate_model_table_and_duplicate_peak_rules_are_rejected() -> None:
    html = _page()
    table = html[html.index("<table>") : html.index("</table>") + len("</table>")]
    with pytest.raises(ValueError, match="one model table"):
        parse_deepseek_page(html.replace("</body>", table + "</body>"), "deepseek-flash")
    with pytest.raises(ValueError, match="rules"):
        parse_deepseek_page(html.replace("</body>", f"<p>{_RULE}</p></body>"), "deepseek-flash")


@pytest.mark.parametrize("html", ["", "not an HTML price page", '<html lang="en"><table><tr>'])
def test_rejects_absent_or_truncated_pages(html: str) -> None:
    with pytest.raises(ValueError):
        parse_deepseek_page(html, "deepseek-flash")


def _calendar() -> dict:
    return {
        "year": 2027,
        "papers": ["https://www.gov.cn/zhengce/example-announcement.htm"],
        "days": [
            {"name": "Example holiday", "date": "2027-01-02", "isOffDay": True},
            {"name": "Example working day", "date": "2027-01-03", "isOffDay": False},
            {"name": "Example holiday", "date": "2027-01-01", "isOffDay": True},
            {"name": "Previous December", "date": "2026-12-31", "isOffDay": True},
        ],
    }


def test_holidays_exclude_makeup_workdays_and_allow_previous_december() -> None:
    assert parse_holiday_calendar(_calendar(), 2027) == [
        "2026-12-31",
        "2027-01-01",
        "2027-01-02",
    ]


@pytest.mark.parametrize("value", [True, "2027", 2027.0, 2026, None])
def test_rejects_mismatched_or_untyped_calendar_year(value) -> None:
    raw = _calendar()
    raw["year"] = value
    with pytest.raises(ValueError, match="year"):
        parse_holiday_calendar(raw, 2027)


@pytest.mark.parametrize("year", [True, "2027", 2027.0, 0, 1, 10000])
def test_rejects_invalid_requested_year(year) -> None:
    with pytest.raises(ValueError, match="year"):
        parse_holiday_calendar(_calendar(), year)


@pytest.mark.parametrize(
    "papers",
    [
        [],
        None,
        "https://www.gov.cn/announcement.htm",
        [True],
        ["https://example.com/announcement.htm"],
        ["https://gov.cn.example.com/announcement.htm"],
        ["https://www.gov.cn@evil.example/announcement.htm"],
        ["https://user@www.gov.cn/announcement.htm"],
        ["https://www.gov.cn:443/announcement.htm"],
        ["ftp://www.gov.cn/announcement.htm"],
        ["https://www.gov.cn/"],
        [" https://www.gov.cn/announcement.htm"],
        ["https://www.gov.cn/a.htm", "https://www.gov.cn/a.htm"],
    ],
)
def test_rejects_missing_or_non_government_calendar_citations(papers) -> None:
    raw = _calendar()
    raw["papers"] = papers
    with pytest.raises(ValueError):
        parse_holiday_calendar(raw, 2027)


@pytest.mark.parametrize(
    "date",
    [
        "20270101",
        "2027-1-1",
        "2027-02-30",
        "2027-01-01T00:00:00Z",
        "2026-11-30",
        "2028-01-01",
        True,
    ],
)
def test_rejects_invalid_dates_and_dates_outside_year_coverage(date) -> None:
    raw = _calendar()
    raw["days"][0]["date"] = date
    with pytest.raises(ValueError):
        parse_holiday_calendar(raw, 2027)


@pytest.mark.parametrize("value", [None, "true", "false", 0, 1])
def test_calendar_off_day_flag_must_be_boolean(value) -> None:
    raw = _calendar()
    raw["days"][0]["isOffDay"] = value
    with pytest.raises(ValueError, match="boolean"):
        parse_holiday_calendar(raw, 2027)


def test_rejects_conflicting_duplicate_calendar_dates() -> None:
    raw = _calendar()
    duplicate = copy.deepcopy(raw["days"][0])
    duplicate["isOffDay"] = False
    raw["days"].append(duplicate)
    with pytest.raises(ValueError, match="duplicate dates"):
        parse_holiday_calendar(raw, 2027)


@pytest.mark.parametrize("days", [[], None, "days", [None], [{"date": "2027-01-01"}]])
def test_rejects_incomplete_calendar_days(days) -> None:
    raw = _calendar()
    raw["days"] = days
    with pytest.raises(ValueError):
        parse_holiday_calendar(raw, 2027)


def test_rejects_calendar_without_current_year_holidays() -> None:
    raw = _calendar()
    for day in raw["days"]:
        if day["date"].startswith("2027"):
            day["isOffDay"] = False
    with pytest.raises(ValueError, match="no holidays"):
        parse_holiday_calendar(raw, 2027)
