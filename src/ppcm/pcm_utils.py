"""PCM file scanning and metadata helpers.

Default format assumed throughout: 16-bit signed, 22050 Hz, mono.
"""

import os
import glob

PCM_SAMPLE_RATE     = 22050
PCM_CHANNELS        = 1
PCM_BYTES_PER_SAMPLE = 2   # 16-bit


def pcm_duration(path: str) -> float:
    """Return playback duration in seconds."""
    size = os.path.getsize(path)
    bps  = PCM_SAMPLE_RATE * PCM_CHANNELS * PCM_BYTES_PER_SAMPLE
    return size / bps


def fmt_size(n: int) -> str:
    """Format byte count as a human-readable string."""
    if n < 1024:      return f"{n}B"
    if n < 1024 ** 2: return f"{n / 1024:.1f}K"
    return                    f"{n / 1024 ** 2:.2f}M"


def scan_pcm(directory: str) -> list:
    """Recursively find all *.pcm files under *directory*, sorted."""
    pattern = os.path.join(directory, '**', '*.pcm')
    return sorted(glob.glob(pattern, recursive=True))
