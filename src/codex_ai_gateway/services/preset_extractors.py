"""预设官方文档模型提取器。"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from pydantic import BaseModel, Field


class PresetExtractionError(RuntimeError):
    def __init__(self, code: str, message: str, *, evidence: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.evidence = evidence or {}


class ExtractionResult(BaseModel):
    model_ids: list[str] = Field(min_length=1)
    evidence: dict[str, Any] = Field(default_factory=dict)
    metadata_by_model: dict[str, dict[str, Any]] = Field(default_factory=dict)


@dataclass(frozen=True)
class PresetExtractor:
    key: str
    version: str
    extract: Callable[[str], ExtractionResult]


_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/@-]{1,127}$")


def _clean_model_id(value: str) -> str | None:
    value = html.unescape(value).strip().strip("`*_[](){}")
    value = re.sub(r"\s+", " ", value)
    if " " in value or not any(character.isalpha() for character in value):
        return None
    return value if _MODEL_ID_RE.fullmatch(value) else None


def _unique_model_ids(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        model_id = _clean_model_id(value)
        if model_id and model_id.lower() not in seen:
            seen.add(model_id.lower())
            result.append(model_id)
    return result


_DOMAIN_LIKE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9.-]*\.(com|org|net|io|ai|cn|dev|app|gov|edu|co|uk|jp|de)(/|$)", re.I,
)
_PURE_WORD_PATH_RE = re.compile(r"^[A-Za-z]+/[A-Za-z]+$")


def _model_ids_from_text(value: str) -> list[str]:
    candidates = re.findall(
        r"(?<![A-Za-z0-9])(?:[A-Za-z0-9]+[._:+/@-]){1,}[A-Za-z0-9]+(?![A-Za-z0-9])",
        value,
    )
    return _unique_model_ids(
        candidate
        for candidate in candidates
        if "://" not in candidate
        and not candidate.lower().startswith(("http", "www"))
        and not _DOMAIN_LIKE_RE.match(candidate)
        and not _PURE_WORD_PATH_RE.match(candidate)
    )


def _find_cur_doc_content(value: Any, *, inside_cur_doc: bool = False) -> Any:
    if isinstance(value, dict):
        if inside_cur_doc and "Content" in value:
            return value["Content"]
        for key, child in value.items():
            if str(key).lower() == "curdoc" and isinstance(child, str):
                return child
            result = _find_cur_doc_content(
                child,
                inside_cur_doc=inside_cur_doc or str(key).lower() == "curdoc",
            )
            if result is not None:
                return result
    if isinstance(value, list):
        for child in value:
            result = _find_cur_doc_content(child, inside_cur_doc=inside_cur_doc)
            if result is not None:
                return result
    return None


def _decode_content(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise PresetExtractionError("extractor_invalid_payload", "官方文档内嵌模型数据不是合法 JSON") from exc
    return value


def _parse_embedded_json(text: str) -> Any:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if parsed is not None:
        content = _find_cur_doc_content(parsed)
        if content is not None:
            return _decode_content(content)

    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\"']?(?:curDoc|cur_doc)[\"']?\s*[:=]\s*", text):
        start = match.end()
        while start < len(text) and text[start].isspace():
            start += 1
        try:
            parsed, _end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        content = _find_cur_doc_content({"curDoc": parsed}, inside_cur_doc=False)
        if content is not None:
            return _decode_content(content)

    match = re.search(r"curDoc\.Content\s*[:=]\s*([\"'])(.*?)\1", text, re.S)
    if match:
        raw_content = match.group(2)
        if match.group(1) == '"':
            try:
                raw_content = json.loads(f'"{raw_content}"')
            except json.JSONDecodeError as exc:
                raise PresetExtractionError("extractor_invalid_payload", "官方文档内嵌模型数据不是合法 JSON") from exc
        return _decode_content(html.unescape(raw_content))
    raise PresetExtractionError("extractor_structure_changed", "未找到官方文档内嵌模型数据")


def extract_volcengine_quill_delta(document: str) -> ExtractionResult:
    content = _parse_embedded_json(document)
    if not isinstance(content, dict) or not isinstance(content.get("ops"), list):
        raise PresetExtractionError("extractor_invalid_payload", "官方文档 Quill Delta 结构无效")
    values: list[str] = []
    for operation in content["ops"]:
        if not isinstance(operation, dict):
            continue
        insert = operation.get("insert")
        if isinstance(insert, str):
            values.extend(_model_ids_from_text(insert))
        elif isinstance(insert, dict):
            values.extend(_model_ids_from_text(json.dumps(insert, ensure_ascii=False)))
    model_ids = _unique_model_ids(values)
    if not model_ids:
        raise PresetExtractionError("extractor_no_models", "官方文档未提取到合法模型列表")
    return ExtractionResult(model_ids=model_ids, evidence={"format": "quill_delta", "operation_count": len(content["ops"])})


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_table = False
        self.in_row = False
        self.in_cell = False
        self.current_cell: list[str] = []
        self.current_row: list[str] = []
        self.tables: list[list[list[str]]] = []
        self.current_table: list[list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "table" and not self.in_table:
            self.in_table = True
            self.current_table = []
        elif self.in_table and tag == "tr":
            self.in_row = True
            self.current_row = []
        elif self.in_row and tag in {"td", "th"}:
            self.in_cell = True
            self.current_cell = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self.in_cell:
            self.current_row.append(" ".join("".join(self.current_cell).split()))
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            if self.current_table is not None and self.current_row:
                self.current_table.append(self.current_row)
            self.in_row = False
        elif tag == "table" and self.in_table:
            if self.current_table is not None:
                self.tables.append(self.current_table)
            self.current_table = None
            self.in_table = False

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.current_cell.append(data)



def extract_volcengine_doc_api(document: str) -> ExtractionResult:
    """Parse volcengine docs API getDocDetail response (Result.Content JSON string)."""
    try:
        response = json.loads(document)
    except json.JSONDecodeError as exc:
        raise PresetExtractionError("extractor_invalid_payload", "官方文档 API 响应不是合法 JSON") from exc
    result = response.get("Result") if isinstance(response, dict) else None
    content_str = result.get("Content") if isinstance(result, dict) else None
    if not isinstance(content_str, str):
        raise PresetExtractionError("extractor_structure_changed", "官方文档 API 响应缺少 Content 字段")
    try:
        content = json.loads(content_str)
    except json.JSONDecodeError as exc:
        raise PresetExtractionError("extractor_invalid_payload", "官方文档 Content 不是合法 JSON") from exc
    data = content.get("data") if isinstance(content, dict) else None
    if not isinstance(data, dict):
        raise PresetExtractionError("extractor_structure_changed", "官方文档 Content 缺少 data 字段")
    values: list[str] = []
    op_count = 0
    for block in data.values():
        if not isinstance(block, dict) or not isinstance(block.get("ops"), list):
            continue
        for operation in block["ops"]:
            if not isinstance(operation, dict):
                continue
            op_count += 1
            insert = operation.get("insert")
            if isinstance(insert, str):
                values.extend(_model_ids_from_text(insert))
            elif isinstance(insert, dict):
                values.extend(_model_ids_from_text(json.dumps(insert, ensure_ascii=False)))
    model_ids = _unique_model_ids(values)
    if not model_ids:
        raise PresetExtractionError("extractor_no_models", "官方文档 API 未提取到合法模型列表")
    return ExtractionResult(model_ids=model_ids, evidence={"format": "doc_api_quill_delta", "operation_count": op_count})
def extract_opencode_html_table(document: str) -> ExtractionResult:
    parser = _TableParser()
    parser.feed(document)
    parser.close()
    for table in parser.tables:
        if not table:
            continue
        header = [cell.lower() for cell in table[0]]
        model_columns = [
            index
            for index, cell in enumerate(header)
            if "model id" in cell or "模型 id" in cell
        ]
        if not model_columns:
            continue
        values = [row[model_columns[0]] for row in table[1:] if len(row) > model_columns[0]]
        model_ids = _unique_model_ids(values)
        if model_ids:
            return ExtractionResult(model_ids=model_ids, evidence={"format": "html_table", "row_count": len(table) - 1})
    raise PresetExtractionError("extractor_no_models", "官方文档未找到包含模型列表的表格")


EXTRACTOR_REGISTRY: dict[str, PresetExtractor] = {
    "volcengine_quill_delta": PresetExtractor(
        key="volcengine_quill_delta",
        version="volcengine-quill-delta-v1",
        extract=extract_volcengine_quill_delta,
    ),
    "volcengine_doc_api_v1": PresetExtractor(
        key="volcengine_doc_api_v1",
        version="volcengine-doc-api-v1",
        extract=extract_volcengine_doc_api,
    ),
    "opencode_html_table": PresetExtractor(
        key="opencode_html_table",
        version="opencode-docs-table-v1",
        extract=extract_opencode_html_table,
    ),
}


def register_extractor(extractor: PresetExtractor) -> None:
    if extractor.key in EXTRACTOR_REGISTRY:
        raise ValueError(f"预设提取器已注册：{extractor.key}")
    EXTRACTOR_REGISTRY[extractor.key] = extractor


def get_extractor(key: str) -> PresetExtractor:
    try:
        return EXTRACTOR_REGISTRY[key]
    except KeyError as exc:
        raise PresetExtractionError("extractor_not_registered", f"预设提取器未注册：{key}") from exc
