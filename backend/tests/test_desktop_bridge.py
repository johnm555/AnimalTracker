"""Desktop onboarding is hermetic: no provider, user directory, or live API."""

import importlib.util
import io
import json
from pathlib import Path
import stat
import sys
from unittest.mock import Mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "desktop_bridge", ROOT / "scripts/desktop_bridge.py"
)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv("ANIMAL_TRACKER_DATA", str(tmp_path))
    monkeypatch.delenv("ANIMAL_TRACKER_SETTINGS", raising=False)
    monkeypatch.delenv("ANIMAL_TRACKER_CAMERAS", raising=False)
    return tmp_path


@pytest.fixture
def request_data():
    return {
        "action": "configure",
        "answers": {
            "animal": {"name": "Max"},
            "cameras": [
                {"device_id": "1", "name": "Door", "zone": "Kitchen"},
                {"device_id": "2", "name": "Yard", "zone": "Garden"},
            ],
            "neighbors": [
                {"a": "Kitchen", "b": "Garden", "min_seconds": 0, "max_seconds": 90}
            ],
            "notifications": {"backend": "log"},
        },
    }


def test_preview_then_private_save_and_backup(site, request_data):
    assert bridge.configure(dict(request_data, preview=True))["camera_count"] == 2
    assert not (site / "config/settings.yaml").exists()
    bridge.configure(request_data)
    env = (site / ".env").read_text()
    assert "ANIMAL_TRACKER_API_TOKEN=" in env
    assert stat.S_IMODE((site / ".env").stat().st_mode) == 0o600
    assert (
        yaml.safe_load((site / "config/settings.yaml").read_text())["api"]["host"]
        == "127.0.0.1"
    )
    with pytest.raises(ValueError, match="Existing configuration"):
        bridge.configure(request_data)
    bridge.configure(dict(request_data, replace_existing=True, allow_lan=True))
    assert (site / ".env").read_text() == env
    assert list((site / "config").glob("settings.yaml.bak-*"))
    assert (
        yaml.safe_load((site / "config/settings.yaml").read_text())["api"]["host"]
        == "0.0.0.0"
    )


def test_invalid_setup_does_not_write(site, request_data):
    request_data["answers"]["cameras"][1]["device_id"] = "1"
    with pytest.raises(ValueError):
        bridge.configure(request_data)
    assert not list(site.glob("config/*"))


def test_rollback_after_write_failure(site, request_data, monkeypatch):
    bridge.configure(request_data)
    before = (site / "config/settings.yaml").read_bytes()
    original = bridge.private_write
    failed = False

    def fail_once(path, text):
        nonlocal failed
        if path == site / "config/settings.yaml" and not failed:
            failed = True
            raise OSError("simulated full disk")
        original(path, text)

    monkeypatch.setattr(bridge, "private_write", fail_once)
    with pytest.raises(OSError):
        bridge.configure(dict(request_data, replace_existing=True, allow_lan=True))
    assert (site / "config/settings.yaml").read_bytes() == before


def test_photo_import_deduplicates_and_uses_configured_path(
    site, request_data, tmp_path
):
    from PIL import Image

    bridge.configure(request_data)
    sp = site / "config/settings.yaml"
    settings = yaml.safe_load(sp.read_text())
    settings["detector"]["reference_images_dir"] = "custom-gallery"
    sp.write_text(yaml.safe_dump(settings))
    photo = tmp_path / "source.png"
    Image.new("RGB", (50, 50), "black").save(photo)
    assert (
        "1 new" in bridge.import_photos({"files": [str(photo), str(photo)]})["message"]
    )
    saved = list((site / "custom-gallery").glob("*.jpg"))
    assert len(saved) == 1
    assert stat.S_IMODE(saved[0].stat().st_mode) == 0o600
    assert not Image.open(saved[0]).getexif()


def test_protocol_redacts_provider_exceptions(monkeypatch):
    wire = io.StringIO()
    monkeypatch.setattr(bridge, "WIRE", wire)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO('{"action":"ring_login","password":"secret-sentinel"}\n'),
    )

    def fail(_):
        print("secret-sentinel")
        raise RuntimeError("secret-sentinel")

    monkeypatch.setattr(bridge, "handle", fail)
    assert bridge.main() == 1
    assert "secret-sentinel" not in wire.getvalue()
    assert json.loads(wire.getvalue())["event"] == "error"


def test_malformed_json_is_protocol_error(monkeypatch):
    wire = io.StringIO()
    monkeypatch.setattr(bridge, "WIRE", wire)
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json\n"))
    assert bridge.main() == 1
    assert json.loads(wire.getvalue())["event"] == "error"


def test_ring_otp_handshake(site, monkeypatch):
    from src import ring_client

    monkeypatch.setattr(sys, "stdin", io.StringIO('{"code":"123456"}\n'))
    wire = io.StringIO()
    monkeypatch.setattr(bridge, "WIRE", wire)
    client = Mock()
    client.authenticate.side_effect = lambda otp_callback: otp_callback()
    client.get_cameras.return_value = [{"device_id": "1", "name": "Door"}]
    factory = Mock(return_value=client)
    monkeypatch.setattr(ring_client, "RingClient", factory)
    result = bridge.handle(
        {"action": "ring_login", "email": "test@example.invalid", "password": "secret"}
    )
    assert result["cameras"][0]["device_id"] == "1"
    assert json.loads(wire.getvalue())["event"] == "otp_required"
    assert not (site / ".env").exists()


def test_findmy_requires_consent(site):
    with pytest.raises(ValueError, match="notice"):
        bridge.handle({"action": "findmy_login", "consent": False})


def test_messages_recipient_and_text_are_arguments(monkeypatch):
    import subprocess
    from src.notification import IMessageSender

    run = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(subprocess, "run", run)
    recipient = 'a" & do shell script "evil'
    text = 'A "quoted" setup test'
    IMessageSender(recipient)._send_text(text)
    args = run.call_args.args[0]
    assert recipient not in args[2]
    assert text not in args[2]
    assert args[-2:] == [recipient, text]


def test_no_message_without_confirmation(site, monkeypatch):
    from src.notification import IMessageSender

    send = Mock()
    monkeypatch.setattr(IMessageSender, "_send_text", send)
    with pytest.raises(ValueError):
        bridge.handle({"action": "imessage_test", "recipient": "test@example.invalid"})
    send.assert_not_called()


def test_saved_profile_name_reaches_alerts_and_journey_summary(
    site, request_data, monkeypatch
):
    from datetime import datetime, timezone
    from src.api import AppContext
    from src.notification import NotificationDecision, NORMAL
    from src.state_machine import TransitionEvent

    bridge.configure(request_data)
    settings = yaml.safe_load((site / "config/settings.yaml").read_text())
    settings["notifications"] = {
        "backend": "imessage",
        "imessage": {"recipient": "test@example.invalid"},
    }
    context = AppContext.build(settings=settings)
    event = TransitionEvent(
        id=1,
        from_zone="kitchen",
        to_zone="garden",
        arrived_at=datetime.now(timezone.utc),
        confidence=0.95,
    )
    try:
        decision = context.notifier.policy.decide(event)
        assert "Max" in decision.title
        assert "Winston" not in decision.title
        sender = context.notifier.sender
        texts = []
        monkeypatch.setattr(sender, "_send_text", texts.append)
        sender._journey = [(event, NotificationDecision(NORMAL, "", ""))]
        sender._flush_journey()
        assert texts and "Max settled" in texts[0]
        assert "Winston" not in texts[0]
    finally:
        context.db.close()


def test_release_refuses_development_certificate_before_build(tmp_path):
    import os
    import subprocess
    env = dict(os.environ, ANIMAL_TRACKER_SIGNING_IDENTITY="Apple Development: Example", ANIMAL_TRACKER_NOTARY_PROFILE="example")
    destination = tmp_path / "release"
    result = subprocess.run(["bash", str(ROOT / "scripts/release-mac-app.sh"), str(destination)], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "Developer ID Application" in result.stderr
    assert not destination.exists()
