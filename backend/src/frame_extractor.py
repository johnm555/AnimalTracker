"""Extract still frames from Ring motion-event video clips.

Ring events give us a short MP4 (and sometimes a snapshot JPEG). We pull a
handful of evenly spaced frames, downscale them so the vision call stays cheap,
and hand them to the detector as JPEG bytes.

Uses OpenCV when available and falls back to the `ffmpeg` binary.
"""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .winston_detector import ImageData

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Frame:
    image: ImageData
    index: int
    offset_seconds: float


@dataclass
class FrameExtractorConfig:
    frames_per_event: int = 4
    max_dimension_px: int = 1024
    jpeg_quality: int = 85
    # Skip the first/last few percent of the clip: Ring clips often start
    # before the subject enters and end after it leaves.
    trim_fraction: float = 0.05


class FrameExtractor:
    def __init__(self, config: FrameExtractorConfig | None = None) -> None:
        self.config = config or FrameExtractorConfig()

    # -- public ------------------------------------------------------------

    def extract(self, video_path: str | Path) -> list[Frame]:
        path = Path(video_path)
        if not path.exists():
            raise FileNotFoundError(path)
        try:
            return self._extract_cv2(path)
        except ImportError:
            log.debug("cv2 not available, falling back to ffmpeg")
        return self._extract_ffmpeg(path)

    def from_snapshot(self, image_path: str | Path) -> list[Frame]:
        """Wrap a single snapshot JPEG (e.g. Ring's event thumbnail) as one frame."""
        data = Path(image_path).read_bytes()
        return [Frame(image=self._resize_jpeg(data), index=0, offset_seconds=0.0)]

    @staticmethod
    def images(frames: Sequence[Frame]) -> list[ImageData]:
        return [f.image for f in frames]

    # -- backends ----------------------------------------------------------

    def _extract_cv2(self, path: Path) -> list[Frame]:
        import cv2  # type: ignore

        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            raise RuntimeError(f"could not open video {path}")
        try:
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0) or 15.0
            if total <= 0:
                # Some Ring clips report 0 frames; count manually.
                total = 0
                while cap.grab():
                    total += 1
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            if total == 0:
                raise RuntimeError(f"video has no frames: {path}")

            frames: list[Frame] = []
            for i, idx in enumerate(self._sample_indices(total)):
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ok, img = cap.read()
                if not ok:
                    continue
                img = self._resize_cv2(img)
                ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), self.config.jpeg_quality])
                if not ok:
                    continue
                frames.append(Frame(
                    image=ImageData(data=buf.tobytes(), media_type="image/jpeg", label=f"frame-{i}"),
                    index=i,
                    offset_seconds=idx / fps,
                ))
            return frames
        finally:
            cap.release()

    def _extract_ffmpeg(self, path: Path) -> list[Frame]:
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if not ffmpeg:
            raise RuntimeError("neither opencv-python nor ffmpeg is available for frame extraction")

        duration = 0.0
        if ffprobe:
            out = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, check=False,
            ).stdout.strip()
            try:
                duration = float(out)
            except ValueError:
                duration = 0.0
        if duration <= 0:
            duration = 20.0  # typical Ring clip length

        n = self.config.frames_per_event
        trim = duration * self.config.trim_fraction
        usable = max(duration - 2 * trim, 0.1)
        offsets = [trim + usable * (i + 0.5) / n for i in range(n)]

        frames: list[Frame] = []
        scale = f"scale='min({self.config.max_dimension_px},iw)':-2"
        with tempfile.TemporaryDirectory() as tmp:
            for i, off in enumerate(offsets):
                out_path = Path(tmp) / f"frame-{i}.jpg"
                subprocess.run(
                    [ffmpeg, "-v", "error", "-ss", f"{off:.3f}", "-i", str(path),
                     "-frames:v", "1", "-vf", scale, "-q:v", "3", str(out_path)],
                    check=False, capture_output=True,
                )
                if out_path.exists():
                    frames.append(Frame(
                        image=ImageData(data=out_path.read_bytes(), media_type="image/jpeg", label=f"frame-{i}"),
                        index=i, offset_seconds=off,
                    ))
        return frames

    # -- helpers -----------------------------------------------------------

    def _sample_indices(self, total: int) -> list[int]:
        n = self.config.frames_per_event
        trim = int(total * self.config.trim_fraction)
        lo, hi = trim, max(total - trim - 1, trim)
        span = max(hi - lo, 0)
        if n == 1:
            return [lo + span // 2]
        return sorted({lo + int(span * (i + 0.5) / n) for i in range(n)})

    def _resize_cv2(self, img):  # type: ignore[no-untyped-def]
        import cv2  # type: ignore

        h, w = img.shape[:2]
        m = max(h, w)
        if m <= self.config.max_dimension_px:
            return img
        s = self.config.max_dimension_px / m
        return cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)

    def _resize_jpeg(self, data: bytes) -> ImageData:
        try:
            from PIL import Image  # type: ignore
        except ImportError:
            return ImageData(data=data, media_type="image/jpeg", label="snapshot")
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            im.thumbnail((self.config.max_dimension_px, self.config.max_dimension_px))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=self.config.jpeg_quality)
        return ImageData(data=buf.getvalue(), media_type="image/jpeg", label="snapshot")
