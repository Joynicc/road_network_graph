"""
Shared geometry and attribute-aggregation helpers used across the pipeline
steps. Keeping this logic in one place avoids re-implementing the same
endpoint/connectivity/aggregation patterns in every step script.
"""

from typing import Iterable, List, Optional, Sequence, Tuple

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import linemerge, unary_union
from shapely.strtree import STRtree


# ---------------------------------------------------------------------------
# Endpoints & intersection detection
# ---------------------------------------------------------------------------

def get_endpoint_points(line: LineString) -> Optional[Tuple[Point, Point]]:
    """Return the (start, end) points of a LineString, or None otherwise."""
    if isinstance(line, LineString) and len(line.coords) > 1:
        return Point(line.coords[0]), Point(line.coords[-1])
    return None


def get_endpoint_coords(line: LineString) -> List[tuple]:
    """Return the raw (start, end) coordinate tuples of a LineString."""
    return [line.coords[0], line.coords[-1]]


def find_high_degree_points(
    gdf: gpd.GeoDataFrame,
    geometry_col: str = "geometry",
    min_degree: int = 3,
) -> gpd.GeoDataFrame:
    """
    Identify points where at least `min_degree` line endpoints coincide.

    This is used to detect road intersections: an endpoint shared by three
    or more segments is treated as a junction rather than a mid-block gap.
    """
    endpoints = gdf[geometry_col].apply(get_endpoint_coords)
    all_points = [Point(coord) for pair in endpoints for coord in pair]
    point_tuples = gpd.GeoSeries(all_points).apply(lambda p: p.coords[0])

    counts = point_tuples.value_counts()
    frequent_points = counts[counts >= min_degree].index

    return gpd.GeoDataFrame(
        geometry=[Point(coords) for coords in frequent_points], crs=gdf.crs
    )


def force_to_linestring(geometry):
    """
    Collapse a MultiLineString produced by a merge/trim operation back into
    a single LineString, concatenating parts if `linemerge` cannot fully
    join them.
    """
    if geometry.geom_type != "MultiLineString":
        return geometry

    merged = linemerge(geometry)
    if merged.geom_type != "MultiLineString":
        return merged

    coords = []
    for part in geometry.geoms:
        coords.extend(list(part.coords))
    return LineString(coords)


def trim_at_intersection_buffers(
    gdf: gpd.GeoDataFrame,
    intersection_points: gpd.GeoDataFrame,
    buffer_m: float,
    geometry_col: str = "geometry",
) -> gpd.GeoDataFrame:
    """
    Cut a small buffer around each intersection point out of every segment
    that touches it, so that segments meeting at a junction no longer share
    an exact endpoint (they are separated by the width of the buffer). This
    is what allows the later connectivity step to distinguish "still the
    same arc" from "separated by a real intersection".

    Segments touching two or more intersection buffers are left unmodified
    and returned separately (they already terminate at real junctions on
    both ends).
    """
    buffers = intersection_points.buffer(buffer_m)
    buffer_tree = STRtree(buffers)

    def intersecting_buffer_indices(geometry) -> List[int]:
        candidates = buffer_tree.query(geometry)
        return [i for i in candidates if buffers.iloc[i].intersects(geometry)]

    working = gdf.copy()
    working["_intersecting_buffers"] = working[geometry_col].apply(
        intersecting_buffer_indices
    )

    multi_junction = working[working["_intersecting_buffers"].apply(len) > 1].copy()
    single_or_no_junction = working[working["_intersecting_buffers"].apply(len) <= 1].copy()

    def trim(geometry, buffer_indices):
        if len(buffer_indices) == 1:
            return geometry.difference(buffers.iloc[buffer_indices[0]])
        return geometry

    single_or_no_junction[geometry_col] = single_or_no_junction.apply(
        lambda row: trim(row[geometry_col], row["_intersecting_buffers"]), axis=1
    )
    single_or_no_junction[geometry_col] = single_or_no_junction[geometry_col].apply(
        force_to_linestring
    )

    multi_junction = multi_junction.drop(columns=["_intersecting_buffers"])
    single_or_no_junction = single_or_no_junction.drop(columns=["_intersecting_buffers"])

    return single_or_no_junction, multi_junction


# ---------------------------------------------------------------------------
# Connectivity grouping (shared-endpoint / touching components)
# ---------------------------------------------------------------------------

def group_by_shared_endpoints(
    gdf: gpd.GeoDataFrame, geometry_col: str = "geometry"
) -> pd.Series:
    """
    Assign a connected-component group id to every row of `gdf` such that
    two geometries sharing an exact endpoint end up in the same group.

    Used to merge trimmed road segments back into continuous arcs: once
    segments have been cut at real intersections, whatever remains
    connected end-to-end belongs to a single arc.
    """
    spatial_index = gdf.sindex
    graph = nx.Graph()
    graph.add_nodes_from(gdf.index)

    for idx, row in gdf.iterrows():
        endpoints = get_endpoint_points(row[geometry_col])
        if endpoints is None:
            continue

        candidate_idx = list(spatial_index.intersection(row[geometry_col].bounds))
        candidates = gdf.iloc[candidate_idx]

        for other_idx, other_row in candidates.iterrows():
            if other_idx == idx:
                continue
            other_endpoints = get_endpoint_points(other_row[geometry_col])
            if other_endpoints is None:
                continue
            if any(p.equals(q) for p in endpoints for q in other_endpoints):
                graph.add_edge(idx, other_idx)

    group_ids = {}
    for group_num, component in enumerate(nx.connected_components(graph), start=1):
        for idx in component:
            group_ids[idx] = f"ARC_{group_num}"

    return gdf.index.map(group_ids)


def group_by_relation(
    gdf: gpd.GeoDataFrame, geometry_col: str = "geometry", relation: str = "touches"
) -> pd.Series:
    """
    Assign a connected-component group id to rows whose geometries satisfy
    a binary spatial relation with one another (e.g. "touches" or
    "intersects", any predicate name available on a shapely geometry).

    Used to group individual roundabout segments that together form a
    single roundabout (`relation="touches"`), and to group nearby
    intersection points into a single logical node
    (`relation="intersects"`, applied to pre-buffered points so that
    "nearby" rather than "identical" points end up connected).
    """
    spatial_index = gdf.sindex
    graph = nx.Graph()
    graph.add_nodes_from(gdf.index)

    for idx, geometry in gdf[geometry_col].items():
        candidate_idx = list(spatial_index.intersection(geometry.bounds))
        candidates = gdf.iloc[candidate_idx]
        for other_idx, other_geometry in candidates[geometry_col].items():
            if idx != other_idx and getattr(geometry, relation)(other_geometry):
                graph.add_edge(idx, other_idx)

    group_ids = {}
    for group_num, component in enumerate(nx.connected_components(graph), start=1):
        for idx in component:
            group_ids[idx] = group_num

    return gdf.index.map(group_ids)


def dissolve_groups_to_polygons(
    gdf: gpd.GeoDataFrame,
    group_col: str,
    geometry_col: str,
    buffer_m: float,
) -> List[Polygon]:
    """
    Buffer every geometry in each group and dissolve the buffers together,
    producing one (or more, if disjoint) polygon per group. Buffering
    before dissolving closes small gaps between segments that belong to
    the same physical feature (e.g. a roundabout) but were not perfectly
    snapped together in the source data.
    """
    all_polygons: List[Polygon] = []

    for _, group_df in gdf.groupby(group_col):
        buffered = group_df[geometry_col].buffer(buffer_m)
        dissolved = unary_union(buffered)

        if isinstance(dissolved, MultiPolygon):
            all_polygons.extend(Polygon(part.exterior) for part in dissolved.geoms)
        elif isinstance(dissolved, Polygon):
            all_polygons.append(Polygon(dissolved.exterior))

    merged = unary_union(all_polygons)
    if isinstance(merged, Polygon):
        return [merged]
    if isinstance(merged, MultiPolygon):
        return list(merged.geoms)
    return []


# ---------------------------------------------------------------------------
# Attribute aggregation (used when several segments collapse into one arc
# or node, and a single representative attribute value is needed)
# ---------------------------------------------------------------------------

def pick_extreme_row(
    group: pd.DataFrame, code_col: str, how: str = "min"
) -> pd.Series:
    """Return the row of `group` holding the min/max value of `code_col`."""
    idx = group[code_col].idxmin() if how == "min" else group[code_col].idxmax()
    return group.loc[idx]


def compute_traffic_range(values: Sequence[float]) -> Tuple[Optional[float], Optional[float]]:
    """
    Summarize an arbitrary number of per-segment annual traffic values into
    a representative (max, min) pair for a merged arc or intersection node:

    - a single value is used for both max and min;
    - with two values, the larger is "max" and the smaller is "min";
    - with more than two, "max" is the average of the two highest values
      and "min" is the average of all remaining values.
    """
    values = [v for v in values if v is not None and not (isinstance(v, float) and np.isnan(v))]

    if not values:
        return None, None
    if len(values) == 1:
        return values[0], values[0]
    if len(values) == 2:
        return max(values), min(values)

    ranked = sorted(values, reverse=True)
    top_two = ranked[:2]
    remaining = ranked[2:]
    max_value = sum(top_two) / 2
    min_value = sum(remaining) / len(remaining) if remaining else None
    return max_value, min_value


def is_endpoint_connected(
    endpoint: Point, other_endpoints: Iterable[Point], tolerance: float
) -> bool:
    """True if `endpoint` lies within `tolerance` of any point in `other_endpoints`."""
    return any(endpoint.distance(p) < tolerance for p in other_endpoints)
