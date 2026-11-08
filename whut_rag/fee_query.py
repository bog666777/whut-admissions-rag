"""按入学年级和收费对象查询原始收费记录，不推算个人应缴总额。"""
from __future__ import annotations
from whut_rag.query_conditions import year_mentions

import re
import time
import json

from whut_rag.fee_store import fee_status, load_fee_records

FEE_WORDS = r"学费|住宿|宿舍|收费|费用|学分费|每学分|辅修|预科.*费|体检费|健康检查费|医保|医疗保险|伙食费|校园卡.*预存|多少钱"
OTHER_TOPIC = r"培养|办学模式|教学|授课|语言|证书|出国|招生吗|是否招生|停招|调剂|退档|色盲|色弱|选科|录取|位次|最低分|最高分|招生计划|计划人数"
ITEM_NAMES = {"tuition": "学费", "accommodation": "住宿费", "minor": "辅修费", "preparatory": "预科生学费", "medical_check": "健康检查费", "insurance": "医保", "meal_deposit": "校园卡伙食费预存", "rule": "学分制收费规则", "overview": "收费项目"}


def canon(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).translate(str.maketrans("（）", "()"))


def fee_intent(question: str, context: dict) -> bool:
    if re.search(r"(?:系统|知识库|你).*(?:支持|覆盖|收录|回答).*(?:年份|哪几年)", question):
        return False
    if re.search(FEE_WORDS, question):
        return True
    if re.search(OTHER_TOPIC, question):
        return False
    pending = context.get("pending_intent") == "fee"
    active = context.get("active_domain") == "fee"
    if pending:
        return bool(re.fullmatch(r"(?:是|查|查询|就)?\s*20\d{2}[年级]?(?:的)?[。！!]?", question)
                    or (len(question) < 90 and not re.search(r"什么|怎么|如何|是否|能否", question)))
    return active and bool(re.search(r"^(?:那|改查|换成|再查)|呢[？?]?$", question))


def item_for(question: str, context: dict) -> str:
    if "学费" in question and re.search(r"住宿|宿舍", question):
        return "overview"
    if re.search(r"住宿|宿舍费|空调.*加收", question):
        return "accommodation"
    if "辅修" in question:
        return "minor"
    if "预科" in question:
        return "preparatory"
    if re.search(r"体检|健康检查", question):
        return "medical_check"
    if re.search(r"医保|医疗保险", question):
        return "insurance"
    if re.search(r"伙食费|校园卡.*预存", question):
        return "meal_deposit"
    if re.search(r"学分制|每学分|学分学费|学分费|收费规则|学费规则|学费组成|怎么计算|如何计算", question):
        return "rule"
    if "学费" in question:
        return "tuition"
    if context.get("active_domain") == "fee" or context.get("pending_intent") == "fee":
        return context.get("fee_item") or "tuition"
    if context.get("project") or context.get("major"):
        return "tuition"
    return "overview"


def record_names(record: dict) -> set[str]:
    names = {str(x) for x in record.get("target_names", []) if x}
    label = record["label"]
    names.add(label)
    # 只在表内明确的“按大类招生，含…”关系下展开包含专业。
    clean = canon(label)
    if record["kind"] == "cooperation":
        if clean == "中英车辆工程项目":
            names.add(clean.removesuffix("项目"))
        historical = re.fullmatch(r"中[英澳法](.+?)项目", clean)
        if historical and re.search(r"车辆工程|金融学", historical[1]):
            names.add(historical[1])
        design = re.fullmatch(r"(.+?)专业本科教育学项目", clean)
        if design:
            names.add(design[1])
    match = re.search(r"\(按大类招生[，,]含(.+)", clean)
    if match:
        prefix = clean[:match.start()]
        names.add(prefix)
        inside = match[1].rsplit(")", 1)[0]
        names.update(x for x in inside.split("、") if x)
    if record["kind"] == "tuition":
        depth, current = 0, ""
        for ch in clean + "、":
            depth += (ch == "(") - (ch == ")")
            if ch == "、" and depth == 0:
                if current:
                    names.add(current)
                current = ""
            else:
                current += ch
    return {canon(n) for n in names if len(canon(n)) >= 2}


def mentions(question: str, records: list[dict]) -> tuple[list[dict], list[str]]:
    text = canon(question)
    matched = {n for r in records for n in record_names(r) if n in text}
    longest = sorted(n for n in matched if not any(n != m and n in m for m in matched))
    return [r for r in records if record_names(r).intersection(longest)], longest


def evidence(record: dict) -> dict:
    text = record["text"]
    raw_cells = record.get("fields", {}).get("raw_cells")
    if raw_cells:
        text = ("PDF原始单元格（按页、表、行定位；专业学费与学分学费为不同列）：\n"
                + json.dumps(raw_cells, ensure_ascii=False, indent=2)
                + "\n\n解析字段与适用说明（依原表转写，非PDF连续段落）：\n" + text)
    return {"chunk_id": record["id"], "source_id": record["source_id"],
            "source_path": record["source_path"], "title": record["title"],
            "year": record["year"], "url": record["url"],
            "location": record["location"], "text": text, "distance": None}


def table_row(record: dict) -> dict:
    fields = record.get("fields", {})
    row = {"适用资料": f"{record['year']}级新生收费资料" if record["kind"] not in {"accommodation", "accommodation_surcharge"} else f"{record['year']}年公布的住宿收费标准",
           "收费项目": record["label"], "收费类别": {"tuition": "普通本科学分制", "cooperation": "中外合作项目原名", "accommodation": "住宿费", "accommodation_surcharge": "空调加收（非全部住宿费）"}.get(record["kind"], record["kind"])}
    labels = {"profession_fee": "专业学费（按年）", "credit_unit_fee": "学分单价", "total_credits": "总学分（非金额）", "annual_fee": "学年制学费", "amount": "原文金额/标准", "unit": "计费单位", "period": "执收时间/周期", "remarks": "备注", "domestic_fee": "国内学费原文", "foreign_fee": "国外学费原文", "scope_note": "适用范围说明"}
    for key, label in labels.items():
        value = fields.get(key)
        if value is not None and value != "":
            if key == "annual_fee" and record["kind"].startswith("accommodation"):
                label = "空调加收标准" if record["kind"] == "accommodation_surcharge" else "住宿费标准"
            row[label] = str(value) + ("元/学分" if key == "credit_unit_fee" else "")
    row["记录编号"] = record["id"]
    return row


def unresolved_target(question: str, known: set[str], partners: list[str]) -> str:
    text = canon(question).replace("武汉理工大学", "").replace("武汉理工", "")
    for name in sorted(known | set(partners), key=len, reverse=True):
        text = text.replace(canon(name), "")
    text = re.sub(r"20\d{2}[年级]?|" + FEE_WORDS, "", text)
    text = re.sub(r"中外合作办学|中外合作|中外|合作办学|合作|合办|普通本科|普通|本科|国内外|国内|国外|境内|境外|澳大利亚|英国|法国|专业|项目|这个|那个|该|上述|它|每学年|每年|一年|全年|第一学期|首学期|入学|新生|标准|收费|请问|请|想了解|帮我|能帮我|我想|查询|查一下|改查|再查|换成|改成|分别|那|查|跟|与|的|是|多少|钱|吗|呢|元|[，,。.!！?？：:、()\s]", "", text)
    return text if len(text) >= 2 else ""


def ordinary_major(record: dict, names: list[str], context: dict) -> str | None:
    name = names[0] if len(names) == 1 else None
    if name == canon(record["label"]):
        previous = context.get("major")
        if previous and canon(previous) in record_names(record):
            return previous
        if "(按大类招生" in name:
            return name.split("(按大类招生", 1)[0]
        if "、" in name:
            return None
    return name


def answer_fee(question: str, year: int | None = None, context: dict | None = None) -> dict | None:
    context = context or {}
    if not fee_intent(question, context):
        return None
    from whut_rag.core import project_catalog, other_school_query, resolve_project
    started = time.perf_counter()
    item = item_for(question, context)
    conditions = {"intent": "fee", "active_domain": "fee", "fee_item": item}
    result = {"status": "clarify", "answer": "", "reason": "", "evidence": [],
              "conditions": conditions, "timings": {}, "structured_table": [], "structured_count": 0}

    def finish(status: str, text: str, reason: str, missing: list[str] | None = None, rows: list[dict] | None = None) -> dict:
        result.update(status=status, answer=text, reason=reason)
        conditions["pending_intent"] = "fee" if missing else None
        if missing:
            result["missing_fields"] = missing
        if rows:
            result["evidence"] = [evidence(r) for r in rows]
            result["structured_table"] = [table_row(r) for r in rows]
            result["structured_count"] = len(rows)
            result["answer_mode"] = "verified_fee_record"
            result["query"] = {"filters": dict(conditions), "record_ids": [r["id"] for r in rows]}
            from whut_rag.fee_presentation import render_fee_presentation
            result["fee_presentation"] = render_fee_presentation(result, question, rows)
            result["answer"] = result["fee_presentation"]["answer"]
        result["timings"]["total_seconds"] = round(time.perf_counter() - started, 3)
        return result

    catalog = project_catalog()
    if other_school_query(question, catalog):
        return finish("refused", "本系统只使用武汉理工大学官方本科收费资料，不能回答其他学校自身的收费。", "other_school")
    partner_projects = [p for p in catalog if any(alias in question for alias in p["aliases"])]
    if partner_projects and not re.search(r"合作|合办|中外", question):
        if re.search(r"自身|自己|独立", question):
            return finish("refused", "没有该合作院校自身收费资料，不能使用武汉理工大学合作项目的收费代替。", "other_school")
        return finish("clarify", "你指武汉理工大学与该校的合作项目，还是该校自身的收费？", "ambiguous_school_subject", ["project"])
    cohorts = {int(y) for y in re.findall(r"(20\d{2})\s*级", question)}
    explicit = year_mentions(question)
    if len(explicit) > 1:
        conditions.update(year=None, cohort_year=None, fee_target=None, project=None, major=None, fee_scope=None)
        return finish("clarify", "请区分入学年级与缴费年度：现有资料按2024、2025、2026级新生公布，不能把某年级在另一年度的个人缴费直接套为该年度新生标准。请明确本次要核对的一个入学年级及收费范围。", "fee_time_roles", ["year"])
    selected = next(iter(cohorts or explicit)) if explicit else year if year is not None else context.get("year", context.get("cohort_year"))
    conditions.update(year=selected, cohort_year=selected, year_source="question" if explicit else "selector" if year is not None else "session" if selected is not None else None)
    if selected is not None:
        result["confirmed_year"] = selected
    if selected is not None and selected not in {2024, 2025, 2026}:
        conditions.update(fee_target=None, project=None, major=None, fee_scope=None)
        return finish("refused", "该年份超出2024—2026年资料范围，不能套用其他年级的收费标准。", "out_of_scope_year")
    ready = fee_status()
    if not ready.get("ready"):
        return finish("error", "收费资料尚未导入或完整性校验未通过，请先导入可直接读取的官方收费资料。", "fees_not_ready")
    all_records = load_fee_records()
    records = [r for r in all_records if r["year"] == selected]
    if item == "overview":
        text = ("你同时询问了学费和住宿费。本轮按费用项目分别查询，请先明确要核对哪一项；两项收费的适用条件不同，不能直接合为应缴总额。"
                if "学费" in question and re.search(r"住宿|宿舍", question)
                else "请明确要查询学费、住宿费、辅修费、体检费或医保等哪一项收费。")
        return finish("clarify", text, "missing_fee_item", ["fee_item"])
    if item != "tuition":
        conditions.update(fee_target=None, fee_scope=None)
        if item == "accommodation" and selected in {2024, 2025} and not cohorts:
            conditions["cohort_year"] = None  # 原表住宿对象为在校生，年份是收费文件年份。
        if selected is None:
            return finish("clarify", "请补充收费资料对应的年份或入学年级；住宿等通用收费无需提供专业、省份、选科或成绩。", "missing_fee_conditions", ["year"])
        if item == "accommodation":
            rows = [r for r in records if r["kind"] in {"accommodation", "accommodation_surcharge"}]
            surcharge = bool(re.search(r"空调.*(?:加收|增加)|加收.*空调", question))
            if surcharge:
                rows = [r for r in rows if r["kind"] == "accommodation_surcharge"]
                room = next((s for s in ["单人间", "双人间", "四人间", "五至六人间", "七至八人间"] if s in question), None)
                if room:
                    rows = [r for r in rows if room in r["label"]]
            else:
                buildings = [n for n in ["海虹公寓", "友谊小区", "狮城公寓", "升升公寓"] if n in question]
                if buildings and selected == 2026:
                    return finish("refused", "2026级新生网页只公开住宿费区间，具体收费根据实际住宿安排确定；没有该楼栋/房型的可确认金额。", "accommodation_detail_missing", rows=rows)
                if buildings:
                    rows = [r for r in rows if any(n in r["label"] for n in buildings)]
            if not rows:
                return finish("refused", "该年份资料中没有符合所问住宿条件的收费记录，不能借用其他年份或其他房型的标准。", "fee_record_missing")
            intro = f"以下为{selected}年公布的住宿收费记录，适用对象、单位和房型以原文为准；个人具体金额需结合实际分配的宿舍。"
            if selected == 2024:
                intro += "\n\n未装空调基价和空调加收费分别列示；加收费不是全部住宿费，未擅自合计。"
            return finish("answered", intro + "\n\n" + "\n\n".join(r["text"] for r in rows), "verified_fee_record", rows=rows)
        kinds = {"minor": {"minor"}, "preparatory": {"preparatory"}, "medical_check": {"medical_check"}, "insurance": {"info"}, "meal_deposit": {"meal_deposit"}, "rule": {"rule"}}[item]
        rows = [r for r in records if r["kind"] in kinds]
        if item == "insurance":
            rows = [r for r in rows if re.search(r"医保|医疗保险", r["text"])]
        if not rows:
            return finish("refused", f"现有{selected}年收费资料未提供可确认的{ITEM_NAMES[item]}记录，不能用其他年级标准补齐。", "fee_record_missing")
        if item == "insurance":
            return finish("refused", "官方网页说明医保将按最新文件执行，未公开可用于本次回答的具体金额。\n\n" + "\n\n".join(r["text"] for r in rows), "fee_amount_missing", rows=rows)
        if item == "rule" and selected == 2026 and re.search(r"多少|单价|标准|每学分", question):
            return finish("refused", "2026年文字说明公开了计费组成，但具体专业学费、学分单价及首学期金额位于图片表中，尚待人工核对；不能套用2024或2025年单价。\n\n" + "\n\n".join(r["text"] for r in rows), "fee_image_pending", rows=rows)
        return finish("answered", f"按{selected}级收费资料核对：\n\n" + "\n\n".join(r["text"] for r in rows), "verified_fee_record", rows=rows)

    target_records = [r for r in all_records if r["kind"] in {"tuition", "cooperation"}]
    named, names = mentions(question, target_records)
    project, explicit_project = resolve_project(question, context, catalog)
    explicit_ordinary = bool(re.search(r"普通|非中外|非合作", question))
    explicit_coop = not explicit_ordinary and bool(re.search(r"合作|合办|中外|中英|中澳|中法", question) or partner_projects)
    scope = "ordinary" if explicit_ordinary else "cooperation" if explicit_coop else None
    all_names = {n for r in target_records for n in record_names(r)}
    partners = [a for p in catalog for a in p["aliases"]]
    unknown = unresolved_target(question, all_names, partners)
    # “这个项目”等代词是延续对象；未知新名称不得触发继承。
    if unknown:
        conditions.update(fee_target=None, major=None, project=None, fee_scope=scope or "unspecified")
        return finish("clarify", f"未识别到“{unknown}”对应的官方收费对象，请提供专业或合作项目原名；不会沿用上轮对象。", "unknown_fee_target", ["project"])
    if not names and not explicit_project:
        inherited = context.get("fee_target") if context.get("active_domain") == "fee" or context.get("pending_intent") == "fee" else None
        if inherited:
            named, names = mentions(str(inherited), target_records)
            scope = scope or context.get("fee_scope")
        elif context.get("active_domain") == "structured" and context.get("project"):
            project, _ = resolve_project("这个项目", context, catalog)
            scope = scope or "cooperation"
        elif context.get("active_domain") == "structured" and context.get("major"):
            named, names = mentions(str(context["major"]), target_records)
            scope = scope or "ordinary"
        elif project:
            scope = scope or "cooperation"
        elif context.get("major") and not context.get("project"):
            named, names = mentions(str(context["major"]), target_records)
    # 历史收费表原名已明确时，不强行套用2026项目的合作方身份。
    if selected in {2024, 2025} and not partner_projects and any(r["year"] == selected and r["kind"] == "cooperation" for r in named):
        project = None
        conditions["project"] = None
    if project and not explicit_ordinary and (explicit_project or not names):
        scope = "cooperation"
        named = [r for r in target_records if r["kind"] == "cooperation" and project["base_name"] in r["label"] and project["partner"] in r["text"]]
        conditions.update(project=project["label"], project_id=project["project_id"])
    elif explicit_project and not project:
        conditions["project"] = None
        # 冲突的专业/合作方不得改用上次项目。
        if partner_projects:
            named = [r for r in named if r["kind"] == "cooperation" and any(p["partner"] in r["text"] for p in partner_projects)]
    if scope:
        named = [r for r in named if r["kind"] == ("tuition" if scope == "ordinary" else "cooperation")]
    if scope is None and any(p["base_name"] in question for p in catalog):
        conditions.update(fee_target=None, project=None, fee_scope="unspecified")
        return finish("clarify", "请确认查询普通专业，还是中外合作项目；合作项目请注明合作院校，以免混用不同收费标准。", "missing_fee_conditions", ["project"] + ([] if selected is not None else ["year"]))
    candidates = [r for r in named if selected is None or r["year"] == selected]
    if names and (scope == "ordinary" or (named and all(r["kind"] == "tuition" for r in named))):
        ordinary = next((r for r in named if r["kind"] == "tuition"), None)
        conditions.update(project=None, major=ordinary_major(ordinary, names, context) if ordinary else names[0] if len(names) == 1 else None,
                          granularity="major")
    conditions["fee_scope"] = scope or ("ordinary" if candidates and all(r["kind"] == "tuition" for r in candidates) else "unspecified")
    if len(candidates) == 1:
        conditions["fee_target"] = candidates[0]["label"]
        if candidates[0]["kind"] == "tuition":
            conditions.update(major=ordinary_major(candidates[0], names, context), project=None, granularity="major")
    elif names:
        conditions["fee_target"] = names[0] if len(names) == 1 else None
    elif project:
        conditions["fee_target"] = project["label"]
    else:
        conditions["fee_target"] = None
    missing = ([] if selected is not None else ["year"]) + ([] if names or project else ["project"])
    if missing:
        return finish("clarify", "请补充" + "和".join("收费资料对应年份或入学年级" if k == "year" else "具体专业或合作项目（多个合作方时需注明合作方）" for k in missing) + "。无需提供省份、选科、分数或位次。", "missing_fee_conditions", missing)
    if selected == 2026 and conditions["fee_scope"] != "cooperation" and not candidates:
        rules = [r for r in records if r["kind"] == "rule"]
        return finish("refused", "2026级该普通专业的具体学费位于官方图片表中，尚待人工核对，未作为正式数据导入。当前只能说明文字收费规则，不能用2024、2025或合作项目的金额替代。", "fee_image_pending", rows=rules)
    if not candidates:
        return finish("refused", f"没有找到{selected}级与该专业、合作方和收费范围一致的官方费用记录；不会用其他年级或其他项目替代。", "fee_record_missing")
    if len(candidates) > 1:
        conditions["fee_target"] = None
        labels = sorted({r["label"] for r in candidates})
        return finish("clarify", "当前名称对应多个收费对象，请明确普通专业或具体合作项目及合作方：\n\n" + "\n\n".join(labels), "ambiguous_fee_target", ["project"])
    row = candidates[0]
    if re.search(r"(?:国外|境外).*学费|学费.*(?:国外|境外)", question) and (row["kind"] != "cooperation" or not row.get("fields", {}).get("foreign_fee")):
        return finish("refused", "该年级该项目的已导入资料未公开可确认的国外学费金额，不能把国内费用或其他项目费用当作国外费用。", "foreign_fee_missing", rows=[row])
    text = f"以下按{selected}级收费资料中的“{row['label']}”核对。"
    if row["kind"] == "tuition":
        f = row["fields"]
        text += f"\n\n专业学费：{f['profession_fee']}{f['unit']}；学分学费单价：{f['credit_unit_fee']}元/学分，按实际课程学分缴纳。"
        text += f"\n\n培养总学分：{f['total_credits']}。专业学费不是全年学费总额；培养总学分不能当作当年学分数或金额，未据此计算全年或四年总费用。"
        if f.get("effective_period"):
            text += f"\n\n原表执收时间：{f['effective_period']}。"
    else:
        text += "\n\n" + row["text"]
        text += "\n\n国内、国外费用与币种、计费周期分别保留，不进行汇率换算或相加。历史项目以当年收费表原名为准，未自动映射到其他年度的合作方。"
    if re.search(r"总共|合计|总费用|全年|一年.*多少|四年|第一学期|首学期|应缴", question) and row["kind"] == "tuition":
        text += "\n\n资料未给出你实际修读学分或本次预缴账单，无法据此确定所问合计/应缴金额。"
    return finish("answered", text, "verified_fee_record", rows=[row])
