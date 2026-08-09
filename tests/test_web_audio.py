from english_vocab_trainer.ports.audio import AudioResult
from english_vocab_trainer.web.audio import build_audio_response


def test_audio_response_range_head_and_etag() -> None:
    result = AudioResult(b"ab", 4, "tag", 1, 2, True)
    response = build_audio_response(result, False, None)
    assert response.status_code == 206 and response.headers["content-range"] == "bytes 1-2/4"
    assert build_audio_response(result, True, None).body == b""
    assert build_audio_response(result, False, '"tag"').status_code == 304
