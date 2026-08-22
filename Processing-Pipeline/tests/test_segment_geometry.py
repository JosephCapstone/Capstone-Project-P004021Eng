#!/usr/bin/env python3
"""
test_segment_geometry.py
===========================
Regression tests for segment_planes.py's convex_hull_2d() and
points_inside_hull() - the two pure-numpy helper functions behind the
outside-envelope junk-point filter (see segment_planes.py's module
docstring and write_classified_cloud()'s docstring for the feature
itself).

segment_planes.py imports open3d at module load time, which is not
installed in every environment that runs this test suite (it's a heavy,
platform-specific dependency only needed on the machine actually running
the pipeline against real scan data). convex_hull_2d() and
points_inside_hull() have zero open3d dependency - they only use numpy -
so this file pulls just those two function definitions out of
segment_planes.py's source text and exec()s them in isolation, instead
of `import segment_planes` (which would fail here on the open3d import
before ever reaching these functions). This is deliberately narrow: if
either function's name or the "def convex_hull_2d" / "def main()"
markers this relies on ever change, this file's own extraction step
below is what needs updating, not the functions themselves.

Run directly: python test_segment_geometry.py
"""

import sys
from pathlib import Path

import numpy as np

_FAILURES = []


def check(description, condition):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {description}")
    if not condition:
        _FAILURES.append(description)


def _load_geometry_functions():
    """Extracts convex_hull_2d and points_inside_hull from
    segment_planes.py's source without importing the module (see this
    file's own docstring for why)."""
    src = (Path(__file__).resolve().parent.parent / "scripts" / "segment_planes.py"
           ).read_text(encoding="utf-8")
    start = src.index("def convex_hull_2d")
    end = src.index("def main()")
    namespace = {"np": np}
    exec(src[start:end], namespace)
    return namespace["convex_hull_2d"], namespace["points_inside_hull"]


convex_hull_2d, points_inside_hull = _load_geometry_functions()


print("=== convex_hull_2d ===")

# A dense random fill of a known 4x4 square - the hull should trace the
# square's actual boundary, and every point used to build it should test
# as inside its own hull.
rng = np.random.RandomState(0)
square_fill = rng.uniform(0.0, 4.0, size=(2000, 2))
hull = convex_hull_2d(square_fill)
check("hull has at least 3 vertices for a real 2D spread", len(hull) >= 3)
check("hull X stays within the square's own bounds",
      hull[:, 0].min() >= 0.0 and hull[:, 0].max() <= 4.0)
check("hull Y stays within the square's own bounds",
      hull[:, 1].min() >= 0.0 and hull[:, 1].max() <= 4.0)
check("hull reaches close to all four corners (min corner)",
      hull[:, 0].min() < 0.05 and hull[:, 1].min() < 0.05)
check("hull reaches close to all four corners (max corner)",
      hull[:, 0].max() > 3.95 and hull[:, 1].max() > 3.95)

# A simple, exact square - hull should be exactly its 4 corners (a
# corner point should not appear twice, and no extra vertices).
exact_square = np.array([[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]])
exact_hull = convex_hull_2d(exact_square)
check("exact 4-corner square hull has exactly 4 vertices", len(exact_hull) == 4)

# Degenerate inputs: fewer than 3 unique points, or all collinear.
collinear = np.array([[0.0, 0.0], [1.0, 0.0], [2.0, 0.0], [3.0, 0.0]])
collinear_hull = convex_hull_2d(collinear)
check("collinear points produce a degenerate (< 3 vertex) hull", len(collinear_hull) < 3)

single_point = np.array([[1.0, 1.0]])
check("a single point produces a degenerate hull",
      len(convex_hull_2d(single_point)) < 3)

duplicate_points = np.array([[1.0, 1.0], [1.0, 1.0], [1.0, 1.0]])
check("all-duplicate points produce a degenerate hull",
      len(convex_hull_2d(duplicate_points)) < 3)


print("\n=== points_inside_hull ===")

room_hull = convex_hull_2d(exact_square)  # the exact 4-corner square from above

queries = np.array([
    [2.0, 2.0],    # dead center - inside
    [0.05, 0.05],  # just inside a corner - inside
    [-0.5, 2.0],   # well outside on -X
    [2.0, 4.3],    # 0.3m outside on +Y - outside a 0.15m margin
    [2.0, 4.1],    # 0.1m outside on +Y - inside a 0.15m margin
    [-10.0, -10.0],  # far outside
])

inside_no_margin = points_inside_hull(queries, room_hull, margin=0.0)
check("center point is inside with no margin", bool(inside_no_margin[0]))
check("near-corner point is inside with no margin", bool(inside_no_margin[1]))
check("point well outside on -X is outside with no margin", not bool(inside_no_margin[2]))
check("point 0.3m past the edge is outside with no margin", not bool(inside_no_margin[3]))
check("point 0.1m past the edge is ALSO outside with zero margin", not bool(inside_no_margin[4]))
check("far-outside point is outside with no margin", not bool(inside_no_margin[5]))

inside_with_margin = points_inside_hull(queries, room_hull, margin=0.15)
check("center point is inside with a 0.15m margin", bool(inside_with_margin[0]))
check("point 0.3m past the edge is STILL outside a 0.15m margin (0.3 > 0.15)",
      not bool(inside_with_margin[3]))
check("point 0.1m past the edge is inside a 0.15m margin (0.1 < 0.15)",
      bool(inside_with_margin[4]))
check("far-outside point is still outside even with a margin",
      not bool(inside_with_margin[5]))

# Every point that went into building a hull must itself test as inside
# that same hull (allowing a hair of numeric slack) - this is the
# invariant the actual envelope filter in segment_planes.py leans on
# (envelope points are always forced to outside_envelope=0, but this
# confirms that forcing is consistent with the geometry, not fighting it).
self_inside = points_inside_hull(square_fill, hull, margin=1e-6)
check("every point used to build a hull tests as inside that hull",
      bool(self_inside.all()))

# Degenerate hull (see above) must fail open (treat everything as
# inside) rather than incorrectly flagging real points as outside -
# segment_planes.py relies on this to skip filtering safely when there
# isn't enough envelope to derive a real footprint from.
degenerate_result = points_inside_hull(queries, collinear_hull, margin=0.15)
check("a degenerate hull treats every query point as inside (fails open)",
      bool(degenerate_result.all()))

empty_hull_result = points_inside_hull(queries, np.zeros((0, 2)), margin=0.15)
check("an empty hull also fails open (all inside)", bool(empty_hull_result.all()))


if _FAILURES:
    print(f"\n{len(_FAILURES)} CHECK(S) FAILED:")
    for f in _FAILURES:
        print(f"  - {f}")
    sys.exit(1)

print("\nALL TESTS PASSED")
