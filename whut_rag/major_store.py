"""专业信息的只读证据适配层；无新数据库、无模型、无普通专业课程推断。"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlparse

import pdfplumber

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_FIELDS = {"大类所含专业", "所属专业大类", "学习或培养内容", "培养安排", "合作院校",
                  "办学地点", "教学语言", "培养目标", "核心课程", "特色课程", "学历学位"}
PDF2024_PATH = "知识库官方资料/06_中外合作与专业介绍/武汉理工大学国际教育学院2024年招生介绍.pdf"
PDF2024_SHA256 = "f12bb2390060fa4c15e19b501f27529a63ea9796744e9a20f4c0d924e81bc2d8"
PDF2024_URL = "https://sie.whut.edu.cn/zsxxw/bkzn/202406/P020240620364928901976.pdf"
PDF_PAGE_HASHES = {
    2: "5598a0b8e16af7e042b4e3ab02624f9d7849368fb106503c1e4f9fcd6d85c74b",
    4: "7008a76d9427c2d0a9de820995ca12676305e7158912e9c9aa701bd61221f7cd",
    6: "5af02678e04e89db03c53115da2e068c5d1704c2567b66ca6e33b020432bee48",
}
# 逐字段边界经原PDF文字层和页面视觉核对，不包括纯图片第3/5页或就业统计。
PDF_SPANS = {
    4: {"approval": (24, 97), "teaching": (147, 201), "学历学位": (370, 462),
        "培养目标": (485, 619), "核心课程": (627, 682), "特色课程": (690, 737),
        "yearly": (737, 1006), "mode": (1712, 1837)},
    6: {"approval": (23, 92), "teaching": (143, 197), "学历学位": (289, 421),
        "培养目标": (444, 636), "核心课程": (644, 674), "特色课程": (682, 725)},
}
_CACHE: tuple[str, list[dict]] | None = None


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canon(value: str) -> str:
    return re.sub(r"\s+", "", value).translate(str.maketrans("（）", "()"))


def _original(relative: str, registry: dict, expected: str | None = None) -> tuple[Path, str]:
    path = (ROOT / relative.replace("\\", "/")).resolve()
    allowed = (ROOT / "知识库官方资料").resolve()
    if not path.is_relative_to(allowed):
        raise ValueError("专业信息来源越过官方资料目录")
    item = registry.get(path.relative_to(allowed).as_posix())
    actual = _hash(path)
    if (not item or actual != item["sha256"] or path.stat().st_size != int(item["size_bytes"])
            or (expected is not None and actual != expected)):
        raise ValueError(f"专业信息源摘要或长度变化：{relative}")
    return path, actual


def _guard_inputs() -> dict:
    from whut_rag.structured_store import structured_status
    from whut_rag.eligibility_store import eligibility_status
    structured, eligibility = structured_status(), eligibility_status()
    for label, status in (("结构化计划", structured), ("目录", eligibility)):
        if not status.get("ready"):
            raise ValueError(f"{label}来源未就绪或完整性校验失败：{status.get('reason', '')}")
    registry_path = ROOT / "知识库官方资料/07_来源与覆盖说明/文件校验清单_SHA256.csv"
    registry_key = registry_path.relative_to(ROOT).as_posix()
    if _hash(registry_path) != structured["source_hashes"].get(registry_key):
        raise ValueError("专业信息来源校验清单与已核验结构化源不一致")
    with registry_path.open(encoding="utf-8-sig", newline="") as stream:
        registry = {r["relative_path"].replace("\\", "/"): r for r in csv.DictReader(stream)}
    manifest_path = ROOT / "data/sample_sources.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    text_hashes = {}
    for source in manifest["sources"]:
        parsed = urlparse(source["url"])
        host = parsed.hostname or ""
        if parsed.scheme not in {"http", "https"} or not host.endswith(".whut.edu.cn"):
            raise ValueError("专业文字来源不是学校官方HTTP地址")
        _, digest = _original(source["path"], registry, source["sha256"])
        text_hashes[source["path"]] = digest
    pdf_path, pdf_hash = _original(PDF2024_PATH, registry, PDF2024_SHA256)
    source_hashes = {**structured["source_hashes"], **eligibility["source_hashes"],
                     **text_hashes, PDF2024_PATH: pdf_hash}
    key = hashlib.sha256(_json({"adapter": "major-v1", "structured": structured["records_fingerprint"],
                               "eligibility": eligibility["records_fingerprint"],
                               "text_manifest": _hash(manifest_path), "sources": source_hashes}).encode()).hexdigest()
    return {"key": key, "registry": registry, "pdf_path": pdf_path, "source_hashes": source_hashes}


def _profile(pid: str, name: str, year: int, scope: str, kind: str,
             fields: dict, evidence: list[dict], **extra) -> dict:
    if set(fields) - ALLOWED_FIELDS or any(not isinstance(v, str) or not v.strip() for v in fields.values()):
        raise ValueError("专业信息字段不在已确认范围，或原值为空")
    return {"profile_id": pid, "name": name, "year": year, "scope": scope,
            "scope_label": "全国普通本科相关官方资料（具体省份/招生类型以证据为准）" if scope == "general" else "台湾学测招生目录，不能替代全国普通高考资料",
            "kind": kind, "fields": fields, "evidence": evidence, "provenance": [], "aliases": [], **extra}


def _record_evidence(record: dict) -> dict:
    p = record["provenance"][0]
    location = f"queries[{p['query_index']}].response.ext.{p['ext_key']}[{p['row_index']}]；官方id={record['official_id']}（索引从0计）"
    if p.get("csv_path"):
        location += f"；交叉核对 {p['csv_path']} 第{p['csv_row']}行"
    return {"chunk_id": record["record_key"], "source_id": p["source_id"], "title": p["title"],
            "year": record["year"], "url": p["official_url"], "location": location,
            "text": json.dumps(record["payload"], ensure_ascii=False, indent=2), "source_path": p["source_path"]}


def _kind(name: str) -> str:
    if "中外合作办学" in name:
        return "project"
    return "category" if "类" in name else "major"


def _plans(records: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for record in records:
        if record["dataset"] == "plan" and record["year"] == 2026 and record["granularity"] == "major":
            groups[(record["major"], _json(record["payload"].get("remarks")))].append(record)
    if not groups:
        raise ValueError("没有已校验的2026分专业计划可供专业信息适配")
    profiles = []
    for (name, _), rows in groups.items():
        rows.sort(key=lambda r: r["record_key"])
        remarks = rows[0]["payload"].get("remarks")
        fields = {"大类所含专业": remarks} if isinstance(remarks, str) and remarks.strip() else {}
        pid = "major-plan-2026-" + hashlib.sha256(_json([name, remarks]).encode()).hexdigest()[:20]
        provinces = sorted({r["province"] for r in rows})
        profiles.append(_profile(pid, name, 2026, "general", _kind(name), fields,
                                 [_record_evidence(rows[0])],
                                 provenance=[{"record_key": r["record_key"], "province": r["province"],
                                              "subject_type": r["subject_type"], "admission_type": r["admission_type"],
                                              "provenance": deepcopy(r["provenance"])} for r in rows],
                                 coverage_provinces=provinces,
                                 coverage_note="仅对应当前计划实际列出的省份和招生类型，不表示全国各省均有该专业招生。"))
    return profiles


def _catalogues(records: list[dict]) -> list[dict]:
    profiles = []
    for record in records:
        if record["kind"] != "catalogue_row":
            continue
        if record["scope"] != "taiwan_gsat" or record.get("usable_for_general_admission") is not False:
            raise ValueError("目录专业信息的台湾学测范围丢失")
        p = record["payload"]
        fields = {}
        if p.get("included_majors"):
            fields["大类所含专业"] = p["included_majors"]
        if p.get("teaching_language"):
            fields["教学语言"] = p["teaching_language"]
        if p.get("remarks") and re.fullmatch(r"学制\s*\d+年", p["remarks"]):
            fields["培养安排"] = p["remarks"]
        evidence = {"chunk_id": record["id"], "source_id": record["source_id"], "title": record["source_title"],
                    "year": record["year"], "url": record["source_url"], "location": record["locator"],
                    "text": record["text"], "source_path": record["source_path"]}
        profiles.append(_profile("major-" + record["id"], p["major"], record["year"], "taiwan_gsat",
                                 _kind(p["major"]), fields, [evidence],
                                 provenance=[{"record_id": record["id"], "source_sha256": record["source_sha256"],
                                              "locator": record["locator"], "raw_cells": deepcopy(p["raw_cells"])}]))
    if len(profiles) != 107:
        raise ValueError("台湾学测专业信息行数变化，需要重新核对")
    return profiles


def _project_name(major: str, partner: str) -> str:
    normalized = canon(major)
    return normalized + f"(与{partner}合办)" if normalized.startswith("车辆工程(") else normalized


def _cooperation_chunks(chunks: list[dict]) -> list[dict]:
    result = []
    for chunk in chunks:
        if chunk["source_id"] != "cooperation-2026":
            continue
        paragraph = chunk["text"].splitlines()[1]
        compact = re.sub(r"\s+", "", paragraph)
        major = re.match(r"[（(]\d+[）)](.+?（中外合作办学）)", compact)
        partner = re.search(r"与([^，。；：()（）]+?大学)合办", compact)
        if not major or not partner:
            raise ValueError("合作项目名称或合作方无法从原段落确认")
        name = _project_name(major[1], partner[1])
        label = f"{major[1]}—{partner[1]}"
        fields = {"合作院校": partner[1]}
        clauses = re.split(r"[，。；]", compact)
        arrangement = [c for c in clauses if re.search(r"\d+\+\d+模式|学生需赴国外学习|如申请外方学位证书需|学生入校后第一年须|方可注册第二学年", c)]
        if arrangement:
            fields["培养安排"] = "；".join(arrangement)
        language = [c for c in clauses if "作为教学和考核语言" in c]
        if language:
            fields["教学语言"] = "；".join(language)
        campus = re.search(r"办学地点[：:]([^，。；]+)", compact)
        if campus:
            fields["办学地点"] = campus[1]
        evidence = {"chunk_id": chunk["chunk_id"], "source_id": chunk["source_id"], "title": chunk["title"],
                    "year": 2026, "url": chunk["url"], "location": chunk["location"],
                    "text": chunk["text"], "source_path": "知识库官方资料/06_中外合作与专业介绍/2026年中外合作办学招生专业说明.html"}
        result.append(_profile("major-html-" + chunk["chunk_id"], name, 2026, "general", "project", fields,
                               [evidence], label=label, aliases=[label],
                               provenance=[{"chunk_id": chunk["chunk_id"], "source_sha256": chunk["sha256"], "locator": chunk["location"]}],
                               coverage_note="2026年面向相关省份的合作项目说明，具体招生计划以省级招生考试机构公布为准。"))
    if len(result) != 6:
        raise ValueError("2026合作项目专业信息数量变化")
    return result


def _pdf_2024(path: Path) -> list[dict]:
    texts = {}
    with pdfplumber.open(path) as pdf:
        for page, expected in PDF_PAGE_HASHES.items():
            text = pdf.pages[page - 1].extract_text() or ""
            if hashlib.sha256(text.encode()).hexdigest() != expected:
                raise ValueError("2024专业介绍文字层边界变化，需重新核验字段，未改用OCR")
            texts[page] = text
    profiles = []
    for page, major, partner, alias in ((4, "车辆工程（中外合作办学）", "英国威尔士三一圣大卫大学", None),
                                        (6, "金融学（中外合作办学）", "澳大利亚纽卡斯尔大学", "中澳金融学")):
        spans = PDF_SPANS[page]
        quotes = {key: texts[page][start:end] for key, (start, end) in spans.items()}
        if canon(partner) not in canon(quotes["approval"]) or major.split("（")[0] not in quotes["approval"]:
            raise ValueError("2024项目的合作院校或专业归属无法核对")
        fields = {key: quotes[key] for key in ("学历学位", "培养目标", "核心课程", "特色课程")}
        fields["合作院校"] = partner
        teaching = quotes["teaching"]
        start = teaching.find("课程采取双语教学")
        if start < 0:
            raise ValueError("2024教学语言原句无法定位")
        fields["教学语言"] = teaching[start:]
        fields["培养安排"] = (quotes["yearly"] + "\n" + quotes["mode"] if page == 4
                            else quotes["teaching"] + "\n" + quotes["学历学位"])
        evidence = [{"chunk_id": f"major-pdf2024:p{page}:{a}-{b}", "source_id": "major-pdf2024",
                     "title": "武汉理工大学国际教育学院2024年招生介绍", "year": 2024, "url": PDF2024_URL,
                     "location": f"PDF第{page}页；{key}段；原文字层字符{a}—{b}",
                     "text": texts[page][a:b], "source_path": PDF2024_PATH} for key, (a, b) in spans.items()]
        name = _project_name(major, partner)
        label = f"{major}—{partner}"
        aliases = [label]
        if alias:
            if alias not in canon(texts[2] + texts[page]):
                raise ValueError("2024项目别名未在直接文字中找到")
            aliases.append(alias)
        profiles.append(_profile(f"major-pdf2024-p{page}", name, 2024, "general", "project", fields, evidence,
                                 label=label, aliases=aliases,
                                 scope_label="2024年国际教育学院本科合作项目介绍，仅限该专业及对应合作方",
                                 provenance=[{"source_path": PDF2024_PATH, "source_sha256": PDF2024_SHA256,
                                              "page": page, "field": key, "start": a, "end": b} for key, (a, b) in spans.items()]))
    return profiles


def _split_members(text: str) -> list[str]:
    result, start, depth = [], 0, 0
    for i, char in enumerate(text):
        if char in "（(":
            depth += 1
        elif char in "）)":
            depth -= 1
        elif char == "、" and depth == 0:
            result.append(text[start:i].strip())
            start = i + 1
    if depth != 0:
        return []
    result.append(text[start:].strip())
    return [member for member in result if member]


def _members(profiles: list[dict]) -> list[dict]:
    result = []
    for parent in profiles:
        children = parent["fields"].get("大类所含专业")
        if not children:
            continue
        for member in _split_members(children):
            if canon(member) == canon(parent["name"]):
                continue
            pid = "major-member-" + hashlib.sha256(_json([parent["profile_id"], member]).encode()).hexdigest()[:20]
            result.append(_profile(pid, member, parent["year"], parent["scope"], "major",
                                   {"所属专业大类": parent["name"]}, deepcopy(parent["evidence"]),
                                   parent_profile_id=parent["profile_id"], provenance=deepcopy(parent["provenance"]),
                                   scope_label=parent["scope_label"], coverage_provinces=parent.get("coverage_provinces", []),
                                   coverage_note=parent.get("coverage_note", "")))
    return result


def _build(guard: dict) -> list[dict]:
    from whut_rag.structured_store import load_records as load_structured
    from whut_rag.eligibility_store import load_records as load_eligibility
    # 必须延迟导入：core的查询路由本身也会调用本适配层。
    from whut_rag.core import parse_sources
    _, chunks = parse_sources()
    profiles = [*_plans(load_structured()), *_catalogues(load_eligibility()),
                *_cooperation_chunks(chunks), *_pdf_2024(guard["pdf_path"])]
    profiles.extend(_members(profiles))
    if len({p["profile_id"] for p in profiles}) != len(profiles):
        raise ValueError("专业信息profile_id重复")
    return sorted(profiles, key=lambda p: p["profile_id"])


def _checked_profiles() -> tuple[list[dict], dict]:
    global _CACHE
    # 每次实际核验源内容和依赖库指纹；缓存只省去解析/聚合，不能掩盖源或DB变化。
    guard = _guard_inputs()
    if _CACHE is None or _CACHE[0] != guard["key"]:
        _CACHE = (guard["key"], _build(guard))
    return deepcopy(_CACHE[1]), guard


def load_profiles() -> list[dict]:
    return _checked_profiles()[0]


def major_status() -> dict:
    base = {"ready": False, "profile_count": 0, "record_count": 0, "years": [], "scopes": [], "gaps": []}
    try:
        profiles, guard = _checked_profiles()
        counts = Counter((p["year"], p["scope"], p["kind"]) for p in profiles)
        gaps = ["普通专业目前主要有2026分省计划名称及大类包含关系，不能据此编造课程、培养目标或就业信息。",
                "2024课程与学位资料仅限国际教育学院两个明确合作项目，不扩展为2025/2026资料。",
                "台湾学测目录独立于全国普通高考；同年同名大类所含专业也可能不同，不跨范围合并。",
                "2026合作说明不具备完整核心课程、特色课程或培养目标；缺失维度保持缺失。",
                "成员专业仅继承原文明确的所属大类关系，不继承大类课程、语言或培养介绍。",
                "未使用纯图片页、未定年份动态索引或非官方薪酬与就业评价。"]
        return {"ready": True, "profile_count": len(profiles), "record_count": len(profiles),
                "years": sorted({p["year"] for p in profiles}), "scopes": sorted({p["scope"] for p in profiles}),
                "by_year_scope_kind": [{"year": y, "scope": s, "kind": k, "record_count": n}
                                       for (y, s, k), n in sorted(counts.items())],
                "source_count": len({e["source_id"] for p in profiles for e in p["evidence"]}),
                "source_hashes": guard["source_hashes"], "content_fingerprint": guard["key"],
                "gaps": gaps, "coverage_gaps": gaps, "human_signoff": False}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {**base, "reason": f"{type(exc).__name__}: {exc}", "human_signoff": False}
