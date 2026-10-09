#!/usr/bin/env python3
"""
blender_surface.py
====================
Stage 7 (Surface) - Blender method. Runs INSIDE Blender (its own bundled
Python), not in the app's Python: it uses only `bpy` and the standard
library, so nothing extra has to be installed into Blender.

What it does, in order:
  1. Opens a .blend file that holds the surfacing recipe: one object with
     a Geometry Nodes modifier (for D.E.L.T.A., configs/ExtraDownsampling.blend
     - stray-point removal, Points to Volume, Volume to Mesh).
  2. Imports --input (a .ply point cloud, usually a Stage 6 output) and
     swaps it in as that object's mesh, so the modifier runs on it. The
     points saved in the .blend itself are never used.
  3. Optionally overrides modifier inputs (--set "Panel/Name=value",
     values in METRES for lengths, the same unit as the .ply).
  4. Optionally carries per-point fields (--carry-field, repeatable or
     comma-separated, e.g. scalar_M3C2_distance,cluster_id) onto the new
     mesh's vertices, by nearest original point - same idea as
     surface_reconstruction.py's --carry-field, so Stage 8 (Export) can
     still colour the mesh by change magnitude. One nearest-point lookup
     is shared by all carried fields.
  5. Evaluates the modifier ONCE and writes the result to --output as a
     binary triangle-mesh .ply (x, y, z + the carried fields), the format
     usd_export.py already reads.

The .blend file is never saved - every change above happens in memory.

Run by the app (pipeline_core.build_blender_surface_command()) as:
    blender --background --factory-startup --python-exit-code 1 \
        --python blender_surface.py -- \
        --blend ExtraDownsampling.blend --input in.ply --output out.ply \
        [--set "Mesh/Voxel Size=0.025"] [--carry-field scalar_M3C2_distance]
        [--carry-field cluster_id]

List the inputs a .blend offers (no --input/--output needed):
    blender --background --factory-startup --python blender_surface.py -- \
        --blend ExtraDownsampling.blend --list-inputs

Also runs under the `bpy` pip module for testing (python blender_surface.py
-- ...), which is how it was tested: bpy 5.2.2, against
ExtraDownsampling.blend saved by Blender 5.2.

Verified against Blender 5.2 behaviour (bpy 5.2.2), not guessed:
  - wm.ply_import(import_attributes=True) brings custom PLY vertex
    properties in as POINT attributes with their original names (e.g.
    scalar_M3C2_distance), and leaves coordinates unchanged with
    forward_axis='Y', up_axis='Z' (Blender's own axes - no conversion).
  - wm.ply_export(export_attributes=True, export_triangulated_mesh=True)
    writes float POINT attributes as vertex properties and faces as a
    'vertex_indices' list - what usd_export.py's read_ply_data() expects.
  - The PLY importer brings EVERY extra vertex property in as a FLOAT
    attribute (int, uchar and double too - values unchanged), and the
    exporter writes every attribute as float. So an integer field such as
    cluster_id is carried with its exact values, written as float.
    Normals and colours are imported as 'normal' and 'Col', not under
    their PLY names, so they cannot be carried.
  - Blender 5 stores Geometry Nodes modifier input values under
    modifier.properties.inputs.<socket identifier>.value (plus a .type of
    VALUE/ATTRIBUTE); Blender 4.x used modifier[<identifier>]. Both are
    handled below.

Exit code: 0 on success, 1 on any error (the app shows the stage as
failed). Every line is printed with flush=True: Blender's Python ignores
the PYTHONUNBUFFERED variable the app sets for other scripts, so without
flushing the Terminal tab would stay empty until the run ended.
"""

import argparse
import os
import sys
import time

import bpy

MIN_BLENDER = (4, 2)
RESULT_OBJECT_NAME = "DELTA_surface_result"
CARRY_TYPES = {
    # PLY import attribute type -> (Sample Index type, Store Named Attribute type)
    "FLOAT": ("FLOAT", "FLOAT"),
    "INT": ("INT", "INT"),
    "BOOLEAN": ("BOOLEAN", "BOOLEAN"),
}

_T0 = time.perf_counter()


class SurfaceError(Exception):
    """An expected failure with a message the user can act on."""


def log(message=""):
    print(message, flush=True)


def step(message):
    log(f"[{time.perf_counter() - _T0:7.1f} s] {message}")


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

def parse_args(argv):
    # Blender passes its own arguments too - ours come after "--".
    args = argv[argv.index("--") + 1:] if "--" in argv else argv[1:]
    parser = argparse.ArgumentParser(
        prog="blender_surface.py",
        description="Stage 7 (Surface) via a Blender Geometry Nodes .blend file.")
    parser.add_argument("--blend", required=True, help=".blend file with the surfacing recipe")
    parser.add_argument("--input", help="Input point cloud .ply")
    parser.add_argument("--output", help="Output mesh .ply")
    parser.add_argument("--object", default=None,
                        help="Object that holds the Geometry Nodes modifier. Default: the "
                             "only object in the file that has one.")
    parser.add_argument("--modifier", default=None,
                        help="Geometry Nodes modifier name. Default: the first one on the "
                             "object.")
    parser.add_argument("--set", action="append", default=[], metavar="PANEL/NAME=VALUE",
                        help="Override a modifier input, e.g. \"Mesh/Voxel Size=0.025\". "
                             "Lengths in metres. Repeat for more inputs. Inputs not set keep "
                             "the value saved in the .blend file.")
    parser.add_argument("--carry-field", action="append", default=[],
                        help="Per-point field of --input to copy onto the mesh vertices by "
                             "nearest original point, e.g. scalar_M3C2_distance. Repeat it, "
                             "or give a comma-separated list, to carry more than one field.")
    parser.add_argument("--list-inputs", action="store_true",
                        help="Print the modifier inputs and their saved values, then stop.")
    parsed = parser.parse_args(args)
    if not parsed.list_inputs and (not parsed.input or not parsed.output):
        parser.error("--input and --output are required (unless --list-inputs is given).")
    return parsed


def parse_override(text):
    if "=" not in text:
        raise SurfaceError(f"--set needs the form PANEL/NAME=VALUE, got: {text!r}")
    path, value = text.rsplit("=", 1)
    path, value = path.strip(), value.strip()
    if not path or not value:
        raise SurfaceError(f"--set needs the form PANEL/NAME=VALUE, got: {text!r}")
    return path, value


# ---------------------------------------------------------------------------
# .blend file: object, modifier, inputs
# ---------------------------------------------------------------------------

def open_blend(path):
    if not os.path.isfile(path):
        raise SurfaceError(f".blend file not found: {path}")
    if tuple(bpy.app.version[:2]) < MIN_BLENDER:
        raise SurfaceError(
            f"Blender {bpy.app.version_string} is too old - this script needs Blender "
            f"{MIN_BLENDER[0]}.{MIN_BLENDER[1]} or newer (5.2 or newer for "
            f"ExtraDownsampling.blend).")
    bpy.ops.wm.open_mainfile(filepath=path, load_ui=False)
    file_version = tuple(bpy.data.version[:2])
    log(f"Blender {bpy.app.version_string}; .blend saved by Blender "
        f"{file_version[0]}.{file_version[1]}: {path}")
    if file_version > tuple(bpy.app.version[:2]):
        log(f"WARNING: the .blend file was saved by a NEWER Blender "
            f"({file_version[0]}.{file_version[1]}) than this one "
            f"({bpy.app.version_string}). Geometry Nodes can change between versions - if "
            f"the result looks wrong, install Blender {file_version[0]}.{file_version[1]} "
            f"or newer.")


def find_target(object_name, modifier_name):
    if object_name:
        obj = bpy.data.objects.get(object_name)
        if obj is None:
            names = sorted(o.name for o in bpy.data.objects)
            raise SurfaceError(f"No object named {object_name!r} in the .blend file. "
                               f"Objects: {names}")
        candidates = [obj]
    else:
        candidates = [o for o in bpy.data.objects
                      if any(m.type == "NODES" and m.node_group for m in o.modifiers)]
        if not candidates:
            raise SurfaceError("No object in the .blend file has a Geometry Nodes modifier "
                               "with a node group.")
        if len(candidates) > 1:
            names = sorted(o.name for o in candidates)
            raise SurfaceError(f"More than one object has a Geometry Nodes modifier: {names}. "
                               f"Set the object name (--object).")
    obj = candidates[0]
    if obj.type != "MESH":
        raise SurfaceError(f"Object {obj.name!r} is a {obj.type}, not a MESH. The input .ply "
                           f"is imported as a mesh, so the modifier must be on a mesh object.")

    node_mods = [m for m in obj.modifiers if m.type == "NODES" and m.node_group]
    if modifier_name:
        node_mods = [m for m in node_mods if m.name == modifier_name]
        if not node_mods:
            names = [m.name for m in obj.modifiers]
            raise SurfaceError(f"Object {obj.name!r} has no Geometry Nodes modifier named "
                               f"{modifier_name!r}. Modifiers: {names}")
    if not node_mods:
        raise SurfaceError(f"Object {obj.name!r} has no Geometry Nodes modifier with a node "
                           f"group.")
    mod = node_mods[0]
    if not mod.show_viewport:
        log(f"NOTE: modifier {mod.name!r} was disabled in the viewport - enabling it for "
            f"this run.")
        mod.show_viewport = True
    return obj, mod


def _input_path(item):
    parts = [item.name]
    parent = item.parent
    while parent is not None and parent.name:
        parts.append(parent.name)
        parent = parent.parent
    return "/".join(reversed(parts))


def list_value_inputs(mod):
    """Every non-geometry input of the modifier's node group, as
    (path, item) - path is 'Panel/Name', or just 'Name' at the top level."""
    result = []
    for item in mod.node_group.interface.items_tree:
        if item.item_type != "SOCKET" or item.in_out != "INPUT":
            continue
        if item.socket_type == "NodeSocketGeometry":
            continue
        result.append((_input_path(item), item))
    return result


def _input_slot(mod, identifier):
    """Blender 5: modifier.properties.inputs.<identifier> (has .value and
    .type). Returns None on Blender 4.x, which stores mod[identifier]."""
    props = getattr(mod, "properties", None)
    inputs = getattr(props, "inputs", None) if props is not None else None
    return getattr(inputs, identifier, None) if inputs is not None else None


def _show(value):
    """Shows Blender's float32 values without float noise (0.05, not
    0.05000000074505806)."""
    return f"{value:.6g}" if isinstance(value, float) else str(value)


def get_input_value(mod, item):
    slot = _input_slot(mod, item.identifier)
    if slot is not None and hasattr(slot, "value"):
        if getattr(slot, "type", "VALUE") == "ATTRIBUTE":
            return f"<attribute {slot.attribute_name!r}>"
        return slot.value
    try:
        return mod[item.identifier]
    except (KeyError, TypeError):
        return getattr(item, "default_value", None)


def set_input_value(mod, item, value):
    slot = _input_slot(mod, item.identifier)
    if slot is not None and hasattr(slot, "value"):
        if getattr(slot, "type", "VALUE") == "ATTRIBUTE":
            log(f"NOTE: input {_input_path(item)!r} was set to use an attribute - "
                f"switching it to a fixed value for this run.")
            slot.type = "VALUE"
        slot.value = value
        return
    mod[item.identifier] = value


def _convert(item, text):
    socket_type = item.socket_type
    try:
        if socket_type.startswith("NodeSocketFloat"):
            return float(text)
        if socket_type.startswith("NodeSocketInt"):
            return int(text)
        if socket_type == "NodeSocketBool":
            lowered = text.strip().lower()
            if lowered in ("1", "true", "yes", "on"):
                return True
            if lowered in ("0", "false", "no", "off"):
                return False
            raise ValueError(text)
    except ValueError:
        raise SurfaceError(f"Value {text!r} is not valid for input {_input_path(item)!r} "
                           f"({socket_type}).")
    raise SurfaceError(f"Input {_input_path(item)!r} is a {socket_type} - only number and "
                       f"on/off inputs can be set from the command line.")


def apply_overrides(mod, overrides):
    inputs = list_value_inputs(mod)
    available = [path for path, _item in inputs]
    for path, text in overrides:
        wanted = path.lower()
        matches = [item for p, item in inputs if p.lower() == wanted]
        if not matches:
            # Allow a bare name when it is unique ('Voxel Size').
            matches = [item for p, item in inputs if p.split("/")[-1].lower() == wanted]
        if not matches:
            raise SurfaceError(f"No modifier input named {path!r}. Inputs in this .blend file: "
                               f"{available}")
        if len(matches) > 1:
            raise SurfaceError(f"{path!r} matches more than one input "
                               f"{[_input_path(i) for i in matches]} - use the full "
                               f"'Panel/Name' form.")
        item = matches[0]
        before = get_input_value(mod, item)
        set_input_value(mod, item, _convert(item, text))
        log(f"  set {_input_path(item)}: {_show(before)} -> "
            f"{_show(get_input_value(mod, item))}")


def print_inputs(mod, heading):
    log(heading)
    for path, item in list_value_inputs(mod):
        log(f"  {path} = {_show(get_input_value(mod, item))}")


# ---------------------------------------------------------------------------
# Input cloud
# ---------------------------------------------------------------------------

def import_input(obj, ply_path):
    if not os.path.isfile(ply_path):
        raise SurfaceError(f"Input .ply not found: {ply_path}")
    before = set(bpy.data.objects)
    result = bpy.ops.wm.ply_import(
        filepath=ply_path, import_attributes=True, merge_verts=False,
        forward_axis="Y", up_axis="Z", global_scale=1.0, use_scene_unit=False)
    new_objects = [o for o in bpy.data.objects if o not in before]
    if "FINISHED" not in result or not new_objects:
        raise SurfaceError(f"Blender could not import {ply_path}.")
    imported = new_objects[0]
    if imported.type != "MESH":
        raise SurfaceError(f"Imported {ply_path} as a {imported.type}, expected a MESH.")

    old_mesh = obj.data
    obj.data = imported.data
    bpy.data.objects.remove(imported)
    if old_mesh.users == 0:
        bpy.data.meshes.remove(old_mesh)  # frees the points saved in the .blend

    # The output must stay in the pipeline's coordinate frame, whatever
    # transform the object had in the .blend file.
    if obj.parent is not None:
        log(f"NOTE: object {obj.name!r} had a parent in the .blend file - removed for this "
            f"run.")
        obj.parent = None
    if any(abs(obj.matrix_world[r][c] - (1.0 if r == c else 0.0)) > 1e-9
           for r in range(4) for c in range(4)):
        log(f"NOTE: object {obj.name!r} was moved, rotated or scaled in the .blend file - "
            f"reset to the origin for this run so the output keeps the input coordinates.")
    obj.matrix_world.identity()

    mesh = obj.data
    fields = sorted(a.name for a in mesh.attributes
                    if a.domain == "POINT" and a.name != "position"
                    and not a.name.startswith("."))
    log(f"Input: {len(mesh.vertices):,} points, {len(mesh.polygons):,} faces; "
        f"point fields: {fields or 'none'}")
    if len(mesh.vertices) == 0:
        raise SurfaceError(f"Input {ply_path} has no points.")
    return fields


# ---------------------------------------------------------------------------
# Carry a field onto the output (nodes added to a COPY of the node group)
# ---------------------------------------------------------------------------

def _first_geometry(sockets):
    for socket in sockets:
        if socket.type == "GEOMETRY":
            return socket
    return None


def carry_field_list(values):
    """--carry-field values (repeatable, each maybe comma-separated) ->
    a list of unique names, in the order given."""
    names = []
    for value in values or []:
        for name in str(value).split(","):
            name = name.strip()
            if name and name not in names:
                names.append(name)
    return names


def add_field_carry(obj, mod, field_names, available_fields):
    """Copies each field in field_names from the nearest original point
    onto every result vertex. Works on a COPY of the node group; one
    Sample Nearest is shared by all fields, then one Sample Index + Store
    Named Attribute per field, chained before the Group Output."""
    types = {}
    missing = []
    for field_name in field_names:
        attribute = obj.data.attributes.get(field_name)
        if attribute is None or attribute.domain != "POINT":
            missing.append(field_name)
        elif attribute.data_type not in CARRY_TYPES:
            raise SurfaceError(f"Field {field_name!r} has type {attribute.data_type} - only "
                               f"{sorted(CARRY_TYPES)} fields can be carried.")
        else:
            types[field_name] = CARRY_TYPES[attribute.data_type]
    if missing:
        raise SurfaceError(f"Field(s) {missing} not found in the input. Available fields: "
                           f"{available_fields or 'none'}")

    tree = mod.node_group.copy()  # never edit the recipe itself
    tree.name = f"{mod.node_group.name} (+carry {', '.join(field_names)})"
    mod.node_group = tree

    outputs = [n for n in tree.nodes if n.bl_idname == "NodeGroupOutput"]
    output = next((n for n in outputs if getattr(n, "is_active_output", False)),
                  outputs[0] if outputs else None)
    target_socket = _first_geometry(output.inputs) if output else None
    if target_socket is None or not target_socket.links:
        raise SurfaceError("The node group has no connected Geometry output to add the "
                           "carried fields to.")
    result_socket = target_socket.links[0].from_socket
    tree.links.remove(target_socket.links[0])

    link = tree.links.new
    original = _first_geometry(tree.nodes.new("NodeGroupInput").outputs)
    nearest = tree.nodes.new("GeometryNodeSampleNearest")
    nearest.domain = "POINT"
    link(original, nearest.inputs["Geometry"])

    geometry = result_socket
    for field_name in field_names:
        sample_type, store_type = types[field_name]
        named = tree.nodes.new("GeometryNodeInputNamedAttribute")
        named.data_type = sample_type
        named.inputs["Name"].default_value = field_name
        sample = tree.nodes.new("GeometryNodeSampleIndex")
        sample.data_type = sample_type
        sample.domain = "POINT"
        store = tree.nodes.new("GeometryNodeStoreNamedAttribute")
        store.data_type = store_type
        store.domain = "POINT"
        store.inputs["Name"].default_value = field_name
        link(original, sample.inputs["Geometry"])
        link(named.outputs["Attribute"], sample.inputs["Value"])
        link(nearest.outputs["Index"], sample.inputs["Index"])
        link(geometry, store.inputs["Geometry"])
        link(sample.outputs["Value"], store.inputs["Value"])
        geometry = store.outputs["Geometry"]
    link(geometry, target_socket)
    log(f"Carrying field(s) {field_names} onto the mesh (nearest original point).")


# ---------------------------------------------------------------------------
# Evaluate, report, export
# ---------------------------------------------------------------------------

def evaluate(obj):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    mesh = bpy.data.meshes.new_from_object(
        evaluated, preserve_all_data_layers=True, depsgraph=depsgraph)
    for warning in _node_warnings(obj):
        log(f"WARNING (Geometry Nodes): {warning}")
    return mesh


def _node_warnings(obj):
    warnings = []
    for mod in obj.modifiers:
        for warning in getattr(mod, "node_warnings", []) or []:
            message = getattr(warning, "message", None) or str(warning)
            warnings.append(f"{mod.name}: {message}")
    return warnings


def report(mesh, carry_fields):
    import numpy as np  # bundled with Blender

    n_vertices = len(mesh.vertices)
    n_faces = len(mesh.polygons)
    log(f"Result: {n_vertices:,} vertices, {n_faces:,} faces")
    if n_faces == 0:
        raise SurfaceError(
            "The result has no faces. Usual causes: the voxel size is too large for the "
            "input, the radius is too small to join the points, or 'Delete Further' removed "
            "almost every point. Check the values above.")

    loop_totals = np.empty(n_faces, dtype=np.int32)
    mesh.polygons.foreach_get("loop_total", loop_totals)
    areas = np.empty(n_faces, dtype=np.float32)
    mesh.polygons.foreach_get("area", areas)
    coords = np.empty(n_vertices * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", coords)
    coords = coords.reshape(-1, 3)
    log(f"  Triangles after export: {int((loop_totals - 2).sum()):,}")
    log(f"  Surface area: {float(areas.sum()):.4f} square metres")
    log(f"  Bounds: min {coords.min(axis=0).round(3).tolist()} "
        f"max {coords.max(axis=0).round(3).tolist()}")

    for carry_field in carry_fields:
        attribute = mesh.attributes.get(carry_field)
        if attribute is None:
            raise SurfaceError(f"The carried field {carry_field!r} is missing from the "
                               f"result - the node group may remove it.")
        values = np.empty(len(attribute.data), dtype=np.float32)
        attribute.data.foreach_get("value", values)
        log(f"  {carry_field}: {values.min():.4f} to {values.max():.4f}")


def export(mesh, output_path):
    folder = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(folder, exist_ok=True)

    result = bpy.data.objects.new(RESULT_OBJECT_NAME, mesh)
    bpy.context.scene.collection.objects.link(result)
    for obj in bpy.context.view_layer.objects:
        obj.select_set(False)
    result.select_set(True)
    bpy.context.view_layer.objects.active = result

    status = bpy.ops.wm.ply_export(
        filepath=output_path, export_selected_objects=True, apply_modifiers=False,
        export_attributes=True, export_normals=False, export_uv=False, export_colors="NONE",
        export_triangulated_mesh=True, ascii_format=False,
        forward_axis="Y", up_axis="Z", global_scale=1.0)
    if "FINISHED" not in status or not os.path.isfile(output_path):
        raise SurfaceError(f"Blender could not write {output_path}.")
    log(f"Saved mesh to: {output_path} ({os.path.getsize(output_path) / 1e6:.1f} MB)")


# ---------------------------------------------------------------------------

def main(argv):
    args = parse_args(argv)
    overrides = [parse_override(text) for text in args.set]

    step("Opening the .blend file")
    open_blend(args.blend)
    obj, mod = find_target(args.object, args.modifier)
    log(f"Recipe: object {obj.name!r}, modifier {mod.name!r}, node group "
        f"{mod.node_group.name!r}")

    if args.list_inputs:
        print_inputs(mod, "Modifier inputs (saved values, lengths in metres):")
        return

    step(f"Importing {args.input}")
    fields = import_input(obj, args.input)

    if overrides:
        log("Overrides:")
        apply_overrides(mod, overrides)
    print_inputs(mod, "Values used (lengths in metres):")

    carry_fields = carry_field_list(args.carry_field)
    if carry_fields:
        add_field_carry(obj, mod, carry_fields, fields)

    step("Running the Geometry Nodes modifier")
    mesh = evaluate(obj)
    report(mesh, carry_fields)

    step("Writing the .ply file")
    export(mesh, args.output)
    step("Done")


if __name__ == "__main__":
    try:
        main(sys.argv)
    except SurfaceError as error:
        log(f"ERROR: {error}")
        sys.exit(1)
