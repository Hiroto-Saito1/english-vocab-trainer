"""Public liveness and database readiness routes."""

from fastapi import APIRouter, HTTPException, Request

from english_vocab_trainer.adapters.local.database import SchemaMismatchError
from english_vocab_trainer.adapters.local.vocabulary import SQLiteVocabularyRepository

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
def readiness(request: Request) -> dict[str, str]:
    """A public readiness probe that opens and initializes the current SQLite schema."""
    container = request.app.state.container
    try:
        with SQLiteVocabularyRepository(container.database) as repository:
            repository.db.execute("SELECT 1 FROM words LIMIT 1").fetchone()
    except SchemaMismatchError as exc:
        raise HTTPException(503, "database schema is incompatible") from exc
    except Exception as exc:
        raise HTTPException(503, "database is not ready") from exc
    return {"status": "ready"}
