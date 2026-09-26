from datetime import datetime, timezone
from types import SimpleNamespace
from src.capture import capture_event
from src.db import Database
from src.ring_client import MotionEvent


def test_capture_persists_identity_and_is_idempotent(tmp_path):
    db = Database(tmp_path / "test.db")
    calls = []
    class Ring:
        def download_video(self, event, dest):
            calls.append(event.device_id)
            dest.write_bytes(b"fixture clip")
            return dest
    frames = SimpleNamespace(extract=lambda _: [SimpleNamespace(index=0, image=SimpleNamespace(data=b"jpeg"))])
    e = MotionEvent("123", "same-name", "device-a", datetime.now(timezone.utc))
    first = capture_event(db, Ring(), e, frames, tmp_path / "captures")
    second = capture_event(db, Ring(), e, frames, tmp_path / "captures")
    assert first == second and calls == ["device-a"]
    assert first["status"] == "captured"
    e.device_id = "device-b"
    capture_event(db, Ring(), e, frames, tmp_path / "captures")
    assert calls == ["device-a", "device-b"]
    assert not db.list_observations()  # Capture is not a fabricated vision verdict.
    db.close()


def test_failed_capture_is_retryable_without_fake_sighting(tmp_path):
    db = Database(tmp_path / "test.db")
    class Broken:
        def download_video(self, *a, **k):
            raise RuntimeError("secret URL")
    e = MotionEvent("123", "camera", "device", datetime.now(timezone.utc))
    row = capture_event(db, Broken(), e, None, tmp_path / "captures")
    assert row["status"] == "failed" and row["error"] == "RuntimeError"
    assert not db.list_observations()
    db.close()
