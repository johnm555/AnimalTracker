from types import SimpleNamespace
from src.ring_client import RingClient, RingSettings
from src.state_machine import Topology
from pathlib import Path
import yaml


def test_device_allowlist_filters_before_polling():
    selected = SimpleNamespace(id=1, name="Selected")
    other = SimpleNamespace(id=2, name="Other property")
    for ids, expected in [([1], {"1"}), ([], set())]:
        client = RingClient(RingSettings.from_dict({"device_ids": ids}))
        client._ring = SimpleNamespace(devices=lambda: SimpleNamespace(video_devices=[selected, other]))
        client._index_devices()
        assert set(client._devices_by_id) == expected


def test_san_rafael_route_and_exclusions():
    config = Path(__file__).resolve().parents[1] / "config"
    topology = Topology.from_yaml(config / "cameras.yaml")
    route = ["wired-sr-2", "outdoor-2", "side-deck", "deck-stairs", "outdoor-winston"]
    zones = [topology.zone_for_camera(c) for c in route]
    assert all(zones)
    assert all(topology.are_neighbors(a, b) for a, b in zip(zones, zones[1:]))
    assert topology.zone_for_camera("winston-bowl") is None
    assert topology.zone_for_camera("winston-5000") == "lower-level-kitchen"
    settings = yaml.safe_load((config / "settings.yaml").read_text())
    assert len(settings["ring"]["device_ids"]) == 8
    assert zones[0] in settings["stats"]["ambiguous_zones"]
