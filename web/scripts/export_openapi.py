"""Writes the API's OpenAPI document, which the client's types are generated from.

    python web/scripts/export_openapi.py
    npm --prefix web run types

tests/test_api_contract.py fails if this file is out of date, so the client cannot drift from the API.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from repomind.api.app import create_app  # noqa: E402
from repomind.api.services import Services  # noqa: E402

OUTPUT = ROOT / "web" / "src" / "api" / "openapi.json"


def document() -> str:
    app = create_app(Services("postgresql://unused", None, None, None, None))
    return json.dumps(app.openapi(), indent=1, sort_keys=True) + "\n"


if __name__ == "__main__":
    OUTPUT.write_text(document(), encoding="utf-8", newline="\n")
    print(f"wrote {OUTPUT.relative_to(ROOT)}")
