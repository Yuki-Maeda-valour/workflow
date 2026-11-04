#!/usr/bin/env python3
"""解析済みの profile 値からタスクの置き場を選ぶ。YAML 自体は呼出側で解析する。"""

from __future__ import annotations

import argparse
import json
import sys


class TaskStoreError(Exception):
    pass


def resolve(value: object = None) -> str:
    if value is None or (isinstance(value, str) and value == ""):
        return "file"
    if isinstance(value, str) and value in ("file", "github-issue"):
        return value
    raise TaskStoreError("task_store は file または github-issue を指定する(無い・null・空文字は file)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--value-json", help="YAML 解析後の task_store の JSON 値。省略時は file")
    args = parser.parse_args()
    try:
        value = json.loads(args.value_json) if args.value_json is not None else None
        store = resolve(value)
    except ValueError:
        print("--value-json が JSON ではない。task_store は file または github-issue を指定する", file=sys.stderr)
        return 2
    except TaskStoreError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"task_store": store}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
