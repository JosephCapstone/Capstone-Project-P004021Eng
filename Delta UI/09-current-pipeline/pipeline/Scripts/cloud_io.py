"""Shared point-cloud loading and change-field selection."""


def find_distance_field(vertex):
    """Finds CloudCompare's M3C2 signed-distance field, not its uncertainty field - both contain 'distance' in the name (e.g."""
    field_names = vertex.data.dtype.names or ()
    normalized = {n: n.lower().replace("_", " ").strip() for n in field_names}

    for n, norm in normalized.items():
        if norm == "m3c2 distance":
            return n

    m3c2_candidates = [
        n
        for n in field_names
        if "m3c2" in n.lower()
        and "uncertain" not in n.lower()
        and "signif" not in n.lower()
    ]
    if m3c2_candidates:
        return m3c2_candidates[0]

    distance_candidates = [
        n
        for n in field_names
        if "distance" in n.lower() and "uncertain" not in n.lower()
    ]
    if distance_candidates:
        return distance_candidates[0]

    fallback = [n for n in field_names if "distance" in n.lower()]
    return fallback[0] if fallback else None


def find_change_field(vertex):
    """The field that colours a layer."""
    if "change_inward" in (vertex.data.dtype.names or ()):
        return "change_inward"
    return find_distance_field(vertex)


def load_cloud(path):
    import open3d as o3d

    pcd = o3d.io.read_point_cloud(str(path))
    if len(pcd.points) == 0:
        raise ValueError(f"'{path}' loaded but has zero points.")
    return pcd
