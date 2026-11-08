"""报考条件的独立证据库；仅导入原文，不执行个人条件比较。"""
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
from urllib.parse import urlparse

from bs4 import BeautifulSoup
import pdfplumber

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data/eligibility_sources.json"
DATABASE = ROOT / "runtime/eligibility.sqlite3"
SCHEMA_VERSION = "eligibility-v1"

# 经本地章程逐条阅读后选定的证据位置。不是个人资格判定规则。
CHARTER_SELECTION = {
    2024: {8: "subjects", 10: "subjects", 11: "general", 17: "language", 18: "physical",
           19: "gender", 20: "language", 21: "general", 22: "general", 23: "general",
           24: "general", 25: "general", 28: "general"},
    2025: {8: "subjects", 10: "subjects", 11: "general", 16: "language", 17: "physical",
           18: "gender", 19: "language", 20: "general", 21: "general", 22: "general",
           23: "general", 26: "general"},
    2026: {9: "subjects", 10: "general", 12: "subjects", 16: "general", 17: "language",
           18: "physical", 19: "gender", 20: "language", 21: "general", 22: "general",
           23: "general", 27: "general"},
}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compact(value: str | None) -> str:
    return re.sub(r"\s+", "", value or "")


def _path(relative: str) -> Path:
    path = (ROOT / relative.replace("\\", "/")).resolve()
    if not path.is_relative_to((ROOT / "知识库官方资料").resolve()):
        raise ValueError("报考条件来源越过官方资料目录")
    return path


def _manifest() -> dict:
    config = json.loads(MANIFEST.read_text(encoding="utf-8-sig"))
    if config.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("报考条件来源清单版本不支持")
    return config


def _sources(config: dict) -> tuple[dict, dict, dict]:
    registry = config["checksum_registry"]
    registry_path = _path(registry["path"])
    if _hash(registry_path) != registry["sha256"]:
        raise ValueError("原SHA256清单发生变化，需要重新核验报考条件资料")
    with registry_path.open(encoding="utf-8-sig", newline="") as stream:
        baseline = {r["relative_path"].replace("\\", "/"): r for r in csv.DictReader(stream)}
    sources, paths, hashes = {}, {}, {registry["path"]: registry["sha256"]}
    for source in config["sources"]:
        sid = source["source_id"]
        if sid in sources or source["scope"] not in {"general_charter", "taiwan_gsat"}:
            raise ValueError("报考条件来源标识或招生范围不明确")
        path = _path(source["path"])
        relative = path.relative_to((ROOT / "知识库官方资料").resolve()).as_posix()
        old = baseline.get(relative)
        if (not old or old["sha256"] != source["sha256"] or _hash(path) != source["sha256"]
                or path.stat().st_size != int(old["size_bytes"])
                or path.stat().st_size != source["size_bytes"]):
            raise ValueError(f"报考条件源摘要或长度校验失败：{sid}")
        for url in [source["url"], source.get("parent_url", source["url"])]:
            parsed = urlparse(url)
            host = parsed.hostname or ""
            if parsed.scheme not in {"http", "https"} or not (host == "whut.edu.cn" or host.endswith(".whut.edu.cn")):
                raise ValueError("报考条件来源链接不是学校官方HTTP地址")
        if source["role"] == "catalogue_pdf" and source["scope"] != "taiwan_gsat":
            raise ValueError("现有三年目录必须保留台湾学测招生范围")
        sources[sid], paths[sid] = source, path
        hashes[source["path"]] = source["sha256"]
    return sources, paths, hashes


def _base(source: dict, rid: str, kind: str, field: str, text: str, locator: str,
          payload: dict, fields: list[str] | None = None, page: int | None = None) -> dict:
    return {"id": rid, "record_id": rid, "kind": kind, "field": field,
            "fields": list(dict.fromkeys([field, *(fields or [])])), "text": text,
            "year": source["year"], "scope": source["scope"], "scope_label": source["scope_label"],
            "usable_for_general_admission": source["scope"] == "general_charter",
            "source_id": source["source_id"], "source_path": source["path"],
            "source_url": source["url"], "source_title": source["title"],
            "title": source["title"], "url": source["url"], "locator": locator,
            "source_sha256": source["sha256"], "payload": payload, "page": page}


def _chinese_number(value: str) -> int:
    units = {c: i for i, c in enumerate("零一二三四五六七八九")}
    if "十" in value:
        tens, ones = value.split("十")
        return (units[tens] if tens else 1) * 10 + (units[ones] if ones else 0)
    return units[value]


def _tags(text: str) -> list[str]:
    patterns = {"physical": r"体检|身体|色盲|色弱|色觉|单色|视力|身高|复视|听力|嗅觉|四肢",
                "language": r"外语语种|第一外语|教学语言|授课语言|英语|法语|B1",
                "gender": r"女生|男女|性别", "subjects": r"选考|选科|考试科目"}
    return [field for field, pattern in patterns.items() if re.search(pattern, text)]


def _charter(source: dict, path: Path) -> tuple[list[dict], dict]:
    soup = BeautifulSoup(path.read_bytes(), "html.parser")
    bodies = soup.select(source["selector"])
    if len(bodies) != 1 or bodies[0].find(["img", "table"]):
        raise ValueError("章程正文不能按完整文字条款安全解析")
    groups, chapter, active = [], "", None
    paragraphs = [(i, re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip())
                  for i, p in enumerate(bodies[0].find_all("p", recursive=False), 1)]
    for index, text in paragraphs:
        if not text:
            continue
        if re.match(r"第[一二三四五六七八九十]+章", text):
            if active:
                groups.append(active)
                active = None
            chapter = text
        elif match := re.match(r"第([一二三四五六七八九十]+)条", text):
            if active:
                groups.append(active)
            active = {"number": _chinese_number(match.group(1)), "article": match.group(),
                      "chapter": chapter, "paragraphs": [(index, text)]}
        elif active:
            active["paragraphs"].append((index, text))
    if active:
        groups.append(active)
    expected = 33 if source["year"] == 2024 else 31
    if len(groups) != expected or {g["number"] for g in groups} != set(range(1, expected + 1)):
        raise ValueError("章程条款数量或序号不完整")
    selection = CHARTER_SELECTION[source["year"]]
    records = []
    for group in groups:
        number = group["number"]
        if number not in selection:
            continue
        text = "\n".join(t for _, t in group["paragraphs"])
        locator = f"{group['chapter']} / {group['article']}；HTML {source['selector']} 原始段落 " + ",".join(f"p{i}" for i, _ in group["paragraphs"])
        payload = {"article_number": number, "article": group["article"], "chapter": group["chapter"],
                   "selector": source["selector"], "paragraphs": [{"index": i, "text": t} for i, t in group["paragraphs"]],
                   "scope_note": "本条款内的专业、招生类型及省份限定须与正文一并使用；不得将不适宜报考改写为禁止录取。"}
        records.append(_base(source, f"eligibility:charter:{source['year']}:article-{number}",
                             "charter_clause", selection[number], text, locator, payload,
                             fields=[*_tags(text), "general"] if selection[number] == "general" else _tags(text)))
    return records, {"source_id": source["source_id"], "all_article_count": len(groups),
                     "selected_article_numbers": sorted(selection), "record_count": len(records)}


def _catalogue(source: dict, path: Path) -> tuple[list[dict], dict]:
    year = source["year"]
    records, notes, current = [], [], None
    pending_prefix = []
    table_rows = []
    with pdfplumber.open(path) as pdf:
        if len(pdf.pages) != (2 if year == 2026 else 3):
            raise ValueError("台湾学测目录PDF页数变化")
        for pi, page in enumerate(pdf.pages, 1):
            found = page.find_tables()
            for ti, table in enumerate(found, 1):
                for ri, cells in enumerate(table.extract(), 1):
                    if _compact(cells[0]).startswith("招生专业"):
                        continue
                    if len(cells) != (5 if year == 2026 else 4) or not _compact(cells[0]):
                        raise ValueError("台湾学测目录专业行列结构变化")
                    payload = {"major": _compact(cells[0]), "included_majors": _compact(cells[1]),
                               "subject_type": cells[2], "teaching_language": cells[3],
                               "remarks": cells[4] if len(cells) == 5 else None,
                               "raw_cells": cells, "page": pi, "table": ti, "row": ri,
                               "subject_semantics": "目录原科类，不是国内高考完整选科组合",
                               "language_semantics": "主要授课/教学语言，不是高考应试外语资格要求"}
                    text = "；".join(f"{label}：{value if value not in {None, ''} else '原表空白'}" for label, value in (
                        ("招生专业（类）", payload["major"]), ("包含专业", payload["included_majors"]),
                        ("科类", payload["subject_type"]), ("教学语言", payload["teaching_language"]),
                        ("备注", payload["remarks"])))
                    fields = ["catalogue", "language", "subjects"]
                    if payload["remarks"] and _tags(payload["remarks"]):
                        fields.extend(_tags(payload["remarks"]))
                    record = _base(source, f"eligibility:catalogue:{year}:p{pi}:t{ti}:r{ri}",
                                   "catalogue_row", "catalogue", text,
                                   f"PDF第{pi}页 表{ti} 第{ri}行（含专业/科类/教学语言原列）", payload, fields, pi)
                    records.append(record)
                    table_rows.append({"page": pi, "table": ti, "row": ri, "major": payload["major"]})
            bottom = max((table.bbox[3] for table in found), default=0)
            tail = page.crop((0, bottom, page.width, page.height)).extract_text() if bottom < page.height else ""
            for line in (tail or "").splitlines():
                stripped = line.strip()
                if not stripped or stripped in {"注：", "注释："} or stripped.isdigit():
                    continue
                match = re.match(r"^(\d+)[.．](?!\d)", stripped)
                if match:
                    if current:
                        notes.append(current)
                    current = {"number": int(match.group(1)), "parts": [(pi, stripped)], "prefix": pending_prefix}
                    pending_prefix = []
                elif current:
                    current["parts"].append((pi, stripped))
                else:
                    pending_prefix.append((pi, stripped))
    if current:
        notes.append(current)
    expected_rows, expected_notes = (35, 2) if year == 2026 else (36, 5)
    if len(records) != expected_rows or {n["number"] for n in notes} != set(range(1, expected_notes + 1)):
        raise ValueError("台湾学测目录行数或附注编号不完整")
    note_ids = []
    for note in notes:
        number = note["number"]
        rid = f"eligibility:catalogue:{year}:note-{number}"
        raw_text = "\n".join(text for _, text in note["parts"])
        raw_with_prefix = "\n".join([*(text for _, text in note["prefix"]), raw_text])
        text = _compact(raw_with_prefix)
        tags = _tags(text)
        field = "physical" if "physical" in tags else "language" if "language" in tags else "catalogue"
        pages = list(dict.fromkeys(pi for pi, _ in note["parts"]))
        payload = {"note_number": number, "raw_text": raw_text, "raw_text_with_prefix": raw_with_prefix,
                   "extraction_prefix": [{"page": pi, "text": text} for pi, text in note["prefix"]],
                   "pages": pages, "is_eligibility_restriction": not (year in {2024, 2025} and number == 1),
                   "scope_note": "本附注仅限本台湾学测目录，不作为全国普通高考完整限制名单。"}
        if year in {2024, 2025} and number == 1:
            payload["certification_marker"] = "*"
            payload["marker_semantics"] = "工程教育认证/专业评估标记，不是报考资格限制"
        records.append(_base(source, rid, "catalogue_note", field, text,
                             "、".join(f"PDF第{pi}页" for pi in pages) + f" 表后附注{number}",
                             payload, ["catalogue", *tags], pages[0]))
        note_ids.append(rid)
    for record in records:
        if record["kind"] == "catalogue_row":
            record["payload"]["note_ids"] = note_ids
    blank_rows = [{"id": r["id"], "major": r["payload"]["major"], "locator": r["locator"]}
                  for r in records if r["kind"] == "catalogue_row"
                  and (r["payload"]["subject_type"] in {None, ""} or r["payload"]["teaching_language"] in {None, ""})]
    return records, {"source_id": source["source_id"], "catalogue_row_count": expected_rows,
                     "note_count": expected_notes, "scope": "taiwan_gsat",
                     "blank_subject_or_language_rows": blank_rows, "table_rows": table_rows,
                     "notes": [{"id": r["id"], "locator": r["locator"]} for r in records if r["kind"] == "catalogue_note"],
                     "scope_visual_title": source["scope_verification"]["visual_title"],
                     "title_missing_from_text_layer": True, "ocr_used": False}


def _parse(sources: dict, paths: dict) -> tuple[list[dict], list[dict]]:
    records, audits = [], []
    for sid, source in sources.items():
        batch, audit = (_charter(source, paths[sid]) if source["role"] == "charter_html" else _catalogue(source, paths[sid]))
        records.extend(batch)
        audits.append(audit)
    if not records or len({r["id"] for r in records}) != len(records):
        raise ValueError("报考条件证据为空或标识重复")
    return sorted(records, key=lambda r: r["id"]), audits


def _fingerprint(records: list[dict]) -> str:
    return hashlib.sha256("\n".join(_json(r) for r in sorted(records, key=lambda r: r["id"])).encode()).hexdigest()


def _counts(records: list[dict], name: str) -> list[dict]:
    counts = Counter((r["year"], r[name]) for r in records)
    return [{"year": year, name: value, "record_count": count} for (year, value), count in sorted(counts.items())]


def _checked() -> tuple[list[dict], dict]:
    config = _manifest()
    sources, _, hashes = _sources(config)
    with closing(sqlite3.connect(f"{DATABASE.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        connection.execute("BEGIN")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("报考条件数据库完整性校验失败")
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        records = []
        for rid, year, scope, payload in connection.execute("SELECT id,year,scope,payload FROM records ORDER BY id"):
            record = json.loads(payload)
            if (record.get("id"), record.get("year"), record.get("scope")) != (rid, year, scope):
                raise ValueError("报考条件外层字段与原记录不一致")
            records.append(record)
    fingerprint = _fingerprint(records)
    if (not records or metadata.get("schema_version") != SCHEMA_VERSION
            or metadata.get("manifest_sha256") != _hash(MANIFEST)
            or metadata.get("source_hashes") != _json(hashes)
            or metadata.get("record_count") != str(len(records))
            or metadata.get("records_fingerprint") != fingerprint):
        raise ValueError("报考条件源摘要、记录数量或数据指纹变化，需要重新核验导入")
    return records, {"ready": True, "record_count": len(records), "source_count": len(sources),
                     "by_year_kind": _counts(records, "kind"), "by_year_scope": _counts(records, "scope"),
                     "coverage_gaps": config["coverage_gaps"], "pending_review": config["pending_review"],
                     "source_hashes": hashes, "records_fingerprint": fingerprint, "human_signoff": False}


def eligibility_status() -> dict:
    status = {"ready": False, "record_count": 0, "source_count": 0, "by_year_kind": [],
              "by_year_scope": [], "coverage_gaps": [], "pending_review": [], "human_signoff": False}
    try:
        config = _manifest()
        status.update(source_count=len(config["sources"]), coverage_gaps=config["coverage_gaps"], pending_review=config["pending_review"])
        return _checked()[1]
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        status["reason"] = f"{type(exc).__name__}: {exc}"
        return status


def load_records() -> list[dict]:
    return _checked()[0]


def import_eligibility() -> dict:
    started = time.perf_counter()
    temporary_path = None
    report_path = ROOT / "reports/step4_eligibility_import.json"
    try:
        config = _manifest()
        manifest_hash = _hash(MANIFEST)
        sources, paths, hashes = _sources(config)
        records, audits = _parse(sources, paths)
        fingerprint = _fingerprint(records)
        DATABASE.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=".eligibility-", suffix=".sqlite3", dir=DATABASE.parent)
        os.close(handle)
        temporary_path = Path(temporary)
        with closing(sqlite3.connect(temporary_path)) as connection:
            connection.executescript("CREATE TABLE records (id TEXT PRIMARY KEY,year INTEGER NOT NULL,scope TEXT NOT NULL,payload TEXT NOT NULL); CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);")
            connection.executemany("INSERT INTO records VALUES (?,?,?,?)", [(r["id"], r["year"], r["scope"], _json(r)) for r in records])
            connection.executemany("INSERT INTO metadata VALUES (?,?)", [
                ("schema_version", SCHEMA_VERSION), ("manifest_sha256", manifest_hash),
                ("source_hashes", _json(hashes)), ("record_count", str(len(records))),
                ("records_fingerprint", fingerprint)])
            connection.commit()
        _sources(config)
        if _hash(MANIFEST) != manifest_hash:
            raise ValueError("报考条件清单在导入期间变化")
        os.replace(temporary_path, DATABASE)
        temporary_path = None
        _, status = _checked()
        report = {"status": "imported", **status, "seconds": round(time.perf_counter() - started, 3),
                  "parse_audit": audits, "verification": config["verification"],
                  "originals_modified": False, "ocr_used": False, "personal_comparison_performed": False}
        report_path.parent.mkdir(exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    except Exception as exc:
        report_path.parent.mkdir(exist_ok=True)
        report_path.write_text(json.dumps({"status": "failed", "ready": False, "error_type": type(exc).__name__,
                                          "reason": str(exc), "originals_modified": False}, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
