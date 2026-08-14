"""Production browser headers, hidden development routes, and safe 500s."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp)
        (data / "config.json").write_text(
            (ROOT / "config.example.json").read_text(encoding="utf-8"), encoding="utf-8")
        os.environ["BASK_DATA_DIR"] = str(data)

        sys.path.insert(0, str(ROOT / "scanner"))
        import db
        db.init_db()
        from fastapi.testclient import TestClient
        from starlette.routing import Route
        from server.app import app

        async def fail_with_secret(_request):
            raise RuntimeError("PRIVATE-INTERNAL-MARKER")

        # StaticFiles is the final catch-all route, so put the synthetic probe
        # ahead of it. This route exists only in this isolated test process.
        app.router.routes.insert(0, Route(
            "/api/__security_failure_probe", fail_with_secret, methods=["GET"]))
        client = TestClient(app, raise_server_exceptions=False)

        for response in (
            client.get("/"),
            client.get("/api/does-not-exist"),
            client.get("/api/__security_failure_probe"),
        ):
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["x-frame-options"] == "DENY"
            assert response.headers["referrer-policy"] == "no-referrer"
            assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
            assert "object-src 'none'" in response.headers["content-security-policy"]
            assert "script-src-elem 'self'" in response.headers["content-security-policy"]
            assert "camera=()" in response.headers["permissions-policy"]

        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404, f"{path} must not be published"

        failure = client.get("/api/__security_failure_probe")
        assert failure.status_code == 500
        assert failure.headers["cache-control"] == "no-store"
        assert failure.json() == {
            "error": "Internal server error",
            "detail": "Internal server error",
        }
        assert "PRIVATE-INTERNAL-MARKER" not in failure.text

    print("Browser and API response security tests passed.")


if __name__ == "__main__":
    main()
