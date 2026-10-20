"""
make_recipe_update25.py
=======================
Builds configs/ExtraDownsampling.blend for update 25 from the update 24
file. Run it with Blender 5.2 or newer (or the bpy 5.2 pip module):

    blender --background --factory-startup --python make_recipe_update25.py -- \
        --input ExtraDownsampling_u24.blend --output ExtraDownsampling.blend

It adds two inputs and two node chains to the node group. Nothing else in
the recipe changes. With "Island Filter Size" = 1 and "Change Type" = -1
the mesh is the same as the mesh of the update 24 file (tested: identical
vertices).

1. Filter/Change Type (integer, default -1 = all types).
   0 to 4 keeps only the input points with that change_type (Stage 6:
   0 none, 1 added, 2 removed, 3 hole, 4 unmatched) before the surface is
   made. An input without change_type is not filtered.

2. Cleaning/Island Filter Size (integer, default 20 vertices; 1 = off).
   After Volume to Mesh, each separate piece of the mesh that has fewer
   vertices than this value is deleted - UNLESS one of its vertices is
   nearest to an input point of a damage site (cluster_id >= 0). Those
   pieces are kept at any size. An input without cluster_id has no
   protection: every small piece is deleted.
   When the value is 1 or less, a Switch node passes the mesh through and
   the filter is not computed (no extra time).
"""
import sys

import bpy

ISLAND_DEFAULT = 20
CHANGE_TYPE_DEFAULT = -1


def args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    values = dict(zip(argv[::2], argv[1::2]))
    return values["--input"], values["--output"]


def find_node(tree, bl_idname, name=None):
    for node in tree.nodes:
        if node.bl_idname == bl_idname and (name is None or node.name == name):
            return node
    raise SystemExit(f"node {bl_idname} {name or ''} not found")


def socket_of(node, identifier):
    for socket in node.outputs:
        if socket.identifier == identifier:
            return socket
    raise SystemExit(f"{node.name} has no output {identifier}")


def set_modifier_value(mod, identifier, value):
    slot = getattr(mod.properties.inputs, identifier)
    slot.value = value


def main():
    src, dst = args()
    bpy.ops.wm.open_mainfile(filepath=src, load_ui=False)
    objects = [o for o in bpy.data.objects
               if any(m.type == "NODES" and m.node_group for m in o.modifiers)]
    if len(objects) != 1:
        raise SystemExit(f"expected one recipe object, found {[o.name for o in objects]}")
    obj = objects[0]
    mod = next(m for m in obj.modifiers if m.type == "NODES")
    tree = mod.node_group
    iface = tree.interface
    if any(getattr(i, "name", "") == "Island Filter Size" for i in iface.items_tree):
        raise SystemExit("this file already has the update 25 inputs")

    panels = {i.name: i for i in iface.items_tree if i.item_type == "PANEL"}

    # -- interface ---------------------------------------------------------
    filter_panel = iface.new_panel("Filter", default_closed=False)
    change_type = iface.new_socket("Change Type", in_out="INPUT",
                                   socket_type="NodeSocketInt", parent=filter_panel)
    change_type.default_value = CHANGE_TYPE_DEFAULT
    change_type.min_value = -1
    change_type.max_value = 4
    change_type.description = ("-1 = all points. 0 to 4 = only the points with this "
                               "change_type (0 none, 1 added, 2 removed, 3 hole, "
                               "4 unmatched)")
    island = iface.new_socket("Island Filter Size", in_out="INPUT",
                              socket_type="NodeSocketInt", parent=panels["Cleaning"])
    island.default_value = ISLAND_DEFAULT
    island.min_value = 1
    island.max_value = 1000000
    island.description = ("Delete each mesh piece with fewer vertices than this, unless it "
                          "has a damage-site point (cluster_id >= 0). 1 = off")

    nodes, links = tree.nodes, tree.links
    group_input = find_node(tree, "NodeGroupInput", "Group Input")
    input_geometry = socket_of(group_input, "Socket_0")

    def gi(identifier, location):
        node = nodes.new("NodeGroupInput")
        node.location = location
        return socket_of(node, identifier)

    def new(bl_idname, location, **props):
        node = nodes.new(bl_idname)
        node.location = location
        for key, value in props.items():
            setattr(node, key, value)
        return node

    # -- 1. change type filter, before the delete-further step ---------------
    x0, y0 = group_input.location
    named_ct = new("GeometryNodeInputNamedAttribute", (x0, y0 - 400), data_type="FLOAT",
                   name="DELTA change_type")
    named_ct.inputs["Name"].default_value = "change_type"
    ct_value = gi(change_type.identifier, (x0, y0 - 560))
    not_equal = new("FunctionNodeCompare", (x0 + 200, y0 - 400), data_type="FLOAT",
                    operation="NOT_EQUAL", name="DELTA type differs")
    not_equal.inputs["Epsilon"].default_value = 0.001
    links.new(named_ct.outputs["Attribute"], not_equal.inputs["A"])
    links.new(ct_value, not_equal.inputs["B"])
    filter_on = new("FunctionNodeCompare", (x0 + 200, y0 - 580), data_type="INT",
                    operation="GREATER_EQUAL", name="DELTA type filter on")
    links.new(ct_value, filter_on.inputs["A"])
    filter_on.inputs["B"].default_value = 0
    and_a = new("FunctionNodeBooleanMath", (x0 + 400, y0 - 450), operation="AND")
    links.new(not_equal.outputs["Result"], and_a.inputs[0])
    links.new(filter_on.outputs["Result"], and_a.inputs[1])
    and_b = new("FunctionNodeBooleanMath", (x0 + 560, y0 - 450), operation="AND")
    links.new(and_a.outputs[0], and_b.inputs[0])
    links.new(named_ct.outputs["Exists"], and_b.inputs[1])
    delete_type = new("GeometryNodeDeleteGeometry", (x0 + 740, y0 - 300), domain="POINT",
                      name="DELTA delete other types")
    links.new(input_geometry, delete_type.inputs["Geometry"])
    links.new(and_b.outputs[0], delete_type.inputs["Selection"])

    # Every link that took the raw input geometry now takes the filtered one.
    for link in list(links):
        if link.from_socket == input_geometry and link.to_node != delete_type:
            target = link.to_socket
            links.remove(link)
            links.new(delete_type.outputs["Geometry"], target)

    # -- 2. island filter, after Volume to Mesh -----------------------------
    to_mesh = find_node(tree, "GeometryNodeVolumeToMesh")
    shade = find_node(tree, "GeometryNodeSetShadeSmooth")
    old = next(l for l in links if l.from_node == to_mesh and l.to_node == shade)
    links.remove(old)
    mx, my = to_mesh.location
    mesh_out = to_mesh.outputs["Mesh"]

    island_index = new("GeometryNodeInputMeshIsland", (mx, my - 300))
    count = new("GeometryNodeAccumulateField", (mx + 200, my - 300), data_type="INT",
                domain="POINT", name="DELTA vertices per piece")
    count.inputs["Value"].default_value = 1
    links.new(island_index.outputs["Island Index"], count.inputs["Group ID"])

    named_cid = new("GeometryNodeInputNamedAttribute", (mx - 400, my - 600),
                    data_type="FLOAT", name="DELTA cluster_id")
    named_cid.inputs["Name"].default_value = "cluster_id"
    is_site = new("FunctionNodeCompare", (mx - 200, my - 600), data_type="FLOAT",
                  operation="GREATER_EQUAL", name="DELTA point is in a site")
    links.new(named_cid.outputs["Attribute"], is_site.inputs["A"])
    is_site.inputs["B"].default_value = 0.0
    site_and = new("FunctionNodeBooleanMath", (mx - 40, my - 600), operation="AND")
    links.new(is_site.outputs["Result"], site_and.inputs[0])
    links.new(named_cid.outputs["Exists"], site_and.inputs[1])
    original = gi("Socket_0", (mx - 400, my - 800))
    nearest = new("GeometryNodeSampleNearest", (mx - 40, my - 800), domain="POINT",
                  name="DELTA nearest input point")
    links.new(original, nearest.inputs["Geometry"])
    sample = new("GeometryNodeSampleIndex", (mx + 160, my - 650), data_type="BOOLEAN",
                 domain="POINT", name="DELTA vertex is at a site")
    links.new(original, sample.inputs["Geometry"])
    links.new(site_and.outputs[0], sample.inputs["Value"])
    links.new(nearest.outputs["Index"], sample.inputs["Index"])
    site_count = new("GeometryNodeAccumulateField", (mx + 360, my - 650), data_type="INT",
                     domain="POINT", name="DELTA site vertices per piece")
    links.new(sample.outputs["Value"], site_count.inputs["Value"])
    links.new(island_index.outputs["Island Index"], site_count.inputs["Group ID"])

    size_value = gi(island.identifier, (mx + 200, my - 460))
    too_small = new("FunctionNodeCompare", (mx + 400, my - 300), data_type="INT",
                    operation="LESS_THAN", name="DELTA piece too small")
    links.new(count.outputs["Total"], too_small.inputs["A"])
    links.new(size_value, too_small.inputs["B"])
    no_site = new("FunctionNodeCompare", (mx + 560, my - 650), data_type="INT",
                  operation="EQUAL", name="DELTA piece has no site")
    links.new(site_count.outputs["Total"], no_site.inputs["A"])
    no_site.inputs["B"].default_value = 0
    delete_sel = new("FunctionNodeBooleanMath", (mx + 740, my - 400), operation="AND")
    links.new(too_small.outputs["Result"], delete_sel.inputs[0])
    links.new(no_site.outputs["Result"], delete_sel.inputs[1])
    delete_small = new("GeometryNodeDeleteGeometry", (mx + 920, my - 150), domain="POINT",
                       name="DELTA delete small pieces")
    links.new(mesh_out, delete_small.inputs["Geometry"])
    links.new(delete_sel.outputs[0], delete_small.inputs["Selection"])

    filter_active = new("FunctionNodeCompare", (mx + 920, my + 100), data_type="INT",
                        operation="GREATER_THAN", name="DELTA island filter on")
    links.new(gi(island.identifier, (mx + 740, my + 100)), filter_active.inputs["A"])
    filter_active.inputs["B"].default_value = 1
    switch = new("GeometryNodeSwitch", (mx + 1120, my), input_type="GEOMETRY",
                 name="DELTA island filter switch")
    links.new(filter_active.outputs["Result"], switch.inputs["Switch"])
    links.new(mesh_out, switch.inputs["False"])
    links.new(delete_small.outputs["Geometry"], switch.inputs["True"])
    links.new(switch.outputs["Output"], shade.inputs["Geometry"])
    shade.location = (mx + 1320, shade.location[1])

    # -- saved values on the modifier ---------------------------------------
    set_modifier_value(mod, change_type.identifier, CHANGE_TYPE_DEFAULT)
    set_modifier_value(mod, island.identifier, ISLAND_DEFAULT)

    bpy.ops.wm.save_as_mainfile(filepath=dst, compress=True)
    print(f"saved {dst}: Change Type={CHANGE_TYPE_DEFAULT}, Island Filter Size={ISLAND_DEFAULT}")


main()
