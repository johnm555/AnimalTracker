import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.observation import Observation  # noqa: E402
from src.state_machine import Topology  # noqa: E402

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
T0 = datetime(2026, 9, 20, 15, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def topology() -> Topology:
    return Topology.from_yaml(Path(__file__).resolve().parent / "fixtures" / "cameras.yaml")


@pytest.fixture
def obs_factory():
    """obs(camera, seconds_after_T0, probability) -> Observation with a fake id."""
    counter = {"id": 0}

    def make(camera: str, seconds: float = 0.0, p: float = 0.95, **kw) -> Observation:
        counter["id"] += 1
        return Observation(camera_id=camera, timestamp=T0 + timedelta(seconds=seconds),
                           winston_probability=p, id=counter["id"], **kw)

    return make
