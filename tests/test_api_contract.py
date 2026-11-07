"""The web client's types are generated from the API's document, so that document must be current."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_web_clients_api_document_is_current():
    spec = importlib.util.spec_from_file_location("export_openapi", ROOT / "web" / "scripts" / "export_openapi.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    saved = (ROOT / "web" / "src" / "api" / "openapi.json").read_text(encoding="utf-8")
    assert saved == module.document(), "Run: python web/scripts/export_openapi.py && npm --prefix web run types"
