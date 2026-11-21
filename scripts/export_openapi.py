"""Export the composed public API contract without connecting to a database."""

import json
from pathlib import Path

from main import app


def main() -> None:
    target = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"
    target.write_text(json.dumps(app.openapi(), indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
