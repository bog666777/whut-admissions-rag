"""从已登记的官方JSON无损导入结构化记录，并逐条核对四份CSV。

原始资料只读；重复接口返回只保存一条事实，但完整保存每次返回的定位。
query_index/row_index是JSON数组的零基索引，csv_row是含表头的物理行号。
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
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


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data" / "structured_sources.json"
DATABASE = ROOT / "runtime" / "structured.sqlite3"
SCHEMA_VERSION = "structured-v1"
RECORD_FIELDS = (
    "record_key", "dataset", "year", "province", "subject_type",
    "admission_type", "granularity", "major", "official_id", "payload", "provenance",
)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_path(relative: str) -> Path:
    path = (ROOT / relative.replace("\\", "/")).resolve()
    if not path.is_relative_to((ROOT / "知识库官方资料").resolve()):
        raise ValueError("结构化源必须位于只读官方资料目录")
    return path


def _official_url(value: str) -> None:
    parsed = urlparse(value)
    host = parsed.hostname or ""
    if parsed.scheme not in {"http", "https"} or not (host == "whut.edu.cn" or host.endswith(".whut.edu.cn")):
        raise ValueError("结构化来源链接不是武汉理工大学官方HTTP地址")


def _manifest() -> dict:
    value = json.loads(MANIFEST.read_text(encoding="utf-8-sig"))
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("结构化来源清单版本不支持")
    if set(value.get("datasets", {})) != {"admission", "plan"}:
        raise ValueError("结构化资料类别登记不完整")
    sources = value.get("sources", [])
    if not sources or len({s["source_id"] for s in sources}) != len(sources):
        raise ValueError("结构化来源标识为空或重复")
    return value


def _registered_sources(config: dict) -> tuple[dict, dict, dict]:
    """同时核对项目登记摘要与原有SHA256校验清单，发现变化即停止。"""
    registry = config["checksum_registry"]
    registry_path = _source_path(registry["path"])
    if _hash(registry_path) != registry["sha256"]:
        raise ValueError("原有文件校验清单发生变化，需要重新登记结构化来源")
    with registry_path.open(encoding="utf-8-sig", newline="") as stream:
        checksums = {row["relative_path"].replace("\\", "/"): row for row in csv.DictReader(stream)}
    paths, sources = {}, {}
    hashes = {registry["path"]: registry["sha256"]}
    for source in config["sources"]:
        path = _source_path(source["path"])
        key = path.relative_to((ROOT / "知识库官方资料").resolve()).as_posix()
        registered = checksums.get(key)
        if not registered or registered["sha256"] != source["sha256"]:
            raise ValueError(f"来源摘要不在原有校验清单中：{source['source_id']}")
        if not re.fullmatch(r"[0-9a-f]{64}", source["sha256"]):
            raise ValueError("来源SHA256格式无效")
        if path.stat().st_size != int(registered["size_bytes"]) or path.stat().st_size != source["size_bytes"]:
            raise ValueError(f"来源长度发生变化：{source['source_id']}")
        if _hash(path) != source["sha256"]:
            raise ValueError(f"来源内容发生变化：{source['source_id']}")
        _official_url(source["official_url"])
        _official_url(source["api_endpoint"])
        for endpoint in source.get("api_endpoints", []):
            _official_url(endpoint)
        sources[source["source_id"]] = source
        paths[source["source_id"]] = path
        hashes[source["path"]] = source["sha256"]
    return sources, paths, hashes


def _csv_value(value) -> str:
    # CSV无法区分JSON null与空字符串；核对时用空值投影，存储仍保留JSON原值。
    return "" if value is None else str(value)


def _prepare_records(config: dict, sources: dict, paths: dict) -> tuple[list[dict], dict]:
    records = {}
    crosschecks = []
    raw_metadata = {}
    query_coverage = Counter()
    all_major_occurrences = 0
    all_major_keys = set()
    total_occurrences = 0
    for dataset, dataset_config in config["datasets"].items():
        raw_source = sources[dataset_config["raw_source_id"]]
        raw_path = paths[raw_source["source_id"]]
        document = json.loads(raw_path.read_text(encoding="utf-8-sig"))
        if not isinstance(document.get("queries"), list):
            raise ValueError("官方JSON缺少查询列表")
        raw_metadata[dataset] = document.get("metadata", {})
        pending, columns, ext_stats = {}, {}, {}
        for ext_key, ext_config in dataset_config["ext_sources"].items():
            csv_source = sources[ext_config["csv_source_id"]]
            with paths[csv_source["source_id"]].open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                if reader.fieldnames != ext_config["csv_columns"]:
                    raise ValueError(f"CSV表头发生变化：{csv_source['source_id']}")
                columns[ext_key] = reader.fieldnames
                queues = defaultdict(deque)
                csv_count = 0
                for physical_line, row in enumerate(reader, 2):
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError("CSV列数与表头不一致")
                    queues[tuple(row[field] for field in reader.fieldnames)].append(physical_line)
                    csv_count += 1
                pending[ext_key] = queues
                ext_stats[ext_key] = {"dataset": dataset, "ext_key": ext_key,
                                      "csv_path": csv_source["path"], "csv_rows": csv_count,
                                      "matched_occurrences": 0}
        for query_index, query in enumerate(document["queries"]):
            request, response = query["request"], query["response"]
            if response.get("success") is not True or response.get("code") != 200:
                raise ValueError(f"原始响应失败，停止导入：{dataset} query_index={query_index}")
            ext = response.get("ext")
            if not isinstance(ext, dict) or set(ext) != set(dataset_config["ext_sources"]):
                raise ValueError("接口返回列表与已登记ext_key不同")
            query_coverage[(dataset, int(request["year"]), "queries")] += 1
            for ext_key, rows in ext.items():
                if not isinstance(rows, list):
                    raise ValueError("接口结果不是记录列表")
                ext_config = dataset_config["ext_sources"][ext_key]
                csv_source = sources[ext_config["csv_source_id"]]
                for row_index, row in enumerate(rows):
                    if not isinstance(row, dict):
                        raise ValueError("官方记录不是对象")
                    year = row.get("year")
                    if type(year) is not int or year not in dataset_config["allowed_years"]:
                        raise ValueError("记录适用年份超出本次登记范围")
                    if row.get("province") != request.get("province") or year != request.get("year"):
                        raise ValueError("记录省份或年份与原始请求不一致")
                    for field in ("province", "subjectType", "type"):
                        if not isinstance(row.get(field), str) or not row[field]:
                            raise ValueError(f"记录缺少可靠的{field}字段")
                    if ext_config["granularity"] == "major" and not isinstance(row.get("majorType"), str):
                        raise ValueError("专业列表缺少官方专业名称")
                    if any(field not in row for field in columns[ext_key]):
                        raise ValueError("原始记录不能完整投影至CSV字段")
                    projection = tuple(_csv_value(row[field]) for field in columns[ext_key])
                    csv_rows = pending[ext_key].get(projection)
                    if not csv_rows:
                        raise ValueError(f"官方JSON记录未在CSV找到逐字段一致行：{dataset}/{ext_key}/{query_index}/{row_index}")
                    csv_row = csv_rows.popleft()
                    ext_stats[ext_key]["matched_occurrences"] += 1
                    official_id = None if row.get("id") is None or row.get("id") == "" else str(row["id"])
                    identity = official_id if official_id is not None else "sha256:" + hashlib.sha256(_json(row).encode("utf-8")).hexdigest()
                    record_key = f"{dataset}/{ext_key}/{year}/{identity}"
                    granularity = ("school_summary" if row.get("majorType") == "全部"
                                   else ext_config["granularity"])
                    record = {
                        "record_key": record_key, "dataset": dataset, "year": year,
                        "province": row["province"], "subject_type": row["subjectType"],
                        "admission_type": row["type"], "granularity": granularity,
                        "major": row.get("majorType") if granularity == "major" else None,
                        "official_id": official_id, "payload": row, "provenance": [],
                    }
                    if record_key in records and _json(records[record_key]["payload"]) != _json(row):
                        raise ValueError(f"相同记录主键存在冲突，停止导入：{record_key}")
                    if record_key not in records:
                        records[record_key] = record
                    records[record_key]["provenance"].append({
                        "source_id": raw_source["source_id"], "source_path": raw_source["path"],
                        "source_sha256": raw_source["sha256"], "title": raw_source["title"],
                        "official_url": raw_source["official_url"], "api_endpoint": raw_source["api_endpoint"],
                        "query_index": query_index, "request": request,
                        "ext_key": ext_key, "row_index": row_index, "official_id": official_id,
                        "csv_path": csv_source["path"], "csv_row": csv_row,
                        "csv_match_basis": "all CSV columns; official ID comes from JSON",
                    })
                    total_occurrences += 1
                    query_coverage[(dataset, year, "returned_occurrences")] += 1
                    if row.get("majorType") == "全部":
                        all_major_occurrences += 1
                        all_major_keys.add(record_key)
        for ext_key, queues in pending.items():
            leftover = sum(len(queue) for queue in queues.values())
            if leftover:
                raise ValueError(f"CSV存在未在JSON匹配的行：{dataset}/{ext_key}，剩余{leftover}行")
            crosschecks.append({**ext_stats[ext_key], "unmatched_rows": 0})
    result = sorted(records.values(), key=lambda record: record["record_key"])
    if not result:
        raise ValueError("没有可导入的官方结构化记录")
    audit = {
        "raw_return_occurrences": total_occurrences, "unique_record_count": len(result),
        "duplicate_return_occurrences": total_occurrences - len(result),
        "csv_crosschecks": crosschecks, "raw_source_metadata": raw_metadata,
        "query_coverage": [{"dataset": d, "year": y,
                            "query_count": query_coverage[(d, y, "queries")],
                            "returned_occurrences": query_coverage[(d, y, "returned_occurrences")]}
                           for d, y in sorted({(d, y) for d, y, _ in query_coverage})],
        "major_type_all_audit": {"return_occurrences": all_major_occurrences,
                                 "unique_record_count": len(all_major_keys),
                                 "classification": "school_summary; ext_key remains part of the key",
                                 "present": bool(all_major_keys)},
        "provenance_indices": {"query_index": "zero-based JSON array index",
                               "row_index": "zero-based response ext list index",
                               "csv_row": "one-based physical CSV line, header is line 1"},
    }
    return result, audit


def _fingerprint(records: list[dict]) -> str:
    digest = hashlib.sha256()
    for record in sorted(records, key=lambda item: item["record_key"]):
        digest.update(_json(record).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _coverage(records: list[dict]) -> list[dict]:
    counts = Counter()
    provinces = defaultdict(set)
    for record in records:
        key = (record["dataset"], record["year"], record["granularity"])
        counts[key] += 1
        provinces[key].add(record["province"])
    return [{"dataset": dataset, "year": year, "granularity": granularity,
             "record_count": count, "province_count": len(provinces[(dataset, year, granularity)])}
            for (dataset, year, granularity), count in sorted(counts.items())]


def _coverage_gaps(records: list[dict]) -> list[str]:
    present = {(record["dataset"], record["year"]) for record in records}
    gaps = []
    for year in (2024, 2025, 2026):
        if ("plan", year) not in present:
            gaps.append(f"{year}年招生计划：现有官方接口响应无可导入记录，不能用其他年份替代。")
        if ("admission", year) not in present:
            gaps.append(f"{year}年录取分数与位次：本步未导入对应结构化记录；图片公告不参与本步查询。")
    return gaps


def _read_database(path: Path) -> tuple[list[dict], dict]:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        connection.execute("BEGIN")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("结构化数据库完整性检查失败")
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        rows = connection.execute("SELECT " + ",".join(RECORD_FIELDS) + " FROM records ORDER BY record_key").fetchall()
        records = []
        for values in rows:
            record = dict(zip(RECORD_FIELDS, values))
            record["payload"] = json.loads(record["payload"])
            record["provenance"] = json.loads(record["provenance"])
            if not isinstance(record["payload"], dict) or not isinstance(record["provenance"], list) or not record["provenance"]:
                raise ValueError("结构化原行或来源定位损坏")
            records.append(record)
    return records, metadata


def _checked_records() -> tuple[list[dict], dict]:
    config = _manifest()
    _, _, hashes = _registered_sources(config)
    if not DATABASE.exists():
        raise ValueError("尚未显式导入结构化资料")
    records, metadata = _read_database(DATABASE)
    fingerprint = _fingerprint(records)
    if (metadata.get("schema_version") != SCHEMA_VERSION
            or metadata.get("manifest_sha256") != _hash(MANIFEST)
            or metadata.get("source_hashes") != _json(hashes)
            or metadata.get("record_count") != str(len(records))
            or metadata.get("records_fingerprint") != fingerprint
            or not records):
        raise ValueError("结构化数据库与已登记来源、行数或数据指纹不一致，需要重新导入")
    return records, {"ready": True, "record_count": len(records),
                     "by_year_dataset_scope": _coverage(records),
                     "coverage_gaps": _coverage_gaps(records), "source_hashes": hashes,
                     "records_fingerprint": fingerprint,
                     "provenance_occurrences": sum(len(record["provenance"]) for record in records),
                     "human_signoff": False, "schema_version": SCHEMA_VERSION}


def structured_status() -> dict:
    status = {"ready": False, "record_count": 0, "by_year_dataset_scope": [],
              "source_hashes": {}, "coverage_gaps": [], "human_signoff": False,
              "schema_version": SCHEMA_VERSION}
    try:
        _, checked = _checked_records()
        return checked
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        status["reason"] = f"{type(exc).__name__}: {exc}"
        return status


def load_records() -> list[dict]:
    """只有源摘要和完整数据库指纹通过校验后才提供原行，失败时不返回旧数据。"""
    records, _ = _checked_records()
    return records


def import_structured() -> dict:
    started = time.perf_counter()
    report_path = ROOT / "reports" / "step2_import.json"
    temporary_path = None
    try:
        config = _manifest()
        manifest_sha256 = _hash(MANIFEST)
        sources, paths, hashes = _registered_sources(config)
        records, audit = _prepare_records(config, sources, paths)
        fingerprint = _fingerprint(records)
        DATABASE.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=".structured-", suffix=".sqlite3", dir=DATABASE.parent)
        os.close(handle)
        temporary_path = Path(temporary)
        with closing(sqlite3.connect(temporary_path)) as connection:
            connection.executescript("""
                PRAGMA synchronous=FULL;
                CREATE TABLE records (
                    record_key TEXT PRIMARY KEY,
                    dataset TEXT NOT NULL CHECK(dataset IN ('admission','plan')),
                    year INTEGER NOT NULL CHECK(year BETWEEN 2024 AND 2026),
                    province TEXT NOT NULL, subject_type TEXT NOT NULL, admission_type TEXT NOT NULL,
                    granularity TEXT NOT NULL CHECK(granularity IN ('school_summary','major')),
                    major TEXT, official_id TEXT, payload TEXT NOT NULL, provenance TEXT NOT NULL
                );
                CREATE INDEX query_dimensions ON records(dataset,year,province,subject_type,admission_type,granularity,major);
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            connection.executemany("INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
                tuple(_json(record[field]) if field in {"payload", "provenance"} else record[field]
                      for field in RECORD_FIELDS) for record in records
            ])
            connection.executemany("INSERT INTO metadata VALUES (?,?)", [
                ("schema_version", SCHEMA_VERSION), ("manifest_sha256", manifest_sha256),
                ("source_hashes", _json(hashes)), ("record_count", str(len(records))),
                ("records_fingerprint", fingerprint),
            ])
            connection.commit()
        roundtrip, metadata = _read_database(temporary_path)
        if len(roundtrip) != len(records) or _fingerprint(roundtrip) != fingerprint:
            raise ValueError("结构化数据库写入后回读不一致")
        # 替换前再次核对，解析期间源或登记清单变化时保留原数据库。
        _registered_sources(config)
        if _hash(MANIFEST) != manifest_sha256:
            raise ValueError("导入期间来源登记清单发生变化")
        os.replace(temporary_path, DATABASE)
        temporary_path = None
        report = {"status": "imported", "ready": True, "record_count": len(records),
                  "by_year_dataset_scope": _coverage(records), "coverage_gaps": _coverage_gaps(records),
                  "source_hashes": hashes, "records_fingerprint": fingerprint,
                  "source_file_count": len(sources), "human_signoff": False,
                  "verification": config["verification"], "schema_version": SCHEMA_VERSION,
                  "seconds": round(time.perf_counter() - started, 3),
                  "official_numeric_values_preserved": True, "original_files_modified": False,
                  "audit": audit}
        report_path.parent.mkdir(exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    except Exception as exc:
        failure = {"status": "failed", "ready": False, "error_type": type(exc).__name__,
                   "reason": str(exc), "seconds": round(time.perf_counter() - started, 3),
                   "original_files_modified": False}
        report_path.parent.mkdir(exist_ok=True)
        report_path.write_text(json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
