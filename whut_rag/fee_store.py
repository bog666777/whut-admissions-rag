"""收费原件的独立、可追溯入库；不读取图片金额，不计算费用合计。"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
import pdfplumber

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data/fee_sources.json"
DATABASE = ROOT / "runtime/fees.sqlite3"
SCHEMA_VERSION = "fees-v1"
KINDS = {"tuition", "cooperation", "accommodation", "accommodation_surcharge",
         "minor", "preparatory", "rule", "info", "medical_check", "meal_deposit"}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compact(value) -> str:
    return re.sub(r"\s+", "", value or "")


def _path(relative: str) -> Path:
    path = (ROOT / relative.replace("\\", "/")).resolve()
    if not path.is_relative_to((ROOT / "知识库官方资料").resolve()):
        raise ValueError("收费来源越过官方资料目录")
    return path


def _manifest() -> dict:
    config = json.loads(MANIFEST.read_text(encoding="utf-8-sig"))
    if config.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("收费清单版本不支持")
    return config


def _sources(config: dict) -> tuple[dict, dict, dict]:
    registry = config["checksum_registry"]
    registry_path = _path(registry["path"])
    if _hash(registry_path) != registry["sha256"]:
        raise ValueError("原SHA256清单发生变化，需要重新核对收费来源")
    with registry_path.open(encoding="utf-8-sig", newline="") as stream:
        checksums = {row["relative_path"].replace("\\", "/"): row for row in csv.DictReader(stream)}
    sources, paths, hashes = {}, {}, {registry["path"]: registry["sha256"]}
    for source in config["sources"]:
        sid = source["source_id"]
        if sid in sources:
            raise ValueError("收费source_id重复")
        path = _path(source["path"])
        relative = path.relative_to((ROOT / "知识库官方资料").resolve()).as_posix()
        baseline = checksums.get(relative)
        if (not baseline or baseline["sha256"] != source["sha256"]
                or _hash(path) != source["sha256"]
                or path.stat().st_size != int(baseline["size_bytes"])
                or path.stat().st_size != source["size_bytes"]):
            raise ValueError(f"收费来源摘要或长度校验失败：{sid}")
        parsed = urlparse(source["url"])
        host = parsed.hostname or ""
        if parsed.scheme not in {"http", "https"} or not (host == "whut.edu.cn" or host.endswith(".whut.edu.cn")):
            raise ValueError("收费来源不是学校官方HTTP地址")
        sources[sid], paths[sid] = source, path
        hashes[source["path"]] = source["sha256"]
    for source in sources.values():
        if source["role"] != "fee_pdf":
            continue
        parent = sources[source["parent_source_id"]]
        soup = BeautifulSoup(paths[parent["source_id"]].read_bytes(), "html.parser")
        links = {urljoin(parent["url"], a["href"]) for a in soup.select(".download a[href]")}
        if source["url"] not in links or str(source["year"]) not in soup.select_one(".display_title").get_text():
            raise ValueError("收费PDF附件链接或适用年级无法在父网页确认")
    return sources, paths, hashes


def _split_names(text: str) -> list[str]:
    names, start, depth = [], 0, 0
    for i, char in enumerate(text):
        if char in "（(":
            depth += 1
        elif char in "）)":
            depth -= 1
        elif char in "、，" and depth == 0:
            names.append(text[start:i])
            start = i + 1
    names.append(text[start:])
    return [name for name in names if name]


def _target_names(label: str) -> list[str]:
    """仅拆分原表显式列出的名称，不用其他年度目录扩充别名。"""
    label = re.sub(r"^\d+[.．]", "", _compact(label))
    result = [label]
    if label.count("（") != label.count("）"):
        return result
    for item in _split_names(label):
        match = re.search(r"（按大类招生[，,]含", item)
        if match:
            prefix = item[:match.start()]
            result.append(prefix)
            # 含专业列表的末尾括号属于该完整的显式招生大类。
            members = item[match.end():]
            if members.endswith("）"):
                result.extend(_split_names(members[:-1]))
        else:
            result.append(item)
    return list(dict.fromkeys(name for name in result if name))


def _record(source: dict, kind: str, label: str, text: str, *, fields: dict | None = None,
            page: int | None = None, locations: list[str], target_names: list[str] | None = None) -> dict:
    if kind not in KINDS or not text.strip():
        raise ValueError("收费记录类型或原文为空")
    base = {"profession_fee": None, "credit_unit_fee": None, "credit_unit": None, "total_credits": None,
            "annual_fee": None, "unit": None, "period": None, "remarks": None, "raw_cells": []}
    base.update(fields or {})
    location = "；".join(locations)
    identity = hashlib.sha256(_json([source["source_id"], kind, label, location]).encode()).hexdigest()[:20]
    return {"id": f"fee-{source['year']}-{kind}-{identity}", "year": source["year"],
            "kind": kind, "label": label, "text": text, "fields": base,
            "source_id": source["source_id"], "source_path": source["path"],
            "source_sha256": source["sha256"], "title": source["title"], "url": source["url"],
            "location": location, "page": page, "target_names": target_names or [label]}


def _table_entry(page: int, table: int, row: int, cells: list) -> dict:
    return {"cells": [_compact(cell) for cell in cells],
            "raw": [{"page": page, "table": table, "row": row, "cells": cells}]}


def _entry_locations(entry: dict) -> list[str]:
    return [f"PDF第{r['page']}页 表{r['table']} 第{r['row']}行" for r in entry["raw"]]


def _row_record(source: dict, kind: str, entry: dict, label: str, fields: dict,
                *, target_names: list[str] | None = None) -> dict:
    fields = {**fields, "raw_cells": entry["raw"]}
    if fields.get("credit_unit_fee") is not None:
        fields["credit_unit"] = "元/学分"
    text = f"{source['year']}年资料；{label}。\n" + "；".join(
        f"{key}：{value}" for key, value in (
            ("专业学费", fields.get("profession_fee")),
            ("学分单价（元/学分）", fields.get("credit_unit_fee")),
            ("总学分", fields.get("total_credits")), ("年费标准", fields.get("annual_fee")),
            ("计费单位", fields.get("unit")), ("收取对象", fields.get("audience")),
            ("执收时间", fields.get("effective_period")), ("备注", fields.get("remarks"))) if value not in {None, ""})
    if kind == "tuition":
        text += "。专业学费不是学费总额；学分学费根据实际课程学分另行缴纳，不根据总学分计算单年应缴额。"
    if kind == "cooperation":
        text += "。" + fields["scope_note"]
    return _record(source, kind, label, text, fields=fields, page=entry["raw"][0]["page"],
                   locations=_entry_locations(entry), target_names=target_names or _target_names(label))


def _extract_pdf(source: dict, path: Path) -> tuple[list[dict], dict]:
    with pdfplumber.open(path) as pdf:
        if len(pdf.pages) != 6:
            raise ValueError("收费PDF页数变化，需要重新核对")
        # 每次正式导入即时解析原PDF，不用inspection缓存或OCR文字。
        pages = [{"page": i, "text": page.extract_text() or "", "tables": page.extract_tables()}
                 for i, page in enumerate(pdf.pages, 1)]
    return (_pdf_2024(source, pages) if source["year"] == 2024 else _pdf_2025(source, pages))


def _pdf_2024(source: dict, pages: list[dict]) -> tuple[list[dict], dict]:
    records, entries, excluded = [], [], []
    merges = []
    header = pages[1]["tables"][0][1]
    rate = re.search(r"(\d+(?:\.\d+)?)元/学分", _compact(header[4]))
    if not rate:
        raise ValueError("2024学分学费表头无法确认")
    credit_rate = rate.group(1)
    for page in pages[1:4]:
        for ri, cells in enumerate(page["tables"][0], 1):
            item = _table_entry(page["page"], 1, ri, cells)
            values = item["cells"]
            if values[0] in {"收费项目", "1.本科生"}:
                continue
            if values[0] and all(not value for value in values[1:]):
                expected = "舶）" if page["page"] == 3 else "工程、人文地理与城乡规划"
                if not entries or not values[0].startswith(expected) or entries[-1]["raw"][-1]["page"] != page["page"] - 1:
                    raise ValueError("2024跨页专业名无法可靠拼接")
                entries[-1]["cells"][0] += values[0]
                entries[-1]["raw"].extend(item["raw"])
                merges.append(_entry_locations(entries[-1]))
                continue
            if len(values) != 5 or values[1] != "2024级":
                raise ValueError("2024本科学费行结构异常")
            entries.append(item)
    if len(merges) != 2:
        raise ValueError("2024跨页专业续行数量变化")
    for entry in entries:
        label, audience, unit, fee, credits = entry["cells"]
        if "少数民族本科预科生" in label:
            records.append(_row_record(source, "preparatory", entry, label,
                                       {"annual_fee": fee, "audience": audience, "unit": unit,
                                        "period": "学年", "year_basis": "entry_cohort"}))
        elif "辅修费" in label:
            records.append(_row_record(source, "minor", entry, label,
                                       {"credit_unit_fee": fee, "audience": audience, "unit": unit,
                                        "period": "学分", "year_basis": "entry_cohort"}))
        else:
            credit_match = re.fullmatch(r"总学分(\d+(?:\.\d+)?)", credits)
            if not fee.isdigit() or not credit_match:
                raise ValueError("2024专业学费或总学分列无法确认")
            records.append(_row_record(source, "tuition", entry, label,
                                       {"profession_fee": fee, "credit_unit_fee": credit_rate,
                                        "total_credits": credit_match.group(1), "audience": audience,
                                        "unit": unit, "period": "学年", "year_basis": "entry_cohort"}))
    formula = re.search(r"本科生学分制学费=专业学费\+学分学费.*?专业学费按学年缴纳，学分学费根据实际课程学分缴纳。", _compact(pages[1]["text"]))
    if not formula:
        raise ValueError("2024学分制公式说明缺失")
    records.append(_record(source, "rule", "本科生学分制收费规则", formula.group() + "\n表头：" + _compact(header[4]),
                           page=2, locations=["PDF第2页 表格前说明及表1第2行学分学费表头"],
                           fields={"credit_unit_fee": credit_rate, "unit": "元/学分", "year_basis": "entry_cohort",
                                   "raw_cells": [{"page": 2, "table": 1, "row": 2, "cells": header}]}))
    housing_state = ""
    for page_no, table_index in ((4, 1), (5, 0)):
        for ri, cells in enumerate(pages[page_no - 1]["tables"][table_index], 1):
            entry = _table_entry(page_no, table_index + 1, ri, cells)
            row = entry["cells"]
            if row[0] == "收费项目":
                continue
            if row[0].startswith("一、未装空调"):
                housing_state = "base"
                continue
            if row[0].startswith("二、装空调"):
                housing_state = "surcharge"
                records.append(_record(source, "rule", "住宿空调加收规则", row[0], page=page_no,
                                       locations=_entry_locations(entry), fields={"year_basis": "fee_schedule", "raw_cells": entry["raw"]}))
                continue
            if len(row) != 4 or row[1] != "在校生" or not row[3].isdigit() or not housing_state:
                raise ValueError("2024住宿费行结构异常")
            kind = "accommodation" if housing_state == "base" else "accommodation_surcharge"
            records.append(_row_record(source, kind, entry, row[0],
                                       {"annual_fee": row[3], "unit": row[2], "audience": row[1],
                                        "period": "年", "year_basis": "fee_schedule",
                                        "remarks": "未装空调基价" if housing_state == "base" else "在规定基础住宿费上加收；未与基价合计"}))
    category = ""
    for page_no, table_index in ((5, 1), (6, 0), (6, 1)):
        for ri, cells in enumerate(pages[page_no - 1]["tables"][table_index], 1):
            entry = _table_entry(page_no, table_index + 1, ri, cells)
            row = entry["cells"]
            if row[0] in {"收费类别", "收费项目"}:
                continue
            if page_no == 6 and table_index == 1:
                excluded.append({"location": _entry_locations(entry), "label": row[0], "reason": "继续教育不属于本步本科招生收费"})
                continue
            if row[0]:
                category = row[0]
            if not category.startswith("一、中外合作"):
                excluded.append({"location": _entry_locations(entry), "label": row[1] if len(row) > 1 else row[0], "reason": category})
                continue
            if len(row) != 5 or row[2] != "2024级" or not row[4].isdigit():
                raise ValueError("2024本科合作收费行无法确认")
            scope_note = "原收费表未说明具体招生渠道或合作院校；不能据此推断统招范围，也不能映射为其他年度项目。"
            records.append(_row_record(source, "cooperation", entry, row[1],
                                       {"annual_fee": row[4], "unit": row[3], "audience": row[2],
                                        "period": "学年", "year_basis": "entry_cohort", "scope_note": scope_note,
                                        "name": row[1], "partner": None}, target_names=[row[1]]))
    counts = Counter(r["kind"] for r in records)
    if any(counts[k] != n for k, n in {"tuition": 27, "cooperation": 4, "accommodation": 5,
                                      "accommodation_surcharge": 5, "minor": 1, "preparatory": 1}.items()):
        raise ValueError("2024收费分类行数变化，需要重新核验")
    return records, {"year": 2024, "cross_page_merges": merges, "excluded_rows": excluded,
                     "record_counts": dict(counts), "credit_unit_from_original_header": credit_rate}


def _pdf_2025(source: dict, pages: list[dict]) -> tuple[list[dict], dict]:
    records, excluded = [], []
    section = ""
    for page in pages:
        for ti, table in enumerate(page["tables"], 1):
            for ri, cells in enumerate(table, 1):
                entry = _table_entry(page["page"], ti, ri, cells)
                row = entry["cells"]
                label = row[0]
                if not label:
                    continue
                if re.match(r"[一二三四五六七]、", label):
                    section = label
                    if label.startswith("一、本科生"):
                        rate = re.search(r"每学分(\d+(?:\.\d+)?)元", label)
                        if not rate:
                            raise ValueError("2025本科学分费单价无法确认")
                        credit_rate = rate.group(1)
                        records.append(_record(source, "rule", "本科生学分制收费规则", label,
                                               page=page["page"], locations=_entry_locations(entry),
                                               fields={"credit_unit_fee": credit_rate, "unit": "元/学分",
                                                       "year_basis": "entry_cohort", "raw_cells": entry["raw"]}))
                    elif label.startswith("七、住宿费"):
                        records.append(_record(source, "rule", "住宿费包含水电项目说明", label,
                                               page=page["page"], locations=_entry_locations(entry),
                                               fields={"year_basis": "fee_schedule", "raw_cells": entry["raw"]}))
                    continue
                if label == "收费项目":
                    continue
                if section.startswith(("一、本科生", "二、预科生", "三、中外合作")):
                    if len(row) != 9 or row[1] != "2025级":
                        raise ValueError("2025本科学费列结构变化")
                    if "硕士" in label or "研究生" in label:
                        excluded.append({"location": _entry_locations(entry), "label": label, "reason": "合作节中的研究生项目不属于本科范围"})
                        continue
                    fields = {"audience": row[1], "unit": row[2], "effective_period": row[7],
                              "remarks": row[8] or None, "fee_basis": row[6], "year_basis": "entry_cohort"}
                    if section.startswith("三、中外合作"):
                        fields.update(annual_fee=row[3], period="学年", name=label, partner=None,
                                      scope_note="原收费表未说明具体招生渠道或合作院校；不能据此推断统招范围，也不能映射为其他年度项目。")
                        records.append(_row_record(source, "cooperation", entry, label, fields, target_names=[label]))
                    elif section.startswith("二、预科生"):
                        records.append(_row_record(source, "preparatory", entry, label,
                                                   {**fields, "annual_fee": row[3], "period": "学年"}))
                    elif label == "辅修":
                        records.append(_row_record(source, "minor", entry, label,
                                                   {**fields, "credit_unit_fee": row[5], "period": "学分"}))
                    else:
                        credits = re.fullmatch(r"总学分(\d+(?:\.\d+)?)", row[5])
                        if not row[4].isdigit() or not credits or row[3]:
                            raise ValueError("2025专业学费/学分费列无法分离")
                        records.append(_row_record(source, "tuition", entry, label,
                                                   {**fields, "profession_fee": row[4], "credit_unit_fee": credit_rate,
                                                    "total_credits": credits.group(1), "period": "学年"}))
                elif section.startswith("七、住宿费"):
                    if len(row) not in {7, 9} or row[1] != "在校生" or not row[3].isdigit():
                        raise ValueError("2025住宿续表列结构异常")
                    basis, effective, remarks = (row[6], row[7], row[8]) if len(row) == 9 else (row[4], row[5], row[6])
                    records.append(_row_record(source, "accommodation", entry, label,
                                               {"annual_fee": row[3], "unit": row[2], "audience": row[1],
                                                "effective_period": effective, "remarks": remarks or "本行明确有空调，按该原行价格，不另加2024空调费", 
                                                "fee_basis": basis, "period": "年", "year_basis": "fee_schedule"}))
                else:
                    excluded.append({"location": _entry_locations(entry), "label": label, "reason": section or "非本科收费表头"})
    counts = Counter(r["kind"] for r in records)
    if any(counts[k] != n for k, n in {"tuition": 31, "cooperation": 4, "accommodation": 11,
                                      "minor": 1, "preparatory": 1}.items()):
        raise ValueError("2025收费分类行数变化，需要重新核验")
    return records, {"year": 2025, "record_counts": dict(counts), "excluded_rows": excluded,
                     "accommodation_continues_pages": [5, 6], "credit_unit_from_original_rule": credit_rate}


def _welcome_2026(source: dict, path: Path) -> tuple[list[dict], dict]:
    soup = BeautifulSoup(path.read_bytes(), "html.parser")
    bodies = soup.select(source["selector"])
    if len(bodies) != 1:
        raise ValueError("2026收费正文定位不唯一")
    paragraphs = []
    excluded_images = []
    for i, p in enumerate(bodies[0].find_all("p"), 1):
        if p.find("img"):
            excluded_images.append(i)
            continue
        text = p.get_text(" ", strip=True)
        if text:
            paragraphs.append((i, text))
    records = []
    for i, text in paragraphs:
        plain = _compact(text)
        location = f"HTML {source['selector']} 原始段落p{i}"
        common = {"year_basis": "entry_cohort", "audience": "2026级本科新生", "original_paragraph": text}
        if "住宿费收费标准区间" in plain:
            match = re.search(r"\d+元/年至\d+元/年", plain)
            if not match:
                raise ValueError("2026住宿费区间无法原文确认")
            records.append(_record(source, "accommodation", "新生住宿费标准区间", text, locations=[location],
                                   fields={**common, "annual_fee": match.group(), "unit": "元/年", "period": "年",
                                           "remarks": "具体住宿费标准根据新生住宿安排情况确定"}))
        elif "健康检查费" in plain:
            amount = re.search(r"按照(\d+)元/人收取", plain)
            if not amount:
                raise ValueError("2026健康检查费无法原文确认")
            records.append(_record(source, "medical_check", "健康检查费", text, locations=[location],
                                   fields={**common, "amount": amount.group(1), "unit": "元/人"}))
        elif "基本医疗保险" in plain:
            records.append(_record(source, "info", "大学生城乡居民基本医疗保险", text, locations=[location],
                                   fields={**common, "amount": None, "remarks": "原网页未列金额，按最新文件执行"}))
        elif "实行学分制收费" in plain:
            continuation = next(((j, t) for j, t in paragraphs if j == i + 1 and "政策有变" in t), None)
            locations = [location]
            if continuation:
                text += "\n" + continuation[1]
                locations.append(f"HTML {source['selector']} 原始段落p{continuation[0]}")
            records.append(_record(source, "rule", "本科生学分制收费及政策变化说明", text, locations=locations,
                                   fields={**common, "remarks": "中外合作办学项目学生除外；本页文字未列学分单价"}))
        elif "一卡通" in plain and "预存" in plain:
            amount = re.search(r"伙食费(\d+)元", plain)
            if not amount:
                raise ValueError("2026一卡通预存金额无法原文确认")
            records.append(_record(source, "meal_deposit", "校园一卡通预存伙食费", text, locations=[location],
                                   fields={**common, "amount": amount.group(1), "unit": "元",
                                           "remarks": "预存至本人校园卡，不是学费或全年生活费标准"}))
        elif "政策有变" in plain or re.match(r"[一二三]、|[12]\.全日制|[12]\.住宿费标准", plain):
            continue
        else:
            raise ValueError(f"2026收费正文出现未分类实质段落p{i}，需核对")
    if len(records) != 5 or len(excluded_images) != 1:
        raise ValueError("2026收费文字或图片段落数量变化")
    return records, {"year": 2026, "role": "welcome_text", "image_paragraphs_excluded": excluded_images,
                     "image_amounts_extracted": False}


def _cooperation_2026(source: dict, path: Path) -> tuple[list[dict], dict]:
    soup = BeautifulSoup(path.read_bytes(), "html.parser")
    bodies = soup.select(source["selector"])
    if len(bodies) != 1 or bodies[0].find(["img", "table"]):
        raise ValueError("2026合作费用正文无法安全直接解析")
    paragraphs = [(i, p.get_text(" ", strip=True)) for i, p in enumerate(bodies[0].find_all("p", recursive=False), 1)]
    paragraphs = [(i, t) for i, t in paragraphs if t]
    records = []
    for i, text in paragraphs[1:-1]:
        plain = _compact(text)
        name = re.match(r"[（(]\d+[）)](.+?（中外合作办学）)", plain)
        partner = re.search(r"与([^，。；：()（）]+?大学)合办", plain)
        domestic = re.search(r"国内学费[：:]([^，。；]+)", plain)
        foreign = re.search(r"国外学费[：:]([^，。；]+)", plain)
        if not name or not partner or not domestic:
            raise ValueError("2026合作名称、合作方或国内费用无法原文确认")
        label = f"{name.group(1)}—{partner.group(1)}"
        records.append(_record(source, "cooperation", label,
                               paragraphs[0][1] + "\n" + text + "\n" + paragraphs[-1][1],
                               locations=[f"HTML {source['selector']} 原始段落p{paragraphs[0][0]},p{i},p{paragraphs[-1][0]}"],
                               fields={"name": name.group(1), "partner": partner.group(1),
                                       "domestic_fee": domestic.group(1),
                                       "foreign_fee": foreign.group(1) if foreign else None,
                                       "period": "年", "audience": "2026年对应中外合作项目",
                                       "year_basis": "entry_cohort", "original_paragraph": text,
                                       "remarks": paragraphs[-1][1]},
                               target_names=[name.group(1), name.group(1).split("（")[0], label]))
    if len(records) != 6:
        raise ValueError("2026合作费用项目数量变化")
    return records, {"year": 2026, "role": "cooperation_text", "project_count": len(records)}


def _parse(config: dict, sources: dict, paths: dict) -> tuple[list[dict], list[dict]]:
    records, audits = [], []
    for sid, source in sources.items():
        role = source["role"]
        if role == "fee_pdf":
            batch, audit = _extract_pdf(source, paths[sid])
        elif role == "welcome_html":
            batch, audit = _welcome_2026(source, paths[sid])
        elif role == "cooperation_html":
            batch, audit = _cooperation_2026(source, paths[sid])
        else:
            continue
        records.extend(batch)
        audits.append({"source_id": sid, **audit})
    if len({r["id"] for r in records}) != len(records) or not records:
        raise ValueError("收费记录为空或标识重复")
    return sorted(records, key=lambda r: r["id"]), audits


def _fingerprint(records: list[dict]) -> str:
    return hashlib.sha256("\n".join(_json(r) for r in sorted(records, key=lambda r: r["id"])).encode()).hexdigest()


def _by_year_kind(records: list[dict]) -> list[dict]:
    counts = Counter((r["year"], r["kind"]) for r in records)
    return [{"year": year, "kind": kind, "record_count": count} for (year, kind), count in sorted(counts.items())]


def _checked() -> tuple[list[dict], dict]:
    config = _manifest()
    sources, _, hashes = _sources(config)
    with closing(sqlite3.connect(f"{DATABASE.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        connection.execute("BEGIN")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("收费数据库完整性校验失败")
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        records = []
        for rid, year, kind, payload in connection.execute("SELECT id,year,kind,payload FROM records ORDER BY id"):
            record = json.loads(payload)
            if (record.get("id"), record.get("year"), record.get("kind")) != (rid, year, kind):
                raise ValueError("收费数据库外层字段不一致")
            records.append(record)
    fingerprint = _fingerprint(records)
    if (not records or metadata.get("manifest_sha256") != _hash(MANIFEST)
            or metadata.get("source_hashes") != _json(hashes)
            or metadata.get("record_count") != str(len(records))
            or metadata.get("records_fingerprint") != fingerprint
            or metadata.get("schema_version") != SCHEMA_VERSION):
        raise ValueError("收费源摘要、记录数量或数据库指纹变化，需要重新核验导入")
    return records, {"ready": True, "record_count": len(records), "source_count": len(sources),
                     "by_year_kind": _by_year_kind(records), "coverage_gaps": config["coverage_gaps"],
                     "pending_review": config["pending_review"], "source_hashes": hashes,
                     "records_fingerprint": fingerprint, "human_signoff": False}


def fee_status() -> dict:
    status = {"ready": False, "record_count": 0, "source_count": 0, "by_year_kind": [],
              "coverage_gaps": [], "pending_review": [], "human_signoff": False}
    try:
        config = _manifest()
        status.update(pending_review=config["pending_review"], coverage_gaps=config["coverage_gaps"], source_count=len(config["sources"]))
        _, checked = _checked()
        return checked
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        status["reason"] = f"{type(exc).__name__}: {exc}"
        return status


def load_fee_records() -> list[dict]:
    records, _ = _checked()
    return records


def import_fees() -> dict:
    started = time.perf_counter()
    temporary_path = None
    report_path = ROOT / "reports/step3_fee_import.json"
    try:
        config = _manifest()
        manifest_hash = _hash(MANIFEST)
        sources, paths, hashes = _sources(config)
        records, audits = _parse(config, sources, paths)
        fingerprint = _fingerprint(records)
        DATABASE.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=".fees-", suffix=".sqlite3", dir=DATABASE.parent)
        os.close(handle)
        temporary_path = Path(temporary)
        with closing(sqlite3.connect(temporary_path)) as connection:
            connection.executescript("CREATE TABLE records (id TEXT PRIMARY KEY, year INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL); CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);")
            connection.executemany("INSERT INTO records VALUES (?,?,?,?)", [(r["id"], r["year"], r["kind"], _json(r)) for r in records])
            connection.executemany("INSERT INTO metadata VALUES (?,?)", [
                ("schema_version", SCHEMA_VERSION), ("manifest_sha256", manifest_hash),
                ("source_hashes", _json(hashes)), ("record_count", str(len(records))),
                ("records_fingerprint", fingerprint)])
            connection.commit()
        _sources(config)
        if _hash(MANIFEST) != manifest_hash:
            raise ValueError("收费清单在导入期间发生变化")
        os.replace(temporary_path, DATABASE)
        temporary_path = None
        _, status = _checked()
        report = {"status": "imported", **status, "seconds": round(time.perf_counter() - started, 3),
                  "pdfplumber_version": pdfplumber.__version__, "parse_audit": audits,
                  "original_files_modified": False, "ocr_used": False, "image_amounts_imported": False,
                  "verification": config["verification"]}
        report_path.parent.mkdir(exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    except Exception as exc:
        report_path.parent.mkdir(exist_ok=True)
        report_path.write_text(json.dumps({"status": "failed", "ready": False,
                                          "error_type": type(exc).__name__, "reason": str(exc),
                                          "original_files_modified": False}, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
