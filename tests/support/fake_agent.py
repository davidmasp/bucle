from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    mode = sys.argv[1]
    _prompt = sys.argv[2]
    output_dir = Path(".bucle")
    if mode == "success":
        append_marker(output_dir / "success.txt", "task1")
        print("task succeeded")
    elif mode == "failure":
        append_marker(output_dir / "failure.txt", "task1,bad result")
        print("task failed")
    elif mode == "none":
        print("task did not write a marker")
    else:
        raise SystemExit(f"unknown mode: {mode}")


def append_marker(path: Path, entry: str) -> None:
    with path.open("a") as marker:
        marker.write(entry + "\n")


if __name__ == "__main__":
    main()
