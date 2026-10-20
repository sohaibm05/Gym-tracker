"""Write the API's OpenAPI document to docs/openapi.json.

    python scripts/export_openapi.py           write it
    python scripts/export_openapi.py --check   exit 1 if the file is stale

The document is generated from the route definitions in api.py, so it cannot
describe a route that does not exist or miss one that does. The file is
committed so it can be read, diffed and loaded into an editor without running
anything; tests/test_api_v2.py fails if it falls behind the code.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import api  # noqa: E402

TARGET = ROOT / "docs" / "openapi.json"


def render() -> str:
    return json.dumps(api.openapi_document(), indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    current = render()
    if "--check" in sys.argv[1:]:
        if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != current:
            print(f"{TARGET.relative_to(ROOT)} is out of date; run scripts/export_openapi.py")
            return 1
        print(f"{TARGET.relative_to(ROOT)} is up to date")
        return 0
    TARGET.write_text(current, encoding="utf-8", newline="\n")
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
