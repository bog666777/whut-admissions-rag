"""同口径历史位次比较；纯函数，不检索、不推断专业对应或录取结果。"""
from __future__ import annotations

from copy import deepcopy
import json
import re

HISTORICAL_YEARS = (2024, 2025)
DIMENSIONS = ("province", "subject_type", "admission_type", "major")
# 仅使用调用记录实际给出的明确身份字段，不从备注或专业简称猜合作方。
IDENTITY_FIELDS = {
    "partner": ("partner", "partner_name", "cooperation_partner"),
    "project": ("official_project_name", "cooperation_project_name"),
}


def canon(value: object) -> str:
    """只规范空白和括号；不删专业后缀、星号、合作方或招生类型。"""
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", "", value).translate(str.maketrans("（）【】", "()[]"))


def _positive_rank(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]+(?:\.0+)?", text):
        return None
    rank = int(text.split(".")[0])
    return rank if rank > 0 else None


def _payload(record: dict) -> dict:
    value = record.get("payload")
    return value if isinstance(value, dict) else {}


def _identities(record: dict) -> dict[str, set[str]]:
    result = {}
    for group, keys in IDENTITY_FIELDS.items():
        values = {canon(container[key]) for container in (record, _payload(record))
                  for key in keys if container.get(key) is not None and canon(container[key])}
        if values:
            result[group] = values
    return result


def _identity_matches(plan: dict, history: dict) -> bool:
    left, right = _identities(plan), _identities(history)
    # 一方明确了身份时，另一方缺失不能被当作同一项目；普通无此字段不额外猜测。
    return all(len(left.get(group, set())) == len(right.get(group, set())) == 1
               and left[group] == right[group] for group in set(left) | set(right))


def _source_evidence(record: dict) -> dict:
    provenance = record.get("provenance") or []
    first = provenance[0] if provenance else {}
    locations = []
    for p in provenance:
        location = (f"queries[{p.get('query_index')}].response.ext.{p.get('ext_key')}"
                    f"[{p.get('row_index')}]；官方id={p.get('official_id', record.get('official_id'))}（索引从0计）")
        if p.get("csv_path"):
            location += f"；交叉核对 {p['csv_path']} 第{p.get('csv_row')}行"
        if location not in locations:
            locations.append(location)
    return {"chunk_id": record["record_key"], "record_key": record["record_key"],
            "source_id": first.get("source_id", "admission"),
            "title": first.get("title", "武汉理工大学官方历史录取记录"), "year": record["year"],
            "url": first.get("official_url"), "source_path": first.get("source_path"),
            "location": "；".join(locations) if locations else "原记录未提供来源定位",
            "locations": locations, "provenance": deepcopy(provenance),
            "text": json.dumps(_payload(record), ensure_ascii=False, indent=2), "distance": None}


def _distinct(records: list[dict]) -> list[dict]:
    """同key同原行只算一个事实；同key不同原行也保留为冲突版本。"""
    results, seen = [], {}
    for record in records:
        key = record.get("record_key")
        if not isinstance(key, str) or not key:
            raise ValueError("历史记录缺少可追溯的record_key")
        signature = (key, json.dumps(_payload(record), ensure_ascii=False, sort_keys=True))
        if signature not in seen:
            seen[signature] = len(results)
            results.append(deepcopy(record))
        else:
            # 重复事实的不同返回定位仍保留，绝不改变传入对象。
            current = results[seen[signature]]
            current.setdefault("provenance", [])
            for p in record.get("provenance") or []:
                if p not in current["provenance"]:
                    current["provenance"].append(deepcopy(p))
    return results


def _empty(year: int | None, reason: str) -> dict:
    return {"year": year, "records": [], "historical_record_keys": [],
            "minimum_score": None, "historical_min_rank": None, "rank_raw": None,
            "delta": None, "position": "无法比较", "missing_reason": reason,
            "conflict": False, "history_major": None, "history_remarks": None,
            "source_evidence": []}


def compare_history(plan_record: dict, admission_records: list[dict],
                    target_year: int, user_rank: int) -> list[dict]:
    """逐个更早年度返回比较或明确缺失，保留所有匹配原记录与来源。

    正常行的minimum_score/rank_raw/history_major/history_remarks保留官方原值。
    冲突行不选取任何数值，rank_raw/history_major/history_remarks以列表保留各版本。
    delta=user_rank-historical_min_rank；只描述位次数字关系，不生成风险等级。
    """
    if type(target_year) is not int or target_year not in {2024, 2025, 2026}:
        raise ValueError("填报年份必须在2024—2026年范围内")
    rank = _positive_rank(user_rank)
    if rank is None:
        raise ValueError("用户位次必须为完整的正整数")
    if (plan_record.get("dataset") != "plan" or plan_record.get("granularity") != "major"
            or type(plan_record.get("year")) is not int or plan_record["year"] != target_year
            or any(not canon(plan_record.get(field)) for field in DIMENSIONS)):
        raise ValueError("必须提供填报当年、维度完整的官方分专业计划记录")
    years = [year for year in HISTORICAL_YEARS if year < target_year]
    if not years:
        return [_empty(None, "知识库没有早于该填报年份的可用历史年份")]
    results = []
    for year in years:
        base = [r for r in admission_records
                if r.get("dataset") == "admission" and r.get("granularity") == "major"
                and type(r.get("year")) is int and r["year"] == year
                and all(canon(r.get(field)) == canon(plan_record.get(field)) for field in DIMENSIONS)]
        matching = _distinct([r for r in base if _identity_matches(plan_record, r)])
        if not matching:
            reason = ("同名记录的合作方或项目明示身份不一致或不完整，无法确认同一项目"
                      if base else "该年未找到同省份、科类、招生类型及专业名称的官方专业记录")
            results.append(_empty(year, reason))
            continue
        row = _empty(year, "")
        row.update(records=matching,
                   historical_record_keys=list(dict.fromkeys(r["record_key"] for r in matching)),
                   source_evidence=[_source_evidence(r) for r in matching])
        if len(matching) > 1:
            row.update(conflict=True, missing_reason="同一筛选口径存在多条不同官方记录，未择取或合并比较",
                       rank_raw=[_payload(r).get("wcz") for r in matching],
                       history_major=[r.get("major") for r in matching],
                       history_remarks=[_payload(r).get("remarks") for r in matching])
            results.append(row)
            continue
        record = matching[0]
        payload = _payload(record)
        raw_rank = payload.get("wcz")
        historical_rank = _positive_rank(raw_rank)
        row.update(minimum_score=payload.get("zdf"), rank_raw=raw_rank,
                   history_major=record.get("major"), history_remarks=payload.get("remarks"),
                   historical_min_rank=historical_rank)
        if historical_rank is None:
            row["missing_reason"] = "官方未提供可比较的数值位次；未从文字、分数或其他字段反推"
        else:
            delta = rank - historical_rank
            row.update(delta=delta, missing_reason=None,
                       position="用户位次数字较小" if delta < 0 else "用户位次数字相同" if delta == 0 else "用户位次数字较大")
        results.append(row)
    return results
