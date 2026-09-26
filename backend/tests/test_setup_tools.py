"""Setup wizard (answers -> configs) and doctor, against a temp data dir."""

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from src.state_machine import Topology

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wizard = _load("setup_wizard")
doctor = _load("doctor")

ANSWERS = {
    "animal": {"name": "Max", "description": "golden retriever"},
    "cameras": [
        {"device_id": "1", "name": "Back Door", "zone": "Kitchen"},
        {"device_id": "2", "name": "Kitchen Cam", "zone": "kitchen"},
        {"device_id": "3", "name": "Yard", "zone": "Back Yard"},
    ],
    "neighbors": [{"a": "kitchen", "b": "back yard", "max_seconds": 45}],
    "high_priority_zones": ["back-yard"],
    "inside_zones": ["kitchen"],
    "notifications": {"backend": "imessage", "recipient": "+15551234567",
                      "quiet_hours": {"start": "22:00", "end": "07:00"}},
}


def test_cameras_share_zones_and_topology_is_valid():
    cams = wizard.build_cameras(ANSWERS)
    topo = Topology.from_dict(cams)
    assert set(topo.zones) == {"kitchen", "back-yard"}
    assert topo.zone_for_camera("back-door") == topo.zone_for_camera("kitchen-cam") == "kitchen"
    w = topo.zones["back-yard"].neighbors["kitchen"]  # made symmetric
    assert (w.min_seconds, w.max_seconds) == (0.0, 45.0)


def test_unknown_zone_references_are_rejected():
    bad = {**ANSWERS, "high_priority_zones": ["garage"]}
    with pytest.raises(ValueError, match="garage"):
        wizard.build_settings(bad, {})
    with pytest.raises(ValueError, match="need at least one camera"):
        wizard.build_cameras({**ANSWERS, "neighbors": [{"a": "kitchen", "b": "garage"}]})


def test_settings_carry_device_ids_zones_and_notifications():
    s = wizard.build_settings(ANSWERS, yaml.safe_load(wizard.EXAMPLE_SETTINGS.read_text()))
    assert s["ring"]["device_ids"] == ["1", "2", "3"]
    assert s["notifications"]["high_priority_zones"] == ["back-yard"]
    assert s["notifications"]["imessage"]["recipient"] == "+15551234567"
    assert s["stats"]["inside_zones"] == ["kitchen"]
    assert s["animal"]["name"] == "Max"
    assert s["detector"]["pipeline"]["accept_threshold"]  # example defaults preserved


def test_answers_file_writes_configs_then_doctor_reads_them(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    monkeypatch.setenv("ANIMAL_TRACKER_DATA", str(data))
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps(ANSWERS))

    assert wizard.main(["--answers", str(answers)]) == 0
    assert (data / "config" / "cameras.yaml").is_file()
    assert "ANIMAL_TRACKER_API_TOKEN=" in (data / ".env").read_text()
    assert wizard.main(["--answers", str(answers)]) == 1  # refuses to overwrite
    assert wizard.main(["--answers", str(answers), "--force"]) == 0
    assert list((data / "config").glob("settings.yaml.bak-*"))

    capsys.readouterr()
    rc = doctor.main(["--json", "--skip-service"])
    report = json.loads(capsys.readouterr().out)
    checks = {c["check"]: c["status"] for c in report["checks"]}
    assert checks["cameras.yaml"] == "PASS" and checks["settings.yaml"] == "PASS"
    assert checks["reference photos"] == "FAIL" and checks["ring token"] == "FAIL"
    assert rc == 1


def test_doctor_flags_settings_that_name_missing_zones(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    monkeypatch.setenv("ANIMAL_TRACKER_DATA", str(data))
    (data / "config").mkdir(parents=True)
    (data / "config" / "cameras.yaml").write_text(yaml.safe_dump(wizard.build_cameras(ANSWERS)))
    (data / "config" / "settings.yaml").write_text(yaml.safe_dump(
        {"notifications": {"backend": "log", "high_priority_zones": ["front-door"]}}))
    doctor.main(["--json", "--skip-service"])
    rows = json.loads(capsys.readouterr().out)["checks"]
    bad = [r for r in rows if r["check"] == "notifications.high_priority_zones"]
    assert bad and bad[0]["status"] == "FAIL" and "front-door" in bad[0]["detail"]


def test_train_export_uses_reviewer_labels_and_keeps_splits_apart(tmp_path):
    train = _load("train")
    ev = tmp_path / "evalset"
    rows = [
        {"device_id": "d", "event_id": "1", "staging_key": "k1", "label": "winston", "split": "dev"},
        {"device_id": "d", "event_id": "2", "staging_key": "k2", "label": "no_animal", "split": "test"},
        {"device_id": "d", "event_id": "3", "staging_key": "k3", "label": "uncertain", "split": "dev"},
        {"device_id": "d", "event_id": "4", "staging_key": "k4", "label": "winston", "split": "excluded"},
    ]
    for r in rows:
        (ev / "frames" / r["staging_key"]).mkdir(parents=True)
        (ev / "frames" / r["staging_key"] / "f0.jpg").write_bytes(b"jpg")
        r["frames"] = [{"name": "f0.jpg", "sha256": "x"}]
    (ev / "manifest.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    counts = train.export(ev, tmp_path / "out")
    assert counts == {"Testing/no_animal": 1, "Training/target": 1}
    assert (tmp_path / "out" / "Training" / "target" / "k1__f0.jpg").is_file()


def test_example_configs_describe_one_consistent_property():
    """The shipped templates are the docs' example property; they must validate together."""
    cfg = Path(__file__).resolve().parents[1] / "config"
    topo = Topology.from_yaml(cfg / "cameras.example.yaml")
    settings = yaml.safe_load((cfg / "settings.example.yaml").read_text())
    zones = set(topo.zones)
    for listed in (settings["notifications"]["high_priority_zones"],
                   settings["stats"]["inside_zones"], settings["stats"]["ambiguous_zones"]):
        assert set(listed) <= zones
    assert all(w.min_seconds == 0 for z in topo.zones.values() for w in z.neighbors.values())
