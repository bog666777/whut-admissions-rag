"""当年计划与选科筛选，以及同口径历史位次的逐年数值对照。

不以历史位次/分数裁掉候选，不推断录取概率；本模块不保存用户查询。
"""
from __future__ import annotations

from decimal import Decimal
import re
import time

from whut_rag.structured_store import load_records, structured_status
from whut_rag.structured_query import canon, unique_mentions, source_evidence
from whut_rag.history_compare import compare_history
from whut_rag.fee_query import FEE_WORDS
from whut_rag.query_conditions import (mask_school_names, subject_type_mentions, subject_type_conflict,
                                     parse_subjects, year_mentions, SUBJECT_WORDS, SUBJECT_ORDER, SHORT)
OPEN = r"哪些专业|什么专业|候选专业|专业筛选|筛选专业|全部专业|所有专业|不限专业|不限定专业|开放式筛选"
EXPLICIT = r"候选|筛选.*专业|专业.*筛选|历史相对位置|相对历史|历史位次比较|比较历史位次|(?:能|可)(?:以|否|不能)?报(?:考)?(?:哪些|什么)专业|(?:可以|可|能)(?:关注|考虑|选择)(?:哪些|什么)专业|(?:可以|可|能)(?:上|进)(?:武汉理工大学|武汉理工|本校)?\s*(?:的)?(?:哪些|什么)专业"
FOLLOWUP_END = r"呢[\s？?。.!！]*$"
SCHOOL_ADMISSION = r"(?:能|可以)(?:上|进)(?:武汉理工大学|武汉理工|本校)(?:吗|呢|[？?]|$)"
OTHER_DOMAIN = r"学费|收费|住宿费|培养模式|培养方式|停招|是否招生|招生吗|调剂规则"


def _natural_open_request(q: str, context: dict) -> bool:
    """有限的关注/考虑句法需有报考条件支撑，不将教学或就业方向当候选。"""
    preference = bool(re.search(r"关注|考虑|值得看|值得了解|想看看", q))
    choice = bool(re.search(r"(?:哪些|什么|有啥|有没有|有无).{0,8}(?:专业|方向)", q)
                  or re.search(r"这(?:个)?(?:成绩|分数).{0,12}(?:关注|考虑)", q))
    explicit_profession = preference and choice and "专业" in q or bool(
        re.search(r"推荐(?:些|一些|点|几个)?(?:能报|可报|可考虑|可以考虑)(?:考)?(?:的)?专业", q))
    if explicit_profession:
        return True
    chance = bool(re.search(r"这(?:个)?(?:成绩|分数).{0,8}有希望", q))
    if not (preference and choice or chance):
        return False
    supplied = (_number(q, "score")[1] or _number(q, "rank")[1]
                or parse_subjects(q)[1] or bool(re.search(r"报考|填报|志愿|考生", q)))
    remembered = context.get("active_domain") in {"candidates", "structured", "eligibility", "compound"} and any(
        context.get(key) for key in ("candidate_score", "candidate_rank", "personal_subjects", "province", "subject_type"))
    return bool(supplied or remembered)


def _generic_open_followup(q: str) -> bool:
    """仅排除无新对象的开放续问；残留专业名等实词仍交给目标确认。"""
    text = mask_school_names(q)
    words = {
        "那么", "那", "还有", "还", "我", "这个", "这", "当前", "本次", "的", "成绩", "分数",
        "哪些", "什么", "有啥", "有没有", "有无", "专业", "方向", "候选", "全部", "所有",
        "可以", "能够", "能", "可", "考虑", "关注", "值得看看", "值得看", "值得了解", "想看看",
        "看看", "了解", "选择", "推荐", "一些", "几个", "请", "帮我", "呢", "吗",
    }
    text = re.sub("|".join(map(re.escape, sorted(words, key=len, reverse=True))), "", text)
    return not re.sub(r"[\s，。！？、,!?；;：:]", "", text)


def candidate_intent(q: str, context: dict | None = None) -> bool:
    context = context or {}
    if re.search(r"(?:系统|知识库|你).*(?:支持|覆盖|收录)", q) or re.search(OTHER_DOMAIN, q) or re.search(FEE_WORDS, q):
        return False
    if re.search(r"课程|专业课|培养(?:方向|目标|内容|安排)|研究方向|就业|职业方向|专业介绍|学习内容|学什么", q):
        return False
    explicit = bool(re.search(EXPLICIT, q))
    school_admission = bool(re.search(SCHOOL_ADMISSION, q))
    natural = _natural_open_request(q, context)
    profile = bool(_number(q, "score")[0] and _number(q, "rank")[0])
    screening_request = bool(re.search(r"候选|筛选.*专业|专业.*筛选|历史相对位置|相对历史|历史位次比较|比较历史位次",q))
    if re.search(r"体检|色弱|色盲|视力|身高|选科要求|选考要求|报考条件|报考要求|报名条件|外语要求|语种要求", q) and not screening_request:
        return False
    if re.search(r"最低分|最高分|平均分|录取数据|招生计划人数|计划人数", q) and not explicit:
        return False
    if explicit or school_admission or natural or (profile and re.search(r"报|专业|类|比较|相对|查询", q)):
        return True
    if context.get("pending_intent") == "candidates":
        return not bool(re.search(r"招生计划|录取分数|体检|选科要求|教学|证书", q))
    if context.get("active_domain") == "candidates":
        return bool(re.search(r"^(?:那|改|换|只查|只看|查询|我|位次|排名|排位|分数|总分|成绩)|" + FOLLOWUP_END, q)) and not bool(re.search(r"招生计划|录取分数|体检|选科要求|教学|证书", q))
    return False


def _number(q: str, field: str) -> tuple[str | None, bool, bool]:
    if field == "score":
        label = r"(?:分数|总分|成绩|考了|考得|考出)"
        patterns = [label + r"(?:是|为|[:：])?\s*(-?\d+(?:\.\d+)?)(?![\d.千百多余])", r"(?<![\d.])(-?\d+(?:\.\d+)?)\s*分(?!数|省|类)"]
        unknown = label + r".{0,4}(?:未知|不知道|不清楚|没有)|(?:不知道|没有).{0,3}(?:分数|总分|成绩)"
        declared = bool(re.search(label + r"(?:是|为|[:：])?\s*(?:约|大概|前|大于|小于|超过|不到|[-\d.])|\d+(?:\.\d+)?(?:\s*[-~—至到或]\s*\d+(?:\.\d+)?|\s*(?:千|百|多|余|左右))?\s*分(?!数|省|类)",q))
    else:
        label = r"(?:位次|排名|排位|名次|(?:全省|省内)排(?:名|位)?|我(?:在)?(?:全省|省内)?排(?:名|位)?)"
        value = r"(-?(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?万|\d+))"
        patterns = [label + r"(?:是|为|[:：])?\s*" + value + r"(?![\d.,千百多余万])", r"(?<![\d.])" + value + r"\s*名"]
        unknown = label + r".{0,4}(?:未知|不知道|不清楚|没有)|(?:不知道|没有).{0,3}(?:位次|排名|排位)"
        declared = bool(re.search(label + r"(?:是|为|[:：])?\s*(?:约|大概|前|大于|小于|超过|不到|[-\d.])|\d+(?:\.\d+)?(?:\s*[-~—至到或]\s*\d+(?:\.\d+)?|\s*(?:千|百|多|余|左右))?\s*名",q))
        if re.search(label + r"(?:是|为|[:：])?\s*(?:约|大概|前|大于|小于|超过|不到)\s*\d", q):
            return None, True, True
    if re.search(unknown, q):
        return None, True, True
    matches = [m for pattern in patterns for m in re.finditer(pattern, q)]
    if not matches:
        return None, declared, declared
    if any(re.search(r"(?:约|大概|前|大于|小于|超过|不到|\d\s*[-~—至到或])\s*$", q[max(0, m.start()-12):m.start()]) or re.match(r"\s*(?:左右|以上|以下|多|余|[-~—至到或]\s*\d)", q[m.end():]) for m in matches):
        return None, True, True
    numbers = set()
    for m in matches:
        raw = m[1].replace(",", "")
        number = Decimal(raw[:-1])*10000 if raw.endswith("万") else Decimal(raw)
        if number <= 0 or field == "rank" and number != number.to_integral_value():
            return None, True, True
        numbers.add(str(int(number)) if number == number.to_integral_value() else format(number.normalize(), "f"))
    return (next(iter(numbers)) if len(numbers) == 1 else None), True, len(numbers) != 1


def _target_mentions(q: str, vocabulary: set[str], available: list[dict]) -> list[str]:
    found = unique_mentions(q, vocabulary)
    if not found:
        # 仅允许省略“含……”说明；班型、项目与合作方属于身份，不能删掉后擅自指定。
        found = [m for m in vocabulary if "(含" in canon(m) and not re.search(r"班|项目|合作|培养",m)
                 and len(canon(m).split("(")[0]) >= 3 and canon(m).split("(")[0] in canon(q)]
    if len(found) > 1:
        narrowed = [m for m in found if any(canon(m) == canon(x["major"]) for x in available)]
        if narrowed:
            found = narrowed
    return sorted(set(found))


def _profile_parts(conditions: dict) -> list[str]:
    system = "艺术" if "艺术" in str(conditions.get("admission_type") or "") + str(conditions.get("subject_type") or "") else "普通高考"
    return [str(conditions.get(k) or "") for k in ("year","province","subject_type")] + [system]


def _subject_result(plan: dict, subjects: set[str]) -> tuple[str, str]:
    primary = plan["subject_type"]
    if primary in {"物理类", "历史类"} and primary[:-1] not in subjects:
        return "excluded", f"不符合记录科类：{primary}"
    req = plan["payload"].get("electiveSubject")
    if req == "物理,化学(2门科目均须选考方可报考)":
        absent = {"物理", "化学"} - subjects
        return ("excluded", "缺少" + "、".join(sorted(absent))) if absent else ("matched", "符合该项选科要求")
    if req == "选考思想政治方可报考":
        return ("matched", "符合该项选科要求") if "思想政治" in subjects else ("excluded", "缺少思想政治")
    if req == "不提科目要求":
        return "matched", "符合该项选科要求（本记录不提科目要求）"
    return "unconfirmed", "官方选科字段缺失，无法确认" if req is None or not str(req).strip() else "选科表述尚未支持自动核对，请核对原文"


def _group_sources(evidence: list[dict]) -> list[dict]:
    groups = {}
    for e in evidence:
        key = (e["source_id"], e["year"], e["url"])
        group = groups.setdefault(key, {k:e[k] for k in ("source_id", "title", "year", "url")})
        group.setdefault("records", []).append({"record_id":e["chunk_id"], "label":e.get("label", e["chunk_id"]), "location":e["location"], "text":e["text"]})
    return list(groups.values())


def answer_candidates(question: str, year: int | None = None, context: dict | None = None) -> dict | None:
    context = context or {}
    if not candidate_intent(question, context):
        return None
    started = time.perf_counter()
    c = {"intent":"candidates", "active_domain":"candidates", "pending_intent":None, "dataset":None, "granularity":"major", "project":None, "eligibility_target":None}
    r = {"status":"clarify", "answer":"", "reason":"", "conditions":c, "evidence":[], "structured_table":[], "structured_count":0, "candidate_summary":{}, "candidate_unconfirmed":[], "candidate_excluded":[], "candidate_source_groups":[], "timings":{}}

    def finish(status, answer, reason, missing=None):
        r.update(status=status, answer=answer, reason=reason, structured_count=len(r["structured_table"]))
        c["pending_intent"] = "candidates" if missing else None
        if missing:
            r["missing_fields"] = list(dict.fromkeys(missing))
        r["timings"]["total_seconds"] = round(time.perf_counter()-started, 3)
        return r

    def cite(record):
        e = source_evidence(record)
        e["label"] = f"{record['major']}｜{record['province']}·{record['subject_type']}·{record['admission_type']}"
        if not any(x["chunk_id"] == e["chunk_id"] for x in r["evidence"]):
            r["evidence"].append(e)

    years = year_mentions(question)
    if len(years) > 1:
        c["year"] = None
        return finish("clarify", "请明确本次填报的一个年份；历史数据年份由系统逐年列出。", "multiple_target_years", ["year"])
    chosen = next(iter(years)) if years else year if year is not None else context.get("year")
    c.update(year=chosen, year_source="question" if years else "selector" if year is not None else "session" if chosen else None)
    if chosen is not None:
        r["confirmed_year"] = chosen
    if chosen not in {None,2024,2025,2026}:
        return finish("refused", "填报年份超出2024—2026年知识库边界，不能借用其他年份的计划。", "out_of_scope_year")
    subjects, subjects_given, subjects_invalid = parse_subjects(question)
    c["personal_subjects"] = subjects if subjects_given else context.get("personal_subjects")
    invalid = []
    given_numbers = {}
    for field, key in (("score","candidate_score"),("rank","candidate_rank")):
        number, given, bad = _number(question, field)
        given_numbers[key] = given
        c[key] = number if given else str(context[key]) if context.get(key) is not None else None
        if bad:
            invalid.append(key)
    if subjects_invalid:
        invalid.append("personal_subjects")
    if not structured_status().get("ready"):
        return finish("error", "结构化资料尚未就绪或完整性核验未通过，暂不能筛选。", "structured_not_ready")
    records = load_records()
    plans = [x for x in records if x["dataset"] == "plan" and x["granularity"] == "major"]
    admission = [x for x in records if x["dataset"] == "admission" and x["granularity"] == "major"]
    provinces = unique_mentions(question, {x["province"] for x in plans})
    if len(provinces) > 1:
        c["province"] = None
        return finish("clarify", "请明确本次填报的一个省份；不同省份的位次不能合并比较。", "multiple_provinces", ["province"])
    c["province"] = provinces[0] if provinces else context.get("province")
    available = [x for x in plans if (chosen is None or x["year"] == chosen) and (not c["province"] or x["province"] == c["province"])]
    types = unique_mentions(question, {x["admission_type"] for x in records})
    if not types and re.search(r"普通(?:类|招生)?", question):
        types = ["普通类"]
    if not types and "国家专项" in question:
        types = ["国家专项计划"]
    if not types and "高校专项" in question:
        types = ["高校专项计划"]
    if not types and re.search(r"中外|合作办学", question):
        types = sorted({x["admission_type"] for x in available if "中外" in x["admission_type"]})
    c["admission_type"] = types[0] if len(types) == 1 else None if types else context.get("admission_type")
    if c["admission_type"]:
        available = [x for x in available if x["admission_type"] == c["admission_type"]]
    subject_types = subject_type_mentions(question, {x["subject_type"] for x in records}, ignore_terms=types)
    offered_types = {x["subject_type"] for x in available}
    combo = set(str(c.get("personal_subjects") or "").split("、"))
    subject_conflict = subject_type_conflict(subject_types, offered_types, c.get("personal_subjects"))
    if subject_conflict:
        c["subject_type"] = None
        invalid.append("subject_type")
    elif subject_types:
        c["subject_type"] = subject_types[0]
    elif len(offered_types) == 1:
        c["subject_type"] = next(iter(offered_types))
    elif ("物理" in combo) != ("历史" in combo) and {"物理类","历史类"} & offered_types:
        c["subject_type"] = "物理类" if "物理" in combo else "历史类"
    else:
        inherited_subject = context.get("subject_type")
        c["subject_type"] = inherited_subject if context.get("active_domain") == "candidates" and inherited_subject in offered_types else None
    if c.get("subject_type"):
        available = [x for x in available if x["subject_type"] == c["subject_type"]]
    vocabulary = set({canon(x["major"]):x["major"] for x in records if x["major"]}.values())
    targets = _target_mentions(question, vocabulary, available)
    explicit_open = bool(re.search(OPEN, question) or re.search(r"筛选.*专业", question)
                         or re.search(SCHOOL_ADMISSION, question)
                         or (_natural_open_request(question, context)
                             and not re.search(r"这(?:个)?(?:成绩|分数).{0,8}有希望", question)))
    bare_new = (bool(re.search(r"^(?:那|改查|换成|改成|只查|只看)", question))
                and not (explicit_open and _generic_open_followup(question))
                and not any((subjects_given, re.search(r"20\d{2}|位次|排名|分|全部专业|所有专业|不限专业|选科|普通|国家专项|高校专项", question), provinces)))
    target_phrases = re.findall(r"(?:我(?:想|要)(?:学|报考|报)|目标(?:是|为)?|指定|只查|只看|查询|筛选|换成|改成|改查)([\u4e00-\u9fff]{2,20}?)(?=的|专业|历史|候选|[，。！？?]|$)", question)
    generic = r"候选|可关注|可报考|可报|符合|条件|选科|合适|全部|所有|不限|不限定|专业大类|专业|科类|历史|相对|位置|数据|位次|查询|这些|这个|上述|本次|哪些|什么|为我|帮我|请|查看|的"
    unknown_phrase = any(re.sub(generic,"",text).strip() for text in target_phrases)
    # 专业简称续问仍按新目标识别；不能因句末“呢”沿用旧专业或退回全专业。
    object_text = mask_school_names(re.sub(FOLLOWUP_END, "", question)).strip()
    for word in sorted({x[k] for x in records for k in ("province","subject_type","admission_type")} | {"武汉理工大学"},key=len,reverse=True):
        object_text = object_text.replace(word," ")
    bare_objects = re.findall(r"([\u4e00-\u9fff]{2,20}(?:工程|技术|学|类))(?=的|专业|历史|候选|[，。！？?]|$)",object_text)
    unknown_phrase = unknown_phrase or any(re.sub(generic+r"|大学|高校|学校|本科","",text).strip() for text in bare_objects)
    suggestions = [] if targets else sorted(m for m in vocabulary if len(canon(m).split("(")[0]) >= 3 and canon(m).split("(")[0] in canon(question))
    referent = bool(re.search(r"这个专业|该专业|上述专业|这个项目|该项目", question))
    if re.search(FOLLOWUP_END, question) and not any((targets, subjects_given, provinces, types, any(given_numbers.values()), explicit_open, referent)):
        unknown_phrase = unknown_phrase or bool(re.sub(generic + r"|^(?:那么|那|还有)", "", object_text).strip())
    unknown_target = not targets and (unknown_phrase or suggestions or bool(re.search(r"火星|不存在", question)) or bare_new)
    if referent and not unknown_phrase and context.get("active_domain") == "candidates" and context.get("candidate_target"):
        unknown_target = False
    if len(targets) > 1:
        c.update(candidate_mode="target", candidate_target=None, major=None)
        return finish("clarify", "请明确本次指定的一个官方专业或专业大类：" + "、".join(targets) + "。", "ambiguous_candidate_target", ["candidate_target"])
    target = targets[0] if targets else None if explicit_open or unknown_target else context.get("candidate_target") if context.get("active_domain") == "candidates" else None
    mode = "target" if target or unknown_target else "open" if explicit_open else (context.get("candidate_mode") or "open") if context.get("active_domain") == "candidates" else "open"
    c.update(candidate_target=target, candidate_mode=mode, major=target)
    current_scope = _profile_parts(c)
    old_scope = str(context.get("candidate_profile_scope") or "").split("|")
    if len(old_scope) != 4:
        old_scope = _profile_parts(context) if context.get("active_domain") == "candidates" else []
    changed_scope = bool(old_scope) and any(current and old and current != old for current,old in zip(current_scope,old_scope))
    if changed_scope:
        for key in ("candidate_score","candidate_rank"):
            if not given_numbers[key]:
                c[key] = None
    c["candidate_profile_scope"] = "|".join(current_scope) if all(current_scope[:3]) else context.get("candidate_profile_scope")
    if "专业组" in question:
        return finish("refused", "现有资料没有专业组或组号字段，不能将学校汇总或专业大类当作专业组筛选。", "professional_group_missing")
    if re.search(r"台湾|学测|香港|澳门|港澳|华侨|联招", question):
        return finish("refused", "本功能使用现有分省普通高考计划及同口径历史录取，未导入该特殊招生范围的完整计划和位次数据，不能套用。", "candidate_scope_missing")
    if chosen is not None and not any(x["year"] == chosen for x in plans):
        return finish("refused", f"现有资料没有{chosen}年招生计划，不能用2026年计划筛选该年的候选，也不能用历史录取代替计划。", "candidate_plan_year_missing")
    labels = {"year":"填报年份", "province":"省份", "admission_type":"招生类型", "personal_subjects":"完整的3门选科（如物理、化学、生物）", "candidate_score":"本次分数", "candidate_rank":"本次准确位次", "subject_type":"科类", "candidate_target":"指定专业的官方名称", "candidate_mode":"查询方式（筛选全部候选，或指定一个专业）"}
    missing = [k for k in ("year","province","admission_type","personal_subjects","candidate_score","candidate_rank") if not c.get(k)]
    if not c.get("subject_type") and c.get("year") and c.get("province") and c.get("admission_type") and c.get("personal_subjects"):
        missing.append("subject_type")
    if mode is None:
        missing.append("candidate_mode")
    if unknown_target or mode == "target" and not target:
        missing.append("candidate_target")
    missing = list(dict.fromkeys([*invalid,*missing]))
    for key, pattern in (("candidate_rank",r"[1-9]\d*"),("candidate_score",r"(?:[1-9]\d*(?:\.\d+)?|0\.\d*[1-9]\d*)")):
        if c.get(key) and not re.fullmatch(pattern,str(c[key])):
            c[key] = None
            missing.append(key)
    if missing:
        hints = ""
        if len(types)>1:
            hints += "当前招生类型候选：" + "、".join(types) + "。"
        if "subject_type" in missing and offered_types:
            hints += "该条件下科类：" + "、".join(sorted(offered_types)) + "。"
        if subject_conflict:
            hints += subject_conflict
        if suggestions:
            hints += "存在的正式对象包括：" + "、".join(suggestions) + "。请明确班型或合作项目，不按简称自动指定。"
        if changed_scope and any(k in missing for k in ("candidate_score","candidate_rank")):
            hints += "年份、省份、科类或普通/艺术成绩口径已变化，请重新提供该口径的分数和准确位次。"
        impacts = []
        if "year" in missing:
            impacts.append("年份用于匹配当年计划")
        if any(k in missing for k in ("province", "subject_type", "admission_type")):
            impacts.append("省份、科类和招生类型用于保持计划及历史数据口径一致")
        if "personal_subjects" in missing:
            impacts.append("完整选科用于筛查专业要求")
        if "candidate_rank" in missing:
            impacts.append("准确位次用于历史相对位置对照，不能仅凭分数反推位次")
        if impacts:
            hints += "补充这些条件是因为：" + "；".join(impacts) + "。"
        return finish("clarify", "请补充或明确" + "、".join(labels[k] for k in missing) + "。" + hints + ("未知专业不会被替换为全部专业或沿用旧目标。" if unknown_target else ""), "missing_candidate_conditions", missing)
    if target:
        available = [x for x in available if canon(x["major"]) == canon(target)]
    available.sort(key=lambda x:(canon(x["major"]),x["record_key"]))
    if not available:
        return finish("refused", "当年计划未找到与上述省份、科类、招生类型及指定专业一致的记录，无法筛选；不代表该专业在其他范围没有招生。", "no_candidate_plan_record")
    if len(combo) != 3 or not combo <= set(SUBJECT_ORDER):
        c["personal_subjects"] = None
        return finish("clarify", "请提供完整且不重复的3门选科。", "invalid_subject_combination", ["personal_subjects"])
    matched = []
    outcomes = {}
    for p in available:
        state, explanation = _subject_result(p,combo)
        outcomes[p["record_key"]] = explanation
        cite(p)
        if state == "matched":
            matched.append(p)
        else:
            row = {"专业/专业大类":p["major"], "筛选结果":explanation, "官方选科要求":p["payload"].get("electiveSubject") or "缺失（官网字段为空）", "计划记录编号":p["record_key"]}
            r["candidate_excluded" if state == "excluded" else "candidate_unconfirmed"].append(row)
    comparable_objects = 0
    history_keys = []
    user_rank = int(c["candidate_rank"])
    for p in matched:
        history = compare_history(p, admission, chosen, user_rank)
        comparable_objects += any(h["historical_min_rank"] is not None and not h["missing_reason"] for h in history)
        for h in history:
            for old in h.get("records",[]):
                cite(old)
            history_keys.extend(h["historical_record_keys"])
            row = {"专业/专业大类":p["major"], "历史年份":h["year"] if h["year"] else "无可用更早年份", "历史最低录取位次":h["historical_min_rank"] if h["historical_min_rank"] is not None else "缺失/不可比较", "用户位次":user_rank, "位次差（用户－历史）":h["delta"] if h["delta"] is not None else "无法计算", "相对位置":h["position"], "历史数据说明":h["missing_reason"] or "同省、同科类、同类型、同官方专业名称", "计划人数":p["payload"].get("recruitNum"), "本年选科核对":outcomes[p["record_key"]], "本年选科要求":p["payload"].get("electiveSubject"), "计划记录编号":p["record_key"], "历史记录编号":"；".join(h["historical_record_keys"]) or "无匹配记录"}
            if h.get("history_remarks"):
                row["历史原备注"] = h["history_remarks"]
            r["structured_table"].append(row)
    r["candidate_summary"] = {"当年匹配计划记录数":len(available), "选科符合专业数":len(matched), "选科不符合专业数":len(r["candidate_excluded"]), "选科待核对专业数":len(r["candidate_unconfirmed"]), "有可比历史的专业数":int(comparable_objects), "无可比历史的专业数":len(matched)-int(comparable_objects)}
    r["candidate_source_groups"] = _group_sources(r["evidence"])
    r["query"] = {"filters":dict(c), "matched_plan_record_keys":[x["record_key"] for x in matched], "historical_record_keys":history_keys}
    r["answer_mode"] = "official_candidate_and_historical_rank_comparison"
    text = f"本次条件：{chosen}年，{c['province']}，{c['subject_type']}，{c['admission_type']}；选科{c['personal_subjects']}，分数{c['candidate_score']}分，位次{user_rank}名。"
    text += f"\n\n{'仅查询' + target + '。' if target else '按当年计划进行开放式专业筛选。'}匹配{len(available)}条专业计划，其中{len(matched)}个专业/专业大类符合本次选科要求，{len(r['candidate_excluded'])}个不符合，{len(r['candidate_unconfirmed'])}个选科要求缺失或待核对。"
    if matched:
        text += f"\n\n{int(comparable_objects)}个候选至少有一年可比较的历史位次，{len(matched)-int(comparable_objects)}个候选暂无可比较的历史位次；各年分别列出，缺失不补值。"
        if len(matched) == 1:
            for row in r["structured_table"]:
                text += f"\n- {row['历史年份']}：历史最低录取位次{row['历史最低录取位次']}；{row['相对位置']}；位次差{row['位次差（用户－历史）']}。" + (f"{row['历史数据说明']}。" if row['历史最低录取位次'] == '缺失/不可比较' else "")
    text += "\n\n位次差＝用户位次－历史最低录取位次；负数表示用户位次数字较小，正数表示数字较大，0表示相同。候选按官方专业名称展示，未按历史位次裁掉候选。"
    text += "\n\n分数只作为本次条件记录，不直接与往年分数比较。只匹配同省、同科类、同招生类型、同官方名称的专业或大类；不自动对应改名专业、不同合作项目，也不使用学校汇总或专业组替代。"
    text += "\n\n当前筛选仅核对计划和选科，体检、外语及专项等其他资格仍需另行核对。各年计划、专业大类组成和规则可能变化，历史相对位置不代表录取概率、风险等级或录取保证。同一计划人数随历史年份重复展示时不可相加。"
    return finish("answered",text,"verified_candidate_screening")
