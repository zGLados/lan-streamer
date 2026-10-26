"""The documentation of the interfaces matches the code."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def normalize(path: str) -> str:
    return re.sub(r"[{<][^}>]+[}>]", "{}", path.split("?")[0])


def test_every_http_endpoint_is_documented_and_exists():
    code = (ROOT / "src" / "lanstreamer" / "web" / "api.py").read_text(encoding="utf-8")
    routes = {
        ("WS" if method == "websocket" else method.upper(), normalize(path))
        for method, path in re.findall(r'@app\.(get|post|put|patch|delete|websocket)\(\s*"([^"]+)"', code)
    }
    routes.discard(("GET", "/favicon.png"))
    doc = (ROOT / "docs" / "interfaces.md").read_text(encoding="utf-8")
    documented = {
        (method, normalize(path))
        for method, path in re.findall(r"`(GET|POST|PUT|PATCH|DELETE|WS) (/[^`\s]*)`", doc)
    }
    assert routes - documented == set(), "in the code, missing in docs/interfaces.md"
    assert documented - routes == set(), "in docs/interfaces.md, missing in the code"
