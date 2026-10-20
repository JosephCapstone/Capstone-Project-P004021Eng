"""Read exported measurements without deriving unsupported severity or percentages."""
import math


def site_summary(stage, path):
    prim = stage.GetPrimAtPath(path)
    def value(name):
        attr = prim.GetAttribute("delta:" + name)
        return attr.Get() if attr else None
    def number(name):
        v = value(name)
        return float(v) if isinstance(v, (int, float)) and math.isfinite(v) else None
    # Exported delta measurement attributes are metres, independent of scene units.
    width, height = number("width"), number("height")
    extent = value("extent")
    if width is not None and height is not None:
        dimensions = f"{width:.3f} × {height:.3f} m"
    elif extent is not None and len(extent) == 3 and all(math.isfinite(v) for v in extent):
        dimensions = " × ".join(f"{abs(v):.3f}" for v in extent) + " m (XYZ)"
    else:
        dimensions = "Not available"
    signed = number("meanChangeInward")
    mean, maximum = number("meanMagnitude"), number("maxMagnitude")
    if signed is not None:
        direction = "added" if signed > 0 else "removed" if signed < 0 else "unchanged"
        baseline = f"{signed * 1000:+.1f} mm mean ({direction})"
    elif mean is not None:
        baseline = f"{abs(mean) * 1000:.1f} mm mean displacement"
    else:
        baseline = "No comparison available"
    return dict(dimensions=dimensions, severity=str(value("severity") or "Not assessed"),
                baseline=baseline, maximum=f"{abs(maximum)*1000:.1f} mm" if maximum is not None else "Not available",
                kind=str(value("type") or "Not specified"), surface=str(value("surface") or "Not specified"))


def card_position(bounds, viewport_size, card_size=(280, 184), gap=18, margin=12):
    """Place beside projected NDC bounds, flip at the right edge and clamp."""
    width, height = viewport_size
    cw, ch = card_size
    left, bottom, right, top = bounds
    x = (right + 1) * width / 2 + gap
    if x + cw > width - margin:
        x = (left + 1) * width / 2 - cw - gap
    y = (1 - top) * height / 2
    return max(margin, min(max(margin, width-cw-margin), x)), max(52, min(max(52, height-ch-margin), y))
