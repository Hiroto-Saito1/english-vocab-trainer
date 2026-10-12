from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest

from english_vocab_trainer.adapters.local.database import initialize_database
from english_vocab_trainer.restore_drill import RestoreDrillError, restore_drill


def test_restore_drill_refuses_live_or_existing_output_and_validates_restored_schema(
    tmp_path: Path,
) -> None:
    live = tmp_path / "live.db"
    connection = sqlite3.connect(live)
    initialize_database(connection)
    connection.execute(
        "INSERT INTO words(id,term,level,definition,audio_key) "
        "VALUES('one','one',1,'definition','one.mp3')"
    )
    connection.commit()
    connection.close()
    output = tmp_path / "drills" / "restored.db"
    commands: list[Sequence[str]] = []

    def restore(command: Sequence[str]) -> None:
        commands.append(command)
        source = sqlite3.connect(live)
        target = sqlite3.connect(output)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

    assert restore_drill(
        live_database=live, output_database=output, config=Path("/safe.yml"), runner=restore
    ) == {
        "words": 1,
        "reviews": 0,
    }
    assert commands == [
        ("litestream", "restore", "-config", "/safe.yml", "-o", str(output), str(live))
    ]
    with pytest.raises(RestoreDrillError):
        restore_drill(live_database=live, output_database=live, config=Path("/safe.yml"))
    with pytest.raises(RestoreDrillError):
        restore_drill(live_database=live, output_database=output, config=Path("/safe.yml"))
    with pytest.raises(RestoreDrillError):
        restore_drill(
            live_database=live, output_database=Path(f"{live}-wal"), config=Path("/safe.yml")
        )


def test_restore_drill_removes_only_its_partial_output(tmp_path: Path) -> None:
    live, output = tmp_path / "live.db", tmp_path / "new.db"
    connection = sqlite3.connect(live)
    initialize_database(connection)
    connection.close()

    def partial(_: Sequence[str]) -> None:
        output.write_text("not sqlite", encoding="utf-8")

    with pytest.raises(RestoreDrillError):
        restore_drill(
            live_database=live, output_database=output, config=Path("/safe.yml"), runner=partial
        )
    assert not output.exists() and live.exists()
