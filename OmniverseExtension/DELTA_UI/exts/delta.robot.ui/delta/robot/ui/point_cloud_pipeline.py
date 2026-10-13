"""PLY refinement, comparison, and USD creation."""

from pathlib import Path

import numpy as np
import open3d as o3d
from pxr import Sdf, Usd, UsdGeom, Vt


# ---------- Processing presets ----------

QUALITY_PRESETS = {
    "Low - fastest": {
        "voxel": 0.08,
        "neighbors": 12,
        "std_ratio": 1.50,
        "change_threshold": 0.10,
        "registration_voxel": 0.20,
        "registration_distance": 0.50,
    },
    "Medium - recommended": {
        "voxel": 0.05,
        "neighbors": 20,
        "std_ratio": 1.00,
        "change_threshold": 0.05,
        "registration_voxel": 0.10,
        "registration_distance": 0.25,
    },
    "High - detailed": {
        "voxel": 0.03,
        "neighbors": 30,
        "std_ratio": 0.75,
        "change_threshold": 0.02,
        "registration_voxel": 0.05,
        "registration_distance": 0.10,
    },
}

BASELINE_COLOR = np.array([0.62, 0.62, 0.62], dtype=np.float32)
LOSS_COLOR = np.array([1.0, 0.06, 0.04], dtype=np.float32)
ADDITION_COLOR = np.array([0.02, 0.82, 1.0], dtype=np.float32)


# ---------- Point-cloud processing ----------

def _load_and_refine(path, preset, report, label):
    report(f"Loading {label}: {path}")
    cloud = o3d.io.read_point_cloud(str(path))
    if cloud.is_empty():
        raise RuntimeError(f"{label.title()} PLY is empty or unreadable.")

    cloud = cloud.voxel_down_sample(preset["voxel"])
    if len(cloud.points) > preset["neighbors"]:
        cloud, _ = cloud.remove_statistical_outlier(
            nb_neighbors=preset["neighbors"],
            std_ratio=preset["std_ratio"],
        )
    if cloud.is_empty():
        raise RuntimeError(f"{label.title()} PLY has no points after refinement.")
    report(f"{label.title()} refined to {len(cloud.points):,} points")
    return cloud


def _align_and_detect(baseline, inspection, preset, report):
    report("Aligning inspection to baseline")
    baseline_sample = baseline.voxel_down_sample(preset["registration_voxel"])
    inspection_sample = inspection.voxel_down_sample(preset["registration_voxel"])
    result = o3d.pipelines.registration.registration_icp(
        inspection_sample,
        baseline_sample,
        preset["registration_distance"],
        np.eye(4),
        o3d.pipelines.registration.TransformationEstimationPointToPoint(),
        o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=100),
    )
    inspection.transform(result.transformation)
    report(f"Alignment fitness: {result.fitness:.3f}")

    baseline_distance = np.asarray(
        baseline.compute_point_cloud_distance(inspection), dtype=np.float32
    )
    inspection_distance = np.asarray(
        inspection.compute_point_cloud_distance(baseline), dtype=np.float32
    )
    baseline_points = np.asarray(baseline.points, dtype=np.float32)
    inspection_points = np.asarray(inspection.points, dtype=np.float32)
    threshold = preset["change_threshold"]

    loss_mask = baseline_distance > threshold
    addition_mask = inspection_distance > threshold
    positions = np.concatenate(
        (baseline_points[loss_mask], inspection_points[addition_mask]), axis=0
    )
    signed_distance = np.concatenate(
        (-baseline_distance[loss_mask], inspection_distance[addition_mask]), axis=0
    )
    report(
        f"Detected {int(loss_mask.sum()):,} loss and "
        f"{int(addition_mask.sum()):,} addition points"
    )
    return positions, signed_distance, result.fitness


# ---------- USD creation ----------

def _vec3_array(values):
    values = np.asarray(values, dtype=np.float32)
    try:
        return Vt.Vec3fArray.FromNumpy(values)
    except AttributeError:
        return Vt.Vec3fArray([tuple(value) for value in values])


def _add_points(stage, path, positions, colors, width, signed_distance=None):
    points = UsdGeom.Points.Define(stage, path)
    points.CreatePointsAttr(_vec3_array(positions))
    points.CreateWidthsAttr(Vt.FloatArray([width] * len(positions)))
    points.CreateDisplayColorPrimvar(UsdGeom.Tokens.vertex).Set(_vec3_array(colors))

    if signed_distance is not None:
        primvar = UsdGeom.PrimvarsAPI(points).CreatePrimvar(
            "deltaSignedDistance",
            Sdf.ValueTypeNames.FloatArray,
            UsdGeom.Tokens.vertex,
        )
        primvar.Set(
            Vt.FloatArray(
                [float(value) for value in np.asarray(signed_distance, dtype=np.float32)]
            )
        )
    return points


def _damage_colors(values):
    values = np.asarray(values, dtype=np.float32)
    if not len(values):
        return np.empty((0, 3), dtype=np.float32)

    limit = max(float(np.percentile(np.abs(values), 98)), 1e-6)
    magnitude = 0.55 + 0.45 * np.clip(np.abs(values) / limit, 0.0, 1.0)
    colors = np.empty((len(values), 3), dtype=np.float32)
    loss = values < 0
    colors[loss] = LOSS_COLOR * magnitude[loss, None]
    colors[~loss] = ADDITION_COLOR * magnitude[~loss, None]
    return colors


def _write_usd(output_path, baseline, damage_positions, signed_distance, report):
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Usd.Stage.CreateNew(str(output))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    world = UsdGeom.Xform.Define(stage, "/World")
    stage.SetDefaultPrim(world.GetPrim())
    UsdGeom.Xform.Define(stage, "/World/Compartment")

    baseline_positions = np.asarray(baseline.points, dtype=np.float32)
    baseline_colors = np.tile(BASELINE_COLOR, (len(baseline_positions), 1))
    _add_points(
        stage,
        "/World/Compartment/Baseline",
        baseline_positions,
        baseline_colors,
        0.025,
    )

    if damage_positions is not None:
        _add_points(
            stage,
            "/World/Compartment/DamageDetail",
            damage_positions,
            _damage_colors(signed_distance),
            0.05,
            signed_distance,
        )

    stage.GetRootLayer().Save()
    report(f"Saved USD: {output}")


# ---------- Public workflow ----------

def build_usd(baseline_path, inspection_path, output_path, quality_name, report):
    if quality_name not in QUALITY_PRESETS:
        raise ValueError(f"Unknown quality preset: {quality_name}")

    preset = QUALITY_PRESETS[quality_name]
    baseline = _load_and_refine(Path(baseline_path), preset, report, "baseline")
    damage_positions = None
    signed_distance = None
    fitness = None

    if inspection_path:
        inspection = _load_and_refine(
            Path(inspection_path), preset, report, "inspection"
        )
        damage_positions, signed_distance, fitness = _align_and_detect(
            baseline, inspection, preset, report
        )

    _write_usd(
        output_path,
        baseline,
        damage_positions,
        signed_distance,
        report,
    )
    return {
        "output": str(output_path),
        "baseline_points": len(baseline.points),
        "damage_points": 0 if damage_positions is None else len(damage_positions),
        "alignment_fitness": fitness,
    }
