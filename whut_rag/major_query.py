"""专业介绍及同年度、同招生范围的证据对照；不从专业名称补写课程。"""
from __future__ import annotations
from whut_rag.query_conditions import year_mentions

import re
import time

from whut_rag.structured_query import canon, unique_mentions

PROFILE_WORDS = r"专业介绍|介绍|学什么|学习什么|学习内容|培养内容|培养目标|课程|专业方向|分流|专业大类.*(?:含|包括)|(?:包含|包括|所含|含有|有哪些).*专业|专业.*(?:包含|包括|所含)|比较|对比|区别|差异"
OTHER_WORDS = r"学费|收费|住宿|多少钱|最低分|最高分|平均分|录取线|录取分|录取数据|位次|排名|排位|候选|筛选|招生计划|计划人数|计划数|招生人数|选科|选考|体检|色弱|色盲|报考条件|报考要求|报名条件|外语要求|语种要求|调剂|退档|招生吗|是否招生|停招|招生章程|招生政策"
DIMENSIONS = ("大类所含专业", "所属专业大类", "培养目标", "核心课程", "特色课程", "学习或培养内容", "培养安排", "大类分流安排", "合作院校", "办学地点", "教学语言", "学历学位")
SCOPES = {"general": "普通本科专业/项目资料（大类包含关系以已收录分省计划为据）", "taiwan_gsat": "台湾学测招生专用目录，不能代替普通高考资料"}
REFERENTS = r"这个专业|该专业|上述专业|这个大类|该大类|上述大类|这个项目|该项目|上述项目|这两个专业|两者|它|其"


def _comparison_method(question: str) -> bool:
    """比较分数的方法咨询交给咨询入口，不按专业介绍或年度字段查询。"""
    return bool(re.search(r"分数|成绩|位次|排名|排位", question)
                and re.search(r"比较|对比|参照|参考", question)
                and re.search(r"为什么|为何|原因|原理|怎么|如何|怎样|能否|是否|能不能|可不可以", question))


def _profile_followup_only(question: str) -> bool:
    """只含指代/字段问法才沿用对象，剩余的新专业名称不得被吞掉。"""
    text = re.sub(r"20\d{2}年?|武汉理工大学", "", question)
    words = (
        REFERENTS + r"|大类分流安排|专业分流安排|分流安排|大类分流|专业分流|分流"
        r"|培养安排|培养模式|培养方式|办学模式|核心课程|特色课程|主要课程|课程设置|课程"
        r"|培养目标|学习内容|培养内容|学历学位|学位|学历|证书|办学地点|校区|教学语言|授课语言"
        r"|如果资料没有|如果资料未收录|资料没有|资料未收录|请告诉我|告诉我|去哪里核实|在哪里核实|到哪里核实"
        r"|介绍一下|介绍|详细|具体|是什么|怎么样|什么时候|有哪些|如何|怎么|怎样|何时|哪种|核实"
        r"|请问|请|那|的|呢|吗|一下"
    )
    text = re.sub(words, "", text)
    return not re.sub(r"[\s，,。.!！?？：:；;（）()、]", "", text)


def _project_identity(name: str, profiles: list[dict], year: int, scope: str) -> dict | None:
    """仅把同年原文确认的专业+伙伴绑定为既有文本引擎的官方项目身份。"""
    if year != 2026 or scope != "general":
        return None
    partners = {p["fields"].get("合作院校") for p in profiles
                if p["name"] == name and p["year"] == year and p["scope"] == scope}
    partners.discard(None)
    partners.discard("")
    if len(partners) != 1:
        return None
    from whut_rag.core import project_catalog
    partner = next(iter(partners))
    matches = [p for p in project_catalog() if p["year"] == year
               and canon(p["partner"]) == canon(partner)
               and canon(p["base_name"]) == canon(name).split("(")[0]]
    return matches[0] if len(matches) == 1 else None


def major_intent(question: str, context: dict | None = None) -> bool:
    context = context or {}
    if _comparison_method(question):
        return False
    if re.search(r"(?:系统|知识库|你).*(?:支持|覆盖|收录)", question) or re.search(OTHER_WORDS, question):
        return False
    if re.search(r"\d+(?:\.\d+)?\s*分|我选|我的选科", question):
        return False
    if re.search(PROFILE_WORDS, question):
        return True
    active = context.get("active_domain") == "major_info" or context.get("pending_intent") == "major_info"
    if context.get("pending_intent") == "major_info":
        return True
    if active and re.search(r"呢[\s？?。.!！]*$|培养|课程|学位|校区|办学地点|^那|^改|^换|^只|^20\d{2}|这个专业|该专业|这个项目|该项目", question):
        return True
    if re.search(r"培养安排|培养方式|培养模式", question) and not context.get("project") and not re.search(r"合作|中外|项目|蒙纳士|纽卡斯尔|威尔士|布鲁内尔|艾克斯|这个|该专业", question):
        return True
    # 保留已经确认的候选、录取、收费和资格核对目的；无场景的纯专业名视作介绍。
    if context.get("active_domain") not in {None, "text", "major_info"} or context.get("pending_intent"):
        return False
    if re.search(r"培养模式|培养方式|办学模式|培养安排|教学语言|授课语言|证书", question):
        return False  # 既有合作项目文字链路继续负责这些明确问法。
    from whut_rag.major_store import load_profiles
    bare = re.sub(r"20\d{2}年?|武汉理工大学|专业|[\s？?。！!]", "", question)
    try:
        return any(bare and bare == re.sub(r"专业", "", canon(r["name"])) for r in load_profiles())
    except (OSError, ValueError):
        return False


def _partners(q: str, records: list[dict]) -> list[str]:
    names = []
    for r in records:
        partner = r.get("fields", {}).get("合作院校")
        if not partner:
            continue
        clean = re.sub(r"^(澳大利亚|英国|法国)", "", partner)
        short = re.sub(r"^(伦敦|威尔士)", "", clean)
        aliases = {partner, clean, short, short.removesuffix("大学")}
        if any(len(x) >= 3 and canon(x) in canon(q) for x in aliases):
            names.append(r["name"])
    return sorted(set(names))


def _names(q: str, records: list[dict]) -> tuple[list[str], list[str]]:
    q = re.sub(r"(?:不是|不属于|不看|不要|不查|非)\s*(?:中外合作办学|中外合作|合作办学|中外)", "", q)
    vocabulary = {r["name"] for r in records}
    matched = unique_mentions(q, vocabulary)
    # 未知括号限定不能退化成同名前缀的普通专业或大类。
    matched = [n for n in matched if not re.search(re.escape(canon(n)) + r"\(", canon(q))]
    aliases = {}
    for r in records:
        for a in r.get("aliases", []):
            if a:
                aliases.setdefault(a, set()).add(r["name"])
    for a in unique_mentions(q, set(aliases)):
        matched.extend(aliases[a])
    partners = _partners(q, records)
    cooperation = bool(re.search(r"中外|合作办学|中英|中澳|中法|合办", q) or partners)
    keep_ordinary = bool(re.search(r"比较|对比|区别|差异", q) and re.search(r"普通", q))
    if cooperation:
        base_hits = [name for name in vocabulary if "中外合作办学" in canon(name) and canon(name).split("(")[0] in canon(q)]
        project_hits = [name for name in base_hits if not partners or name in partners]
        if not base_hits and partners:
            project_hits = partners
        if project_hits:
            # 同一基础名下的普通专业不可以替代合作项目。
            bases = {canon(n).split("(")[0] for n in project_hits}
            matched = [n for n in matched if keep_ordinary or canon(n).split("(")[0] not in bases or n in project_hits]
            if keep_ordinary:
                matched.extend(n for n in vocabulary if canon(n) in bases and re.search(r"普通(?:专业)?(?:的)?" + re.escape(canon(n)), canon(q)))
            matched.extend(project_hits)
        elif partners:
            return [], partners
    found = sorted(set(matched))
    found = [x for x in found if not any(x != y and canon(x) in canon(y) and not (keep_ordinary and "中外合作办学" in canon(y) and canon(x) == canon(y).split("(")[0]) for y in found)]
    found = [n for n in found if not re.search(re.escape(canon(n)) + r"\(", canon(q))]
    # 完整限定名称不命中时仅提示正式候选，不把普通专业改成某个班型。
    suggestions = []
    if not found:
        suggestions = sorted(n for n in vocabulary if len(canon(n).split("(")[0]) >= 3 and canon(n).split("(")[0] in canon(q))
    return found, suggestions


def _requested_fields(q: str) -> set[str]:
    fields = set()
    for pattern, names in (
        (r"培养目标", {"培养目标"}),
        (r"核心课程|主要课程|课程设置|学什么|学习什么", {"核心课程", "特色课程"}),
        (r"特色课程", {"特色课程"}),
        (r"学习内容|培养内容|专业方向", {"学习或培养内容", "培养目标", "核心课程", "特色课程"}),
        (r"包含|包括|所含|含有|有哪些.*专业", {"大类所含专业"}),
        (r"属于|所属", {"所属专业大类"}),
        (r"培养安排|培养方式|培养模式|怎么培养|办学模式", {"培养安排"}),
        (r"分流", {"大类分流安排"}),
        (r"学位|学历|证书", {"学历学位"}),
        (r"办学地点|校区", {"办学地点"}),
        (r"教学语言|授课语言", {"教学语言"}),
    ):
        if re.search(pattern, q):
            fields.update(names)
    if "课程" in q and not fields.intersection({"核心课程", "特色课程"}):
        fields.update({"核心课程", "特色课程"})
    return fields


def answer_major(question: str, year: int | None = None, context: dict | None = None, *, intent_confirmed: bool = False) -> dict | None:
    context = context or {}
    if not intent_confirmed and not major_intent(question, context):
        return None
    from whut_rag.major_store import load_profiles, major_status
    started = time.perf_counter()
    conditions = {"intent": "major_info", "active_domain": "major_info", "pending_intent": None,
                  "major_info_target": None, "major_info_compare_target": None, "major_info_mode": "single",
                  "project": None, "major": None, "dataset": None}
    result = {"status": "clarify", "answer": "", "reason": "", "conditions": conditions,
              "evidence": [], "structured_table": [], "structured_count": 0,
              "major_source_groups": [], "major_missing_dimensions": [], "timings": {}}

    def finish(status, answer, reason, missing=None):
        result.update(status=status, answer=answer, reason=reason, structured_count=len(result["structured_table"]))
        if missing:
            result["missing_fields"] = missing
            conditions["pending_intent"] = "major_info"
        result["timings"]["total_seconds"] = round(time.perf_counter() - started, 3)
        return result

    if not major_status().get("ready"):
        return finish("error", "专业资料完整性检查未通过，暂不能查询。", "major_sources_not_ready")
    records = load_profiles()
    years = year_mentions(question)
    if len(years) > 1:
        return finish("clarify", "请先明确一个资料年份；不同年度的专业范围和培养安排不能合并比较。", "multiple_major_years", ["year"])
    chosen = next(iter(years)) if years else year if year is not None else context.get("year")
    conditions.update(year=chosen, year_source="question" if years else "selector" if year is not None else "session" if chosen else None)
    scope_text = re.sub(r"(?:不是|不属于|不按|不看|不要|不查|非)\s*(?:台湾地区学测|台湾学测|台湾地区|台湾|学测)(?:招生|考生|目录|范围|资料)?", "", question)
    taiwan = bool(re.search(r"台湾|学测", scope_text))
    general = bool(re.search(r"普通高考|全国|普通本科", scope_text) or scope_text != question and not taiwan)
    scope_conflict = taiwan and general
    scope = "taiwan_gsat" if taiwan else "general" if general else context.get("major_info_scope", "general") if context.get("active_domain") == "major_info" else "general"
    conditions["major_info_scope"] = scope
    candidates = [r for r in records if r["scope"] == scope and (not chosen or r["year"] == chosen)]
    # 先用全年度词表识别对象，年度缺资料时不能把它替换为其他对象。
    names, suggestions = _names(question, candidates)
    if not names:
        names, suggestions = _names(question, [r for r in records if r["scope"] == scope])
    compare = bool(re.search(r"比较|对比|区别|差异|哪个好|哪个更好", question))
    referent = bool(re.search(REFERENTS, question))
    is_supplement = bool(re.fullmatch(r"(?:是|查|查询|就)?\s*20\d{2}年?[\s。！!？?]*", question) or re.fullmatch(r"(?:那|改为|换成)?(?:台湾学测|普通高考|普通本科|全国)(?:呢)?[\s。！!？?]*", question))
    followup = _profile_followup_only(question)
    # 有新对象线索或不匹配的伙伴时，不能借指代复用旧项目。
    can_inherit = not names and not suggestions and (is_supplement or followup)
    if can_inherit and context.get("active_domain") == "major_info":
        names = [context[k] for k in ("major_info_target", "major_info_compare_target") if context.get(k)]
        compare = compare or context.get("major_info_mode") == "compare"
    elif can_inherit and referent and context.get("project") and scope == "general":
        inherited, _ = _names(str(context["project"]), candidates)
        if not inherited:
            inherited, _ = _names(str(context["project"]), [r for r in records if r["scope"] == scope])
        if len(inherited) == 1:
            names = inherited
    if names:
        conditions.update(major_info_target=names[0], major_info_compare_target=names[1] if len(names)>1 else None,
                          major_info_mode="compare" if compare else "single", major=names[0] if len(names)==1 else None)
    if scope_conflict:
        conditions["major_info_scope"] = None
        return finish("clarify", "问题同时指定了台湾学测与普通高考资料，请明确本次采用哪一种招生范围；两种目录不能混用。", "ambiguous_major_info_scope", ["major_info_scope"])
    if not names:
        extra = "可核对的正式名称包括：" + "、".join(suggestions) + "。" if suggestions else ""
        return finish("clarify", "请明确要查询的官方专业、专业大类或合作项目名称；合作项目请保留合作方。" + extra, "missing_major_info_target", ["major_info_target"])
    if len(names) != (2 if compare else 1):
        return finish("clarify", "请明确比较的两个专业或大类，并区分合作方、班型；单个介绍请指定一个对象。当前匹配：" + "、".join(names) + "。", "ambiguous_major_info_targets", ["major_info_compare_target" if compare else "major_info_target"])
    if chosen is None:
        supported = sorted({r["year"] for r in candidates if r["name"] in names})
        if len(supported) == 1:
            chosen = supported[0]
            conditions.update(year=chosen, year_source="available")
            candidates = [r for r in candidates if r["year"] == chosen]
        else:
            return finish("clarify", "请明确资料年份（2024—2026年）。现有对应年份：" + ("、".join(map(str,supported)) or "暂无") + "；不混用不同年份的培养资料。", "missing_major_info_year", ["year"])
    result["confirmed_year"] = chosen
    if chosen not in {2024, 2025, 2026}:
        return finish("refused", "资料年份超出2024—2026年范围，无法提供该年度专业介绍。", "major_info_year_out_of_scope")
    if len(names) == 1:
        project = _project_identity(names[0], candidates, chosen, scope)
        if project:
            conditions.update(project=project["label"], project_id=project["project_id"])
    by_name = {}
    for name in names:
        rows = [r for r in candidates if r["name"] == name]
        fields = {}
        for r in rows:
            for field, value in r["fields"].items():
                if value:
                    fields.setdefault(field, set()).add(value)
            for evidence in r["evidence"]:
                if not any(e["chunk_id"] == evidence["chunk_id"] for e in result["evidence"]):
                    result["evidence"].append(evidence)
        # 冲突字段不择取、不拼成一个跨范围规则。
        by_name[name] = {f: next(iter(v)) for f,v in fields.items() if len(v)==1}
        for field, values in fields.items():
            if len(values)>1:
                result["major_missing_dimensions"].append(f"{name}的{field}存在多种原文，未合并，请核对来源范围")
    groups = {}
    for e in result["evidence"]:
        key = (e["source_id"], e["year"], e["url"])
        g = groups.setdefault(key, {k: e[k] for k in ("source_id", "title", "year", "url")})
        g.setdefault("records", []).append({"record_id":e["chunk_id"], "label":e.get("label", e["chunk_id"]), "location":e["location"], "text":e["text"]})
    result["major_source_groups"] = list(groups.values())
    requested = _requested_fields(question)
    header = f"资料年份：{chosen}年；适用范围：{SCOPES[scope]}。"
    if conditions["year_source"] == "available":
        header += "未指定年份，本次展示唯一有对应记录的年度，并非跨年通用结论。"
    coverage = "大类包含关系只说明原资料列出的专业组成，不等于分流保证或各省均有招生；具体招生另查当年计划。"
    if compare:
        left, right = names
        common = set(by_name[left]) & set(by_name[right])
        if requested:
            common &= requested
        for field in DIMENSIONS:
            if field in common:
                result["structured_table"].append({"对照维度":field, left:by_name[left][field], right:by_name[right][field]})
        missing = sorted(requested-common) if requested else [f for f in DIMENSIONS if f not in common and (f in by_name[left] or f in by_name[right] or f in {"培养目标","核心课程","培养安排"})]
        result["major_missing_dimensions"].extend(missing)
        if not common:
            return finish("refused", header + "\n\n双方缺少同年度、同招生范围" + ("的" + "、".join(sorted(requested)) if requested else "的") + "共同可比字段，不能据专业名称补写差异。已有来源可在下方核对。", "no_shared_major_dimensions")
        text = header + f"\n\n仅对照官方名称“{left}”与“{right}”在双方资料中均有的维度，内容见下表。"
        if missing:
            text += "\n\n未形成双方共同依据的维度：" + "、".join(missing) + "。这些维度不作差异判断。"
        return finish("answered", text + "\n\n" + coverage + "不依据这些资料判定哪个专业更好或个人适合程度。", "official_major_comparison")
    name = names[0]
    fields = by_name[name]
    selected = [f for f in DIMENSIONS if not requested or f in requested]
    for field in selected:
        if field in fields:
            result["structured_table"].append({"信息维度":field, "官方资料内容":fields[field]})
    missing = [f for f in (requested or {"培养目标", "核心课程", "培养安排"}) if f not in fields]
    result["major_missing_dimensions"].extend(sorted(missing))
    text = header + f"\n\n官方名称：{name}。"
    if not fields or requested and not result["structured_table"]:
        text += "\n\n现有该年度、该范围的官方资料未收录" + ("、".join(sorted(requested)) if requested else "可回答的专业介绍") + "，无法据此回答。"
        if fields.get("所属专业大类"):
            text += f"已确认的包含关系：所属专业大类为{fields['所属专业大类']}；这不能代替该专业课程介绍。"
        return finish("refused", text + "不会从其他年份、其他专业或大类介绍推断。", "major_dimension_missing")
    text += "已收录的信息如下。"
    if missing:
        text += "\n\n资料尚缺：" + "、".join(sorted(missing)) + "；未作补写。"
    return finish("answered", text + "\n\n" + coverage, "official_major_profile")
