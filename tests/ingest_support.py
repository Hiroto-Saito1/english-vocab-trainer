"""Small, explicit fixtures shared by ingest responsibility tests."""

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from english_vocab_trainer.ingest.models import CatalogRecord


def audio(root: Path, source: str, level: str, name: str, content: bytes) -> Path:
    path = root / source / level / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def approved_source_tree(root: Path, count: int = 1, *, label: str = "") -> None:
    for source_index, source in enumerate(("上級SVL", "超上級SVL")):
        for index in range(count):
            term = f"{label} term {chr(ord('a') + index + source_index * 10)}".strip()
            audio(
                root,
                source,
                "2",
                f"STAGE 01　{index:04d} {term}.mp3",
                f"{source}-{index}".encode(),
            )


def records(count: int = 20) -> list[CatalogRecord]:
    return [
        CatalogRecord(
            f"audio-{sha256(f'catalog-{index}'.encode()).hexdigest()}",
            f"上級SVL/2/STAGE 01　00{index:02d} term {chr(ord('a') + index)}.mp3",
            "上級SVL",
            2,
            f"term {chr(ord('a') + index)}",
            sha256(f"catalog-{index}".encode()).hexdigest(),
            "A clear English definition includes an example sentence.",
        )
        for index in range(count)
    ]


def materialize_catalog_audio(
    root: Path, source_records: list[CatalogRecord]
) -> list[CatalogRecord]:
    result: list[CatalogRecord] = []
    for index, record in enumerate(source_records):
        content = f"audio-{index}".encode()
        path = root / record.audio_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        digest = sha256(content).hexdigest()
        result.append(replace(record, id=f"audio-{digest}", checksum=digest))
    return result
