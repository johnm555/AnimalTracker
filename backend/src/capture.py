"""Bounded Ring event capture and durable evidence, independent of vision."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from .api import load_settings
from .db import Database
from .frame_extractor import FrameExtractor
from .paths import db_path as resolve_db_path, ring_downloads_dir
from .ring_client import MotionEvent, RingClient
from .state_machine import normalize_camera_id


def capture_event(db: Database, ring: Any, event: MotionEvent,
                  extractor: FrameExtractor, root: Path) -> dict[str, Any]:
    """Persist event identity first; failures remain retryable, never negative sightings."""
    key = hashlib.sha256(f"{event.device_id}:{event.event_id}".encode()).hexdigest()
    folder = root / key
    existing = db.get_capture(event.device_id, event.event_id)
    if existing and existing['status'] == 'captured':
        paths = json.loads(existing['frame_paths'])
        if paths and all(Path(p).is_file() for p in [existing['clip_path'], *paths]):
            return existing
    db.save_capture(event, 'pending')
    try:
        folder.mkdir(parents=True, exist_ok=True)
        clip = ring.download_video(event, dest=folder / 'event.mp4')
        frames = extractor.extract(clip)
        if not frames:
            raise RuntimeError('No frames decoded')
        paths = []
        for frame in frames:
            path = folder / f'frame-{frame.index}.jpg'
            path.write_bytes(frame.image.data)
            paths.append(str(path.resolve()))
        db.save_capture(event, 'captured', str(clip.resolve()), paths)
    except Exception as error:
        # Signed recording URLs and credentials must never enter error records.
        db.save_capture(event, 'failed', error=type(error).__name__)
    return db.get_capture(event.device_id, event.event_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('camera', help='Camera name, normalized name, or numeric Ring device ID')
    args = parser.parse_args(argv)
    settings = load_settings()
    ring = RingClient.from_settings(settings.get('ring'))
    ring.authenticate()
    matches = [c for c in ring.get_cameras() if c['device_id'] == args.camera
               or c['camera_id'] == normalize_camera_id(args.camera)]
    if len(matches) != 1:
        print(json.dumps({'error': 'Camera not enabled or name ambiguous; use a device ID',
                          'matches': matches}, indent=2))
        return 2
    camera = matches[0]
    # Restrict the actual history query to the selected device.
    ring._devices_by_id = {camera['device_id']: ring._devices_by_id[camera['device_id']]}
    events = ring.poll_events(limit=20)
    ready = [e for e in events if e.recording_status == 'ready']
    if not ready:
        print(json.dumps({'camera': camera, 'status': 'no_ready_event'}))
        return 1
    event = max(ready, key=lambda e: e.timestamp)
    db = Database(resolve_db_path(settings))
    try:
        result = capture_event(db, ring, event, FrameExtractor(), ring_downloads_dir(settings) / 'captures')
        print(json.dumps({'camera': camera, 'capture': result,
                          'vision_status': 'not_analyzed',
                          'location_claim': None}, indent=2))
        return 0 if result['status'] == 'captured' else 1
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
