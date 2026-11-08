"""页面展示适配：不改查询结果、计数、原始证据或会话条件。"""
from __future__ import annotations

import re
from copy import deepcopy

CATEGORY_NOTE = "大类包含关系只说明原资料列出的专业组成，不等于分流保证或各省均有招生；具体招生另查当年计划。"
CATEGORY_FIELDS = {"大类所含专业", "所属专业大类"}
COURSE_FIELDS = {"核心课程", "特色课程"}
CJK = r"\u3400-\u9fff"


def display_major_text(value: str, *, field: str | None = None) -> str:
    """只清理排版；课程列表保留分隔，英文词间仍保留空格。"""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ").replace("\u3000", " ").strip()
    if field in COURSE_FIELDS:
        # 当前可读课程表以空白分隔中文课程；含英文的字段保守保留词间空格。
        if not re.search(r"[A-Za-z]", text):
            return "、".join(text.split())
        return re.sub(r"\s+", " ", text)
    paragraphs = re.split(r"\n[ \t]*\n", text)
    cleaned = []
    for paragraph in paragraphs:
        paragraph = re.sub(rf"(?<=[{CJK}])[ \t]*\n[ \t]*(?=[{CJK}])", "", paragraph)
        paragraph = re.sub(r"\s*\n\s*", " ", paragraph)
        paragraph = re.sub(r"[ \t]+", " ", paragraph)
        paragraph = re.sub(rf"(?<=[{CJK}]) +(?=[，。；：、！？）,])", "", paragraph)
        paragraph = re.sub(rf"(?<=[，。；：、！？（]) +(?=[{CJK}])", "", paragraph)
        paragraph = re.sub(rf"(?<=\d) +(?=[{CJK}])|(?<=[{CJK}]) +(?=\d)", "", paragraph)
        cleaned.append(paragraph.strip())
    return "\n\n".join(p for p in cleaned if p)


def display_major_table(rows: list[dict]) -> list[dict]:
    """返回展示副本，来源原句与业务原字段仍由调用方保留。"""
    table = deepcopy(rows)
    for row in table:
        field = row.get("信息维度") or row.get("对照维度")
        for key, value in row.items():
            if isinstance(value, str) and key not in {"信息维度", "对照维度"}:
                row[key] = display_major_text(value, field=field)
    return table


def _shows_category_relation(rows: list[dict]) -> bool:
    for row in rows:
        dimension = row.get("信息维度") or row.get("对照维度")
        if dimension in CATEGORY_FIELDS and any(str(value).strip() if isinstance(value, str) else value for key, value in row.items() if key not in {"信息维度", "对照维度"}):
            return True
    return False


def display_answer(result: dict) -> str:
    text = str(result.get("answer") or "")
    intent = (result.get("conditions") or {}).get("intent")
    if intent == "major_info":
        text = text.replace("（大类包含关系以已收录分省计划为据）", "")
        if not _shows_category_relation(result.get("structured_table") or []):
            text = text.replace(CATEGORY_NOTE, "")
        else:
            # 固定说明只保留一次；不对任意官方原句去重。
            first = text.find(CATEGORY_NOTE)
            if first >= 0:
                text = text[:first + len(CATEGORY_NOTE)] + text[first + len(CATEGORY_NOTE):].replace(CATEGORY_NOTE, "")
        return display_major_text(text)
    if intent != "candidates" or result.get("reason") != "verified_candidate_screening":
        return text
    summary = result.get("candidate_summary") or {}
    if "选科符合专业数" not in summary:
        return text
    number = summary["选科符合专业数"]
    target = (result.get("conditions") or {}).get("candidate_target")
    parts = [f"仅查询：{target}。" if target else "按当年招生计划与完整选科完成候选筛选。"]
    parts.append(f"符合本次计划与选科要求的专业/大类：{number}个。" if number else "本次未筛选出可确认符合计划与选科要求的候选。")
    # 指定专业的逐年结论保留原句，不能因精简规则丢失历史缺口或位次关系。
    if target:
        yearly = [line for line in text.splitlines() if line.lstrip().startswith("- ")]
        if yearly:
            parts.append("\n".join(yearly))
    parts.append("历史相对位置仅为数值对照，不代表录取概率或录取保证。")
    return "\n\n".join(parts)


def display_candidate_summary(summary: dict) -> str:
    labels = {
        "当年匹配计划记录数": "当年专业计划记录", "选科符合专业数": "选科符合",
        "选科不符合专业数": "选科不符合", "选科待核对专业数": "选科待核对",
        "有可比历史的专业数": "有可比历史", "无可比历史的专业数": "无可比历史",
    }
    return "；".join(f"{labels.get(key, key)}：{value}" for key, value in summary.items())
