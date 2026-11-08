"""面向页面的故障分类；不暴露异常原文、模型草稿或内部思考。"""
from __future__ import annotations

import json
import sqlite3

import httpx
from pydantic import ValidationError

MESSAGES = {
    "service_timeout": "本地模型服务响应超时，本次未完成回答。已确认的查询条件保留，可以重试。",
    "service_unavailable": "暂时无法连接本地模型服务，已确认的查询条件保留，请在服务恢复后重试。",
    "service_http_error": "本地模型服务返回请求错误，本次未展示未经核验的回答。",
    "generation_truncated": "模型未在本次生成预算内完成回答，未展示不完整草稿；已确认的查询条件保留。",
    "answer_validation_failed": "本次回答未通过格式或原文引用校验，已拦截未经核验的内容；可以重试。",
    "data_integrity_error": "资料或索引一致性检查未通过，本次停止回答；需维护人员核对资料与索引。",
    "data_not_ready": "本次所需资料库尚未就绪，暂时无法查询；已确认的查询条件保留。",
    "invalid_service_response": "本地服务返回的数据结构不完整，本次未展示未经核验的回答。",
    "internal_error": "本次处理发生程序异常，已确认的查询条件保留；可在调试信息中查看故障分类后反馈。",
}


def classify_exception(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "service_timeout"
    if isinstance(exc, httpx.ConnectError):
        return "service_unavailable"
    if isinstance(exc, httpx.HTTPError):
        return "service_http_error"
    if isinstance(exc, (ValidationError, json.JSONDecodeError)):
        return "answer_validation_failed"
    if isinstance(exc, sqlite3.Error):
        return "data_integrity_error"
    if isinstance(exc, KeyError):
        return "invalid_service_response"
    if isinstance(exc, ValueError):
        if any(word in str(exc) for word in ("模型摘录", "模型引用", "模型草稿", "证据中没有可核验")):
            return "answer_validation_failed"
        if any(word in str(exc) for word in ("不一致", "完整性", "校验值", "源文件", "未就绪", "索引尚未")):
            return "data_integrity_error"
        return "answer_validation_failed"
    return "internal_error"


def make_error_result(exc: Exception, *, stage: str = "routing") -> dict:
    code = classify_exception(exc)
    return {"status": "error", "answer": MESSAGES[code], "reason": "pipeline_or_validation_error",
            "error_code": code, "error_stage": stage, "error_type": type(exc).__name__,
            "conditions": {}, "evidence": [], "timings": {}}


def decorate_error(result: dict) -> dict:
    if result.get("status") != "error":
        return result
    code = result.get("error_code")
    reason = str(result.get("reason") or "")
    if not code:
        if reason == "generation_truncated":
            code = reason
        elif reason in {"index_not_ready", "structured_not_ready", "fees_not_ready", "major_sources_not_ready", "eligibility_not_ready"}:
            code = "data_not_ready"
        elif reason == "consultation_sources_not_ready":
            code = "data_integrity_error"
        else:
            code = "internal_error"
    result["error_code"] = code
    result.setdefault("error_stage", "data_check" if code.startswith("data_") else "generation" if code == "generation_truncated" else "query")
    result["answer"] = MESSAGES.get(code, MESSAGES["internal_error"])
    return result
