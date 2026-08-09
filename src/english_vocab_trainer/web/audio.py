from fastapi.responses import Response

from english_vocab_trainer.ports.audio import AudioMetadata, AudioResult, parse_single_range


def build_audio_response(result: AudioResult, head: bool, if_none_match: str | None) -> Response:
    etag = f'"{result.etag}"'
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Content-Type": "audio/mpeg",
        "Content-Length": str(max(0, result.end - result.start + 1)),
    }
    etags = {part.strip() for part in if_none_match.split(",")} if if_none_match else set()
    if etag in etags or f"W/{etag}" in etags:
        return Response(status_code=304, headers=headers)
    if result.partial:
        headers["Content-Range"] = f"bytes {result.start}-{result.end}/{result.size}"
    return Response(
        content=b"" if head else result.body,
        status_code=206 if result.partial else 200,
        headers=headers,
    )


def build_audio_head_response(
    metadata: AudioMetadata, range_header: str | None, if_none_match: str | None
) -> Response:
    """Build a HEAD response from metadata without ever loading object bytes."""
    selected = parse_single_range(range_header, metadata.size)
    start, end = selected if selected is not None else (0, metadata.size - 1)
    return build_audio_response(
        AudioResult(b"", metadata.size, metadata.etag, start, end, selected is not None),
        True,
        if_none_match,
    )
