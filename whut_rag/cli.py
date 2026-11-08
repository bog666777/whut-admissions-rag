"""项目命令行入口；只有显式smoke命令会保存合成测试结果。"""
from __future__ import annotations

import argparse
import json

from whut_rag.core import answer_question, build_index, get_status
from whut_rag.structured_store import import_structured
from whut_rag.fee_store import import_fees
from whut_rag.eligibility_store import import_eligibility


def main() -> None:
    parser = argparse.ArgumentParser(description="武汉理工大学最小文本RAG验证")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("index")
    sub.add_parser("import-structured")
    sub.add_parser("import-fees")
    sub.add_parser("import-eligibility")
    ask = sub.add_parser("ask")
    ask.add_argument("question")
    ask.add_argument("--year", type=int)
    args = parser.parse_args()
    if args.command == "status":
        result = get_status()
    elif args.command == "index":
        result = build_index()
    elif args.command == "import-structured":
        result = import_structured()
    elif args.command == "import-fees":
        result = import_fees()
    elif args.command == "import-eligibility":
        result = import_eligibility()
    else:
        result = answer_question(args.question, args.year)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
