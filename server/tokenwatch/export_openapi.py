import json
from pathlib import Path

from .api import create_app


if __name__ == "__main__":
    Path("openapi.json").write_text(json.dumps(create_app().openapi(), indent=2, ensure_ascii=False) + "\n")
