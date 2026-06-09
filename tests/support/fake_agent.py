from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    mode = sys.argv[1]
    _prompt = sys.argv[2]
    output_dir = Path(".bucle")
    if mode == "success":
        append_marker(output_dir / "success.json", {"name": "task1"})
        print("task succeeded")
    elif mode == "failure":
        append_marker(output_dir / "failure.json", {"name": "task1", "reason": "bad result"})
        print("task failed")
    elif mode == "none":
        print("task did not write a marker")
    else:
        raise SystemExit(f"unknown mode: {mode}")


def append_marker(path: Path, entry: dict[str, str]) -> None:
    data = json.loads(path.read_text())
    data.append(entry)
    path.write_text(json.dumps(data, indent=2) + "\n")


if __name__ == "__main__":
    main()
