"""武汉理工大学本科招生问答与报考条件核对的交互验证入口。"""

from __future__ import annotations

import re
from html import escape
from urllib.parse import urlparse

import streamlit as st

from whut_rag.core import answer_question, get_status
from whut_rag.errors import make_error_result
from whut_rag.answer_presentation import display_answer, display_major_table, display_candidate_summary


APP_BUILD_VERSION = "1.2.0"
CONTEXT_FIELDS = (
    "year", "project", "province", "subject_type", "admission_type",
    "major", "granularity", "dataset", "fee_item", "fee_target", "fee_scope", "cohort_year",
    "active_domain",
    "eligibility_topic", "eligibility_scope", "eligibility_target", "personal_subjects",
    "personal_foreign_language", "personal_color_vision", "personal_height", "personal_vision", "personal_gender",
    "candidate_score", "candidate_rank", "candidate_target", "candidate_mode", "candidate_profile_scope",
    "major_info_target", "major_info_compare_target", "major_info_mode", "major_info_scope",
    "compound_request",
    "conflict_request",
)
CONDITION_LABELS = {
    "province": "省份", "subject_type": "科类", "admission_type": "招生类型",
    "major": "专业/专业类", "granularity": "数据粒度", "dataset": "查询类别",
}
FEE_CONDITION_LABELS = {
    "fee_target": "收费对象", "fee_item": "费用项目", "fee_scope": "收费类别",
    "cohort_year": "适用入学年级",
}
ELIGIBILITY_CONDITION_LABELS = {
    "eligibility_topic": "核对事项", "eligibility_scope": "适用招生范围",
    "eligibility_target": "核对专业/项目", "personal_subjects": "已提供选科",
    "personal_foreign_language": "自述外语语种", "personal_color_vision": "自述色觉",
    "personal_height": "自述身高", "personal_vision": "自述视力", "personal_gender": "自述性别",
}
CANDIDATE_CONDITION_LABELS = {
    "candidate_mode": "查询方式", "candidate_target": "指定目标",
    "candidate_score": "本轮分数", "candidate_rank": "本人位次",
}
MAJOR_CONDITION_LABELS = {
    "major_info_target": "专业介绍对象", "major_info_compare_target": "比较对象",
    "major_info_mode": "专业查询方式", "major_info_scope": "专业资料范围",
}
VALUE_LABELS = {
    "school_summary": "学校汇总", "major": "分专业", "professional_group": "专业组",
    "admission": "录取数据", "plan": "招生计划",
    "ordinary": "普通专业", "cooperation": "中外合作办学", "unspecified": "尚未明确",
    "tuition": "学费", "accommodation": "住宿费", "accommodation_surcharge": "住宿附加收费",
    "minor": "辅修收费", "preparatory": "预科收费", "rule": "收费规则",
    "info": "公开收费说明", "medical_check": "体检费", "meal_deposit": "餐卡预存款",
    "insurance": "保险费", "overview": "收费概况",
    "general_charter": "普通本科招生章程", "taiwan_gsat": "台湾学测招生",
    "charter_clause": "招生章程条款", "catalogue_row": "专业目录记录", "catalogue_note": "专业目录说明",
    "subjects": "选科", "physical": "体检", "language": "外语",
    "general": "其他公开条件", "all": "报考条件综合查询",
}
COLUMN_LABELS = {
    "year": "年份", "province": "省份", "subject_type": "科类",
    "admission_type": "招生类型", "major": "专业/专业类", "granularity": "数据粒度",
    "dataset": "查询类别", "min_score": "最低分", "max_score": "最高分",
    "average_score": "平均分", "min_rank": "最低位次", "max_rank": "最高位次",
    "average_rank": "平均位次", "plan_count": "计划数", "professional_group": "专业组",
    "subject_requirements": "选考要求", "included_majors": "包含专业",
    "record_count": "记录数", "province_count": "省份数", "source_url": "官方来源",
    "source_location": "原始记录位置",
    "kind": "收费类别", "source_count": "来源资料数", "cohort_year": "适用入学年级",
    "scope": "适用招生范围",
}


st.set_page_config(
    page_title="武汉理工大学本科招生问答",
    page_icon="🎓",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# 仅调整本地问答界面的布局，不影响条件识别、查询或原始资料。
st.markdown("""
<style>
[data-testid="stToolbar"], [data-testid="stAppDeployButton"],
[data-testid="stMainMenu"], #MainMenu,
[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] {
    display: none !important;
}
header[data-testid="stHeader"] { display: none !important; }
[data-testid="stAppViewContainer"], [data-testid="stBottom"] { background: #fff; }
[data-testid="stMainBlockContainer"] {
    max-width: 62rem; padding: 2rem 1.5rem 7rem;
}
#stMain h1 {
    color: #153e67; font-size: 1.5rem; line-height: 1.4;
    padding-top: 0; padding-bottom: .35rem;
}
#stMain h3 { color: #153e67; font-size: 1.15rem; }
/* 吸顶作用于容器外层，避免被仅有标题高度的父容器限制。 */
[data-testid="stLayoutWrapper"]:has(> .st-key-page_header) {
    position: sticky; top: 0; z-index: 40; background: #fff;
}
.st-key-page_header {
    background: #fff; border-bottom: 1px solid #e6ebf1;
    padding-top: .75rem; padding-bottom: .8rem;
}
#stMain { scroll-padding-top: 7rem; }
.st-key-page_header [data-testid="stCaptionContainer"] { font-size: .85rem; }
.st-key-page_header button { border-color: #dce3ec; color: #153e67; }
.condition-tags { display: flex; flex-wrap: wrap; gap: .45rem; margin-bottom: .35rem; }
.condition-tag {
    display: inline-block; padding: .25rem .65rem; border-radius: .35rem;
    background: #f0f4f8; color: #25486c; font-size: .85rem; line-height: 1.6;
    overflow-wrap: anywhere;
}
.st-key-welcome { min-height: calc(100dvh - 25rem); justify-content: center; }
.st-key-welcome button {
    min-height: 5rem; padding: .8rem 1rem; border: 1px solid #dfe6ee;
    background: #f8fafc; color: #25486c; text-align: left; justify-content: flex-start;
}
.st-key-welcome button:hover { background: #eef3f8; border-color: #a8bbd0; }
[data-testid="stChatMessage"] { background: #f8fafc; border-radius: .5rem; }
[data-testid="stChatMessageAvatarUser"], [data-testid="stChatMessageAvatarAssistant"] {
    background: #e9eff6; color: #25486c;
}
[data-testid="stChatInput"] { background: #f6f8fb; border-color: #dce3ec; }
[data-testid="stChatInput"]:focus-within { border-color: #25486c; }
[data-testid="stBottomBlockContainer"] { max-width: 62rem; padding: 1rem 1.5rem 1.5rem; }
@media (min-width: 768px) { #stMain h1 { white-space: nowrap; } }
@media (max-width: 767px) {
    [data-testid="stMainBlockContainer"] { padding: 1rem 1rem 6rem; }
    #stMain h1 { font-size: 1.3rem; }
    .st-key-welcome { min-height: 16rem; }
}
</style>
""", unsafe_allow_html=True)


def final_text(value: object) -> str:
    """仅显示最终回答，防止模型思考标签意外进入界面。"""
    text = str(value or "")
    text = re.sub(
        r"<(think|analysis)\b[^>]*>.*?</\1\s*>",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.split(r"<(?:think|analysis)\b[^>]*>", text, maxsplit=1, flags=re.IGNORECASE)[0]
    return text.strip()


def official_url(value: object) -> str | None:
    candidate = str(value or "").strip()
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if parsed.scheme in {"http", "https"} and (
        host == "whut.edu.cn" or host.endswith(".whut.edu.cn")
    ):
        return candidate
    return None


def chinese_value(value: object) -> object:
    return VALUE_LABELS.get(value, value) if isinstance(value, str) else value


def condition_value(key: str, value: object) -> object:
    if key == "cohort_year" and isinstance(value, int) and not isinstance(value, bool):
        return f"{value}级"
    if key == "candidate_score":
        return str(value) if str(value).endswith("分") else f"{value}分"
    if key == "candidate_rank":
        return str(value) if str(value).endswith("名") else f"{value}名"
    if key == "candidate_mode":
        return {"open": "开放式专业筛选", "target": "指定目标历史对照"}.get(value, value)
    if key == "major_info_mode":
        return {"single": "单个介绍", "compare": "两个对象对照"}.get(value, value)
    if key == "major_info_scope":
        return {"general": "普通本科专业/项目资料", "taiwan_gsat": "台湾学测专用目录"}.get(value, value)
    return chinese_value(value)


def pending_review_details(item: dict) -> dict:
    image_path = item.get("image_path") or item.get("original_image_path") or item.get("source_path")
    fields = item.get("fields_to_check") or item.get("blocked_fields") or item.get("pending_fields") or []
    if isinstance(fields, str):
        fields = [fields]
    return {
        "image_path": image_path if isinstance(image_path, str) else None,
        "fields_to_check": [field for field in fields if isinstance(field, str)] if isinstance(fields, list) else [],
    }


def fee_debug_data(value: dict) -> dict:
    data = dict(value)
    if isinstance(data.get("pending_review"), list):
        data["pending_review"] = [pending_review_details(item) for item in data["pending_review"] if isinstance(item, dict)]
    return data


def chinese_rows(rows: list[dict]) -> list[dict]:
    return [
        {COLUMN_LABELS.get(key, key): chinese_value(value) for key, value in row.items()}
        for row in rows if isinstance(row, dict)
    ]


def has_markdown_table(text: str) -> bool:
    lines = text.splitlines()
    return any(
        "|" in lines[index - 1]
        and "---" in line
        and bool(re.fullmatch(r"[\s|:\-]+", line))
        for index, line in enumerate(lines) if index > 0
    )


def condition_caption(conditions: dict | None) -> str:
    """历史消息只说明后端实际采用的条件，不从提交前的控件推断。"""
    if not isinstance(conditions, dict):
        return "正在识别查询条件……"
    labels = []
    year = conditions.get("year")
    if isinstance(year, int) and not isinstance(year, bool):
        source = {
            "question": "问题中明确",
            "session": "沿用会话",
            "selector": "本次选择",
            "available": "现有唯一对应年度",
        }.get(conditions.get("year_source"), "已确认")
        year_label = (
            "本次采用资料年份" if conditions.get("intent") == "fee"
            else "本次填报年份" if conditions.get("intent") == "candidates"
            else "会话已确认年份（非对比资料年度）" if conditions.get("intent") == "conflict"
            else "本次采用年份"
        )
        labels.append(f"{year_label}：{year}年（{source}）")
    project = conditions.get("project")
    if isinstance(project, str) and project.strip():
        labels.append(f"项目：{project}")
    for key, label in {
        **CONDITION_LABELS, **FEE_CONDITION_LABELS, **ELIGIBILITY_CONDITION_LABELS,
        **CANDIDATE_CONDITION_LABELS,
        **MAJOR_CONDITION_LABELS,
    }.items():
        value = conditions.get(key)
        if value is not None and value != "":
            labels.append(f"{label}：{condition_value(key, value)}")
    return " · ".join(labels) or "本次尚无已确认的查询条件。"


def remember_conditions(result: dict) -> dict:
    """仅复用后端确认的查询条件；显式空值清除对应字段。"""
    raw = result.get("conditions")
    conditions = dict(raw) if isinstance(raw, dict) else {}
    if "year" not in conditions and result.get("confirmed_year") is not None:
        conditions["year"] = result["confirmed_year"]
    # 服务故障与资料校验失败不能清除已经确认的会话条件。
    if result.get("status") == "error":
        return conditions
    for key in CONTEXT_FIELDS:
        if key not in conditions:
            continue
        value = conditions[key]
        if value is None:
            st.session_state.confirmed_context.pop(key, None)
        elif key in {"year", "cohort_year"}:
            if isinstance(value, int) and not isinstance(value, bool):
                st.session_state.confirmed_context[key] = value
        elif isinstance(value, str) and value.strip():
            st.session_state.confirmed_context[key] = value.strip()
    if "pending_intent" in conditions:
        pending = conditions["pending_intent"]
        if pending in {"structured", "fee", "eligibility", "candidates", "major_info", "compound", "conflict", "purpose"}:
            st.session_state.confirmed_context["pending_intent"] = pending
        else:
            st.session_state.confirmed_context.pop("pending_intent", None)
    elif result.get("status") == "clarify" and conditions.get("intent") == "fee":
        st.session_state.confirmed_context["pending_intent"] = "fee"
    else:
        st.session_state.confirmed_context.pop("pending_intent", None)
    return conditions


def show_candidate_sources(groups: list[dict]) -> None:
    if groups:
        st.caption("以下依据按官方资料及年份集中展示，保留每条记录的来源编号和原文位置。")
    for number, group in enumerate(groups, start=1):
        title = str(group.get("title") or "未提供标题")
        year = group.get("year")
        year_label = f" · {year}年" if year is not None else ""
        with st.expander(f"依据 {number}｜{title}{year_label}", expanded=False):
            url = official_url(group.get("url"))
            if url:
                st.link_button("打开官方来源", url)
            else:
                st.caption("此资料没有可打开的官方链接，请按标题和原文位置核对。")
            for record in group.get("records") or []:
                st.write(str(record.get("label") or "未提供记录名称"))
                if record.get("record_id"):
                    st.caption(f"来源编号：{record['record_id']}")
                if record.get("location"):
                    st.caption(f"原文位置：{record['location']}")
                st.text(str(record.get("text") or "未提供原始记录"))


def show_reply(result: dict) -> None:
    status = result.get("status", "error")
    answer = final_text(display_answer(result))
    if status == "error":
        st.error(answer or "本次处理发生异常，已确认的查询条件保留，请查看折叠的调试信息。")
    elif answer:
        st.markdown(answer)
    else:
        st.warning("本次没有可展示的最终回答，请重新提问。")

    if (result.get("conditions") or {}).get("intent") == "conflict":
        comparison_years = result.get("comparison_years") or sorted({c.get("year") for c in result.get("claims", []) if isinstance(c.get("year"), int)})
        if comparison_years:
            st.caption("本次核对资料年度：" + "、".join(str(y) for y in comparison_years) + "年；各条适用范围分别保留，未用资料年度覆盖会话中的填报条件。")

    if (result.get("conditions") or {}).get("intent") == "compound":
        for part in result.get("parts") or []:
            if not isinstance(part, dict) or not isinstance(part.get("result"), dict):
                continue
            with st.expander(f"查看{part.get('title') or '分项'}的详细记录与来源", expanded=False):
                show_reply(part["result"])
        show_debug(result)
        return

    is_fee = (result.get("conditions") or {}).get("intent") == "fee"
    is_candidates = (result.get("conditions") or {}).get("intent") == "candidates"
    is_major = (result.get("conditions") or {}).get("intent") == "major_info"
    presentation = result.get("fee_presentation")
    has_fee_presentation = is_fee and isinstance(presentation, dict)
    if not has_fee_presentation:
        presentation = {}
    table = (presentation.get("table") or []) if has_fee_presentation else (result.get("structured_table") or [])
    if is_candidates:
        summary = result.get("candidate_summary") or {}
        if isinstance(summary, dict) and summary:
            st.caption("筛选概况：" + display_candidate_summary(summary))
        if isinstance(table, list):
            preferred_columns = (
                "专业", "专业/专业大类", "历史年份", "相对位置", "位次差", "位次差（用户－历史）",
                "历史最低位次", "历史最低录取位次", "用户位次", "计划人数", "选科筛选", "本年选科核对",
                "本年选科要求", "历史数据说明", "来源编号", "计划记录编号", "历史记录编号",
            )
            table = [
                {
                    **{key: row[key] for key in preferred_columns if key in row},
                    **{key: value for key, value in row.items() if key not in preferred_columns},
                }
                for row in table if isinstance(row, dict)
            ]
    if (result.get("conditions") or {}).get("intent") == "eligibility" and isinstance(table, list):
        preferred_columns = ("核对项目", "核对结果", "个人条件", "官方要求")
        table = [
            {
                **{key: row[key] for key in preferred_columns if key in row},
                **{key: value for key, value in row.items() if key not in preferred_columns},
            }
            for row in table if isinstance(row, dict)
        ]
    if is_major and isinstance(table, list):
        table = display_major_table(table)
    if isinstance(table, list) and table and not has_markdown_table(answer):
        count = result.get("structured_count", len(table))

        def show_table() -> None:
            st.caption(
                f"匹配记录：{count}条。"
                + (f"当前展示{len(table)}条，请进一步限定条件查看。" if count != len(table) else "")
            )
            st.dataframe(chinese_rows(table), hide_index=True, width="stretch")

        if is_candidates:
            with st.expander(f"查看候选与逐年历史明细（{count}条）", expanded=False):
                st.caption("位次差＝用户位次－历史最低录取位次；负数表示用户位次数字较小，正数表示较大，0表示相同。")
                st.caption("仅对照同省、同科类、同招生类型及同官方专业名称的记录；分数不跨年比较。同一专业的当年计划人数不可按历史年份重复相加。")
                show_table()
        elif is_major:
            with st.expander("查看专业资料明细", expanded=False):
                if (result.get("conditions") or {}).get("major_info_mode") == "compare":
                    st.table(table)
                else:
                    for row in table:
                        st.markdown(f"**{row.get('信息维度', '官方资料')}**")
                        st.write(str(row.get("官方资料内容") or "资料缺失"))
        elif presentation.get("details_collapsed"):
            details_label = presentation.get("details_label") or f"查看详细住宿标准（{count}条）"
            with st.expander(str(details_label), expanded=False):
                show_table()
        else:
            with st.expander(f"查看详细记录（{count}条）", expanded=False):
                show_table()

    if is_candidates:
        for key, label in (
            ("candidate_unconfirmed", "选科待核对"),
            ("candidate_excluded", "选科不符合"),
        ):
            records = result.get(key) or []
            if isinstance(records, list) and records:
                with st.expander(f"{label}（{len(records)}条）", expanded=False):
                    st.dataframe(chinese_rows(records), hide_index=True, width="stretch")

    evidence = result.get("evidence") or []
    source_groups = presentation.get("source_groups")
    candidate_groups = result.get("candidate_source_groups")
    if is_major and isinstance(result.get("major_source_groups"), list):
        show_candidate_sources(result["major_source_groups"])
    elif is_candidates and isinstance(candidate_groups, list):
        show_candidate_sources(candidate_groups)
    elif has_fee_presentation:
        if isinstance(source_groups, list) and source_groups:
            st.caption("以下依据按官方资料集中展示，可展开核对官方原始记录。")
        else:
            source_groups = []
        for number, group in enumerate(source_groups, start=1):
            title = str(group.get("title") or "未提供标题")
            year = group.get("year")
            year_label = f" · {year}年" if year is not None else ""
            with st.expander(f"依据 {number}｜{title}{year_label}", expanded=False):
                url = official_url(group.get("url"))
                if url:
                    st.link_button("打开官方来源", url)
                else:
                    st.caption("此资料没有可打开的官方链接，请按标题和原文位置核对。")
                for record_number, record in enumerate(group.get("records") or [], start=1):
                    st.write(f"记录 {record_number}｜{record.get('label') or '未提供记录名称'}")
                    if record.get("location"):
                        st.caption(f"原文位置：{record['location']}")
                    st.text(str(record.get("text") or "未提供原始记录"))
    else:
        if evidence and is_fee:
            st.caption("以下是本次使用的官方原始记录及解析字段，可按来源逐条核对。")
        elif evidence:
            st.caption("以下是本次使用的官方原文，可逐条核对。")
        for number, item in enumerate(evidence, start=1):
            title = str(item.get("title") or "未提供标题")
            year = item.get("year")
            year_label = f" · {year}年" if year is not None else ""
            with st.expander(f"依据 {number}｜{title}{year_label}"):
                if is_candidates and item.get("chunk_id"):
                    st.caption(f"来源编号：{item['chunk_id']}")
                if item.get("scope"):
                    st.caption(f"适用范围：{chinese_value(item['scope'])}")
                location = item.get("location")
                if location:
                    st.caption(f"原文位置：{location}")
                url = official_url(item.get("url"))
                if url:
                    st.link_button("打开官方来源", url)
                else:
                    st.caption("此片段没有可打开的官方链接，请按标题和原文位置核对。")
                st.text(str(item.get("text") or "未提供原文片段"))

    guidance = result.get("official_guidance") or {}
    for item in guidance.get("evidence") or []:
        with st.expander(f"官方核实渠道来源｜{item.get('year')}年联系方式资料", expanded=False):
            st.caption("此来源仅用于核实渠道，不作为其他年份专业规则或就业数据。")
            st.caption(str(item.get("location") or ""))
            url = official_url(item.get("url"))
            if url:
                st.link_button("打开官方渠道来源", url)
            st.text(str(item.get("text") or ""))
    show_debug(result)

def show_debug(result: dict) -> None:
    status = result.get("status", "error")
    evidence = result.get("evidence") or []
    with st.expander("调试信息", expanded=False):
        st.json(
            {
                "处理结果": status,
                "说明": result.get("reason"),
                "故障分类": result.get("error_code"),
                "处理环节": result.get("error_stage"),
                "异常类型": result.get("error_type"),
                "模型运行": result.get("model_run"),
                "资料核对结果": result.get("comparison"),
                "实际采用条件": {
                    key: value for key, value in (result.get("conditions") or {}).items()
                    if key != "active_domain"
                },
                "仍需补充": result.get("missing_fields") or [],
                "耗时": result.get("timings") or {},
                "检索记录": [
                    {"片段": item.get("chunk_id"), "距离": item.get("distance")}
                    for item in evidence
                ],
            }
        )


if "messages" not in st.session_state:
    st.session_state.messages = []
if "confirmed_context" not in st.session_state:
    st.session_state.confirmed_context = {}
else:
    st.session_state.confirmed_context = {
        key: value for key, value in st.session_state.confirmed_context.items()
        if key in (*CONTEXT_FIELDS, "pending_intent")
    }
if "year_choice" not in st.session_state:
    st.session_state.year_choice = None
if "pending_year_choice" in st.session_state:
    st.session_state.year_choice = st.session_state.pop("pending_year_choice")
st.session_state.pop("pending_year", None)

status_error = None
try:
    current_status = get_status()
except Exception as exc:
    current_status = {"ready": False, "source_count": 0, "chunk_count": 0}
    status_error = f"{type(exc).__name__}: {exc}"
structured_status = current_status.get("structured") or {}
if not isinstance(structured_status, dict):
    structured_status = {}
fees_status = current_status.get("fees") or {}
if not isinstance(fees_status, dict):
    fees_status = {}
eligibility_status = current_status.get("eligibility") or {}
if not isinstance(eligibility_status, dict):
    eligibility_status = {}

with st.container(key="page_header"):
    heading_column, scope_column, clear_column = st.columns([7, 1.6, 1.6], vertical_alignment="center")
    with heading_column:
        st.title("武汉理工大学本科招生问答")
        st.caption("查询官方招生资料，答案附来源；资料不足时会说明。")
    with scope_column:
        with st.popover("资料范围", width="stretch"):
            st.markdown("**2024—2026年官方本科招生资料**")
            st.write("录取数据：已收录2024、2025年的公开分数和位次。")
            st.write("招生计划：已收录2026年计划及选科字段。")
            st.write("其他资料：招生章程、公开收费、报考条件、专业及中外合作项目介绍，按原资料年份和招生范围使用。")
            st.caption("仅依据已收录的官方资料回答；并非每个年份、专业都有完整信息。只存在于图片且尚未人工核对的内容不用于正式回答。")
    with clear_column:
        if st.button("清空会话", width="stretch"):
            st.session_state.clear()
            st.rerun()

context = st.session_state.confirmed_context
with st.expander("当前查询条件", expanded=bool(context)):
    condition_labels = {
        "year": "年份", **CONDITION_LABELS, "project": "合作项目",
        **FEE_CONDITION_LABELS, **ELIGIBILITY_CONDITION_LABELS,
        **CANDIDATE_CONDITION_LABELS,
    }
    if context.get("active_domain") == "major_info":
        condition_labels.update(MAJOR_CONDITION_LABELS)
    tags = []
    for key, label in condition_labels.items():
        value = context.get(key)
        if value is not None and value != "":
            text = f"{value}年" if key == "year" else str(condition_value(key, value))
            tags.append(f'<span class="condition-tag">{escape(label)}：{escape(text)}</span>')
    if tags:
        st.markdown('<div class="condition-tags">' + "".join(tags) + "</div>", unsafe_allow_html=True)
    else:
        st.caption("年份等条件会从问题中识别，也可在此修正年份。")
    year_column, _ = st.columns([1, 2])
    with year_column:
        selected_year = st.selectbox(
            "年份修正（可选）",
            options=[None, 2024, 2025, 2026],
            format_func=lambda value: "使用问题或会话中已明确的年份" if value is None else f"{value}年",
            key="year_choice",
            placeholder="自动识别年份（可选修正）",
            help="问题中明确写出的年份优先；否则使用本次选择，再沿用会话已确认年份。处理后，已确认年份会保留在会话中。",
        )
    if selected_year is not None:
        st.caption(f"若问题未明确年份，本次将使用你选择的{selected_year}年。")

example_question = None
if not st.session_state.messages:
    with st.container(key="welcome"):
        st.subheader("从一个问题开始")
        st.caption("点击示例直接提问，也可以在下方输入你的问题。")
        examples = (
            "2025年湖北物理普通类计算机类录取最低分和位次是多少？",
            "2026年湖北物理普通类计算机类招生计划是多少？",
            "2024年中英车辆工程项目学费是多少？",
            "2026年计算机类包含哪些专业？",
        )
        for row in range(2):
            for column, prompt in zip(st.columns(2), examples[row * 2:row * 2 + 2]):
                with column:
                    if st.button(prompt, key=f"example_{row}_{prompt}", width="stretch"):
                        example_question = prompt

for index, message in enumerate(st.session_state.messages):
    with st.chat_message(message["role"]):
        if message["role"] == "user":
            st.markdown(message["content"])
            conditions = message.get("conditions")
            if conditions is None and index + 1 < len(st.session_state.messages):
                following = st.session_state.messages[index + 1]
                if following.get("role") == "assistant":
                    conditions = following.get("result", {}).get("conditions", {})
            with st.expander("本次采用条件", expanded=False):
                st.caption(condition_caption(conditions))
        else:
            show_reply(message["result"])

question = st.chat_input(
    "输入你想核对的招生问题",
    disabled=not (
        current_status.get("ready", False)
        or structured_status.get("ready", False)
        or fees_status.get("ready", False)
        or eligibility_status.get("ready", False)
    ),
    max_chars=1200,
)
question = question or example_question
if question:
    question = question.strip()
    if question:
        user_message = {"role": "user", "content": question, "conditions": None}
        st.session_state.messages.append(user_message)
        with st.chat_message("user"):
            st.markdown(question)
            condition_slot = st.empty()
            condition_slot.caption("正在识别查询条件……")
        with st.chat_message("assistant"):
            try:
                with st.spinner("正在核对官方资料，完成后展示最终回答……"):
                    result = answer_question(
                        question,
                        year=selected_year,
                        context=dict(st.session_state.confirmed_context),
                    )
            except Exception as exc:
                result = make_error_result(exc, stage="page_query")
            user_message["conditions"] = remember_conditions(result)
            condition_slot.caption(condition_caption(user_message["conditions"]))
            st.session_state.messages.append({"role": "assistant", "result": result})
            st.session_state.pending_year_choice = None
            show_reply(result)
        st.rerun()
