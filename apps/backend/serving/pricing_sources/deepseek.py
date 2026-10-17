"""Parse the official English DeepSeek price table without guessing changed rules.

The caller must fetch the fixed official English pricing URL: that page denotes
USD with ``$``. This parser requires that marker and explicit per-million-token
row labels, and never follows links or fetches data itself. Calendar data comes
from holiday-cn's annual JSON; its government citations are validated here, not
independently fetched or authenticated.

The current scheduler applies calendar exclusions to UTC dates. Only peak
windows whose UTC and China dates agree are representable safely; a changed
window crossing China's midnight is rejected pending explicit engine support.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

_MAX_ROWS = 256
_MAX_COLUMNS = 64
_MODEL_ID = re.compile(r"deepseek-[a-z0-9][a-z0-9.-]*")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_PRICE = re.compile(r"\$([0-9]+(?:\.[0-9]+)?)")
_TIME_RANGE = r"[0-9]{2}:[0-9]{2}\s*-\s*[0-9]{2}:[0-9]{2}"
_PEAK_RULE = re.compile(
    r"(?:\([0-9]+\)\s*)?"
    r"(?P<half>Off-peak rates are half of the peak rates\.\s*)?"
    r"Peak hours are (?P<windows>"
    + _TIME_RANGE
    + r"(?:(?:\s+and\s+|,\s*(?:and\s+)?)"
    + _TIME_RANGE
    + r")*) UTC, Monday through Friday, excluding Chinese public holidays\. "
    r"All other hours are off-peak, including weekends and Chinese public holidays in full\.",
    re.IGNORECASE,
)
_RATE_LABELS = {
    "1M INPUT TOKENS (CACHE HIT)": "input_cache_reads",
    "1M INPUT TOKENS (CACHE MISS)": "prompt",
    "1M OUTPUT TOKENS": "completion",
}


class MissingModelError(ValueError):
    """An otherwise valid official price table lacks the requested model."""


def _text(value: str) -> str:
    return " ".join(value.replace("\u2013", "-").replace("\u2014", "-").split())


@dataclass
class _Cell:
    rowspan: int
    colspan: int
    parts: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return _text("".join(self.parts))


class _PriceDocument(HTMLParser):
    """Read table cells and prose, ignoring scripts and superscript footnotes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.languages: list[str | None] = []
        self.tables: list[list[list[_Cell]]] = []
        self.paragraphs: list[str] = []
        self._table: list[list[_Cell]] | None = None
        self._row: list[_Cell] | None = None
        self._cell: _Cell | None = None
        self._paragraph: list[str] | None = None
        self._ignored: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "template", "sup"}:
            self._ignored.append(tag)
        if self._ignored:
            return
        values = dict(attrs)
        if tag == "html":
            self.languages.append(values.get("lang"))
        elif tag == "table":
            if self._table is not None:
                raise ValueError("DeepSeek pricing contains nested tables")
            self._table = []
        elif tag == "tr" and self._table is not None:
            if self._row is not None:
                raise ValueError("DeepSeek pricing contains an unclosed table row")
            self._row = []
        elif tag in {"td", "th"} and self._table is not None:
            if self._row is None or self._cell is not None:
                raise ValueError("DeepSeek pricing contains malformed table cells")
            spans = []
            for name, maximum in (("rowspan", _MAX_ROWS), ("colspan", _MAX_COLUMNS)):
                raw = values.get(name, "1")
                if raw is None or len(raw) > 3 or not re.fullmatch(r"[1-9][0-9]*", raw):
                    raise ValueError("DeepSeek pricing contains an invalid table span")
                value = int(raw)
                if value > maximum:
                    raise ValueError("DeepSeek pricing table span exceeds the limit")
                spans.append(value)
            self._cell = _Cell(*spans)
        elif tag == "p":
            if self._paragraph is not None:
                raise ValueError("DeepSeek pricing contains nested paragraphs")
            self._paragraph = []
        elif tag == "br":
            self.handle_data(" ")

    def handle_endtag(self, tag: str) -> None:
        if self._ignored:
            if tag == self._ignored[-1]:
                self._ignored.pop()
            return
        if tag in {"td", "th"} and self._table is not None:
            if self._cell is None or self._row is None:
                raise ValueError("DeepSeek pricing contains an unexpected cell end")
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._table is not None:
            if self._row is None or self._cell is not None:
                raise ValueError("DeepSeek pricing contains an incomplete table row")
            self._table.append(self._row)
            self._row = None
        elif tag == "table":
            if self._table is None or self._row is not None or self._cell is not None:
                raise ValueError("DeepSeek pricing contains an incomplete table")
            self.tables.append(self._table)
            self._table = None
        elif tag == "p" and self._paragraph is not None:
            self.paragraphs.append(_text("".join(self._paragraph)))
            self._paragraph = None

    def handle_data(self, data: str) -> None:
        if self._ignored:
            return
        if self._cell is not None:
            self._cell.parts.append(data)
        if self._paragraph is not None:
            self._paragraph.append(data)

    def finish(self) -> None:
        self.close()
        if self._table is not None or self._paragraph is not None or self._ignored:
            raise ValueError("DeepSeek pricing document is incomplete")
        if self.languages != ["en"]:
            raise ValueError("DeepSeek pricing requires the official English USD page")


def _expand_table(rows: list[list[_Cell]]) -> list[list[_Cell]]:
    if not rows or len(rows) > _MAX_ROWS:
        raise ValueError("DeepSeek pricing table has an invalid row count")
    occupied: dict[tuple[int, int], _Cell] = {}
    width = 0
    for row_index, row in enumerate(rows):
        column = 0
        for cell in row:
            while (row_index, column) in occupied:
                column += 1
            if row_index + cell.rowspan > len(rows) or column + cell.colspan > _MAX_COLUMNS:
                raise ValueError("DeepSeek pricing table span exceeds its dimensions")
            for y in range(row_index, row_index + cell.rowspan):
                for x in range(column, column + cell.colspan):
                    if (y, x) in occupied:
                        raise ValueError("DeepSeek pricing table has overlapping spans")
                    occupied[y, x] = cell
            column += cell.colspan
            width = max(width, column)
    if not width or len(occupied) != len(rows) * width:
        raise ValueError("DeepSeek pricing table has missing or misaligned cells")
    return [[occupied[y, x] for x in range(width)] for y in range(len(rows))]


def _labels(cells: list[_Cell]) -> list[str]:
    result: list[str] = []
    previous: _Cell | None = None
    for cell in cells:
        if cell is not previous:
            result.append(cell.text.upper())
        previous = cell
    return result


def _parse_windows(paragraphs: list[str]) -> tuple[list[tuple[str, str]], bool]:
    rules = [text for text in paragraphs if "peak hours" in text.lower()]
    if len(rules) != 1 or (match := _PEAK_RULE.fullmatch(rules[0])) is None:
        raise ValueError("DeepSeek peak rules are missing, duplicated, or unsupported")
    windows: list[tuple[str, str]] = []
    occupied: set[int] = set()
    china_offset_minutes = 8 * 60
    for raw in re.findall(_TIME_RANGE, match["windows"]):
        start, end = (part.strip() for part in raw.split("-"))
        start_time, end_time = (dt.time.fromisoformat(value) for value in (start, end))
        start_minute = start_time.hour * 60 + start_time.minute
        end_minute = end_time.hour * 60 + end_time.minute
        if not start_minute < end_minute <= 24 * 60 - china_offset_minutes:
            raise ValueError(
                "DeepSeek peak window crosses a China calendar date; "
                "UTC-only holiday exclusions cannot represent this rule"
            )
        minutes = set(range(start_minute, end_minute))
        if occupied.intersection(minutes):
            raise ValueError("DeepSeek peak windows overlap or repeat")
        occupied.update(minutes)
        windows.append((start, end))
    return sorted(windows), match["half"] is not None


def _parse_price(text: str) -> str:
    match = _PRICE.fullmatch(text)
    if match is None or len(match[1]) > 64 or Decimal(match[1]) <= 0:
        raise ValueError("DeepSeek price must be a positive USD amount per 1M tokens")
    return match[1]


def parse_deepseek_page(html: str, model_id: str) -> dict[str, Any]:
    """Return validated model version, default rates, windows, and ISO weekdays.

    ``model_id`` must exactly name an API model column; aliases and substrings
    are never inferred. Unknown markup, units, prices, or calendar prose raise
    ``ValueError`` so the caller can retain its last verified snapshot.
    """
    if not isinstance(html, str) or not html or len(html) > 2_000_000:
        raise ValueError("DeepSeek pricing HTML is missing or exceeds the size limit")
    if not isinstance(model_id, str) or _MODEL_ID.fullmatch(model_id) is None:
        raise ValueError("DeepSeek pricing model_id must be an exact API model name")
    document = _PriceDocument()
    document.feed(html)
    document.finish()
    model_tables: list[tuple[list[list[_Cell]], int, list[str]]] = []
    for raw_table in document.tables:
        table = _expand_table(raw_table)
        for row in table:
            if row[0].text.upper() != "MODEL":
                continue
            label_count = 0
            while label_count < len(row) and row[label_count] is row[0]:
                label_count += 1
            names = [cell.text for cell in row[label_count:]]
            if not names or any(_MODEL_ID.fullmatch(name) is None for name in names):
                raise ValueError("DeepSeek pricing model column names are unsupported")
            if len(names) != len(set(names)):
                raise ValueError("DeepSeek pricing model columns are duplicated")
            model_tables.append((table, label_count, names))
    if len(model_tables) != 1:
        raise ValueError("DeepSeek pricing must contain exactly one model table")
    table, label_count, names = model_tables[0]
    if model_id not in names:
        # Absence is safe to distinguish only after verifying the rest of the
        # table. A truncated or changed page must reject the whole refresh.
        for index in range(len(names)):
            _parse_model_column(table, label_count + index, label_count, document.paragraphs)
        raise MissingModelError(f"DeepSeek pricing has no matching model column for {model_id}")
    return _parse_model_column(
        table, label_count + names.index(model_id), label_count, document.paragraphs
    )


def _parse_model_column(
    table: list[list[_Cell]], model_column: int, label_count: int, paragraphs: list[str]
) -> dict[str, Any]:
    versions: list[str] = []
    rates: dict[str, dict[str, str]] = {"OFF-PEAK": {}, "PEAK": {}}
    for row in table:
        labels = _labels(row[:label_count])
        if labels == ["MODEL VERSION"]:
            versions.append(row[model_column].text)
        elif labels and labels[0] == "PRICING":
            if len(labels) != 3 or labels[1] not in _RATE_LABELS or labels[2] not in rates:
                raise ValueError("DeepSeek pricing row has unsupported units or rate labels")
            key = _RATE_LABELS[labels[1]]
            bucket = rates[labels[2]]
            if key in bucket:
                raise ValueError("DeepSeek pricing contains a duplicate rate")
            bucket[key] = _parse_price(row[model_column].text)
    if (
        len(versions) != 1
        or re.fullmatch(r"DeepSeek-[A-Za-z0-9][A-Za-z0-9.-]*", versions[0]) is None
    ):
        raise ValueError("DeepSeek model version is missing, duplicated, or unsupported")
    expected_keys = set(_RATE_LABELS.values())
    if any(set(bucket) != expected_keys for bucket in rates.values()):
        raise ValueError("DeepSeek pricing is incomplete")
    windows, half_rate = _parse_windows(paragraphs)
    for key, low in rates["OFF-PEAK"].items():
        high = Decimal(rates["PEAK"][key])
        if high <= Decimal(low) or (half_rate and high != 2 * Decimal(low)):
            raise ValueError("DeepSeek peak and off-peak amounts contradict the published rule")
    return {
        "model_version": versions[0],
        "default": rates["OFF-PEAK"],
        "windows": [
            {"start": start, "end": end, "pricing": dict(rates["PEAK"])} for start, end in windows
        ],
        "weekdays": [1, 2, 3, 4, 5],
    }


def parse_holiday_calendar(raw: dict[str, Any], year: int) -> list[str]:
    """Validate holiday-cn's annual calendar and return only actual days off.

    Make-up working days are validated but are not exclusions. Annual files may
    include the previous December when a New Year holiday straddles years.
    """
    if type(year) is not int or not 2 <= year <= 9999:
        raise ValueError("Holiday calendar year must be an integer between 2 and 9999")
    if not isinstance(raw, dict) or type(raw.get("year")) is not int or raw["year"] != year:
        raise ValueError("Holiday calendar year does not match the requested year")
    papers = raw.get("papers")
    if not isinstance(papers, list) or not papers or len(papers) > 100:
        raise ValueError("Holiday calendar must cite government papers")
    seen_papers: set[str] = set()
    for paper in papers:
        if (
            not isinstance(paper, str)
            or not paper
            or any(character.isspace() for character in paper)
            or paper in seen_papers
        ):
            raise ValueError("Holiday calendar contains an invalid or duplicate paper")
        url = urlsplit(paper)
        hostname = url.hostname or ""
        if (
            url.scheme not in {"http", "https"}
            or not (hostname == "gov.cn" or hostname.endswith(".gov.cn"))
            or url.username is not None
            or url.password is not None
            or url.port is not None
            or url.path in {"", "/"}
        ):
            raise ValueError("Holiday calendar papers must link to a gov.cn announcement")
        seen_papers.add(paper)
    days = raw.get("days")
    if not isinstance(days, list) or not days or len(days) > 400:
        raise ValueError("Holiday calendar days must be a non-empty annual list")
    seen_dates: set[str] = set()
    holidays: list[str] = []
    for day in days:
        if not isinstance(day, dict):
            raise ValueError("Holiday calendar day must be an object")
        text = day.get("date")
        if not isinstance(text, str) or _DATE.fullmatch(text) is None:
            raise ValueError("Holiday calendar date must use YYYY-MM-DD")
        date = dt.date.fromisoformat(text)
        if not (date.year == year or (date.year == year - 1 and date.month == 12)):
            raise ValueError("Holiday calendar date lies outside its annual coverage")
        if text in seen_dates:
            raise ValueError("Holiday calendar contains duplicate dates")
        if type(day.get("isOffDay")) is not bool:
            raise ValueError("Holiday calendar isOffDay must be a boolean")
        if not isinstance(day.get("name"), str) or not day["name"].strip():
            raise ValueError("Holiday calendar day must have a name")
        seen_dates.add(text)
        if day["isOffDay"]:
            holidays.append(text)
    if not any(text.startswith(f"{year:04d}-") for text in holidays):
        raise ValueError("Holiday calendar contains no holidays for its year")
    return sorted(holidays)
