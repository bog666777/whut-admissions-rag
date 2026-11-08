"""查询条件的共用文本处理；校名不是科类，不跨口径推断科类。"""
from __future__ import annotations

import re
from collections.abc import Iterable

SUBJECT_WORDS = r"物理|历史|化学|生物|思想政治|政治|地理|技术"
SUBJECT_ORDER = ("物理", "历史", "化学", "生物", "思想政治", "地理", "技术")
SHORT = dict(zip("物史化生政地技", SUBJECT_ORDER))


def parse_subjects(q: str) -> tuple[str | None, bool, bool]:
    """解析明确列出的完整三科，保留是否曾提供及是否有歧义。"""
    short = re.findall(r"(?<![物史化生政地技])([物史化生政地技]{3,})(?![物史化生政地技])", q)
    words = re.findall(r"(?:" + SUBJECT_WORDS + r")(?:[、,，+/和及\s]*(?:" + SUBJECT_WORDS + r")){1,}", q)
    combinations = [[SHORT[x] for x in value] for value in short]
    combinations.extend(["思想政治" if x == "政治" else x for x in re.findall(SUBJECT_WORDS, value)] for value in words)
    provided = bool(combinations or re.search(r"选科(?:是|为|[:：])|我选|没选|未选|改选|完整选科", q))
    if re.search(r"没选|未选|不选|不知道选科", q):
        return None, True, True
    normalized = {"、".join(x for x in SUBJECT_ORDER if x in combo) for combo in combinations if len(combo) == len(set(combo)) == 3}
    valid = len(normalized) == 1 and all(len(combo) == len(set(combo)) == 3 for combo in combinations)
    return (next(iter(normalized)) if valid else None), provided, provided and not valid


def mask_subject_combinations(question: str) -> str:
    """只遮蔽已完整识别的三科组合，供其他实体抽取排除条件文本。"""
    if not parse_subjects(question)[0]:
        return question
    text = re.sub(r"(?<![物史化生政地技])[物史化生政地技]{3}(?![物史化生政地技])", " ", question)
    return re.sub(r"(?:" + SUBJECT_WORDS + r")(?:[、,，+/和及\s]*(?:" + SUBJECT_WORDS + r")){2,}", " ", text)


def mask_school_names(question: str) -> str:
    """仅遮蔽已确认的本校全称/简称，保留用户另外声明的理工等条件。"""
    return re.sub(r"武汉理工大学|武汉理工", " ", question)


def year_mentions(question: str) -> set[int]:
    """年份与分数/位次/金额分角色；只识别20xx，不推相对日期或中文数字。"""
    explicit = {int(value) for value in re.findall(r"(?<![\d.])(20\d{2})(?![\d.])\s*[年级]", question)}
    number = r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:万|千|百)?"
    numeric_role = r"(?:位次|排名|排位|名次|(?:全省|省内)排(?:名|位)?|我(?:在)?(?:全省|省内)?排(?:名|位)?|分数|总分|成绩|考了|考得|考出)"
    numeric_values = number + r"(?:\s*(?:至|到|[-~—/]|或)\s*" + number + r")?(?![\d.,])"
    text = re.sub(numeric_role + r"(?:是|为|在|[:：])?\s*(?:约|大概|前|大于|小于|超过|不到)?\s*" + numeric_values, " ", question)
    text = re.sub(r"(?<![\d.])" + numeric_values + r"\s*(?:名|分(?!数)|(?:澳|美|欧)?元)", " ", text)
    return explicit | {int(value) for value in re.findall(r"(?<![\d.])(20\d{2})(?![\d.])", text)}


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).translate(str.maketrans("（）【】", "()[]"))


def _declared_subject_alias(text: str, alias: str) -> bool:
    """短词须出现在科类声明/独立补充/招生类型之前，而非任意叙述中。"""
    if alias in {"理科", "文科"}:
        return alias in text  # 词本身已明确带“科”，只对应旧制原字段。
    escaped = re.escape(alias)
    return bool(
        re.fullmatch(r"(?:那|改成|改查|换成|查询|查)?" + escaped + r"(?:呢)?[，,。；;！!？?]*", text)
        or re.search(r"(?:科类|科别|首选(?:科目)?)(?:是|为|[:：])?" + escaped, text)
        or re.search(escaped + r"(?=类|方向|科|考生|\x00|普通|中外|国家专项|高校专项|[，,。；;！!？?]|$)", text)
    )


def subject_type_mentions(question: str, options: Iterable[str], *, ignore_terms: Iterable[str] = ()) -> list[str]:
    """返回原字段或已有别名对应的原字段；理科只对应理工，不对应物理类。"""
    text = _compact(mask_school_names(question))
    vocabulary = set(options)
    exact_values = {_compact(value) for value in vocabulary if value}
    for term in sorted(set(ignore_terms), key=len, reverse=True):
        if _compact(term) not in exact_values:
            # 同词既是科类又是类型（如艺术类）时保留；复合类型整体遮蔽。
            # 标记只保存其为招生类型的位置，内部字词不再参与科类匹配。
            text = text.replace(_compact(term), "\x00")
    found = [value for value in vocabulary if value and _compact(value) in text]
    found = [value for value in found if not any(value != other and _compact(value) in _compact(other) for other in found)]
    # 完整的三科组合是选科，不把组合中的“物理/历史”冒充额外科类声明。
    alias_text = _compact(mask_subject_combinations(text))
    aliases = (
        ("物理", "物理类"), ("历史", "历史类"),
        ("理科", "理工"), ("文科", "文史"), ("艺术", "艺术类"),
    )
    # 别名也参与矛盾检查；已有明确字段时不让其短别名产生重复值。
    for alias, target in aliases:
        if target in vocabulary and _declared_subject_alias(alias_text, alias):
            if not any(target != value and _compact(target) in _compact(value) for value in found):
                found.append(target)
    return sorted(set(found))


def subject_type_conflict(mentions: list[str], available: Iterable[str], subjects: str | None = None) -> str | None:
    """只提示显式冲突，绝不把另一科类或三科组合改写成缺失的官方口径。"""
    offered = set(available)
    if len(mentions) > 1:
        return "问题同时给出了不同科类：" + "、".join(mentions) + "，请确认本次采用的一个科类。"
    if not mentions:
        return None
    chosen = mentions[0]
    if offered and chosen not in offered:
        return f"明确给出的科类“{chosen}”与当前年份、省份或招生类型的原字段不一致；当前科类为" + "、".join(sorted(offered)) + "。请确认，不能自动互换理工与物理类。"
    combo = set(str(subjects or "").split("、"))
    if len(combo) == 3 and chosen in {"物理类", "历史类"}:
        primary = chosen.removesuffix("类")
        if primary not in combo or {"物理", "历史"} <= combo:
            return f"科类“{chosen}”与所述完整选科“{subjects}”不能一致确认，请核实科类、首选科目和完整三科组合。"
    return None
