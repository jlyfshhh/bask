import json
import os
import tempfile
from pathlib import Path


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(__file__).parent.parent
        data = Path(tmp)
        (data / "config.json").write_text(
            (root / "config.example.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        os.environ["BASK_DATA_DIR"] = str(data)

        from scanner import db
        from server.app import dashboard, health, room_dashboard

        assert db.DB_PATH == data / "readings.db"
        assert health() == {"ok": True}
        result = dashboard()
        assert result["enclosures"]
        room = room_dashboard()
        assert room["bask"]["enclosures"]
        assert room["shed"]["configured"] is False
        assert json.loads((data / "config.json").read_text())["enclosures"]
        assert (data / "readings.db").is_file()

    print("Bask data-path and API smoke tests passed.")


if __name__ == "__main__":
    main()
