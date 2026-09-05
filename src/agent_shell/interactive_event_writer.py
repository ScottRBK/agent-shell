"""Standalone, harness-neutral JSON hook recorder. Never prints hook output."""

import fcntl
import json
import sys


def main() -> None:
    record = json.loads(sys.argv[2]) if len(sys.argv) > 2 else json.load(sys.stdin)
    if not isinstance(record, dict):
        raise ValueError("Expected a JSON object")
    with open(sys.argv[1], "a", encoding="utf-8") as log:
        fcntl.flock(log, fcntl.LOCK_EX)
        log.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.flush()


if __name__ == "__main__":
    main()
