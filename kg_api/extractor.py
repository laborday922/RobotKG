from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ExtractedGraph:
    entities: set[str]
    relations: list[tuple[str, str, str]]


@dataclass(frozen=True)
class ExtractedSection:
    title: str
    key: str
    text: str


@dataclass(frozen=True)
class ExtractedDocument:
    graph: ExtractedGraph
    structured: dict[str, Any]


class LLMExtractor(Protocol):
    def coarse_structure(self, *, file_name: str, content: str) -> list[ExtractedSection] | None: ...

    def fine_values(
        self, *, file_name: str, section_key: str, section_title: str, section_text: str
    ) -> dict[str, Any] | None: ...


COARSE_STRUCTURE_PROMPT: str = (
    "从文件内容中识别章节结构。输出一个章节数组，每个元素包含 title(原始章节名), key(规范字段key), text(该章节正文)。"
    "章节通常以“标题：”或“标题:”开头。key 优先映射到既定字段集合："
    "service_item, service_content, service_object, policy_basis, materials, process, channels, handling_method, time_limit, delivery, fee, working_time, organization, address, contact, complaint。"
)

FINE_VALUE_PROMPTS: dict[str, str] = {
    "service_item": "提取服务事项名称，输出 {service_item: string}。",
    "service_content": "提取服务内容，输出 {service_content: string}。",
    "service_object": "提取服务对象/适用人群，输出 {service_object: string}。",
    "policy_basis": "提取政策依据条目列表，输出 {policy_basis: [string]}，每项尽量为法规/文件名称，不要包含序号。",
    "materials": "提取申请/所需材料清单，输出 {materials: [string]}，每项为一条材料描述，不要包含序号。",
    "process": "提取办理流程步骤列表，输出 {process: [string]}，每项为一步，不要包含序号。",
    "channels": "提取咨询渠道中的电话/网址等，输出 {phones: [string], urls: [string]}。",
    "handling_method": "提取办理方式，输出 {handling_method: string}。",
    "time_limit": "提取办理时限/办理期限，输出 {time_limit: string}。",
    "delivery": "提取结果送达/领取方式，输出 {delivery: string}。",
    "fee": "提取收费标准/费用，输出 {fee: string}。",
    "working_time": "提取办理时间/受理时间，输出 {working_time: string}。",
    "organization": "提取办理单位/承办机构，输出 {organization: string}。",
    "address": "提取办理地点/地址，输出 {address: string}。",
    "contact": "提取联系方式，输出 {phones: [string]}。",
    "complaint": "提取监督投诉渠道，输出 {phones: [string]}。",
}

_ENTITY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"《([^《》]{1,80})》"),
    re.compile(r"“([^“”]{1,80})”"),
    re.compile(r"\"([^\"]{1,80})\""),
    re.compile(r"\b([A-Z][A-Za-z0-9_]{1,80})\b"),
]

_RELATION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?P<a>[\u4e00-\u9fffA-Za-z0-9_]{2,40})\s*是\s*(?P<b>[\u4e00-\u9fffA-Za-z0-9_]{2,40})"), "IS_A"),
    (re.compile(r"(?P<a>[\u4e00-\u9fffA-Za-z0-9_]{2,40})\s*属于\s*(?P<b>[\u4e00-\u9fffA-Za-z0-9_]{2,40})"), "BELONGS_TO"),
    (re.compile(r"(?P<a>[\u4e00-\u9fffA-Za-z0-9_]{2,40})\s*包含\s*(?P<b>[\u4e00-\u9fffA-Za-z0-9_]{2,40})"), "CONTAINS"),
    (re.compile(r"(?P<a>[\u4e00-\u9fffA-Za-z0-9_]{2,40})\s*位于\s*(?P<b>[\u4e00-\u9fffA-Za-z0-9_]{2,40})"), "LOCATED_IN"),
    (re.compile(r"(?P<a>[\u4e00-\u9fffA-Za-z0-9_]{2,40})\s*[-=]?>\s*(?P<b>[\u4e00-\u9fffA-Za-z0-9_]{2,40})"), "RELATED_TO"),
]


def _normalize_entity(text: str) -> str:
    text = text.strip()
    text = re.sub(r"\s+", " ", text)
    text = text.strip(".,;:!?，。；：！？()（）[]{}<>《》“”\"'`")
    return text


def _normalize_heading(text: str) -> str:
    text = text.strip()
    text = re.sub(r"[：:\s]+", "", text)
    text = re.sub(r"[★*]+", "", text)
    text = re.sub(r"[（）()\[\]【】]", "", text)
    text = re.sub(r"[，。；;,.!?！？]", "", text)
    return text


_CANONICAL_FIELDS: dict[str, list[str]] = {
    "service_item": ["服务事项", "事项名称", "服务名称", "办事事项", "事项"],
    "service_content": ["服务内容", "事项内容", "内容"],
    "service_object": ["服务对象", "适用对象", "对象", "服务人群", "适用人群"],
    "policy_basis": ["政策依据", "依据", "政策法规", "法律依据", "政策法规依据"],
    "materials": ["申请材料", "所需材料", "材料", "材料清单", "提交材料", "申请资料", "所需资料"],
    "process": ["办理流程", "办理程序", "办理步骤", "流程", "办理过程"],
    "channels": ["咨询渠道", "咨询方式", "咨询了解社保政策的渠道", "咨询", "咨询电话", "咨询途径"],
    "handling_method": ["办理方式", "办理形式"],
    "time_limit": ["办理时限", "时限", "办理期限"],
    "delivery": ["结果送达", "送达方式", "送达", "结果领取"],
    "fee": ["收费标准", "收费", "费用标准", "是否收费", "费用"],
    "working_time": ["办理时间", "受理时间", "工作时间", "办公时间"],
    "organization": ["办理单位", "实施机构", "承办单位", "主管部门", "办理机构", "机构"],
    "address": ["办理地点", "地址", "地点", "办理地址"],
    "contact": ["联系电话", "咨询电话", "联系方式", "电话"],
    "complaint": ["监督投诉", "投诉电话", "监督电话", "投诉", "投诉举报"],
}

_SYNONYM_TO_KEY: dict[str, str] = {
    _normalize_heading(syn): key for key, syns in _CANONICAL_FIELDS.items() for syn in syns
}


def _resolve_key(key: str) -> str:
    if key in _CANONICAL_FIELDS:
        return key
    normalized = _normalize_heading(key)
    if normalized in _SYNONYM_TO_KEY:
        return _SYNONYM_TO_KEY[normalized]
    return "other"


def _map_heading_to_key(title: str, *, heading_aliases: dict[str, str] | None) -> str:
    if heading_aliases:
        if title in heading_aliases:
            return _resolve_key(str(heading_aliases[title]))
        normalized_title = _normalize_heading(title)
        for k, v in heading_aliases.items():
            if _normalize_heading(str(k)) == normalized_title:
                return _resolve_key(str(v))

    normalized = _normalize_heading(title)
    if normalized in _SYNONYM_TO_KEY:
        return _SYNONYM_TO_KEY[normalized]

    if "材料" in normalized or "资料" in normalized:
        return "materials"
    if "依据" in normalized or "法规" in normalized or "政策" in normalized or "法律" in normalized:
        return "policy_basis"
    if "流程" in normalized or "步骤" in normalized or "程序" in normalized:
        return "process"
    if "咨询" in normalized or "热线" in normalized:
        return "channels"
    if "单位" in normalized or "机构" in normalized or "部门" in normalized:
        return "organization"
    if "地点" in normalized or "地址" in normalized:
        return "address"
    if "电话" in normalized or "联系方式" in normalized:
        return "contact"
    if "投诉" in normalized or "监督" in normalized:
        return "complaint"
    if "时限" in normalized or "期限" in normalized:
        return "time_limit"
    if "方式" in normalized:
        return "handling_method"
    if "时间" in normalized:
        return "working_time"

    return "other"


def _split_lines(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u3000", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    return [ln for ln in lines if ln]


_HEADING_LINE_RE = re.compile(r"^(?P<title>[^:：]{1,40})\s*[:：]\s*(?P<rest>.*)$")


def coarse_extract_sections_regex(
    *, file_name: str, content: str, heading_aliases: dict[str, str] | None
) -> list[ExtractedSection]:
    text = (content or "").strip()
    if not text:
        return []

    lines = _split_lines(text)
    sections: list[ExtractedSection] = []
    current_title: str | None = None
    current_key: str | None = None
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_title, current_key, current_lines
        if current_title is None:
            return
        section_text = "\n".join(current_lines).strip()
        sections.append(ExtractedSection(title=current_title, key=current_key or "other", text=section_text))
        current_title = None
        current_key = None
        current_lines = []

    for ln in lines:
        m = _HEADING_LINE_RE.match(ln)
        if m:
            title = m.group("title").strip().strip("★*")
            rest = m.group("rest").strip()
            key = _map_heading_to_key(title, heading_aliases=heading_aliases)
            flush()
            current_title = title
            current_key = key
            if rest:
                current_lines.append(rest)
            continue
        if current_title is None:
            continue
        current_lines.append(ln)

    flush()
    return sections


def _extract_list_items(section_text: str) -> list[str]:
    lines = _split_lines(section_text)
    items: list[str] = []
    for ln in lines:
        ln = re.sub(r"^(?:[（(]?\d+[)）]?\s*[\.\、:]?|\d+\s*[\.\、:]|[★*]\s*)", "", ln).strip()
        if not ln:
            continue
        items.append(ln)
    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        if it in seen:
            continue
        seen.add(it)
        out.append(it)
    return out


_PHONE_RE = re.compile(r"(?:0\d{2,3}-\d{7,8})|(?:1\d{10})|(?:\b\d{5}\b)")
_URL_RE = re.compile(r"https?://[^\s）)]+")


def fine_extract_values_regex(*, file_name: str, sections: list[ExtractedSection]) -> dict[str, Any]:
    structured: dict[str, Any] = {"file_name": file_name, "sections": []}
    values: dict[str, Any] = {}

    for sec in sections:
        structured["sections"].append({"title": sec.title, "key": sec.key, "text": sec.text})

        if sec.key == "service_item":
            values["service_item"] = sec.text.strip()
        elif sec.key == "service_content":
            values["service_content"] = sec.text.strip()
        elif sec.key == "service_object":
            values["service_object"] = sec.text.strip()
        elif sec.key == "policy_basis":
            laws = [_normalize_entity(m) for m in re.findall(r"《([^《》]{1,120})》", sec.text)]
            lines = _extract_list_items(sec.text)
            extra: list[str] = []
            for ln in lines:
                titles = re.findall(r"《([^《》]{1,120})》", ln)
                if titles:
                    extra.extend([_normalize_entity(t) for t in titles])
                else:
                    extra.append(_normalize_entity(ln))
            merged = [x for x in laws + extra if x]
            uniq: list[str] = []
            seen: set[str] = set()
            for x in merged:
                if x in seen:
                    continue
                seen.add(x)
                uniq.append(x)
            values["policy_basis"] = uniq
        elif sec.key == "materials":
            values["materials"] = _extract_list_items(sec.text)
        elif sec.key == "process":
            values["process"] = _extract_list_items(sec.text)
        elif sec.key in {"channels", "contact", "complaint"}:
            phones = _PHONE_RE.findall(sec.text)
            urls = _URL_RE.findall(sec.text)
            if phones:
                values.setdefault("phones", [])
                values["phones"] = list(dict.fromkeys(values["phones"] + phones))
            if urls:
                values.setdefault("urls", [])
                values["urls"] = list(dict.fromkeys(values["urls"] + urls))
            if sec.key == "channels":
                values["channels_text"] = sec.text.strip()
        elif sec.key in {"handling_method", "time_limit", "delivery", "fee", "working_time", "organization", "address"}:
            values[sec.key] = sec.text.strip()

    structured["values"] = values
    structured["keys"] = sorted({s.key for s in sections})
    return structured


def _derive_entities_from_structured(structured: dict[str, Any]) -> set[str]:
    values = structured.get("values") or {}
    entities: set[str] = set()

    for key in ("service_item", "organization", "address"):
        v = values.get(key)
        if isinstance(v, str):
            ent = _normalize_entity(v)
            if 2 <= len(ent) <= 80:
                entities.add(ent)

    pb = values.get("policy_basis")
    if isinstance(pb, list):
        for x in pb:
            if not isinstance(x, str):
                continue
            ent = _normalize_entity(x)
            if 2 <= len(ent) <= 120:
                entities.add(ent)

    return entities


def extract_graph(file_name: str, content: str) -> ExtractedGraph:
    extracted = extract_document(file_name=file_name, content=content, llm=None, heading_aliases=None)
    return extracted.graph


def extract_document(
    *, file_name: str, content: str, llm: LLMExtractor | None, heading_aliases: dict[str, str] | None = None
) -> ExtractedDocument:
    text = (content or "").strip()
    if not text:
        return ExtractedDocument(graph=ExtractedGraph(entities=set(), relations=[]), structured={"file_name": file_name, "sections": [], "values": {}, "keys": []})

    sections = coarse_extract_sections_regex(file_name=file_name, content=text, heading_aliases=heading_aliases)
    if len(sections) < 2 and llm is not None:
        llm_sections = llm.coarse_structure(file_name=file_name, content=text)
        if llm_sections:
            sections = llm_sections

    structured = fine_extract_values_regex(file_name=file_name, sections=sections)
    if llm is not None:
        values = structured.get("values") or {}
        for sec in sections:
            if sec.key in values:
                continue
            llm_values = llm.fine_values(file_name=file_name, section_key=sec.key, section_title=sec.title, section_text=sec.text)
            if llm_values:
                values.update(llm_values)
        structured["values"] = values

    entities: set[str] = set()
    entities |= _derive_entities_from_structured(structured)

    for pattern in _ENTITY_PATTERNS:
        for match in pattern.findall(text):
            if isinstance(match, tuple):
                match = match[0]
            entity = _normalize_entity(str(match))
            if 2 <= len(entity) <= 120:
                entities.add(entity)

    relations: list[tuple[str, str, str]] = []
    for pattern, rel_type in _RELATION_PATTERNS:
        for m in pattern.finditer(text):
            a = _normalize_entity(m.group("a"))
            b = _normalize_entity(m.group("b"))
            if not a or not b or a == b:
                continue
            relations.append((a, rel_type, b))
            entities.add(a)
            entities.add(b)

    entities.discard(_normalize_entity(file_name))
    entities = {e for e in entities if e}
    return ExtractedDocument(graph=ExtractedGraph(entities=entities, relations=relations), structured=structured)
