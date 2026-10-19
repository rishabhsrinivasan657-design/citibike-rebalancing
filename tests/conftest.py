import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def status_payload() -> dict:
    return json.loads((FIXTURES / "station_status.json").read_text())


@pytest.fixture
def info_payload() -> dict:
    return json.loads((FIXTURES / "station_information.json").read_text())


class FakePublisher:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str | None, dict]] = []
        self.flushes = 0

    def publish(self, topic, key, value) -> None:
        self.messages.append((topic, key, value))

    def flush(self) -> None:
        self.flushes += 1

    def on(self, topic: str) -> list[dict]:
        return [v for t, _, v in self.messages if t == topic]


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()
