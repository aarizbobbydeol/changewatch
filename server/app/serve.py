"""Start the API with the number of worker processes set by API_WORKERS ("auto" or a number).

docker compose runs `python -m app.serve`; a fixed `uvicorn --workers 2` ignored the setting.
"""
import uvicorn

from app.config import resolve_api_workers


def main() -> None:
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, workers=resolve_api_workers())


if __name__ == "__main__":
    main()
