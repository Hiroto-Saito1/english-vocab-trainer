from fastapi.responses import Response

from english_vocab_trainer.ports.audio import AudioResult


def build_audio_response(result: AudioResult, head: bool, if_none_match: str | None) -> Response:
    etag = f'"{result.etag}"'
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Content-Type": "audio/mpeg",
        "Content-Length": str(len(result.body)),
    }
    if if_none_match in {etag, f"W/{etag}"}:
        return Response(status_code=304, headers=headers)
    if result.partial:
        headers["Content-Range"] = f"bytes {result.start}-{result.end}/{result.size}"
    return Response(
        content=b"" if head else result.body,
        status_code=206 if result.partial else 200,
        headers=headers,
    )
