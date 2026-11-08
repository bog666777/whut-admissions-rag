"""收费结果的纯展示层：不读取数据、不筛选记录、不改变查询条件或原值。"""
from __future__ import annotations

from decimal import Decimal
import re


def _unit(value: object) -> str:
    return str(value or "").replace("生.年", "生·年")


def _official_remark(record: dict) -> str:
    """只取官方原单元格/原段落，不展示解析器追加的处理备注。"""
    fields = record["fields"]
    if record["source_id"] == "fee-cooperation-2026":
        return str(fields.get("remarks") or "")
    if record["year"] == 2025:
        notes = [str(row["cells"][-1]).strip() for row in fields.get("raw_cells", [])
                 if len(row["cells"]) in {7, 9} and row["cells"][-1]]
        return "；".join(dict.fromkeys(notes))
    return ""


def _public_fields(record: dict) -> dict:
    fields = record["fields"]
    labels = {"profession_fee": "专业学费（按年）", "credit_unit_fee": "学分单价",
              "total_credits": "培养总学分（非金额）", "annual_fee": "学年制学费",
              "domestic_fee": "国内学费", "foreign_fee": "国外学费",
              "amount": "收费标准", "audience": "适用对象", "unit": "计费单位",
              "effective_period": "执收时间"}
    shown = {}
    for key, label in labels.items():
        value = fields.get(key)
        if value is None or value == "":
            continue
        if key == "annual_fee" and record["kind"].startswith("accommodation"):
            label = "空调加收标准" if record["kind"] == "accommodation_surcharge" else "住宿费标准"
        shown[label] = _unit(value) if key == "unit" else str(value) + ("元/学分" if key == "credit_unit_fee" else "")
    return shown


def _source_text(record: dict) -> str:
    fields = record["fields"]
    raw = fields.get("raw_cells") or []
    if not raw:
        # 网页段落与独立收费规则均为原文，不使用拼接了处理备注的PDF行text。
        original = fields.get("original_paragraph") or record["text"]
        remark = _official_remark(record)
        return str(original) + ("\n" + remark if remark and remark not in original else "")
    paragraphs = []
    for row in raw:
        cells = " | ".join("" if c is None else str(c) for c in row["cells"])
        paragraphs.append(f"PDF第{row['page']}页 表{row['table']} 第{row['row']}行，原表单元格：\n{cells}")
    shown = _public_fields(record)
    if shown:
        paragraphs.append("收费字段（依原表列头解析）：\n" + "；".join(f"{key}：{value}" for key, value in shown.items()))
    return "\n\n".join(paragraphs)


def _source_groups(records: list[dict]) -> list[dict]:
    grouped = {}
    for record in records:
        key = (record["source_id"], record["source_path"], record["url"], record["year"])
        if key not in grouped:
            grouped[key] = {field: record[field] for field in ("source_id", "source_path", "title", "year", "url")}
            grouped[key]["records"] = []
        grouped[key]["records"].append({"record_id": record["id"], "label": record["label"],
                                         "location": record["location"], "text": _source_text(record)})
    return list(grouped.values())


def _table(records: list[dict], housing: bool) -> list[dict]:
    table = []
    for record in records:
        fields = record["fields"]
        if housing:
            kind = ("空调加收" if record["kind"] == "accommodation_surcharge"
                    else "住宿基价（未装空调）" if record["year"] == 2024 else "住宿费")
            label = record["label"]
            row = {"费用项目": kind, "收费标准": str(fields.get("annual_fee") or "未提供"),
                   "计费单位": _unit(fields.get("unit")),
                   "适用住宿条件（节选）": label[:72] + ("…" if len(label) > 72 else "")}
            if fields.get("effective_period"):
                row["执收时间"] = fields["effective_period"]
        else:
            row = {"收费对象": record["label"], **_public_fields(record)}
            remark = _official_remark(record)
            if remark:
                row["官方备注"] = remark
        row["原表位置"] = record["location"]
        table.append(row)
    return table


def _ranges(records: list[dict]) -> list[dict]:
    # 基价与加收以及不同单位分别统计，仅对已经匹配的收费标准取两端。
    categories = {}
    for record in records:
        key = (record["kind"], _unit(record["fields"].get("unit")))
        amounts = [Decimal(n) for n in re.findall(r"\d+(?:\.\d+)?", str(record["fields"].get("annual_fee") or ""))]
        if amounts:
            categories.setdefault(key, []).extend(amounts)
    return [{"kind": kind, "minimum": format(min(values), "f"), "maximum": format(max(values), "f"), "unit": unit}
            for (kind, unit), values in categories.items()]


def _housing_answer(records: list[dict], ranges: list[dict]) -> str:
    lines = [f"按{records[0]['year']}年收费资料，本次匹配{len(records)}条标准："]
    for band in ranges:
        name = ("空调加收费" if band["kind"] == "accommodation_surcharge"
                else "未装空调住宿基价" if records[0]["year"] == 2024 else "住宿费")
        low, high, unit = band["minimum"], band["maximum"], band["unit"]
        lines.append(f"{name}：最低、最高均为{low}{unit}。" if low == high
                     else f"{name}：最低{low}{unit}，最高{high}{unit}。")
    lines.append("具体费用取决于实际住宿安排、宿舍条件和房型；详细标准可在下方展开查看。")
    if any(r["kind"] == "accommodation_surcharge" for r in records):
        lines.append("空调加收费属于附加费用，不能单独当作全部住宿费。")
    return "\n\n".join(lines)


def _cooperation_answer(question: str, record: dict) -> str:
    fields = record["fields"]
    text = f"按{record['year']}级收费资料，{record['label']}："
    amounts = []
    if fields.get("annual_fee"):
        amounts.append(f"学费{fields['annual_fee']}{_unit(fields.get('unit'))}")
    if fields.get("domestic_fee"):
        amounts.append(f"国内学费{fields['domestic_fee']}")
    if fields.get("foreign_fee"):
        amounts.append(f"国外学费{fields['foreign_fee']}")
    text += "\n\n" + "；".join(amounts) + "。"
    if fields.get("effective_period"):
        text += f"\n\n原表执收时间：{fields['effective_period']}。"
    if re.search(r"汇率|换算|折合|折算|合计|总共|一共|加起来|总费用|总额", question) and fields.get("foreign_fee"):
        text += "\n\n国内外费用的币种与计费周期不同，不能直接相加；本次未进行汇率换算。"
    if re.search(r"映射|对应|同一项目|同一个项目|更名|改名|前身|往年|历史|去年|相比|对比", question) and not fields.get("partner"):
        text += "\n\n该年收费表仅列项目原名，未写明合作院校，无法仅凭该表确认跨年度的项目对应关系。"
    return text


def render_fee_presentation(result: dict, question: str, records: list[dict]) -> dict:
    """只返回展示内容；原result、记录、命中顺序和全部源字段均不修改。"""
    housing = bool(records) and all(r["kind"] in {"accommodation", "accommodation_surcharge"} for r in records)
    ranges = _ranges(records) if housing else []
    answer = result["answer"]
    if result["status"] == "answered" and housing:
        answer = _housing_answer(records, ranges)
    elif result["status"] == "answered" and len(records) == 1 and records[0]["kind"] == "cooperation":
        answer = _cooperation_answer(question, records[0])
    return {"answer": answer, "table": _table(records, housing), "details_collapsed": housing,
            "details_label": f"查看详细住宿标准（{len(records)}条）" if housing else "收费明细",
            "source_groups": _source_groups(records), "range_summary": ranges}
