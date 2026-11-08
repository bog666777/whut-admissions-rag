"""有边界的政策解释：使用核验后的运行证据，不生成招生事实或个人结论。"""
from __future__ import annotations
from whut_rag.query_conditions import year_mentions

from contextlib import closing
from copy import deepcopy
import hashlib
import json
import re
import sqlite3
import time


BUSINESS_WORDS = r"招生计划|计划人数|计划数|招生人数|选科|选考|学费|住宿费|收费|录取最低|最低录取|最低分|录取位次|招生吗|是否招生|暂不招生|停招|获批"
FIT_WORDS = r"适合|适不适合|适配|一定适合|个人匹配"


def _checked_chunks() -> dict[str, dict]:
    """复用原索引就绪守卫，并在同一只读快照中重新核对全部正文和元数据。"""
    from whut_rag import core

    if not core.get_status().get("ready"):
        raise ValueError("运行文本索引未就绪或完整性检查未通过")
    sources, parsed = core.parse_sources()  # 包含白名单路径/域名和原件SHA校验。
    expected = {chunk["chunk_id"]: chunk for chunk in parsed}
    with closing(sqlite3.connect(core.DATABASE.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("运行文本数据库完整性检查失败")
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        stored = {}
        for cid, year, payload in db.execute("SELECT chunk_id,year,payload FROM chunks"):
            chunk = json.loads(payload)
            if chunk.get("chunk_id") != cid or chunk.get("year") != year or cid in stored:
                raise ValueError("运行文本索引外层字段与正文不一致")
            stored[cid] = chunk
    if (stored != expected or metadata.get("embedding_model") != core.EMBED_MODEL
            or metadata.get("manifest_sha256") != hashlib.sha256(core.MANIFEST.read_bytes()).hexdigest()
            or metadata.get("sample_version") != core.manifest()["sample_version"]):
        raise ValueError("运行文本与官方源、清单或模型登记不一致")
    paths = {source["source_id"]: source["path"] for source in sources}
    return {cid: {**deepcopy(chunk), "source_path": paths[chunk["source_id"]]} for cid, chunk in stored.items()}


def _charter(numbers: tuple[int, ...], year: int = 2026) -> list[dict]:
    from whut_rag import core

    chunks = _checked_chunks()
    evidence = [chunks[f"charter-{year}:{number:02d}"] for number in numbers]
    if any(item["source_id"] != f"charter-{year}" for item in evidence):
        raise ValueError("政策证据不是指定年度的普通本科招生章程")
    core.enforce_project_scope(evidence, None, year)
    return evidence


def official_channels(year: int | None = None) -> dict:
    """招生核实入口；source_year是出处年份，调用方不得用它覆盖用户会话年份。"""
    chosen = 2026 if year is None else year
    base = {"evidence": [], "source_year": chosen, "channels": {}}
    if isinstance(chosen, bool) or chosen != 2026:
        return {**base, "status": "refused", "reason": "consultation_channels_year_not_covered",
                "answer": f"当前运行文本索引未收录{chosen}年章程的对应联系条款，不能把2026年渠道冒充该年度资料。"}
    try:
        evidence = _charter((29,), chosen)
        text = evidence[0]["text"]
        website = re.search(r"本科招生网[：:]\s*(https?://[^\s，,。；;]+)", text)
        email = re.search(r"电子信箱[：:]\s*([A-Za-z0-9_.+\-]+@[A-Za-z0-9.\-]+)", text)
        phone = re.search(r"招生咨询电话[：:]\s*([0-9、,，\-— ]+)", text)
        if not website or not email or not phone:
            raise ValueError("章程原文不能完整定位招生咨询入口")
        channels = {"website": website[1], "email": email[1], "admission_phone": phone[1].strip(" ,，、")}
        answer = (f"可通过2026年普通本科招生章程第二十九条列出的招生渠道核实：\n"
                  f"- 本科招生网：{channels['website']}\n"
                  f"- 电子信箱：{channels['email']}\n"
                  f"- 招生咨询电话：{channels['admission_phone']}\n\n"
                  "上述渠道出自2026年章程，仅说明渠道资料年份。可请招生办公室指引对应学院或主管部门。")
        return {**base, "status": "answered", "reason": "verified_official_channels",
                "answer": answer, "evidence": evidence, "channels": channels}
    except Exception as exc:
        return {**base, "status": "error", "reason": "consultation_sources_not_ready",
                "error_type": type(exc).__name__,
                "answer": "官方渠道资料完整性检查未通过，暂不展示未经核验的联系方式。"}


def _method_question(q: str) -> bool:
    return bool(re.search(r"分数|分数线|位次", q)
                and re.search(r"为什么|为何|怎么理解|如何理解|能否直接|能不能直接|可以直接|可比|有什么区别|区别是什么|更有参考", q)
                and re.search(r"比较|对比|跨年|不同年|区别|参考|可比", q))


def _other_school(q: str) -> bool:
    from whut_rag.core import other_school_query, project_catalog
    return other_school_query(q, project_catalog())


def answer_consultation(question: str, year: int | None = None, context: dict | None = None) -> dict | None:
    q = question.strip()
    if not q or len(q) > 1200:
        return None
    ctx = deepcopy(context or {})
    method = _method_question(q)
    fit = bool(re.search(FIT_WORDS, q) and re.search(r"我|本人|个人|兴趣|喜欢|性格|能力", q))
    if fit and not method:
        from whut_rag.eligibility_query import TOPICS
        if any(re.search(TOPICS[topic], q) for topic in ("subjects", "physical", "language")):
            # “适合报”也可能在问客观报考资格；完整复用资格模块的显式词表。
            # 即使同时询问核实渠道，也先交资格模块处理，不抢成适配或渠道边界。
            return None
    # 组合业务由上层聚合路由处理，单项业务继续由原所有者回答。
    if not method and not fit and re.search(BUSINESS_WORDS, q):
        return None
    if not method and not fit and re.search(r"分流|课程|培养目标|学习内容|专业介绍|培养模式|培养安排|教学语言|授课语言|包含.*专业", q):
        return None  # 原专业路由先说明对应字段缺口，再由调用方附加核实渠道。
    kind = ("comparison_method" if method else "personal_fit_boundary" if fit
            else "adjustment" if re.search(r"调剂|退档", q)
            else "employment_boundary" if re.search(r"就业|职业去向|从事什么工作|做什么工作|毕业.*(?:工作|去向)", q)
            else "living_cost_boundary" if re.search(r"生活费|生活开支|生活成本|四年总费用|四年总花费|四年总成本", q)
            else "official_channels" if re.search(r"官方.*(?:核实|咨询|渠道|电话|邮箱)|去哪里核实|去哪核实|向谁咨询|招生办.*(?:电话|邮箱|联系)|招生咨询.*(?:电话|邮箱|方式)", q)
            else None)
    if kind is None:
        return None
    started = time.perf_counter()
    conditions = {**ctx, "intent": "adjustment" if kind == "adjustment" else "consultation"}
    if kind in {"personal_fit_boundary", "living_cost_boundary", "employment_boundary"}:
        conditions["pending_intent"] = None
    result = {"conditions": conditions, "evidence": [], "structured_table": [], "structured_count": 0,
              "consultation_kind": kind, "timings": {}, "answer_mode": "bounded_consultation",
              "generation_skipped": "系统规则说明或已核验章程解释，不调用生成模型"}

    def finish(status: str, answer: str, reason: str) -> dict:
        result.update(status=status, answer=answer, reason=reason)
        result["timings"]["total_seconds"] = round(time.perf_counter() - started, 3)
        return result

    if method:
        return finish("answered", "这是本系统的比较方法说明，不是新增的招生章程条款。\n\n"
                      "不同年度的试题、考生成绩分布和招生计划可能不同，分数增减不能直接解释为录取难度增减，"
                      "也不能把某年分数当作另一年的录取线。\n\n"
                      "本系统先核对同省份、同科类、同招生类型和同官方专业名称，再逐年并列历史最低位次与原始分数。"
                      "位次表示当年的排序，比直接搬用另一年的分数更便于对照，但跨年同一位次也不等价，仍须结合当年计划和口径。"
                      "位次缺失或专业口径不同就标明不可比较；不据此给出录取概率或保证。已有查询条件继续保留。",
                      "system_comparison_method")
    if fit:
        return finish("refused", "本期不提供个人专业适配判定，不能仅凭兴趣、某科成绩或性格断言你一定适合或不适合某个专业。"
                      "可以继续核对有官方依据的课程、培养目标和报考条件，再由你结合自身情况判断；无需为此补充报考年份。",
                      "personal_fit_not_in_scope")
    if kind == "living_cost_boundary":
        return finish("refused", "当前没有可用于估算个人生活费或四年总费用的统一官方依据，不能编造生活开支或把个人支出当成固定标准。"
                      "可分别查询已公布的当年学费和住宿费；这些项目也不能直接冒充全部生活费或四年总成本。",
                      "living_cost_not_in_scope")
    try:
        if _other_school(q):
            return finish("refused", "本系统只使用武汉理工大学本科资料，不能把本校规则或联系方式用于其他学校。", "other_school")
        if kind in {"employment_boundary", "official_channels"}:
            channel_years = year_mentions(q) if kind == "official_channels" else set()
            if len(channel_years) > 1:
                return finish("clarify", "请明确要核对哪一年度章程中的官方联系方式。", "multiple_channel_years")
            channel_year = (next(iter(channel_years)) if channel_years else year) if kind == "official_channels" else None
            channels = official_channels(channel_year)
            result.update(evidence=channels["evidence"], source_year=channels["source_year"])
            if channels.get("error_type"):
                result["error_type"] = channels["error_type"]
            prefix = ("当前未收录能回答本题的对应专业就业去向、就业率等官方资料，无法确认；不会用专业名称、网络印象或模型记忆补齐。\n\n"
                      if kind == "employment_boundary" else "")
            suffix = "\n\n以上为招生咨询渠道，未收录就业部门联系方式。" if kind == "employment_boundary" and channels["status"] == "answered" else ""
            return finish("refused" if kind == "employment_boundary" and channels["status"] == "answered" else channels["status"],
                          prefix + channels["answer"] + suffix, "employment_data_not_covered" if kind == "employment_boundary" and channels["status"] == "answered" else channels["reason"])
        years = year_mentions(q)
        if len(years) > 1:
            return finish("clarify", "调剂与退档政策须按具体年度核对，请明确一个政策年份。", "multiple_policy_years")
        chosen = next(iter(years)) if years else year if year is not None else ctx.get("year")
        if chosen is None:
            result["missing_fields"] = ["year"]
            return finish("clarify", "请补充要核对的招生年份；不同年份的调剂与退档条款不能相互替代。", "missing_policy_year")
        if isinstance(chosen, bool) or chosen not in {2024, 2025, 2026}:
            return finish("refused", "该年份超出2024—2026年资料边界，不能使用其他年份政策替代。", "out_of_scope_year")
        if chosen != 2026:
            return finish("refused", f"{chosen}年在项目范围内，但当前运行文本索引未收录该年度完整的调剂与退档条款；不能用2026年规则替代。", "policy_year_not_covered")
        evidence = _charter((13, 15), chosen)
        result["evidence"] = evidence
        conditions.update(year=chosen, year_source="question" if years else "selector" if year is not None else "session")
        return finish("answered", "依据2026年普通本科招生章程第十三、十五条，可这样理解：\n\n"
                      "1. 录取和专业分配仍须满足思想政治品德、身体健康及相关专业培养要求，达到相应批次控制线并符合学校调档要求；"
                      "进档后按分数优先、遵循志愿、不设专业级差的原则分配专业。\n"
                      "2. 平行志愿批次中，所报专业都无法满足且服从专业调剂的，调剂到该专业组尚有计划余额的专业；"
                      "不是在全校任意专业间选择。填报时未明确表示服从调剂的，视为不服从。\n"
                      "3. 服从调剂不等于保证不退档。章程的“原则上不退档”还以平行志愿进档、品德合格、身体条件符合相关专业录取和培养要求等为前提。"
                      "不能仅凭服从调剂这一项，就承诺个人一定录取或一定不退档。\n\n"
                      "以上是当年普通本科章程的一般规则；具体合作项目或特殊招生类别的限制仍须另行核对，不能据此推断跨项目调剂资格。完整条款见下方来源。",
                      "verified_adjustment_policy")
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        return finish("error", "政策来源完整性检查未通过，暂不展示未经核验的政策或联系方式。", "consultation_sources_not_ready")
