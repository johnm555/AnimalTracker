#!/usr/bin/env python3
"""Private JSON-lines IPC for the native Mac app. Secrets arrive on stdin only.

One command per process. A provider may emit otp_required and read a second
JSON line. stdout is reserved for protocol events; provider logs are suppressed.
No command includes credentials in argv. The app never exposes this as an HTTP API.
"""

from __future__ import annotations
import contextlib
import io
import json
import logging
import os
from pathlib import Path
import secrets
import sys
import tempfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))
WIRE = sys.stdout


def emit(event, **values):
    WIRE.write(json.dumps(dict(event=event, **values)) + "\n")
    WIRE.flush()


def otp(_="Verification code"):
    emit(
        "otp_required",
        message="Enter the verification code from your trusted device or message.",
    )
    value = json.loads(sys.stdin.readline()).get("code", "").strip()
    if not value or len(value) > 12 or not value.isdigit():
        raise ValueError("Enter a valid numeric verification code.")
    return value


def private_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(text)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def configure(request):
    import yaml
    from src import paths
    from setup_wizard import build_cameras, build_settings, EXAMPLE_SETTINGS

    answers = request["answers"]
    selected = answers.get("cameras", [])
    from setup_wizard import zone_id

    if any(not zone_id(c.get("zone", "")) for c in selected):
        raise ValueError("Zone names need at least one letter or number.")
    if not selected or len({str(c["device_id"]) for c in selected}) != len(selected):
        raise ValueError(
            "Choose at least one camera; each camera may be selected once."
        )
    if any(
        not str(c["device_id"]).strip()
        or not c["name"].strip()
        or not c["zone"].strip()
        for c in selected
    ):
        raise ValueError("Each selected camera needs a name, device ID, and zone.")
    if not answers.get("animal", {}).get("name", "").strip():
        raise ValueError("Enter your animal’s name.")
    if (
        answers.get("notifications", {}).get("backend") == "imessage"
        and not answers["notifications"].get("recipient", "").strip()
    ):
        raise ValueError("Enter an iMessage recipient, or choose local records only.")
    sp, cp = paths.settings_path(), paths.cameras_path()
    base = yaml.safe_load((sp if sp.exists() else EXAMPLE_SETTINGS).read_text())
    cameras = build_cameras(answers)
    settings = build_settings(answers, base)
    settings.setdefault("api", {})["host"] = (
        "0.0.0.0" if request.get("allow_lan") else "127.0.0.1"
    )
    if request.get("preview", False):
        return {
            "zones": list(cameras["zones"]),
            "camera_count": len(selected),
            "existing": sp.exists() or cp.exists(),
        }
    if (sp.exists() or cp.exists()) and not request.get("replace_existing"):
        raise ValueError(
            "Existing configuration found. Review and explicitly allow a backed-up replacement."
        )
    # Validate all content before any writes. Keep private rollback copies.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    originals = {
        p: p.read_text() if p.exists() else None for p in (sp, cp, paths.env_path())
    }
    for p, text in originals.items():
        if text is not None:
            private_write(p.with_name(p.name + ".bak-" + stamp), text)
    from dotenv import dotenv_values

    env = dict(dotenv_values(paths.env_path())) if paths.env_path().exists() else {}
    if not (env.get("ANIMAL_TRACKER_API_TOKEN") or env.get("WINSTON_API_TOKEN")):
        env["ANIMAL_TRACKER_API_TOKEN"] = secrets.token_hex(32)

    def quote(value):
        return "'" + str(value or "").replace("\\", "\\\\").replace("'", "\\'") + "'"

    try:
        private_write(cp, yaml.safe_dump(cameras, sort_keys=False))
        private_write(sp, yaml.safe_dump(settings, sort_keys=False))
        private_write(
            paths.env_path(),
            "\n".join(k + "=" + quote(v) for k, v in env.items()) + "\n",
        )
    except Exception:
        for p, text in originals.items():
            if text is None:
                p.unlink(missing_ok=True)
            else:
                private_write(p, text)
        raise
    return {
        "message": "Configuration saved. Existing files were backed up. Restart tracking to apply changes."
    }


def status():
    import yaml
    from src import paths

    sp = paths.settings_path()
    settings = yaml.safe_load(sp.read_text()) if sp.exists() else {}
    settings = settings or {}
    refs = paths.reference_images_dir(settings)
    result = {
        "configured": sp.exists() and paths.cameras_path().exists(),
        "animal": settings.get("animal", {}).get("name", ""),
        "data_directory": str(paths.data_dir()),
        "ring_saved": paths.ring_token_path(settings).is_file(),
        "findmy_saved": (paths.secrets_dir() / "findmy_account.json").is_file(),
        "reference_count": sum(
            p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            for p in refs.glob("*")
        ),
        "port": int(settings.get("api", {}).get("port", 8420)),
        "service_running": False,
    }
    import httpx

    try:
        base = f"http://127.0.0.1:{result['port']}"
        response = httpx.get(base + "/healthz", timeout=2, trust_env=False)
        response.raise_for_status()
        health = response.json()
        if "pipeline" in health and "detector" in health:
            result["service_running"] = True
            result["health"] = health.get("status", "unknown")
            result["pipeline_state"] = health.get("pipeline", {}).get(
                "state", "unknown"
            )
            r = httpx.get(base + "/tracker/location", timeout=2, trust_env=False)
            if r.is_success:
                result["location"] = r.json()
    except (httpx.HTTPError, ValueError):
        pass
    return result


def import_photos(request):
    from src import paths
    from PIL import Image, ImageOps
    import hashlib
    import yaml

    settings = (
        yaml.safe_load(paths.settings_path().read_text())
        if paths.settings_path().exists()
        else {}
    )
    destination = paths.reference_images_dir(settings)
    destination.mkdir(parents=True, exist_ok=True)
    count = 0
    for name in request.get("files", []):
        source = Path(name)
        if not source.is_file():
            raise ValueError("A selected photo is no longer available.")
        # Decode first; re-encode strips location and camera EXIF metadata.
        with Image.open(source) as image:
            photo = ImageOps.exif_transpose(image).convert("RGB")
            photo.thumbnail((1600, 1600))
            output = io.BytesIO()
            photo.save(output, "JPEG", quality=90)
        data = output.getvalue()
        target = destination / (
            "photo_" + hashlib.sha256(data).hexdigest()[:20] + ".jpg"
        )
        if not target.exists():
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            count += 1
    return {"message": f"Imported {count} new reference photos."}


def handle(request):
    from src import paths
    from dotenv import load_dotenv

    load_dotenv(paths.env_path(), override=False)
    action = request.get("action")
    if action == "status":
        return status()
    if action == "configure":
        return configure(request)
    if action == "import_photos":
        return import_photos(request)
    if action == "doctor":
        import doctor

        report = doctor.Report()
        doctor.check_data_dir(report)
        settings, _ = doctor.check_configs(report)
        doctor.check_references(report, settings)
        doctor.check_ring(report, settings)
        doctor.check_db(report, settings)
        doctor.check_notifications(report, settings)
        return {"checks": report.rows, "ok": not report.failed}
    if action == "ring_login":
        from src.ring_client import RingClient, RingSettings

        client = RingClient(
            RingSettings.from_dict({}),
            username=request.get("email"),
            password=request.get("password"),
        )
        client.authenticate(otp_callback=otp)
        return {
            "cameras": client.get_cameras(),
            "message": "Ring connected. Choose the cameras for this property.",
        }
    if action == "findmy_login":
        from urllib.parse import urlparse

        if not request.get("consent"):
            raise ValueError(
                "Review the experimental Find My and anisette service notice first."
            )
        server = request.get("anisette_url", "")
        if urlparse(server).scheme != "https" or not urlparse(server).hostname:
            raise ValueError("Use an HTTPS anisette service URL.")
        from findmy.reports import AppleAccount, LoginState, RemoteAnisetteProvider

        account = AppleAccount(RemoteAnisetteProvider(server))
        state = account.login(request["email"], request["password"])
        if state == LoginState.REQUIRE_2FA:
            account.td_2fa_request()
            state = account.td_2fa_submit(otp())
        if state not in (LoginState.AUTHENTICATED, LoginState.LOGGED_IN):
            raise ValueError("Apple account verification did not complete. Try again.")
        private_write(
            paths.secrets_dir() / "findmy_account.json", json.dumps(account.to_json())
        )
        return {
            "message": "Find My account session saved. AirTag keys and the polling integration are still required; this does not enable tracking."
        }
    if action == "imessage_test":
        from src.notification import IMessageSender

        recipient = request.get("recipient", "").strip()
        if not recipient or not request.get("confirmed"):
            raise ValueError("Choose a recipient and confirm the test message.")
        IMessageSender(recipient)._send_text(
            "Animal Tracker setup test — Messages is connected. This is not an animal sighting."
        )
        return {
            "message": "Messages accepted the test. Confirm it arrived on your phone or watch; delivery is not verified here."
        }
    if action == "watch_token":
        token = paths.env("API_TOKEN")
        if not token:
            raise ValueError("Save setup first to generate an API token.")
        return {"token": token}
    if action == "serve":
        import yaml

        settings = yaml.safe_load(paths.settings_path().read_text())
        if not paths.env("API_TOKEN"):
            raise ValueError("Configure an API token before starting tracking.")
        import uvicorn

        uvicorn.run(
            "src.api:app",
            host=settings.get("api", {}).get("host", "127.0.0.1"),
            port=int(settings.get("api", {}).get("port", 8420)),
            log_level="warning",
        )
        return {}
    raise ValueError("Unknown desktop operation.")


def main():
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    request = {}
    try:
        request = json.loads(sys.stdin.readline())
        with (
            open(os.devnull, "w") as sink,
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
        ):
            result = handle(request)
        emit("result", data=result)
    except (ValueError, KeyError) as e:
        # Validation text only: never surface a provider exception containing credentials.
        safe = (
            str(e)
            if type(e) is ValueError
            and request.get("action") in {"configure", "watch_token"}
            else "The operation could not be completed. Check the fields and try again."
        )
        emit("error", message=safe)
        return 1
    except Exception:
        emit(
            "error",
            message="Connection failed. Check your credentials, network, and permissions, then try again. No password was saved.",
        )
        return 1
    finally:
        logging.disable(previous_logging)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
