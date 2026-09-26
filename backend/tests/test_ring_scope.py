from types import SimpleNamespace
from src.ring_client import RingClient, RingSettings


def test_device_allowlist_filters_before_polling():
    selected = SimpleNamespace(id=1, name="Selected")
    other = SimpleNamespace(id=2, name="Other property")
    for ids, expected in [([1], {"1"}), ([], set())]:
        client = RingClient(RingSettings.from_dict({"device_ids": ids}))
        client._ring = SimpleNamespace(devices=lambda: SimpleNamespace(video_devices=[selected, other]))
        client._index_devices()
        assert set(client._devices_by_id) == expected
