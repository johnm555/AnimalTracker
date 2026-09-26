from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path
from contextlib import contextmanager
import pytest
import httpx
from src.ring_client import RingClient, MotionEvent


def event():
    return MotionEvent("123", "living-room-cam", "42", datetime.now(timezone.utc))


def test_playback_fallback_after_direct_failure(tmp_path, monkeypatch):
    def direct(*args, **kwargs):
        Path(kwargs["filename"]).write_bytes(b"partial")
        raise RuntimeError("404")
    client = RingClient()
    client._ring = object()
    client._devices_by_id = {"42": SimpleNamespace(recording_download=direct,
        recording_url=lambda _: "https://example.com/private-clip")}
    @contextmanager
    def stream(*args, **kwargs):
        yield httpx.Response(200, content=b"complete clip", request=httpx.Request("GET", args[1]))
    monkeypatch.setattr(httpx, "stream", stream)
    dest = tmp_path / "clip.mp4"
    assert client.download_video(event(), dest, retries=1).read_bytes() == b"complete clip"
    assert not dest.with_suffix(".mp4.part").exists()


def test_failed_download_not_cached_or_signed_url_exposed(tmp_path, monkeypatch):
    def direct(*args, **kwargs):
        Path(kwargs["filename"]).write_bytes(b"partial")
        raise RuntimeError("failure")
    client = RingClient()
    client._ring = object()
    client._devices_by_id = {"42": SimpleNamespace(recording_download=direct,
        recording_url=lambda _: "https://example.com/?secret=token")}
    @contextmanager
    def stream(*args, **kwargs):
        raise RuntimeError("https://example.com/?secret=token")
        yield
    monkeypatch.setattr(httpx, "stream", stream)
    dest = tmp_path / "clip.mp4"
    with pytest.raises(RuntimeError) as error:
        client.download_video(event(), dest, retries=1)
    assert "secret" not in str(error.value)
    assert not dest.exists()
    assert not dest.with_suffix(".mp4.part").exists()


@pytest.mark.parametrize("failure", [IndexError("empty timestamps"), KeyError("timestamps"),
                                      RuntimeError("https://private/?token=secret")])
def test_snapshot_failure_is_unavailable_without_file_or_sensitive_log(tmp_path, caplog, failure):
    def snapshot():
        raise failure
    client = RingClient()
    client._devices_by_id = {"42": SimpleNamespace(get_snapshot=snapshot)}
    dest = tmp_path / "snapshot.jpg"
    with caplog.at_level("DEBUG", logger="src.ring_client"):
        assert client.download_snapshot(event(), dest) is None
    assert not dest.exists()
    assert "token=secret" not in caplog.text


@pytest.mark.parametrize("data", [None, b""])
def test_empty_snapshot_is_unavailable(tmp_path, data):
    client = RingClient()
    client._devices_by_id = {"42": SimpleNamespace(get_snapshot=lambda: data)}
    dest = tmp_path / "snapshot.jpg"
    assert client.download_snapshot(event(), dest) is None
    assert not dest.exists()


def test_snapshot_does_not_rewrite_trigger_event_time(tmp_path):
    client = RingClient()
    client._devices_by_id = {"42": SimpleNamespace(get_snapshot=lambda: b"image")}
    trigger = event()
    timestamp = trigger.timestamp
    assert client.download_snapshot(trigger, tmp_path / "snapshot.jpg").read_bytes() == b"image"
    assert trigger.timestamp == timestamp
