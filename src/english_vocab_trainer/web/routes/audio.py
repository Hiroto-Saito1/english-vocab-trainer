"""Private audio GET and HEAD routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response

from english_vocab_trainer.adapters.local.vocabulary import SQLiteVocabularyRepository
from english_vocab_trainer.ports.audio import (
    AudioMetadata,
    AudioStorageError,
    AudioStore,
    InvalidRangeError,
    parse_single_range,
)
from english_vocab_trainer.web.audio_response import (
    build_audio_head_response,
    build_audio_response,
    if_none_match_matches,
)
from english_vocab_trainer.web.dependencies import audio_store
from english_vocab_trainer.web.dependencies import repository as repository_dependency

Repository = Annotated[SQLiteVocabularyRepository, Depends(repository_dependency)]
Audio = Annotated[AudioStore, Depends(audio_store)]
RangeHeader = Annotated[str | None, Header(alias="Range")]
IfNoneMatch = Annotated[str | None, Header(alias="If-None-Match")]
router = APIRouter()


def _audio_metadata(store: AudioStore, key: str) -> AudioMetadata:
    try:
        return store.head(key)
    except FileNotFoundError as exc:
        raise HTTPException(404, "audio not found") from exc
    except AudioStorageError as exc:
        raise HTTPException(502, "audio storage is unavailable") from exc


@router.get("/api/v1/audio/{word_id}")
def audio(
    word_id: str,
    repository: Repository,
    store: Audio,
    range_header: RangeHeader = None,
    if_none_match: IfNoneMatch = None,
) -> Response:
    word = repository.get_word(word_id)
    if word is None:
        raise HTTPException(404, "word not found")
    # RFC 7233 evaluates Range only after preconditions: a matching cache validator
    # wins even over an otherwise invalid range and must not fetch the object body.
    if if_none_match is not None or range_header is not None:
        metadata = _audio_metadata(store, word.audio_key)
        if if_none_match is not None and if_none_match_matches(metadata.etag, if_none_match):
            return build_audio_head_response(metadata, None, if_none_match)
        if range_header is not None:
            try:
                parse_single_range(range_header, metadata.size)
            except InvalidRangeError:
                return Response(
                    status_code=416,
                    headers={
                        "Accept-Ranges": "bytes",
                        "Content-Range": f"bytes */{metadata.size}",
                    },
                )
    try:
        result = store.get(word.audio_key, range_header)
    except FileNotFoundError as exc:
        raise HTTPException(404, "audio not found") from exc
    except InvalidRangeError:
        # An upstream may reject a range after local validation (for example, a race).
        metadata = _audio_metadata(store, word.audio_key)
        return Response(
            status_code=416,
            headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes */{metadata.size}"},
        )
    except AudioStorageError as exc:
        raise HTTPException(502, "audio storage is unavailable") from exc
    return build_audio_response(result, if_none_match)


@router.head("/api/v1/audio/{word_id}")
def audio_head(
    word_id: str,
    repository: Repository,
    store: Audio,
    range_header: RangeHeader = None,
    if_none_match: IfNoneMatch = None,
) -> Response:
    word = repository.get_word(word_id)
    if word is None:
        raise HTTPException(404, "word not found")
    metadata = _audio_metadata(store, word.audio_key)
    try:
        return build_audio_head_response(metadata, range_header, if_none_match)
    except InvalidRangeError:
        return Response(
            status_code=416,
            headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes */{metadata.size}"},
        )
