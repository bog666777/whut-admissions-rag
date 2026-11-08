"""F11：对已核验结构字段作口径解释，不从任意文本猜测事实或新旧优先级。"""
from __future__ import annotations
from whut_rag.query_conditions import year_mentions

from collections import defaultdict
from copy import deepcopy
import hashlib
import json
import re
import time


def _canon(value) -> str:
    return re.sub(r"\s+", "", str(value)).translate(str.maketrans("（）", "()"))


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _key(claim: dict) -> tuple:
    return (_canon(claim.get("object", "")), claim.get("fact"), claim.get("year"), _json(claim.get("scope")))


def _known_scope(scope) -> bool:
    if isinstance(scope, dict):
        return bool(scope) and all(_known_scope(v) for v in scope.values())
    if isinstance(scope, list):
        return bool(scope) and all(_known_scope(v) for v in scope)
    return scope is not None and str(scope).strip() not in {"", "未知", "不详", "未登记", "未说明", "未明确", "未提供", "缺失", "待核实", "None", "null", "unknown", "unspecified", "原收费表未标明"}


def link_explicit_corrections(claims: list[dict]) -> list[dict]:
    """有限句法：同一肯定原句须有《目标标题》、更正动词和当前值。

    仅连接同完整口径、已定位的条目；否定/假设/拟议语句不连接。无法识别的
    格式保持None，不能靠文件类型、发布时间或“说明”标题猜替代链。
    """
    rows = deepcopy(claims)
    for current in rows:
        if (current.get("correction_relation") or current.get("source_located") is not True
                or not all(current.get(k) for k in ("source", "title", "url", "location", "evidence"))):
            continue
        if isinstance(current.get("year"), bool) or not _known_scope(current.get("scope")):
            continue
        for evidence in current.get("evidence", []):
            sentences = re.findall(r"[^。！？；;\n]+[。！？；;]?", evidence.get("text", ""))
            for raw in sentences:
                basis = raw.strip()
                if (not re.search(r"更正为|替代|取代|废止", basis)
                        or re.search(r"不|未|没有|并非|不能|无需|如果|假如|假设|若|拟|可能|计划|建议|是否", basis)
                        or _canon(current.get("value", "")) not in _canon(basis)):
                    continue
                targets = [target for target in rows if target["claim_id"] != current["claim_id"]
                           and target.get("source") != current.get("source") and target.get("source_located") is True
                           and not isinstance(target.get("year"), bool) and _known_scope(target.get("scope"))
                           and all(target.get(k) for k in ("source", "title", "url", "location", "evidence"))
                           and target.get("title") and f"《{target['title']}》" in basis and _key(target) == _key(current)]
                if targets:
                    current["correction_relation"] = {"type": "corrects" if "更正为" in basis else "replaces",
                                                      "target_claim_ids": [t["claim_id"] for t in targets],
                                                      "basis": basis, "source_id": current["source"]}
                    break
            if current.get("correction_relation"):
                break
    return rows


def compare_claims(claims: list[dict]) -> dict:
    """纯规则；外部合成claim只供单元测试，不作为官网实际分歧的证明。

    correction_relation合同：type=corrects/replaces，target_claim_ids，source_id，
    basis（当前claim原文中的精确关系说明）。只有同完整口径的明确目标可被取代。
    published_on永远不参与取胜规则。
    """
    rows = deepcopy(claims)
    by_id = {c["claim_id"]: c for c in rows}
    if len(by_id) != len(rows):
        raise ValueError("事实条目标识重复")
    grouped = defaultdict(list)
    for row in rows:
        grouped[_key(row)].append(row)
    groups, warnings = [], []
    for key, values in grouped.items():
        metadata_ok = all(c.get("fact") and c.get("object") and isinstance(c.get("year"), int) and not isinstance(c.get("year"), bool)
                          and isinstance(c.get("scope"), dict) and _known_scope(c["scope"])
                          and c.get("value") is not None and c.get("value") != "" and c.get("source_located") is True
                          and all(c.get(k) for k in ("source", "title", "url", "location", "evidence")) for c in values)
        replaced, relations = set(), []
        for current in values:
            relation = current.get("correction_relation")
            if not relation:
                continue
            targets = relation.get("target_claim_ids") or []
            basis = relation.get("basis")
            originals = [e.get("text", "") for e in current.get("evidence", [])]
            valid = (metadata_ok and relation.get("type") in {"corrects", "replaces"}
                     and relation.get("source_id") == current.get("source") and isinstance(basis, str)
                     and basis and re.search(r"更正|替代|取代|代替|作废|废止|替换|以本.*为准", basis)
                     and any(basis in text for text in originals) and targets
                     and all(target in by_id and target != current["claim_id"]
                             and _key(by_id[target]) == key for target in targets))
            if valid:
                replaced.update(targets)
                relations.append({"claim_id": current["claim_id"], **deepcopy(relation)})
            else:
                warnings.append("更正关系缺少同对象、同字段、同年度、同范围的明确目标或原文，未用于裁决。")
        remaining = [c for c in values if c["claim_id"] not in replaced]
        original_values = {_json(c.get("value")) for c in values}
        active_values = {_json(c.get("value")) for c in remaining}
        if not metadata_ok:
            state = "insufficient_metadata"
        elif len(original_values) <= 1:
            state = "consistent"
        elif remaining and len(active_values) == 1 and relations:
            state = "explicit_correction"
        else:
            state = "unresolved_conflict"
        groups.append({"fact": values[0].get("fact"), "object": values[0].get("object"),
                       "year": values[0].get("year"), "scope": values[0].get("scope"), "state": state,
                       "claim_ids": [c["claim_id"] for c in values], "relations": relations,
                       "selected_claim_ids": [c["claim_id"] for c in remaining] if state in {"consistent", "explicit_correction"} else []})
    axes = [name for name in ("fact", "object", "year", "scope") if len({_json(c.get(name)) for c in rows}) > 1]
    states = {g["state"] for g in groups}
    classification = ("no_claims" if not rows else "unresolved_conflict" if "unresolved_conflict" in states
                      else "insufficient_metadata" if "insufficient_metadata" in states
                      else "explicit_correction" if "explicit_correction" in states
                      else "different_dimensions" if len(groups) > 1 else "consistent")
    return {"classification": classification, "groups": groups, "different_axes": axes,
            "warnings": list(dict.fromkeys(warnings)), "date_priority_used": False,
            "requires_official_confirmation": classification in {"unresolved_conflict", "insufficient_metadata"}}


def _claim(fact, obj, value, year, scope, evidence, dates, *, aliases=(), partner=None, object_name=None):
    evidence = deepcopy(evidence)
    located = bool(evidence and all(e.get("location") and e.get("url") and e.get("text") for e in evidence))
    fragments = [part for part in str(value).split("\n") if part.strip()]
    joined = _canon("\n".join(e.get("text", "") for e in evidence))
    located = located and bool(fragments) and all(_canon(part) in joined for part in fragments)
    first = evidence[0]
    identity = [fact, obj, value, year, scope, [e["chunk_id"] for e in evidence]]
    return {"claim_id": "f11-" + hashlib.sha256(_json(identity).encode()).hexdigest()[:20],
            "fact": fact, "object": obj, "value": value, "year": year, "scope": deepcopy(scope),
            "source": first["source_id"], "title": first["title"], "url": first["url"],
            "location": "；".join(e["location"] for e in evidence), "published_on": dates.get(first["url"]),
            "correction_relation": None, "source_located": bool(located), "evidence": evidence,
            "aliases": list(dict.fromkeys([obj, *aliases])), "partner": partner, "object_name": object_name or obj}


def _project_name(name, partner):
    name = _canon(name)
    return name + f"（合作方：{partner}）" if partner and _canon(partner) not in name else name


def _text_claims(chunks, dates):
    claims = []
    for chunk in chunks.values():
        text = chunk["text"]
        name = re.search(r"招生专业为([^，。；\s]+?（中外合作办学）)", text)
        partner = re.search(r"与([^，。；：()（）\s]+?大学)合作举办", text)
        if not name or not partner:
            continue
        obj = _project_name(name[1], partner[1])
        scope = {"招生范围": "普通本科合作项目", "合作方": partner[1]}
        kwargs = {"aliases": [name[1], name[1].split("（")[0]], "partner": partner[1], "object_name": name[1]}
        if "获批" in text:
            claims.append(_claim("项目获批", obj, "获批", chunk["year"], scope, [chunk], dates, **kwargs))
        status = re.search(r"(?:专业|项目)(20\d{2})年(暂不招生|停止招生|暂停招生)", text)
        if status:
            claims.append(_claim("当年招生状态", obj, status[2], int(status[1]), scope, [chunk], dates, **kwargs))
    return claims


def _profile_claims(profiles, facts, dates):
    claims = []
    locations = {"合作院校": ("approval段",), "教学语言": ("teaching段",),
                 "培养安排": ("yearly段", "mode段", "teaching段", "学历学位段")}
    for profile in profiles:
        partner = profile["fields"].get("合作院校")
        obj = _project_name(profile["name"], partner)
        scope = {"招生范围": profile["scope"]}
        if profile.get("coverage_provinces"):
            scope["分省计划已收录省份"] = profile["coverage_provinces"]
        if partner:
            scope["合作方"] = partner
        for fact in facts:
            value = profile["fields"].get(fact)
            if not value:
                continue
            evidence = profile["evidence"]
            if profile["profile_id"].startswith("major-pdf2024"):
                markers = locations.get(fact, (fact + "段",))
                evidence = [e for e in evidence if any(marker in e["location"] for marker in markers)]
            if evidence:
                claims.append(_claim(fact, obj, value, profile["year"], scope, evidence, dates,
                                     aliases=[profile["name"], profile["name"].split("(")[0], *profile.get("aliases", [])], partner=partner, object_name=profile["name"]))
    return claims


def _fee_claims(records, dates):
    claims = []
    for row in records:
        if row["kind"] not in {"tuition", "cooperation"}:
            continue  # 不读图片、规则中的未知金额或住宿口径来冒充专业学费。
        fields = row["fields"]
        partner = fields.get("partner")
        obj = _project_name(fields.get("name") or row["label"], partner)
        scope = {"收费范围": row["kind"], "年度含义": fields.get("year_basis"), "入学年级": row["year"]}
        if row["kind"] == "cooperation":
            scope["合作方"] = partner or "原收费表未标明"
        evidence = [{"chunk_id": row["id"], "source_id": row["source_id"], "title": row["title"],
                     "year": row["year"], "url": row["url"], "location": row["location"],
                     "text": row["text"], "source_path": row["source_path"]}]
        for fact, field in (("国内学费", "domestic_fee"), ("国外学费", "foreign_fee"),
                            ("专业学费（按年）", "profession_fee"), ("学分学费单价", "credit_unit_fee"), ("学年制学费", "annual_fee")):
            value = fields.get(field)
            if value is not None and str(value).strip():
                unit = fields.get("credit_unit") if field == "credit_unit_fee" else fields.get("unit")
                claim_scope = {**scope, **({"原计费单位": unit} if unit else {}), **({"原周期": fields["period"]} if fields.get("period") else {})}
                claims.append(_claim(fact, obj, str(value), row["year"], claim_scope, evidence, dates,
                                     aliases=row["target_names"], partner=partner,
                                     object_name=_canon(fields.get("name") or row["label"]).split("(")[0] if row["kind"] == "tuition" else fields.get("name") or row["label"]))
    return claims


def _matches(question, claims):
    text = _canon(question)
    matched = [c for c in claims if any(_canon(alias) and _canon(alias) in text for alias in c["aliases"])]
    partners = {c["partner"] for c in claims if c.get("partner")
                and any(len(_canon(a)) >= 3 and _canon(a) in text for a in
                        (c["partner"], re.sub(r"^(?:英国|法国|澳大利亚)", "", c["partner"]),
                         re.sub(r"^(?:英国|法国|澳大利亚)", "", c["partner"]).removesuffix("大学")))}
    if partners:
        matched = [c for c in claims if c.get("partner") in partners
                   and (c in matched or not matched)]
        objects = {_canon(c["object"]) for c in matched}
        if len(objects) == 1:
            # 年份别名可能只命中一版；已唯一绑定同项目及合作方时纳入其他年份，
            # 不凭合作方相同把别的项目也带入。
            matched = [c for c in claims if _canon(c["object"]) in objects and c.get("partner") in partners]
    return matched


def _intent(question):
    personal_requirement = re.search(r"我|本人|我的", question) and re.search(r"选科|体检|色弱|色盲|视力|身高|外语|语种", question)
    two_official_sources = re.search(r"两份|两个.{0,5}(?:文件|来源|目录|说法)|一份.{0,30}另一份", question)
    if personal_requirement and not two_official_sources:
        return False  # 个人条件与一份规则的核对，不是两份官方说法相互冲突。
    if re.search(r"我|本人|我的", question) and not re.search(r"官方|资料|文件|来源|网页|目录|章程|说法|两份|表格|通知|公告", question):
        return False  # 个人输入条件矛盾交给原条件核对。
    strong = bool(re.search(r"冲突|矛盾|不一致|说法不一|说法相反|对不上|不相符|口径.*(?:不同|不一)|以哪.*为准|哪(?:个|份).*(?:为准|正确|写错|说错|搞错|对)", question))
    if re.search(r"不能直接(?:比较|比)|怎么比较|如何比较|比较方法|比较原理|跨年.*(?:方法|原理)", question) and not strong:
        return False
    sourced = re.search(r"官方|资料|文件|来源|网页|目录|章程|说法|通知|公告", question)
    replacement_question = bool(sourced and re.search(
        r"(?:新|后).{0,12}(?:旧|原).{0,12}(?:作废|失效|替代|取代|覆盖)|(?:是否|是不是|能否).{0,20}(?:作废|替代|取代|覆盖)", question))
    cross_year_fact = len(set(re.findall(r"20\d{2}", question))) > 1 and re.search(r"录取|分数|位次|学费|招生状态", question)
    return strong or replacement_question or bool((sourced or cross_year_fact) and re.search(r"不一样|不同|(?:能否|是否).*(?:替代|代替|适用)", question))


def _date_rule_question(question):
    return bool(re.search(r"发布日期|发布时间|发布.*(?:较晚|更晚)|晚发布", question)
                and re.search(r"替代|取代|覆盖|旧文件|新旧|为准", question)
                and not re.search(r"学费|录取分|位次|招生状态|获批|核心课程|包含.*专业", question))


def _explicit_reference_only(question):
    if not re.search(r"这个(?:项目|专业|大类)|该(?:项目|专业|大类)|上述(?:项目|专业|大类)|这两份资料|它", question):
        return False
    remainder = _canon(question)
    remainder = re.sub(r"武汉理工大学|武汉理工|20\d{2}[年级]?|\d+(?:\.\d+)?(?:万|元|澳元|英镑|美元)?", "", remainder)
    words = (r"这个项目|这个专业|这个大类|该项目|该专业|该大类|上述项目|上述专业|上述大类|这两份资料|它|"
             r"国内外|国内|国外|境外|学费|收费|费用|培养安排|培养目标|核心课程|特色课程|课程|包含专业|包含哪些专业|"
             r"招生状态|是否招生|暂不招生|停招|获批|官方|资料|文件|来源|网页|目录|章程|标准|说法|"
             r"不一致|不一样|冲突|矛盾|不同|以哪个为准|以哪份为准|哪个正确|另一份|一份|两份|"
             r"为什么|请问|请|到底|核实|对比|比较|的|是|和|与|又|吗|呢")
    remainder = re.sub(words, "", remainder)
    return not re.sub(r"[\W_]+", "", remainder)


def _clear_object_fields(conditions):
    # 显式None使页面真正移除旧标签；不能仅pop而保留旧session键。
    fields = ("project", "major", "dataset", "granularity", "fee_target", "fee_item", "fee_scope", "cohort_year",
              "major_info_target", "major_info_compare_target", "major_info_mode", "major_info_scope",
              "eligibility_target", "eligibility_topic", "eligibility_scope", "candidate_target", "candidate_mode", "compound_request")
    conditions.update({field: None for field in fields})


def _sync_object(conditions, matched, catalog):
    objects = {claim["object"] for claim in matched}
    _clear_object_fields(conditions)
    if len(objects) != 1:
        return
    first = matched[0]
    name, partner = first["object_name"], first.get("partner")
    conditions.update(major=name, granularity="major")
    if partner:
        projects = [project for project in catalog if project["partner"] == partner
                    and _canon(project["base_name"]) == _canon(name).split("(")[0]]
        if len(projects) == 1:
            conditions.update(major=projects[0]["name"], project=projects[0]["label"])


def answer_conflict(question: str, year: int | None = None, context: dict | None = None) -> dict | None:
    ctx = deepcopy(context or {})
    resume = (ctx.get("pending_intent") == "conflict" and ctx.get("conflict_request")
              and len(question) < 100 and not re.search(r"多少|为什么|怎么|查询|筛选|能报|适合|推荐|我选|分[，,。]|招生计划是多少", question))
    date_rule = _date_rule_question(question)
    if not question.strip() or len(question) > 1200 or not (_intent(question) or resume or date_rule):
        return None
    started = time.perf_counter()
    conditions = {**ctx, "intent": "conflict", "active_domain": "conflict", "pending_intent": None}
    result = {"conditions": conditions, "claims": [], "evidence": [], "structured_table": [], "timings": {},
              "answer_mode": "verified_claim_comparison", "generation_skipped": "核验原字段并比较口径，不调用LLM"}

    def finish(status, answer, reason, missing=None):
        result.update(status=status, answer=answer, reason=reason, structured_count=len(result["structured_table"]))
        if missing:
            result["missing_fields"] = missing
            conditions.update(pending_intent="conflict", conflict_request=q)
        else:
            conditions.update(pending_intent=None, conflict_request=None)
        result["timings"]["total_seconds"] = round(time.perf_counter() - started, 3)
        return result

    q = (str(ctx["conflict_request"]) + "；补充：" + question.strip()) if resume and not _intent(question) else question.strip()
    if date_rule:
        conditions.update({key: deepcopy(ctx[key]) for key in ("pending_intent", "conflict_request") if key in ctx})
        result.update(status="answered", reason="system_conflict_review_rule", answer_mode="system_rule_explanation", structured_count=0,
                      answer="这是本系统核对官方说法的规则说明，不是新增的官方招生条款。发布日期较晚，本身不能证明它替代了旧文件。"
                      "先核对对象、事实字段、适用年度和招生范围是否相同；只有原文明确指向旧资料并肯定说明更正、替代或废止，且同口径目标可核验时，才按该关系处理。"
                      "没有明确关系或元数据不足时，保留并列说法，不凭日期、文件名或“说明”标签给出唯一结论。",
                      timings={"total_seconds": round(time.perf_counter() - started, 3)})
        return result
    status_topic = bool(re.search(r"获批|批准|招生吗|是否招生|暂不招生|停招", q))
    fee_topic = bool(re.search(r"学费|收费|费用|国内|国外|入学年级", q))
    admission_topic = bool(re.search(r"录取|最低分|最高分|平均分|分数|位次", q)) and not status_topic
    profile_facts = set()
    for pattern, facts in ((r"包含|所含|大类|目录|台湾|学测", {"大类所含专业"}),
                           (r"课程", {"核心课程", "特色课程"}), (r"培养|介绍", {"培养安排", "培养目标"}),
                           (r"学位|证书", {"学历学位"}), (r"教学语言|授课语言", {"教学语言"})):
        field_question = re.sub(r"申请(?:外方)?学位", "", q) if pattern == r"学位|证书" else q
        if re.search(pattern, field_question):
            profile_facts.update(facts)
    if not (status_topic or fee_topic or admission_topic or profile_facts):
        return finish("clarify", "请先说明具体专业或项目，以及两种说法分别是什么；如果记得资料标题、原文或官方链接，也可一并提供。未定位对应官方证据前，不能判断哪份唯一正确。", "conflict_details_missing", ["object", "claims"])
    explicit_years = year_mentions(q)
    years = explicit_years or ({year} if year is not None else set())
    result["comparison_years"] = sorted(years)
    if years - {2024, 2025, 2026}:
        return finish("refused", "比较中包含2024—2026年以外的年份，无法用范围内资料替代该年度说法。", "conflict_year_out_of_scope")
    try:
        from whut_rag.consultation import _checked_chunks
        from whut_rag.core import manifest, other_school_query, project_catalog
        chunks = _checked_chunks()
        catalog = project_catalog()
        if other_school_query(q, catalog):
            return finish("refused", "现有资料只支持武汉理工大学对应官方说法，不能裁决其他学校自身的文件。", "other_school")
        dates = {source["url"]: source.get("published_on") for source in manifest()["sources"]}
        claims, profiles = [], []
        if status_topic:
            claims.extend(_text_claims(chunks, dates))
        if profile_facts:
            from whut_rag.major_store import load_profiles
            profiles = load_profiles()
            claims.extend(_profile_claims(profiles, profile_facts, dates))
        if fee_topic:
            from whut_rag.fee_store import load_fee_records
            claims.extend(_fee_claims(load_fee_records(), dates))
        if admission_topic:
            from whut_rag.structured_store import load_records
            from whut_rag.structured_query import unique_mentions, source_evidence
            from whut_rag.query_conditions import subject_type_mentions
            records = load_records()
            allowed = {}
            for field in ("province", "subject_type", "admission_type"):
                matches = (subject_type_mentions(q, {r[field] for r in records}) if field == "subject_type"
                           else unique_mentions(q, {r[field] for r in records}))
                allowed[field] = matches or ([ctx[field]] if ctx.get(field) else [])
            allowed["subject_type"] = allowed["subject_type"] or (["物理类"] if "物理" in q else ["历史类"] if "历史" in q else [])
            allowed["admission_type"] = allowed["admission_type"] or (["普通类"] if "普通" in q else [])
            missing = [field for field, values in allowed.items() if not values]
            if missing:
                return finish("clarify", "请补充录取记录对应的省份、科类和招生类型；不能凭分数反推口径。", "conflict_admission_scope_missing", missing)
            wanted = {"最低分": "zdf", "位次（官方字段）": "wcz"}
            if "最高分" in q:
                wanted = {"最高分": "zgf"}
            elif "平均分" in q:
                wanted = {"平均分": "pjf"}
            for record in records:
                if record["dataset"] != "admission" or record["granularity"] != "major" or any(record[k] not in values for k, values in allowed.items()):
                    continue
                scope = {field: record[field] for field in allowed}
                for fact, field in wanted.items():
                    value = record["payload"].get(field)
                    if value is not None:
                        claims.append(_claim(fact, record["major"], str(value), record["year"], {**scope, "granularity": "major"}, [source_evidence(record)], dates, aliases=[record["major"]]))
        if profile_facts:
            taiwan_scope = bool(re.search(r"台湾|学测", q))
            general_scope = bool(re.search(r"普通高考|普通本科|全国|分省计划", q))
            if taiwan_scope != general_scope:
                requested_scope = "taiwan_gsat" if taiwan_scope else "general"
                claims = [c for c in claims if "招生范围" not in c["scope"] or c["scope"]["招生范围"] == requested_scope]
        before_year_filter = _matches(q, claims)
        if years:
            claims = [c for c in claims if c["year"] in years]
        if fee_topic:
            domestic, foreign = "国内" in q, bool(re.search(r"国外|境外", q))
            if domestic != foreign:
                claims = [c for c in claims if c["fact"] not in {"国内学费", "国外学费"}
                          or c["fact"] == ("国内学费" if domestic else "国外学费")]
        matched = _matches(q, claims)
        inherited = False
        if not matched and not before_year_filter and _explicit_reference_only(q):
            target = ctx.get("project") or ctx.get("major_info_target") or ctx.get("major") or ctx.get("fee_target")
            if target:
                matched = _matches(str(target), claims)
                inherited = bool(matched)
        if not matched:
            if before_year_filter:
                _clear_object_fields(conditions)
                return finish("refused", "当前未收录这组指定年度与对象的对应可读字段，不能用其他年份或尚未人工核验的图片数值替代。", "conflict_requested_fields_not_covered")
            if re.search(r"[\u4e00-\u9fff]{2,}(?:工程|技术|专业|项目|类)", q) and not _explicit_reference_only(q):
                _clear_object_fields(conditions)
            return finish("clarify", "尚不能定位这组说法的明确官方对象及可用字段，请补充专业或项目原名、合作方，以及对应资料或说法。", "conflict_object_or_evidence_missing", ["object", "claims"])
        partners = {c.get("partner") for c in matched if c.get("partner")}
        if len(partners) > 1 and not any(_canon(partner) in _canon(q) for partner in partners):
            return finish("clarify", "同名专业涉及多个合作方，请明确对比的合作项目；不能把不同合作院校的说法混作一个项目冲突。", "conflict_project_ambiguous", ["project"])
        matched = link_explicit_corrections(matched)
        if not inherited:
            _sync_object(conditions, matched, catalog)
        comparison = compare_claims(matched)
        result.update(claims=matched, comparison=comparison)
        result["requires_official_confirmation"] = comparison["classification"] in {"unresolved_conflict", "insufficient_metadata"}
        for claim in matched:
            for evidence in claim["evidence"]:
                if not any((e["chunk_id"], e["year"]) == (evidence["chunk_id"], evidence["year"]) for e in result["evidence"]):
                    result["evidence"].append(deepcopy(evidence))
            result["structured_table"].append({"事实字段": claim["fact"], "官方对象": claim["object"], "资料年份/入学年级": claim["year"],
                                                "适用范围与口径": _json(claim["scope"]), "原字段值": claim["value"],
                                                "发布日期": claim["published_on"] or "未登记，不能据此排序", "来源编号": claim["source"], "原文位置": claim["location"]})
        parts = []
        if {"项目获批", "当年招生状态"}.issubset({c["fact"] for c in matched}):
            parts.append("项目获批和当年是否招生是两个事实字段，可以同时成立；获批不能直接推出当年招生。这里按同一官方说明分别列出获批与年度招生状态。")
            for claim in matched:
                if claim["fact"] == "当年招生状态":
                    parts.append(f"该项目的已定位年度状态是：{claim['year']}年{claim['value']}。")
        axes = comparison["different_axes"]
        labels = {"fact": "事实字段", "object": "官方对象或合作方", "year": "资料年度/入学年级", "scope": "招生范围或统计口径"}
        if axes:
            parts.append("这组记录的" + "、".join(labels[a] for a in axes) + "不同，应先分别核对，不能把不同口径的值认定为相互推翻。")
        from whut_rag.answer_presentation import display_major_text
        shown = []
        for claim in matched:
            scope = claim["scope"]
            mode = scope.get("招生范围")
            scope_label = {"general": "普通高考分省计划/普通本科项目资料（仅对应已收录范围）", "taiwan_gsat": "台湾学测专用目录"}.get(mode, str(mode or ""))
            if scope.get("province"):
                scope_label = "、".join(str(scope[k]) for k in ("province", "subject_type", "admission_type"))
            elif scope.get("入学年级"):
                scope_label = f"{scope['入学年级']}级收费资料"
            value = display_major_text(str(claim["value"]), field=claim["fact"])
            shown.append(f"- {claim['year']}年｜{scope_label}｜{claim['object']}｜{claim['fact']}：{value}")
        parts.append("已定位的原字段分别为：\n" + "\n".join(shown))
        incomplete = len(matched) == 1
        unresolved_user_values = []
        if fee_topic:
            raw_values = [_canon(str(c["value"]) + str(c["scope"].get("原计费单位") or "")) for c in matched]
            mentioned_money = re.findall(r"(?<![\d.])\d+(?:\.\d+)?\s*(?:万(?:澳元|美元|英镑|欧元|元)?|澳元|美元|英镑|欧元|元)", q)
            unresolved_user_values = list(dict.fromkeys(v for v in mentioned_money if not any(_canon(v) in raw for raw in raw_values)))
            incomplete = incomplete or bool(unresolved_user_values)
        if profile_facts:
            parts.append("普通高考分省计划与台湾学测目录各有适用范围；不跨范围拼接专业组成。培养与课程资料也不能从2024年自动外推为2026年依据。")
            for fact in sorted(profile_facts):
                for wanted_year in sorted(years):
                    if not any(c["year"] == wanted_year and c["fact"] == fact for c in matched):
                        parts.append(f"当前已核验资料未收录{wanted_year}年该对象的“{fact}”字段，保持缺失，不用其他年份补写。")
                        incomplete = True
        if fee_topic:
            parts.append("国内与国外学费、专业学费与学分单价分别列示，保留原币种、预计及周期；不同入学年级不合并，也不换算或相加。2026普通专业图片学费未用于本次事实条目。")
        if "year" in axes and admission_topic:
            parts.append("2024、2025年的录取分数和位次分别属于各自年度，不因数值不同就认定其中一份错误，也不将分数增减解释为录取难度变化。")
        state = comparison["classification"]
        if unresolved_user_values:
            parts.append("用户提及的“" + "、".join(unresolved_user_values) + "”尚未在对应官方字段中定位，不能当作已核验官方事实，也不能拿其他费用字段充当该说法。")
        if state == "unresolved_conflict":
            parts.append("同对象、同字段、同年度、同适用范围仍存在不同值，且没有足以消解分歧的明确更正关系：只能并列原文，不能给出唯一结论。发布日期较晚本身不构成覆盖依据。")
        elif state == "insufficient_metadata":
            parts.append("部分适用范围或原文定位不足，无法形成可靠的同口径裁决，只能并列核对，不能给出唯一结论。")
        elif state == "explicit_correction":
            parts.append("仅按已核实的明确更正/替代关系采用相应条目，原说法及关系依据仍保留。")
        elif not incomplete:
            parts.append("本次可定位记录未形成需要凭发布日期选优的同口径冲突；值即使一致，也不能据此扩大适用范围。")
        parts.append("这里只核对已定位记录；未定位的另一说法仍需提供标题、原文和出处。不能据当前记录宣称已裁决未定位文件，也不能仅按较晚发布日期覆盖它。")
        if any(c["published_on"] is None for c in matched):
            parts.append("部分来源未登记可核验发布日期，未从文件名、URL或年度推测发布时间。")
        parts.extend(comparison["warnings"])
        if incomplete and state not in {"unresolved_conflict", "insufficient_metadata"}:
            result.update(requires_official_confirmation=True, unlocated_user_values=unresolved_user_values)
            parts.append("目前的证据只覆盖部分比较条件，不能给出两份说法一致或哪一份唯一正确的结论。")
            return finish("partial", "\n\n".join(parts), "official_claims_incomplete_evidence", ["other_source"])
        return finish("refused" if state in {"unresolved_conflict", "insufficient_metadata"} else "answered", "\n\n".join(parts), "official_claims_" + state,
                      ["source_scope_or_original"] if state == "insufficient_metadata" else None)
    except Exception as exc:
        result.update(error_type=type(exc).__name__, error_code="data_integrity_error", error_stage="conflict_source_check")
        return finish("error", "冲突核对所需来源未通过完整性或字段检查，暂不裁决，也不采用未经核验的说法。", "conflict_source_check_failed")
