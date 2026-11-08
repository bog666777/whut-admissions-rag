"""同一对象的多项查询：分别保留资料年份、统计口径及原有查询结果。"""
from __future__ import annotations

from copy import deepcopy
import re
import time
from whut_rag.query_conditions import subject_type_mentions, subject_type_conflict, year_mentions

TOPICS = (
    ("plan", "招生计划", r"招生计划|计划人数|计划数|招生人数|招生名额|招多少人"),
    ("subjects", "选科要求", r"选科要求|选考要求|选考科目|科目要求|选科"),
    ("admission", "历史录取", r"录取情况|录取数据|录取分|最低分|分数线|录取位次|近两年.*录取|往年.*录取"),
    ("training", "培养安排", r"培养模式|培养方式|培养安排|怎么培养|如何培养|必须出国|是否出国|需要出国|境外学习"),
    ("tuition", "学费", r"学费|收费信息|收费标准|(?<!总)费用"),
    ("accommodation", "住宿费", r"住宿费|宿舍费"),
    ("living", "生活费", r"生活费|生活支出"),
)


def requested_topics(question: str) -> list[str]:
    topics = [key for key, _, pattern in TOPICS if re.search(pattern, question)]
    # “住宿费用”“生活费用”不是另一个学费子问题。
    if "tuition" in topics and not re.search(r"学费|收费信息|收费标准|国内外费用|项目费用", question):
        if re.search(r"住宿费用|生活费用", question):
            topics.remove("tuition")
    return topics


def _sub_summary(result: dict) -> str:
    from whut_rag.answer_presentation import display_answer, display_major_text
    text = display_answer(result)
    if result.get("conditions", {}).get("intent") == "major_info" and result.get("structured_table"):
        text = "\n".join(f"{row['信息维度']}：{display_major_text(row['官方资料内容'], field=row['信息维度'])}。"
                         for row in result["structured_table"] if "信息维度" in row)
    elif result.get("conditions", {}).get("intent") == "eligibility" and result.get("structured_table"):
        requirements = list(dict.fromkeys(str(row["官方要求"]) for row in result["structured_table"] if row.get("官方要求")))
        if requirements:
            text += "\n\n官方要求：" + "；".join(requirements)
    return text


def answer_compound(question: str, year: int | None, context: dict, *, answer_single) -> dict | None:
    if re.search(r"(?:系统|知识库|你).*(?:支持|覆盖|收录)", question):
        return None
    topics = requested_topics(question)
    resume = context.get("pending_intent") == "compound" and not topics and len(question) < 100
    if len(topics) < 2 and not resume:
        return None
    started = time.perf_counter()
    original = str(context.get("compound_request") or "") if resume else question
    topics = requested_topics(original) if resume else topics
    if len(topics) < 2:
        return None
    from whut_rag.core import other_school_query, project_catalog, resolve_project
    from whut_rag.structured_store import load_records
    from whut_rag.structured_query import unique_mentions, canon
    from whut_rag.candidate_query import parse_subjects

    common = deepcopy(context)
    common.update(intent="compound", active_domain="compound", pending_intent=None,
                  compound_request=original, dataset=None)
    result = {"status": "clarify", "answer": "", "reason": "", "conditions": common,
              "parts": [], "evidence": [], "timings": {}}

    def finish(status, answer, reason, missing=None):
        result.update(status=status, answer=answer, reason=reason)
        common["pending_intent"] = "compound" if missing else None
        if missing:
            result["missing_fields"] = list(dict.fromkeys(missing))
        result["timings"]["total_seconds"] = round(time.perf_counter() - started, 3)
        return result

    catalog = project_catalog()
    if other_school_query(question, catalog):
        return finish("refused", "本系统仅使用武汉理工大学官方本科资料，无法回答其他学校自身的招生和收费。", "other_school")
    fresh_years = year_mentions(question)
    if len(fresh_years) > 1:
        return finish("clarify", "请分别明确招生计划的填报年份、历史录取年份和收费入学年级；这些年份不能合成一个查询条件。", "compound_year_roles", ["year"])
    selected = next(iter(fresh_years)) if fresh_years else year if year is not None else common.get("year")
    common.update(year=selected, year_source="question" if fresh_years else "selector" if year is not None else "session" if selected else None)
    if selected is not None:
        result["confirmed_year"] = selected
    if selected is not None and selected not in {2024, 2025, 2026}:
        return finish("refused", "该年份超出2024—2026年知识库范围，不能使用其他年份资料代替。", "out_of_scope_year")
    records = load_records()
    entity_text = question if not resume else question + " " + original
    for field in ("province", "admission_type"):
        vocabulary = {r[field] for r in records}
        fresh = unique_mentions(question, vocabulary)
        mentions = fresh or (unique_mentions(original, vocabulary) if resume else [])
        if len(mentions) > 1:
            return finish("clarify", "请明确同一次查询的一个省份、科类和招生类型；不同范围将分别核对。", "compound_multiple_scopes", [field])
        if mentions:
            common[field] = mentions[0]
    if not common.get("admission_type") and "普通" in entity_text:
        common["admission_type"] = "普通类"
    subjects, provided, invalid = parse_subjects(question)
    if provided and not invalid:
        common["personal_subjects"] = subjects
    elif provided:
        common["personal_subjects"] = None
    named_types = unique_mentions(entity_text, {r["admission_type"] for r in records})
    subject_mentions = subject_type_mentions(question, {r["subject_type"] for r in records}, ignore_terms=named_types)
    if not subject_mentions and resume:
        subject_mentions = subject_type_mentions(original, {r["subject_type"] for r in records}, ignore_terms=named_types)
    scope_records = [r for r in records if (not selected or r["year"] == selected)
                     and (not common.get("province") or r["province"] == common["province"])
                     and (not common.get("admission_type") or r["admission_type"] == common["admission_type"])]
    offered_subjects = {r["subject_type"] for r in scope_records}
    conflict = subject_type_conflict(subject_mentions, offered_subjects, common.get("personal_subjects"))
    if conflict:
        common["subject_type"] = None
    elif subject_mentions:
        common["subject_type"] = subject_mentions[0]
    else:
        combo = set(str(common.get("personal_subjects") or "").split("、"))
        primary = "物理类" if "物理" in combo and "历史" not in combo else "历史类" if "历史" in combo and "物理" not in combo else None
        if primary in offered_subjects:
            common["subject_type"] = primary
        elif common.get("subject_type") not in offered_subjects:
            common["subject_type"] = None
    project, explicit_project = resolve_project(question, common, catalog)
    if project:
        common.update(project=project["label"], major=project["name"])
    else:
        majors = {r["major"] for r in records if r["major"]}
        mentions = unique_mentions(question, majors)
        if len(mentions) > 1:
            return finish("clarify", "本轮集中查询针对同一对象，请明确一个专业、专业大类或具体合作项目。", "compound_multiple_targets", ["major"])
        if mentions:
            common.update(major=mentions[0], project=None)
        elif explicit_project:
            common.update(project=None, major=None)
        elif not resume and not re.search(r"这个|该|上述|其|它|呢[？?]?$", question):
            common.update(major=None, project=None)
        elif not common.get("major"):
            common["major"] = common.get("major_info_target")
    target = project["label"] if project else common.get("major")
    if not target:
        return finish("clarify", "请明确要集中查询的一个官方专业、专业大类或合作项目；同一专业有多个合作方时还需注明合作方。", "compound_target_missing", ["major"])
    if provided and invalid:
        return finish("clarify", "请确认完整且不重复的三门选科，不能用旧组合代替本次无法确认的选科。", "compound_invalid_subjects", ["personal_subjects"])
    if conflict:
        return finish("clarify", conflict, "compound_subject_conflict", ["subject_type"])
    common["granularity"] = "major"
    # 集中查询共享同一对象；清除其他业务遗留的目标标签，保留个人自述条件。
    common.update(fee_target=None, fee_item=None, fee_scope=None,
                  cohort_year=selected if any(t in topics for t in ("tuition", "accommodation")) else None,
                  eligibility_target=None, eligibility_topic=None,
                  major_info_target=None, major_info_compare_target=None,
                  major_info_mode=None, candidate_target=None, candidate_mode=None)
    labels = {key: label for key, label, _ in TOPICS}
    missing = []
    answered = 0

    def add_part(title, normalized, child_context, child_year):
        nonlocal answered
        sub = answer_single(normalized, child_year, child_context)
        result["parts"].append({"title": title, "question": normalized, "result": sub})
        answered += sub.get("status") == "answered"
        missing.extend(sub.get("missing_fields") or [])
        for evidence in sub.get("evidence") or []:
            identity = (evidence.get("chunk_id"), evidence.get("year"))
            if not any((e.get("chunk_id"), e.get("year")) == identity for e in result["evidence"]):
                result["evidence"].append(deepcopy(evidence))

    for topic in topics:
        child_context = deepcopy(common)
        child_context.pop("compound_request", None)
        child_context.update(pending_intent=None, active_domain=None)
        prefix = f"{selected}年" if selected else ""
        if topic in {"plan", "admission"}:
            years = [selected]
            historical = topic == "admission" and bool(re.search(r"近两年|近2年|往年|历年|历史", original))
            if historical:
                if selected is None:
                    years = [None]
                else:
                    # 对照填报年以前的两个日历年；没有资料也逐年明确标缺。
                    years = [selected - 2, selected - 1]
            for data_year in years:
                data_context = deepcopy(child_context)
                scope = [r for r in records if r["dataset"] == ("plan" if topic == "plan" else "admission")
                         and r["year"] == data_year and r["granularity"] == "major"
                         and canon(r["major"] or "") == canon(common.get("major") or "")
                         and (not common.get("province") or r["province"] == common["province"])
                         and (not common.get("admission_type") or r["admission_type"] == common["admission_type"])]
                available_types = sorted({r["subject_type"] for r in scope})
                if not data_context.get("subject_type") and len(available_types) == 1:
                    # 唯一记录的科类用于客观资料查询，不能回写成考生个人科类。
                    data_context["subject_type"] = available_types[0]
                pieces = [f"{data_year}年" if data_year else "", common.get("province", ""),
                          data_context.get("subject_type", ""), common.get("admission_type", ""),
                          common.get("major", ""), "招生计划人数" if topic == "plan" else "最低录取分数和位次"]
                add_part(f"{labels[topic]}（{str(data_year)+'年' if data_year else '年份待补充'}）", "".join(str(v or "") for v in pieces), data_context, data_year)
        elif topic == "subjects":
            add_part(labels[topic], prefix + str(common.get("province") or "") + str(common.get("admission_type") or "") + str(target) + "选科要求", child_context, selected)
        elif topic == "training":
            add_part(labels[topic], prefix + str(target) + "专业介绍中的培养安排", child_context, selected)
        elif topic in {"tuition", "accommodation"}:
            ordinary = "普通" if not project and common.get("admission_type") == "普通类" else ""
            fee_question = prefix + ordinary + (str(target) if topic == "tuition" else "") + labels[topic] + "是多少？"
            add_part(labels[topic] + (f"（{selected}级）" if selected else ""), fee_question, child_context, selected)
        else:
            add_part(labels[topic], "请说明个人生活开支的资料边界。", child_context, selected)
    # 保存共同对象，不以某个历史子问题的年份覆盖填报年份。
    common.update(intent="compound", active_domain="compound")
    text = [f"围绕“{target}”分项核对；招生计划、历史录取和收费分别保留原资料年份及统计口径。"]
    for part in result["parts"]:
        sub = part["result"]
        state = {"answered": "已有依据", "refused": "资料缺失或范围限制", "clarify": "仍需条件", "error": "处理异常", "partial": "部分有依据"}.get(sub.get("status"), "待核对")
        text.append(f"**{part['title']}｜{state}**\n\n{_sub_summary(sub)}")
    if not common.get("subject_type") and any(part["result"].get("conditions", {}).get("subject_type") for part in result["parts"]):
        text.append("未提供个人科类；以上科类标注来自各条官方记录，不代表已确认你的个人科类或选科。")
    statuses = [p["result"].get("status") for p in result["parts"]]
    status = "answered" if statuses and all(s == "answered" for s in statuses) else "partial" if answered else "clarify" if missing else "error" if statuses and all(s == "error" for s in statuses) else "refused"
    return finish(status, "\n\n".join(text), "compound_query_results", missing)
