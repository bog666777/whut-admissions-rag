"""按明确条件查询官方结构化记录；所有数值直接取原字段，不经LLM计算。"""
from __future__ import annotations

import json
import re
import time

from whut_rag.structured_store import load_records, structured_status
from whut_rag.query_conditions import mask_school_names, mask_subject_combinations, subject_type_mentions, subject_type_conflict, parse_subjects, year_mentions

PLAN = r"招生计划|计划人数|计划数|招生人数|招生名额|计划名额|招几人|招多少人|计划招|招多少"
ADMISSION = r"录取分|录取线|分数线|最低分|最高分|平均分|投档线|投档分|位次|多少分|录取数据|录取情况|录取成绩"
POLICY = r"学费|收费|费用|培养|出国|证书|教学|授课|调剂|退档|体检|色盲|色弱|近视|就业|招生章程|招生政策|招生规则|停招|招生吗|是否招生"
SCOPE_LABELS = {"major": "专业/专业大类", "school_summary": "学校省级汇总（限定科类与招生类型）", "professional_group": "专业组"}


def canon(value: str) -> str:
    return re.sub(r"\s+", "", value).translate(str.maketrans("（）【】", "()[]"))


def dataset_intent(question: str, context: dict) -> str | None:
    plan, admission = bool(re.search(PLAN, question)), bool(re.search(ADMISSION, question))
    if plan and admission:
        return "both"
    if plan:
        return "plan"
    if admission:
        return "admission"
    if re.search(POLICY, question):
        return None
    if context.get("pending_intent") == "structured":
        return context.get("dataset", "unspecified")
    if context.get("active_domain") in {None, "structured"} and context.get("dataset") in {"plan", "admission"} and re.search(r"^(?:那|改查|再查|换成)|呢[？?]?$", question):
        return context["dataset"]
    return None


def unique_mentions(question: str, options: set[str]) -> list[str]:
    text = canon(question)
    matches = [x for x in options if canon(x) and canon(x) in text]
    # 仅去掉被更长、明确原名包含的短项；不将多个不同对象混作一个。
    return sorted(x for x in matches if not any(x != other and canon(x) in canon(other) for other in matches))


def unknown_major(question: str, vocabulary: set[str]) -> str | None:
    """移除已识别条件后保留新专业提法，禁止悄悄继承上轮专业。"""
    remainder = canon(mask_school_names(mask_subject_combinations(question)))
    remainder = re.sub(r"(?:我(?:的)?选科|我选(?:的是|了)?|选科)(?:是|为|[:：])?", "", remainder)
    for item in sorted(vocabulary, key=len, reverse=True):
        remainder = remainder.replace(canon(item), "")
    remainder = re.sub(r"学校汇总|省级汇总|学校整体|全校|不分专业|学校层面|学校最低|所有专业|全部专业|各专业|分专业列表|专业清单|具体专业|专业大类", "", remainder)
    remainder = re.sub(r"20\d{2}年?|" + PLAN + "|" + ADMISSION, "", remainder)
    remainder = re.sub(r"物理方向|历史方向|物理|历史|理科|文科|普通招生|普通|中外合作办学|中外合作|中外|合作办学", "", remainder)
    remainder = re.sub(r"请问|请|查询|查一下|改查|再查|换成|改成|看看|看下|那|查|多少|几个|人数|分别|对应|录取|招生|计划|情况|数据|分数|最低|最高|平均|大概|大约|分|位次|排名|是什么|是多少|是|的|和|在|呢|吗|年|省|[\s，,。.!！?？：:、()0-9]", "", remainder)
    remainder = remainder.strip("与")
    # 不让无法解析的新对象（包括简称或不存在的名称）触发旧专业继承。
    if len(remainder) >= 2:
        return remainder
    return None


def display_value(value: object) -> str:
    return "缺失（官网未提供）" if value is None or str(value).strip() in {"", "--"} else str(value)


def rank_value(value: object) -> str:
    text = display_value(value)
    if text.startswith("缺失"):
        return text
    if re.fullmatch(r"\d+(?:\.0+)?", text):
        return text.split(".")[0]
    return f"未提供数值位次（官方原值：{text}；不是排名）"


def source_evidence(record: dict) -> dict:
    p = record["provenance"][0]
    where = f"queries[{p['query_index']}].response.ext.{p['ext_key']}[{p['row_index']}]；官方id={record['official_id']}（索引从0计）"
    if p.get("csv_path"):
        where += f"；交叉核对 {p['csv_path']} 第{p['csv_row']}行"
    return {
        "chunk_id": record["record_key"], "source_id": p.get("source_id", record["dataset"]),
        "title": p.get("title", "武汉理工大学官方结构化招生数据"),
        "year": record["year"], "url": p["official_url"], "location": where,
        "text": json.dumps(record["payload"], ensure_ascii=False, indent=2),
        "source_path": p.get("source_path"), "distance": None,
    }


def table_record(record: dict) -> dict:
    payload = record["payload"]
    row = {"年份": record["year"], "省份": record["province"], "科类": record["subject_type"],
           "招生类型": record["admission_type"], "统计口径": SCOPE_LABELS[record["granularity"]],
           "专业或专业大类": record["major"] or "不分专业的学校汇总"}
    if record["dataset"] == "plan":
        row["计划人数"] = display_value(payload.get("recruitNum"))
    else:
        row.update({"最低分": display_value(payload.get("zdf")), "最高分": display_value(payload.get("zgf")),
                    "平均分": display_value(payload.get("pjf")), "位次（官方字段）": rank_value(payload.get("wcz"))})
    if record["granularity"] == "major":
        row["选科要求"] = display_value(payload.get("electiveSubject"))
    if payload.get("remarks"):
        row["官方备注"] = str(payload["remarks"])
    row["记录编号"] = record["record_key"]
    return row


def answer_structured(question: str, year: int | None = None, context: dict | None = None) -> dict | None:
    context = context or {}
    dataset = dataset_intent(question, context)
    if dataset is None:
        return None
    started = time.perf_counter()
    conditions = {"dataset": None if dataset == "both" else dataset, "intent": "structured"}
    result = {"status": "clarify", "answer": "", "evidence": [], "conditions": conditions,
              "timings": {}, "reason": "", "structured_table": [], "structured_count": 0}

    def finish(status: str, answer: str, reason: str, missing: list[str] | None = None) -> dict:
        result.update(status=status, answer=answer, reason=reason)
        if missing:
            result["missing_fields"] = missing
            conditions["pending_intent"] = "structured"
        else:
            conditions["pending_intent"] = None
        result["timings"]["total_seconds"] = round(time.perf_counter() - started, 3)
        return result

    if dataset in {"both", "unspecified"}:
        return finish("clarify", "请明确本次查录取分数与位次，还是招生计划人数。两类数据分别按各自年份和统计口径查询。", "choose_dataset", ["dataset"])
    explicit_years = year_mentions(question)
    if len(explicit_years) > 1:
        return finish("clarify", "请明确本次查询的一个年份；不同年份的数据将分别核对，不合并计算。", "multiple_years", ["year"])
    year_source = "question" if explicit_years else "selector" if year is not None else "session" if context.get("year") is not None else None
    selected_year = next(iter(explicit_years)) if explicit_years else year if year is not None else context.get("year")
    conditions.update(year=selected_year, year_source=year_source)
    if selected_year is not None:
        result["confirmed_year"] = selected_year
    if selected_year is not None and selected_year not in {2024, 2025, 2026}:
        return finish("refused", "该年份超出2024—2026年资料边界，不能借用其他年份数据。", "out_of_scope_year")
    status = structured_status()
    if not status.get("ready"):
        return finish("error", "结构化数据尚未导入或完整性校验未通过，请先导入现有结构化资料。", "structured_not_ready")
    records = load_records()
    available = [r for r in records if r["dataset"] == dataset and (selected_year is None or r["year"] == selected_year)]
    provinces = {r["province"] for r in records}
    mentioned_provinces = unique_mentions(question, provinces)
    if len(mentioned_provinces) > 1:
        return finish("clarify", "请明确本次查询的一个省份，避免把不同省份的数据混在一起。", "multiple_provinces", ["province"])
    province = mentioned_provinces[0] if mentioned_provinces else context.get("province")
    conditions["province"] = province
    if province:
        available = [r for r in available if r["province"] == province]
    subjects = {r["subject_type"] for r in available}
    named_types = unique_mentions(question, {r["admission_type"] for r in records})
    subject_mentions = subject_type_mentions(question, {r["subject_type"] for r in records}, ignore_terms=named_types)
    combination, provided, invalid = parse_subjects(question)
    if provided:
        conditions["personal_subjects"] = combination
    subject_conflict = subject_type_conflict(subject_mentions, subjects, combination if provided else context.get("personal_subjects"))
    subject = None if subject_conflict else subject_mentions[0] if subject_mentions else context.get("subject_type")
    conditions["subject_type"] = subject
    if subject:
        available = [r for r in available if r["subject_type"] == subject]
    types = {r["admission_type"] for r in available}
    type_mentions = unique_mentions(question, {r["admission_type"] for r in records})
    # 中外合作属于多种官方招生类型，只有唯一原始类型时才能规范化匹配。
    if not type_mentions and re.search(r"中外|合作办学", question):
        type_mentions = sorted(t for t in types if "中外" in t)
    if not type_mentions and "普通" in question:
        type_mentions = ["普通类"]
    admission_type = type_mentions[0] if len(type_mentions) == 1 else None if len(type_mentions) > 1 else context.get("admission_type")
    conditions["admission_type"] = admission_type
    if admission_type:
        available = [r for r in available if r["admission_type"] == admission_type]
    if "专业组" in question:
        conditions.update(granularity="professional_group", major=None)
        return finish("refused", "现有结构化原始资料没有专业组或组号字段，无法查询该口径。学校汇总和专业明细不能代替专业组数据。", "professional_group_missing")
    all_majors = {r["major"] for r in records if r["major"]}
    major_mentions = unique_mentions(question, all_majors)
    unknown = unknown_major(question, all_majors | provinces | {r["subject_type"] for r in records} | {r["admission_type"] for r in records})
    # 同名合作项目可能对应不同合作方；使用已明确的类型及当年候选缩小，不自行任选。
    if len(major_mentions) > 1:
        in_available = {r["major"] for r in available}
        narrowed = [m for m in major_mentions if m in in_available]
        if narrowed:
            major_mentions = narrowed
    major = major_mentions[0] if len(major_mentions) == 1 else None if len(major_mentions) > 1 else context.get("major")
    school_scope = bool(re.search(r"学校汇总|省级汇总|学校整体|全校|不分专业|学校层面|学校最低", question))
    list_majors = bool(re.search(r"所有专业|全部专业|各专业|分专业列表|专业清单", question))
    if school_scope and major_mentions:
        return finish("clarify", "问题同时指定学校汇总和具体专业，请选择本次统计口径；两者不能合并。", "conflicting_granularity", ["granularity"])
    if school_scope:
        granularity, major = "school_summary", None
    elif major_mentions or list_majors or unknown:
        granularity = "major"
    else:
        granularity = context.get("granularity")
    conditions.update(granularity=granularity, major=major)
    if unknown:
        major = None
        conditions["major"] = None
    if list_majors:
        major = None
        conditions["major"] = None
    if granularity == "professional_group":
        return finish("refused", "现有结构化资料没有专业组字段，不能用学校或专业数据替代。", "professional_group_missing")
    if selected_year is not None and not any(r["dataset"] == dataset and r["year"] == selected_year for r in records):
        label = "招生计划" if dataset == "plan" else "录取分数与位次"
        return finish("refused", f"本轮现有结构化资料没有{selected_year}年{label}数据。接口空响应或未收录不代表当年没有招生，也不能套用其他年份。", "dataset_year_missing")
    if unknown:
        return finish("clarify", f"未识别到“{unknown}”对应的官方专业或专业大类名称，请核对名称。不会沿用上轮专业，也不会把某个具体专业自动替换为专业大类。", "unknown_major", ["major"])
    missing = []
    if selected_year is None:
        missing.append("year")
    if not province:
        missing.append("province")
    if not subject:
        missing.append("subject_type")
    if not admission_type:
        missing.append("admission_type")
    if not granularity:
        missing.append("granularity")
    if granularity == "major" and not major and not list_majors:
        missing.append("major")
    if missing:
        labels = {"year": "资料年份", "province": "招生省份", "subject_type": "科类（如物理类、历史类或理工）", "admission_type": "招生类型（如普通类、中外合作或专项）", "granularity": "统计口径（学校汇总或具体专业/专业大类；专业组暂无资料）", "major": "官方专业或专业大类名称"}
        hints = []
        if len(type_mentions) > 1:
            hints.append("当前类型候选：" + "、".join(type_mentions))
        if len(major_mentions) > 1:
            hints.append("当前专业候选：" + "、".join(major_mentions))
        if "subject_type" in missing and subjects:
            hints.append("该条件下的科类：" + "、".join(sorted(subjects)))
        if subject_conflict:
            hints.append(subject_conflict)
        return finish("clarify", "请补充" + "、".join(labels[k] for k in missing) + "。" + ("\n\n" + "；".join(hints) if hints else ""), "missing_structured_conditions", missing)
    matches = [r for r in available if r["granularity"] == granularity and (major is None or r["major"] == major)]
    if not matches:
        return finish("refused", "没有找到与上述年份、省份、科类、招生类型及统计口径完全一致的官方记录。这里表示资料缺失或条件无匹配，不等于计划人数或录取分数为0。", "no_matching_structured_record")
    matches.sort(key=lambda r: (r["major"] or "", r["record_key"]))
    result["structured_table"] = [table_record(r) for r in matches]
    result["structured_count"] = len(matches)
    result["evidence"] = [source_evidence(r) for r in matches]
    result["answer_mode"] = "verified_structured_record"
    result["query"] = {"filters": dict(conditions), "record_keys": [r["record_key"] for r in matches]}
    kind = "招生计划" if dataset == "plan" else "录取分数与位次"
    text = f"找到{len(matches)}条{selected_year}年{province}、{subject}、{admission_type}的{kind}记录。统计口径：{SCOPE_LABELS[granularity]}。"
    if len(matches) == 1:
        raw = matches[0]["payload"]
        if dataset == "plan":
            text += f"\n\n计划人数：{display_value(raw.get('recruitNum'))}。"
        else:
            text += f"\n\n最低分：{display_value(raw.get('zdf'))}；位次：{rank_value(raw.get('wcz'))}。"
    text += "\n\n完整字段与官方原始记录见下方表格及来源。"
    if granularity == "school_summary":
        text += "\n\n这是该省该科类、该招生类型的学校汇总，不是全省所有类型合计，也不是专业组或某个专业的数据。"
    if dataset == "admission" and any(rank_value(r["payload"].get("wcz")).startswith(("缺失", "未提供")) for r in matches):
        text += "\n\n部分记录未提供可用数值位次；空值、--及“高省控线…分”等文字均未反推为排名。"
    return finish("answered", text, "verified_structured_record")
