#!/usr/bin/env python3
"""Assemble the animated README hero from the individual interface captures.

The README's front-door image is an animated WebP rather than a GIF: the
captures are dense with small text, and GIF's 256-colour palette turns that
text to mush at roughly seven times the file size. WebP animation is rendered
natively by browsers, and a viewer that will not animate it falls back to the
first frame -- which is why the inbox leads the sequence.

Run after replacing any capture in ``screenshots/webp/``::

    uv run python scripts/build_screenshot_tour.py

The stills stay the source of truth; this file only ever composes them.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

# (capture, milliseconds on screen). Denser views are held longer: the
# reservations table and the compose form carry more to read than a project
# card grid does.
SEQUENCE: tuple[tuple[str, int], ...] = (
    ("iris_inbox", 2600),
    ("iris_message_detail", 2600),
    ("iris_thread", 2400),
    ("iris_search", 2600),
    ("iris_reservations", 3200),
    ("iris_compose", 3000),
    ("iris_administration", 2600),
    ("iris_projects", 2200),
    ("iris_account", 2200),
)

# Lossy, but well above the point where the interface font degrades; measured
# at roughly 490 KiB for the sequence above versus 3.2 MB for the GIF this
# replaced.
QUALITY = 92
METHOD = 6

ROOT = Path(__file__).resolve().parent.parent
CAPTURES = ROOT / "screenshots" / "webp"
OUTPUT = CAPTURES / "iris_tour.webp"


def main() -> int:
    frames: list[Image.Image] = []
    durations: list[int] = []
    expected_size: tuple[int, int] | None = None

    for name, duration in SEQUENCE:
        source = CAPTURES / f"{name}.webp"
        if not source.is_file():
            print(f"missing capture: {source}", file=sys.stderr)
            return 1
        frame = Image.open(source).convert("RGB")
        # A frame of a different size would be silently letterboxed or
        # cropped by the encoder, so refuse rather than ship a jumping hero.
        if expected_size is None:
            expected_size = frame.size
        elif frame.size != expected_size:
            print(
                f"{source.name} is {frame.size}, expected {expected_size}; recapture at a consistent viewport",
                file=sys.stderr,
            )
            return 1
        frames.append(frame)
        durations.append(duration)

    frames[0].save(
        OUTPUT,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        quality=QUALITY,
        method=METHOD,
    )

    written = Image.open(OUTPUT)
    # n_frames is contributed by the WebP plugin at runtime, so it is reached
    # through getattr rather than as a declared attribute of ImageFile.
    written_frames = getattr(written, "n_frames", 1)
    if written_frames != len(frames):
        print(
            f"wrote {written_frames} frames, expected {len(frames)}",
            file=sys.stderr,
        )
        return 1

    print(
        f"{OUTPUT.relative_to(ROOT)}: {written_frames} frames, "
        f"{written.size[0]}x{written.size[1]}, "
        f"{OUTPUT.stat().st_size / 1024:.0f} KiB, "
        f"{sum(durations) / 1000:.1f}s loop"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
