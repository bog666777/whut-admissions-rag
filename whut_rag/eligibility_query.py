"""按年度和招生范围检索报考要求，逐项核对明确的个人条件。

规则只解析当前官方原字段；未知格式/未提供的医学判定不推断为合格。
个人条件只由调用方保留在会话中，本模块不写用户查询或健康资料。
"""
from __future__ import annotations

import re
import time

from whut_rag.structured_query import canon, unique_mentions, source_evidence
from whut_rag.query_conditions import subject_type_mentions, subject_type_conflict, parse_subjects, year_mentions
from whut_rag.fee_query import FEE_WORDS
from whut_rag.structured_store import load_records as load_plan_records, structured_status
from whut_rag.eligibility_store import load_records, eligibility_status

TOPICS = {
    "subjects": r"选科|选考|科目要求|物化生|物化地|物化政|物生地|物生政|物地政|史地政|史政地|史生地|史化生|物理.{0,4}化学",
    "physical": r"体检|身体|健康|色盲|色弱|色觉|单色|视力|裸眼|裸视|矫正|近视|身高|听力|嗅觉|复视|残疾",
    "language": r"外语|语种|日语|俄语|德语|法语|英语|授课语言|教学语言",
    "general": r"报考条件|报考要求|报名条件|报考资格|招生条件|专业志愿|男女比例|女生|男生|专项.*(?:退学|放弃)|定向.*协议",
}
FEE_OR_DATA = r"学费|住宿费|收费|体检费|最低分|最高分|录取线|位次|计划人数|计划数|招生人数|招生计划|录取数据|培养模式|培养方式|招生吗|是否招生|停招|调剂规则"
SUBJECTS = ("物理", "历史", "化学", "生物", "思想政治", "地理", "技术")
SHORT_SUBJECT = {"物": "物理", "史": "历史", "化": "化学", "生": "生物", "政": "思想政治", "地": "地理", "技": "技术"}
SCOPE_LABEL = {"general_charter": "普通本科招生章程（特殊类别还须依当年相应简章）", "taiwan_gsat": "台湾学测招生"}


def topic_intent(q: str, context: dict) -> str | None:
    if re.search(r"(?:系统|知识库|你).*(?:支持|覆盖|收录)", q):
        return None
    if re.search(FEE_WORDS, q):
        return None
    if re.search(r"教学语言|授课语言", q) and not re.search(r"外语|语种|台湾|学测", q) and context.get("eligibility_scope") != "taiwan_gsat":
        return None
    if re.search(FEE_OR_DATA, q) and not re.search(r"选科|体检|报考条件|外语要求|语种要求", q):
        return None
    hits = [k for k, pattern in TOPICS.items() if re.search(pattern, q)]
    if "general" in hits and re.search(r"报考条件|报考要求|报名条件|报考资格|招生条件", q):
        specific = [x for x in hits if x != "general"]
        if not specific and re.search(r"专项|定向|男女比例|专业志愿", q):
            return "general"
        return specific[0] if len(specific) == 1 and not re.search(r"全部|所有|综合|还(?:有|需)", q) else "all"
    if hits:
        return hits[0] if len(hits) == 1 else "all"
    pending = context.get("pending_intent") == "eligibility"
    followup = context.get("active_domain") == "eligibility" and bool(re.search(r"^(那|改|换|我|查)|呢[？?]?$|能报|符合|满足|核对", q))
    if pending or followup:
        if re.search(r"学费|收费|录取|位次|计划|培养|停招|调剂", q):
            return None
        return context.get("eligibility_topic", "all")
    return None


def personal_conditions(q: str) -> dict:
    """仅捕获个人自述/明确条件；疑问中的官方要求不作为个人事实。"""
    out = {}
    personal = bool(re.search(r"我|本人|考生(?:选|是|有|身高)|选科(?:是|为|[:：])|选了|已选|高考外语(?:是|为)", q))
    if personal:
        if re.search(r"没选|未选|不选|没有选|改选|选科改", q):
            out["personal_subjects"] = None
        subject_word = r"(?:物理|历史|化学|生物|思想政治|政治|地理|技术|[物史化生政地技])"
        statement = re.search(r"(?:我的?选科(?:是|为|[:：])?|我选科(?:是|为|[:：])?|选科(?:是|为|[:：])|选考科目(?:是|为|[:：])|我选(?:的是|了)?|已选(?:科目)?|我是)\s*(" + subject_word + r"(?:[、，,和+\s]*" + subject_word + r"){1,6})", q)
        segment = statement[1] if statement else ""
        short = re.fullmatch(r"[物史化生政地技]{3}", segment)
        named = {s for s in SUBJECTS if s in segment}
        if "政治" in segment:
            named.add("思想政治")
        if short:
            named = {SHORT_SUBJECT[x] for x in short[0]}
        # 仅提到某科的要求/未选某科，不构成完整选科组合。
        if len(named) == 3 and not re.search(r"没选|未选|不选|没有选", q):
            out["personal_subjects"] = "、".join(x for x in SUBJECTS if x in named)
        languages = re.findall(r"(?:我(?:的)?(?:高考外语|应试外语|外语|语种)|我(?:现在)?(?:改考|考的是|考)|(?:现在)?改考|高考外语)(?:语种)?(?:是|为|用|考|[:：])?\s*(英语|日语|俄语|德语|法语|西班牙语)", q)
        if languages:
            out["personal_foreign_language"] = languages[-1]
        color_statements = list(re.finditer(r"(?:我|本人)(?:的色觉(?:情况|结果)?(?:是|为|[:：]))?(?:是|有|患有|没有|不是|不属于|无)?\s*(?:色弱|色盲|色觉正常|单色识别能力异常)", q))
        color_text = "；".join(re.split(r"[，,。？?；;]", q[x.start():], maxsplit=1)[0] for x in color_statements)
        if color_statements:
            color_text += "；".join(re.findall(r"(?:但|而)(?:我)?有(?:色弱|色盲|单色识别能力异常)", q))
        positive_color = re.sub(r"(?:无|没有|不是|不属于|不|非)(?:色盲[、和及或]?色弱|色弱[、和及或]?色盲|色盲|色弱|单色识别能力异常)", "", color_text)
        if "单色" in positive_color and re.search(r"异常|不能|不准确", positive_color):
            out["personal_color_vision"] = "单色识别能力异常"
        elif "色弱" in positive_color:
            out["personal_color_vision"] = "色弱"
        elif "色盲" in positive_color:
            out["personal_color_vision"] = "色盲"
        elif "色觉正常" in color_text or re.search(r"(?:无|没有)(?:色盲[、和及或]?色弱|色弱[、和及或]?色盲)", color_text):
            out["personal_color_vision"] = "无色盲色弱（自述）"
        elif color_statements:
            out["personal_color_vision"] = None
        h = re.search(r"(?:我(?:的)?|本人(?:的)?)身高(?:是|为|[:：])?\s*(\d+(?:\.\d+)?)\s*(厘米|cm|米|m)?", q, re.I)
        if h:
            value = float(h[1]); value = value / 100 if h[2] in {"厘米", "cm"} or value > 3 else value
            out["personal_height"] = str(value)
        gender = re.search(r"(?:我是|本人是?|我)(女生|男生|女性|男性)", q)
        if gender:
            out["personal_gender"] = gender[1]
        if re.search(r"(?:我|本人)(?:的|有|是)?(?:近视|视力|双眼|裸眼|裸视|矫正|复视|听力|嗅觉|四肢)", q):
            sight = []
            for label, pattern in (("裸眼", r"双眼(?:裸眼|裸视)(?:视力)?(?:均为|都为|都是|均是|均达到|为|是)?\s*([0-5]\.\d)"), ("矫正", r"(?:双眼矫正(?:视力)?|矫正(?:视力)?双眼)(?:均为|都为|都是|均是|均达到|为|是)?\s*([0-5]\.\d)")):
                match = re.search(pattern, q)
                if match:
                    sight.append(f"双眼{label}视力均为{match[1]}")
            out["personal_vision"] = "；".join(sight) or "有相关个人描述，需补充双眼裸眼及矫正视力（5分记录法）"
    return out


def queried_color_condition(q: str) -> str | None:
    """只识别本问正在咨询的色觉条件，不把政策咨询记作个人健康自述。"""
    if not re.search(r"报考|可以报|能(?:否|不能)?报|可报|录取|限制|要求|体检|规定", q):
        return None
    positive = re.sub(r"(?:并非|不是|不属于|没有|无|非)(?:色盲[、和及或]?色弱|色弱[、和及或]?色盲|单色识别能力异常|色盲|色弱)", "", q)
    conditions = [name for name in ("单色识别能力异常", "色弱", "色盲") if name in positive]
    if set(conditions) == {"色弱", "色盲"}:
        return "色弱或色盲"
    return conditions[0] if len(conditions) == 1 else None


def evidence(record: dict) -> dict:
    return {"chunk_id": record.get("record_id", record.get("id")), "source_id": record["source_id"],
            "title": record.get("source_title", record.get("title", record["source_id"])),
            "year": record["year"], "url": record.get("source_url", record.get("url")),
            "location": record["locator"], "text": record["text"], "source_path": record["source_path"],
            "scope": record.get("scope_label", SCOPE_LABEL.get(record["scope"], record["scope"])), "distance": None}


def _major(record: dict) -> str:
    return str(record.get("major") or record.get("payload", {}).get("major") or "")


def rules_for_catalogue(records: list[dict], year: int | None, scope: str) -> list[dict]:
    return [x for x in records if x["year"] == year and x["scope"] == scope and _major(x)]


def _paragraphs(record: dict, predicate) -> list[dict]:
    found = []
    for p in record.get("payload", {}).get("paragraphs", []):
        if predicate(p["text"]):
            found.append({**record, "record_id": record["record_id"] + f":p{p['index']}",
                          "text": p["text"], "locator": record["locator"] + f"；本次引用p{p['index']}"})
    return found


def match_targets(q: str, vocabulary: set[str]) -> list[str]:
    matches = unique_mentions(q, vocabulary)
    if matches:
        return matches
    # 官方全称中的括号说明可省略，但同一基本名称的不同项目仍保留多个候选。
    bases = {m: canon(m).split("(")[0] for m in vocabulary}
    found = [m for m, base in bases.items() if len(base) >= 3 and base in canon(q)]
    if re.search(r"中外|合作|蒙纳士|布鲁内尔|圣大卫", q):
        found = [m for m in found if "合作" in m]
    elif "普通" in q:
        found = [m for m in found if "合作" not in m]
    if "蒙纳士" in q:
        found = [m for m in found if "材料科学与工程" in m and "合作" in m]
    if "布鲁内尔" in q:
        found = [m for m in found if "布鲁内尔" in m]
    if "圣大卫" in q:
        found = [m for m in found if "圣大卫" in m]
    return sorted(found)


def _restriction_targets(text: str, color: str) -> set[str]:
    t = canon(text)
    # 先切割独立的单色/色弱条款，防止把两类病况的名单混在一起。
    if color == "单色识别能力异常":
        part = t.split("单色识别能力异常", 1)[-1] if "单色识别能力异常" in t else ""
    else:
        part = t.split("单色识别能力异常", 1)[0] if "色弱" in t else ""
    if not part:
        return set()
    lists = re.findall(r"[：:]([^：:；;。\n]+)", part)
    lists = [x for x in lists if "、" in x and re.search(r"类|工程|技术|动画", x)]
    if not lists:
        return set()
    return {x.strip(" 、；;。") for x in lists[0].split("、")}


def answer_eligibility(question: str, year: int | None = None, context: dict | None = None) -> dict | None:
    context = context or {}
    topic = topic_intent(question, context)
    if topic is None:
        return None
    start = time.perf_counter()
    c = {"intent": "eligibility", "active_domain": "eligibility", "eligibility_topic": topic,
         "pending_intent": None}
    r = {"status": "clarify", "answer": "", "reason": "", "conditions": c, "evidence": [],
         "structured_table": [], "structured_count": 0, "timings": {}}

    def finish(status: str, text: str, reason: str, missing: list[str] | None = None):
        r.update(status=status, answer=text, reason=reason, structured_count=len(r["structured_table"]))
        c["pending_intent"] = "eligibility" if missing else None
        if missing:
            r["missing_fields"] = missing
        r["timings"]["total_seconds"] = round(time.perf_counter() - start, 3)
        return r

    def cite(items: list[dict]):
        for x in items:
            e = evidence(x)
            if not any(a["chunk_id"] == e["chunk_id"] for a in r["evidence"]):
                r["evidence"].append(e)

    def row(item: str, requirement: str, personal: str = "未提供（本次查询不强制）", outcome: str = "官方要求，尚未核对个人条件"):
        r["structured_table"].append({"核对项目": item, "官方要求": requirement, "个人条件": personal, "核对结果": outcome})

    years = year_mentions(question)
    if len(years) > 1:
        return finish("clarify", "请明确本次要核对的一个年份，不混用不同年度的要求。", "multiple_years", ["year"])
    chosen = next(iter(years)) if years else year if year is not None else context.get("year")
    c.update(year=chosen, year_source="question" if years else "selector" if year is not None else "session" if chosen else None)
    if chosen is not None:
        r["confirmed_year"] = chosen
    if chosen not in {None, 2024, 2025, 2026}:
        return finish("refused", "该年份超出2024—2026年资料边界，不能借用其他年份规则。", "out_of_scope_year")
    supplied_personal = personal_conditions(question)
    c.update(supplied_personal)
    # 个人自述可跨本会话复用；更换专业时仍是同一人的条件，显式修正优先。
    for key in ("personal_subjects", "personal_foreign_language", "personal_color_vision", "personal_height", "personal_gender", "personal_vision"):
        if key not in c and context.get(key):
            c[key] = context[key]
    all_plan = [x for x in load_plan_records() if x["dataset"] == "plan" and x["granularity"] == "major"] if structured_status().get("ready") else []
    scope_question = re.sub(r"(?:不是|不属于|非|不参加)(?:台湾)?学测(?:考生|招生)?", "", question)
    provincial_query = bool(unique_mentions(scope_question, {x["province"] for x in all_plan})) and not re.search(r"台湾|学测", scope_question)
    explicit_mainland = bool(re.search(r"内地|普通高考|全国统考|普通类", scope_question)) or provincial_query
    if ("台湾" in scope_question or "学测" in scope_question) and explicit_mainland:
        c["eligibility_scope"] = None
        return finish("clarify", "问题同时出现普通高考和台湾学测，请确认本次招生范围。", "ambiguous_scope", ["eligibility_scope"])
    if "台湾" in scope_question or "学测" in scope_question:
        scope = "taiwan_gsat"
    elif re.search(r"香港|澳门|港澳|华侨|联招", question):
        c["eligibility_scope"] = None
        return finish("refused", "现有专业目录仅适用于台湾学测招生；尚未导入你所问类别的完整报考条件，不能套用该目录或普通高考计划。", "special_scope_missing")
    elif explicit_mainland:
        scope = "general_charter"
    else:
        scope = context.get("eligibility_scope", "general_charter") if context.get("active_domain") == "eligibility" else "general_charter"
    c["eligibility_scope"] = scope
    if not eligibility_status().get("ready"):
        return finish("error", "报考条件资料尚未导入或完整性核验未通过，请先导入。", "eligibility_not_ready")
    all_rules = load_records()
    vocabulary = {x["major"] for x in all_plan if x.get("major")} | {_major(x) for x in all_rules if _major(x)}
    # 专业限制名单自身也是当年官方名称，不据此声称存在当年招生计划。
    for x in all_rules:
        if x["scope"] == "general_charter":
            for color in ("色弱", "单色识别能力异常"):
                vocabulary.update(_restriction_targets(x["text"], color))
    vocabulary.discard("")
    vocabulary = set({canon(m): m for m in sorted(vocabulary)}.values())
    targets = match_targets(question, vocabulary)
    # 只依据当年官方“包含专业/备注”建立子专业到招生大类的映射。
    mapped_from = None
    if not targets:
        mappings = {}
        for x in all_plan if chosen == 2026 and scope == "general_charter" else rules_for_catalogue(all_rules, chosen, scope):
            parent = x.get("major") or _major(x)
            children = x.get("payload", {}).get("remarks") or x.get("payload", {}).get("included_majors") or ""
            for name in re.split(r"[、,，]", str(children)):
                name = name.replace("*", "").strip()
                if len(name) >= 3 and canon(name) in canon(question):
                    mappings.setdefault(parent, set()).add(name)
        targets = sorted(mappings)
        if len(targets) == 1:
            mapped_from = "、".join(sorted(mappings[targets[0]]))
    if len(targets) > 1:
        c.update(eligibility_target=None, major=None, project=None)
        return finish("clarify", "请明确具体专业或合作项目：" + "、".join(targets) + "。不同项目的要求分别核对。", "ambiguous_target", ["eligibility_target"])
    explicit_target = targets[0] if targets else None
    # 不认识的新工程/专业名称不能悄悄复用上轮专业。
    remainder = canon(question)
    remainder = re.sub(r"^(?:那|换成|改查|改成)", "", remainder)
    remainder = re.sub(r"(?:呢|的选科要求|的体检要求|的外语要求)[？?]?$", "", remainder)
    known_condition = bool(unique_mentions(question, {x[k] for x in all_plan for k in ("province", "subject_type", "admission_type")}))
    bare_new = bool(re.search(r"^(?:那|换成|改查|改成)", question)) and not known_condition and not personal_conditions(question) and not re.search(r"这个|该|上述|它|要求|条件|选科|外语|体检|身高|色弱|色盲|日语|20\d{2}|普通", remainder) and len(remainder.strip("？?")) >= 2
    new_unknown = not explicit_target and (bare_new or bool(re.search(r"火星|不存在|[\u4e00-\u9fff]{2,12}(?:工程|专业)(?:的|能|可|选科|报考|体检|外语)", question))) and not re.search(r"该专业|这个专业|哪些专业|所有专业|航海类专业|合作专业", question)
    inherited_target = context.get("eligibility_target") if context.get("active_domain") == "eligibility" else context.get("major") or context.get("project")
    if inherited_target:
        inherited_matches = match_targets(str(inherited_target), vocabulary)
        if len(inherited_matches) == 1:
            inherited_target = inherited_matches[0]
    target = (explicit_target or inherited_target) if not new_unknown else None
    c.update(eligibility_target=target, major=target, project=target if target and "合作" in target else None)
    if chosen is None:
        return finish("clarify", "请补充要查询或核对的年份（2024、2025或2026）。", "missing_year", ["year"])
    if new_unknown:
        return finish("refused", "未识别到该专业或项目的官方报考条件记录，不能使用上一个专业的资料代替。请核对官方名称。", "unknown_target")
    rules = [x for x in all_rules if x["year"] == chosen and x["scope"] == scope]
    if not rules:
        return finish("refused", "现有资料没有该年份、该招生范围的报考条件，无法核对。", "scope_year_missing")
    if scope == "taiwan_gsat":
        return _taiwan(question, topic, target, rules, c, r, row, cite, finish)
    topics = [topic] if topic != "all" else ["subjects", "physical", "language", "general"]
    if re.search(r"女生|男生", question) and target in {"航海技术", "轮机工程"} and "physical" not in topics:
        topics.append("physical")
    notes, missing = [], []
    if mapped_from:
        notes.append(f"官方该年记录将{mapped_from}列入{target}，本次按该招生大类核对；不把大类要求冒称独立专业计划。")
    if "subjects" in topics:
        if chosen != 2026:
            notes.append(f"{chosen}年现有资料没有分省、分专业的完整选科字段，不能用2026年计划或台湾学测目录替代。")
        elif not all_plan:
            notes.append("2026年招生计划尚未就绪，无法核对选科。")
        else:
            provinces = unique_mentions(question, {x["province"] for x in all_plan})
            if len(provinces) > 1:
                return finish("clarify", "请明确本次核对的一个省份。", "multiple_provinces", ["province"])
            province = provinces[0] if provinces else context.get("province")
            c["province"] = province
            needed = ([] if target else ["eligibility_target"]) + ([] if province else ["province"])
            if needed:
                missing += needed
            else:
                candidates = [x for x in all_plan if x["province"] == province and canon(x["major"]) == canon(target)]
                types = unique_mentions(question, {x["admission_type"] for x in all_plan})
                if "普通" in question:
                    types = ["普通类"]
                selected_type = types[0] if len(types) == 1 else context.get("admission_type")
                if selected_type:
                    candidates = [x for x in candidates if x["admission_type"] == selected_type]
                elif len({x["admission_type"] for x in candidates}) == 1:
                    selected_type = candidates[0]["admission_type"]
                elif len({x["admission_type"] for x in candidates}) > 1:
                    notes.append("该专业匹配的招生类型：" + "、".join(sorted({x["admission_type"] for x in candidates})) + "。请确认本次招生类型。")
                    missing.append("admission_type"); candidates = []
                c["admission_type"] = selected_type
                subject_types = subject_type_mentions(question, {x["subject_type"] for x in all_plan}, ignore_terms=types)
                supplied_subjects = c.get("personal_subjects") or ""
                primary = "物理类" if "物理" in supplied_subjects and "历史" not in supplied_subjects else "历史类" if "历史" in supplied_subjects and "物理" not in supplied_subjects else None
                offered_subjects = {x["subject_type"] for x in candidates}
                question_combo, combo_provided, combo_invalid = parse_subjects(question)
                conflict = subject_type_conflict(subject_types, offered_subjects, question_combo if combo_provided and not combo_invalid else supplied_subjects)
                inherited = context.get("subject_type")
                selected_subject = None if conflict else subject_types[0] if len(subject_types) == 1 else primary if primary in offered_subjects else inherited if inherited in offered_subjects else None
                if conflict:
                    notes.append(conflict)
                    missing.append("subject_type")
                    candidates = []
                if selected_subject:
                    candidates = [x for x in candidates if x["subject_type"] == selected_subject]
                elif len({x["subject_type"] for x in candidates}) == 1:
                    selected_subject = candidates[0]["subject_type"]
                elif len({x["subject_type"] for x in candidates}) > 1:
                    notes.append("请确认科类：" + "、".join(sorted({x["subject_type"] for x in candidates})) + "。")
                    missing.append("subject_type"); candidates = []
                c["subject_type"] = selected_subject
                if not candidates and not missing:
                    notes.append("该年份、省份、招生类型、科类与专业组合没有计划记录，无法核对；不据此断言该专业在其他范围不招生。")
                for p in candidates:
                    req = p["payload"].get("electiveSubject")
                    personal = c.get("personal_subjects")
                    outcome = "官方未提供选科字段，无法核对" if not req else "官方要求，尚未核对个人选科"
                    if req and personal:
                        selected = set(personal.split("、"))
                        if req == "物理,化学(2门科目均须选考方可报考)":
                            outcome = "符合该项选科要求" if {"物理", "化学"} <= selected else "不符合该项选科要求（缺少" + "、".join(sorted({"物理", "化学"} - selected)) + "）"
                        elif req == "选考思想政治方可报考":
                            outcome = "符合该项选科要求" if "思想政治" in selected else "不符合该项选科要求（未选思想政治）"
                        elif req == "不提科目要求":
                            outcome = "符合该项选科要求（仍须属于本记录科类）"
                        else:
                            outcome = "无法自动核对该选科表述，请对照原文"
                    if personal and selected_subject in {"物理类", "历史类"} and selected_subject.removesuffix("类") not in personal:
                        outcome = "不符合该项选科要求（不符合该记录科类要求）"
                    row(f"选科｜{p['province']}·{p['subject_type']}·{p['admission_type']}·{p['major']}", req or "缺失（官网字段为空）", personal or "未提供完整个人选科组合", outcome)
                    r["evidence"].append(source_evidence(p))
                if not c.get("personal_subjects") and re.search(r"我.*(?:符合|能报|可以报|核对|满足)|帮我核对", question):
                    missing.append("personal_subjects")
    if "physical" in topics:
        physical = [x for x in rules if "physical" in x.get("fields", [x.get("field")])]
        if not target and not re.search(r"哪些|所有|全校|学校|体检.*要求|体检.*限制", question):
            missing.append("eligibility_target")
        else:
            cite(physical)
            personal = c.get("personal_color_vision")
            queried_color = queried_color_condition(question)
            color = queried_color or personal
            policy_query = bool(queried_color) and not supplied_personal.get("personal_color_vision")
            if policy_query:
                r["query_conditions"] = {"color_vision": queried_color, "scope": "current_question_only"}
            if color and target:
                matched = [x for x in physical if canon(target) in _restriction_targets(x["text"], color)]
                if matched and color != "无色盲色弱（自述）":
                    outcome = (f"{color}者不予录取，不能报考该专业（不符合该项体检要求）" if policy_query
                               else "不符合该项要求（官方明确列为不予录取）")
                    if policy_query:
                        notes.append(f"核对结果：{color}者不予录取，不能报考{target}。")
                        for x in matched:
                            parts = [p["text"] for p in x["payload"]["paragraphs"]]
                            heading = [t for t in parts if "不予录取" in t and not re.search(r"色弱|单色", t)]
                            clause = [t for t in parts if canon(target) in _restriction_targets(t, color)]
                            notes.append(f"依据{chosen}年《普通本科招生章程》{x['payload']['article']}：\n\n"
                                         + "\n\n".join("> " + t for t in [*heading, *clause]))
                elif color == "无色盲色弱（自述）":
                    outcome = "仅该项自述未触及色盲色弱限制；其他体检项目尚未核对"
                else:
                    outcome = "章程该病况名单未列此专业；不能据此判定体检合格"
                reqs = [x["text"] for x in physical if "单色" in x["text"]] or [x["text"] for x in physical]
                row("色觉｜" + target, "\n".join(reqs), f"本次询问条件：{color}（非个人自述）" if policy_query else personal, outcome)
            else:
                row("体检要求" + ("｜" + target if target else ""), "\n".join(x["text"] for x in physical), personal or "未提供完整体检结果", "请对照官方原文；未列明不代表无限制")
            if target and target in {"航海技术", "轮机工程"}:
                if c.get("personal_height"):
                    # 仅当同年原文明确含1.65米门槛才比较，保留其余条件。
                    reqtext = "\n".join(x["text"] for x in physical)
                    if re.search(r"1\.65\s*米\s*(?:及以上|以上)", reqtext):
                        h = float(c["personal_height"])
                        row("身高｜" + target, "1.65米及以上（另见完整航海类体检条款）", f"{h:g}米", "符合该项身高要求" if h >= 1.65 else "不符合该项身高要求")
                if c.get("personal_gender") in {"女生", "女性"} or "女生" in question:
                    row("报考提示｜" + target, "不适宜女生报考", c.get("personal_gender") or "本次询问女生报考提示", "官方不适宜报考提示，不改写为禁止录取")
            if c.get("personal_vision"):
                own = c["personal_vision"]
                if target in {"航海技术", "轮机工程"}:
                    reqtext = canon("\n".join(x["text"] for x in physical))
                    block = re.search(re.escape(target) + r"(?:[：:]|专业还要求考生)[^。\n]+", reqtext)
                    thresholds = re.findall(r"(?:双眼裸视力均能达到|矫正视力均能达到)([0-5]\.\d)", block[0]) if block else []
                    bare = re.search(r"双眼裸眼视力均为([0-5]\.\d)", own)
                    corrected = re.search(r"双眼矫正视力均为([0-5]\.\d)", own)
                    if len(thresholds) == 3 and bare:
                        b = float(bare[1]); direct, minimum, correction = map(float, thresholds)
                        if b >= direct or b >= minimum and corrected and float(corrected[1]) >= correction:
                            outcome = "符合该项视力数值要求；其他体检项目尚未核对"
                        elif b < minimum or corrected:
                            outcome = "不符合该项视力数值要求"
                        else:
                            outcome = "尚需双眼矫正视力，不能确认是否满足第二条路径"
                            missing.append("personal_vision")
                        row("视力数值｜" + target, block[0], own, outcome)
                    else:
                        missing.append("personal_vision")
                notes.append("视力、听力等个人描述不能形成完整体检结论；请对照双眼裸眼/矫正视力及其他官方条件，本系统不推断医学判定。")
    if "language" in topics:
        language = [x for x in rules if "language" in x.get("fields", [x.get("field")])]
        # 普遍语种条款可用于所有专业；法语注册等项目条款仅用于该项目。
        relevant = []
        for x in language:
            relevant.extend(_paragraphs(x, lambda t: bool(re.search(r"不限.*外语|不限制.*外语", t)) or (bool(target) and canon(target) in canon(t))))
        cite(relevant)
        if relevant:
            own = c.get("personal_foreign_language")
            outcome = "符合章程该项应试外语语种要求；教学语言及专业提示仍适用" if own else "官方要求，尚未核对个人外语"
            if own and target in {"航海技术", "轮机工程"} and own != "英语":
                outcome = "应试语种未被限制；官方建议非英语语种考生谨慎报考"
            universal = [x for x in relevant if re.search(r"不限.*外语|不限制.*外语", x["text"])]
            specific = [x for x in relevant if x not in universal]
            if universal:
                row("应试外语语种", "\n".join(x["text"] for x in universal), own or "未提供（一般查询不强制）", outcome)
            for x in specific:
                row("专业教学语言及后续安排", x["text"], "尚未提供相应学习或注册条件", "后续学习/注册要求尚未核对；不等同高考语种限制或禁止报考")
            if re.search(r"单科|外语.*成绩|英语.*(?:成绩|分)|日语.*分", question):
                notes.append("章程所示为语种与教学安排；现有证据未提供该专业外语单科成绩门槛，不能据此核对成绩是否达标。")
        else:
            notes.append("现有资料没有可确认的该项外语要求，不能据主要授课语言推断高考语种限制。")
    if "general" in topics:
        general = [x for x in rules if "general" in x.get("fields", [x.get("field")]) or re.search(r"专业志愿|男女比例|定向", x["text"])]
        focus = re.search(r"专项|志愿|定向|男女比例|女生|男生", question)
        tokens = [x for x in ("专项", "志愿", "定向", "男女比例") if x in question]
        if re.search(r"女生|男生", question):
            tokens.append("男女比例")
        relevant = []
        for x in general:
            relevant.extend(_paragraphs(x, lambda t: (not focus or any(k in t for k in tokens)) and (not re.search(r"生物科学类|中外合作|建筑类|航海技术", t) or not target or canon(target) in canon(t) or ("合作" in target and "中外合作办学专业仅" in t))))
        cite(relevant)
        for x in relevant:
            outcome = "已公开条件，尚未核对个人完整资格"
            if "专项计划录取后放弃" in x["text"] and re.search(r"我|本人", question) and re.search(r"放弃|退学", question):
                outcome = "若确属往年被专项计划录取后放弃入学资格或退学，不再具有专项计划报考资格"
            row("其他公开报考条件", x["text"], "见当前提问中的个人描述" if re.search(r"我|本人", question) else "未提供（一般查询不强制）", outcome)
    labels = {"eligibility_target": "具体专业或合作项目", "province": "生源省份", "admission_type": "招生类型", "subject_type": "科类", "personal_subjects": "完整个人选科组合（如物理、化学、生物）", "personal_vision": "双眼裸眼及矫正视力（请使用5分记录法的明确数值，注明双眼；近视度数不能直接换算）"}
    missing = list(dict.fromkeys(missing))
    prefix = f"{chosen}年｜{SCOPE_LABEL[scope]}" + (f"｜{target}" if target else "")
    message = prefix + "。\n\n" + "\n\n".join(notes)
    if missing:
        message += "\n\n请补充" + "、".join(labels[x] for x in missing) + "，以便核对相应项目。"
    if r["structured_table"]:
        checked = [x for x in r["structured_table"] if any(k in x["核对结果"] for k in ("符合", "未列", "自述", "谨慎", "不适宜"))]
        if checked:
            message += "\n\n" + "\n".join(f"- {x['核对项目']}：{x['核对结果']}。" for x in checked)
        message += "\n\n具体要求与逐项结果见下表及官方原文。核对只覆盖已提供条件，不代表完整报考资格或录取结果。"
    if missing:
        return finish("clarify", message.strip(), "missing_eligibility_conditions", missing)
    return finish("answered" if r["evidence"] else "refused", message.strip(), "official_eligibility_requirements" if r["evidence"] else "eligibility_evidence_missing")


def _taiwan(q, topic, target, rules, c, r, row, cite, finish):
    """特殊目录只在其招生范围内展示；不把科类或授课语言转写为普通高考门槛。"""
    if not target:
        return finish("clarify", "本次按台湾学测招生目录查询，请补充具体专业或专业大类。", "missing_target", ["eligibility_target"])
    matched = [x for x in rules if _major(x) and canon(_major(x)) == canon(target)]
    if not matched:
        return finish("refused", "该年台湾学测目录未找到此官方专业名称，不能使用其他年份或普通高考计划替代。", "catalogue_target_missing")
    cite(matched)
    for x in matched:
        row("台湾学测专业目录｜" + target, x["text"], "仅核对所示目录事项", "目录科类/主要授课语言不等同普通高考选科/应试外语限制")
    # 全部附注可含多个专业；仅按官方明确指向目标的完整注项引用。
    notes = [x for x in rules if not _major(x) and canon(target).split("(")[0] in canon(x["text"])]
    cite(notes)
    for x in notes:
        row("目录附注", x["text"], c.get("personal_color_vision") or "尚未提供完整个人条件", "请按该项原文核对；不扩大到内地普通高考")
    color = c.get("personal_color_vision")
    result_note = ""
    if color and topic in {"physical", "all"}:
        restricted = set().union(*(_restriction_targets(x["text"], color) for x in notes))
        outcome = "不符合该项要求（该年台湾学测目录明确限制）" if canon(target) in restricted and color != "无色盲色弱（自述）" else "目录证据不足以确认完整体检结果，不能判断合格"
        row("色觉｜台湾学测｜" + target, "以本次所示目录附注为准", color, outcome)
        result_note = "\n\n色觉核对：" + outcome + "。"
    return finish("answered", f"{c['year']}年｜台湾学测招生｜{target}。本次仅依据该范围的官方目录；未导入台湾学测报名资格完整材料，无法确认完整报考资格。目录表和附注见下表与来源。" + result_note, "taiwan_catalogue_requirements")
