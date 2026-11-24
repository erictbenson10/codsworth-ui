"""Compact progress text shared by Home and background conversations."""


def work_duration(seconds):
    seconds = max(0, int(seconds or 0))
    return f'{seconds}s' if seconds < 60 else f'{seconds//60}m {seconds%60:02d}s'


def shimmer_levels(text, frame, reduced=False):
    """Move a soft highlight across the letters, with a gap between sweeps."""
    if reduced:
        return [0] * len(text)
    peak = (frame % 32) / 32 * (len(text) + 6) - 3
    return [2 if abs(index-peak) < .8 else 1 if abs(index-peak) < 1.8 else 0
            for index in range(len(text))]
