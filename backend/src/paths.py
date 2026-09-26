"""Central path resolution for Animal Tracker.

Site-specific data (configs, database, reference images, Ring downloads,
secrets) lives outside the source tree so the framework can be committed
to a public repo.  The layout:

    DATA_DIR/                         # ~/Library/Application Support/AnimalTracker
        config/
            settings.yaml             # thresholds, notification backend, Ring device-ids
            cameras.yaml              # property topology: zones, cameras, travel windows
        reference_images/             # enrolled animal photos for the vision layer
        secrets/                      # FindMy keys, APNs certs
        staging/                      # pending / archive / thumbnails
        ring_downloads/               # clips, frames, captures
        ring_token.cache
        tracker.db
        .env

Resolution order for DATA_DIR:
  1. ANIMAL_TRACKER_DATA env var (absolute path)
  2. ~/Library/Application Support/AnimalTracker  (macOS)
  3. ~/.local/share/AnimalTracker                 (Linux / fallback)
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

# The source tree — only for locating example configs and test fixtures.
SRC_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SRC_DIR.parent


def env(name: str, default: str | None = None) -> str | None:
    """`ANIMAL_TRACKER_<name>`, falling back to the deprecated `WINSTON_<name>`."""
    v = os.environ.get(f"ANIMAL_TRACKER_{name}")
    return v if v is not None else os.environ.get(f"WINSTON_{name}", default)


def data_dir() -> Path:
    """Return the site data directory, creating it if needed."""
    explicit = os.environ.get("ANIMAL_TRACKER_DATA")
    if explicit:
        p = Path(explicit)
    elif platform.system() == "Darwin":
        p = Path.home() / "Library" / "Application Support" / "AnimalTracker"
    else:
        p = Path.home() / ".local" / "share" / "AnimalTracker"
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_dir() -> Path:
    return data_dir() / "config"


def settings_path() -> Path:
    return Path(os.environ.get("ANIMAL_TRACKER_SETTINGS",
                               config_dir() / "settings.yaml"))


def cameras_path() -> Path:
    return Path(os.environ.get("ANIMAL_TRACKER_CAMERAS",
                               config_dir() / "cameras.yaml"))


def db_path(settings: dict | None = None) -> str:
    if settings and settings.get("database", {}).get("path"):
        p = Path(settings["database"]["path"])
        return str(p if p.is_absolute() else data_dir() / p)
    return str(data_dir() / "tracker.db")


def reference_images_dir(settings: dict | None = None) -> Path:
    if settings:
        rel = (settings.get("detector") or {}).get("reference_images_dir")
        if rel:
            p = Path(rel)
            return p if p.is_absolute() else data_dir() / p
    return data_dir() / "reference_images"


def secrets_dir() -> Path:
    d = data_dir() / "secrets"
    d.mkdir(exist_ok=True)
    return d


def staging_dir(settings: dict | None = None) -> Path:
    if settings:
        rel = (settings.get("detector") or {}).get("staging_dir")
        if rel:
            p = Path(rel)
            return p if p.is_absolute() else data_dir() / p
    return data_dir() / "staging"


def ring_downloads_dir(settings: dict | None = None) -> Path:
    if settings:
        rel = (settings.get("ring") or {}).get("download_dir")
        if rel:
            p = Path(rel)
            return p if p.is_absolute() else data_dir() / p
    return data_dir() / "ring_downloads"


def ring_token_path(settings: dict | None = None) -> Path:
    if settings:
        rel = settings.get("ring", {}).get("token_cache")
        if rel:
            p = Path(rel)
            return p if p.is_absolute() else data_dir() / p
    return data_dir() / "ring_token.cache"


def env_path() -> Path:
    return data_dir() / ".env"


def resolve(p: str | Path) -> Path:
    """Resolve a relative path against DATA_DIR (not the source tree)."""
    path = Path(p)
    return path if path.is_absolute() else data_dir() / path
