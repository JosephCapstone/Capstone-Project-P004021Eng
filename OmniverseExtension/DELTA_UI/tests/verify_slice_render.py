"""Verify the rendered mesh/point fixture, independently of UI state assertions."""
import json
from pathlib import Path
import numpy as np
from PIL import Image

root = Path(__file__).resolve().parent
counts = {}
for name in ("off", "left", "right", "restored"):
    image = np.asarray(Image.open(root / f"slice_{name}.png").convert("RGB")).astype(int)
    r, g, b = image[:, :, 0], image[:, :, 1], image[:, :, 2]
    counts[name] = {
        "red_mesh": int(((r > 90) & (r > g * 2) & (r > b * 2)).sum()),
        "green_mesh": int(((g > 90) & (g > r * 2) & (g > b * 2)).sum()),
        "blue_points": int(((b > 90) & (b > r * 2) & (b > g * 2)).sum()),
        "yellow_points": int(((r > 90) & (g > 90) & (b < 60)).sum()),
    }
assert counts["left"]["green_mesh"] == 0 and counts["left"]["yellow_points"] == 0, counts
assert counts["right"]["red_mesh"] == 0 and counts["right"]["blue_points"] == 0, counts
assert counts["left"]["red_mesh"] > 1000 and counts["left"]["blue_points"] > 1000, counts
assert counts["right"]["green_mesh"] > 1000 and counts["right"]["yellow_points"] > 1000, counts
assert all(counts[name][color] > 1000 for name in ("off", "restored") for color in counts[name]), counts
result = json.loads((root / "kit_slice_smoke_result.json").read_text())
assert result["ok"], result
result["rendered_mesh_and_point_clipping"] = True
result["pixel_counts"] = counts
(root / "kit_slice_smoke_result.json").write_text(json.dumps(result, indent=2))
print("Rendered mesh and point clipping verified; both halves restore correctly.")
