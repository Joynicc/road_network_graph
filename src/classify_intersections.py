"""
Classify intersections and associate them with arcs.

Run this after build_arcs.py: it reads the finished arc dataset plus the
raw segment layer (to detect intersections and roundabouts) and writes the
intersection-node dataset.

Every point where three or more arcs converge is a candidate intersection
("node"). Nodes are classified into three categories:

- roundabout nodes: groups of segments explicitly tagged as roundabout
  geometry in the source data, dissolved into a single polygon per
  roundabout (the source tagging is sometimes incomplete, so nearby
  roundabout segments are grouped by spatial contact rather than trusting
  the tag alone to define each roundabout's full extent);
- simple nodes: an isolated intersection point, not part of a roundabout;
- grouped simple nodes: two or more simple nodes close enough to each
  other (within GROUPED_SIMPLE_NODE_DISTANCE_M) that they represent one
  logical intersection rather than several distinct junctions -- e.g. a
  short slip road connecting two nearby junctions.

For every node, the arcs physically touching or crossing its boundary are
identified, and a representative set of attributes (road class, surface,
etc.) and an optional annual traffic summary are derived from those arcs.
Finally, arcs that fall entirely inside a node's buffer are flagged, since
such short stub arcs typically belong to the intersection itself rather
than to the open road network (useful, for example, to exclude them when
attaching point-based records such as recorded incidents to the network).
"""

import geopandas as gpd
import pandas as pd
from shapely.ops import unary_union

import config
import utils


CODE_ATTRIBUTE_RULES = [
    ("road_class_code", "road_class", "min"),
    ("operational_status_code", "operational_status", "min"),
    ("road_surface_code", "road_surface", "min"),
    ("grade_separation_code", "grade_separation", "min"),
    ("carriageway_type_code", "carriageway_type", "max"),
]


def load_intersection_source(path) -> gpd.GeoDataFrame:
    segments = gpd.read_file(path)
    segments = segments.rename(columns=config.RAW_SEGMENT_COLUMN_MAP)
    return segments.to_crs(config.CRS_METRIC)


def detect_roundabout_nodes(segments: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Group roundabout-tagged segments into one polygon per roundabout."""
    roundabout_segments = segments[segments["road_type"] == config.ROUNDABOUT_ROAD_TYPE_VALUE].copy()

    if roundabout_segments.empty:
        return gpd.GeoDataFrame(
            {"node_id": [], "node_type": [], "member_count": []},
            geometry=[],
            crs=segments.crs,
        )

    roundabout_segments["roundabout_group_id"] = utils.group_by_relation(
        roundabout_segments, relation="touches"
    )
    polygons = utils.dissolve_groups_to_polygons(
        roundabout_segments, "roundabout_group_id", "geometry", config.ROUNDABOUT_BUFFER_M
    )

    return gpd.GeoDataFrame(
        {
            "node_id": [f"N_R_{i + 1}" for i in range(len(polygons))],
            "node_type": "roundabout",
            "member_count": None,
        },
        geometry=polygons,
        crs=segments.crs,
    )


def find_simple_intersection_points(
    segments: gpd.GeoDataFrame, roundabout_nodes: gpd.GeoDataFrame
) -> gpd.GeoDataFrame:
    """Intersection points that do not fall inside a roundabout polygon."""
    intersection_points = utils.find_high_degree_points(
        segments, min_degree=config.MIN_INTERSECTION_DEGREE
    )

    if roundabout_nodes.empty:
        return intersection_points

    joined = gpd.sjoin(
        intersection_points,
        roundabout_nodes[["geometry", "node_id"]],
        how="left",
        predicate="within",
    ).drop(columns=["index_right"])

    return joined[joined["node_id"].isna()][["geometry"]].reset_index(drop=True)


def group_nearby_simple_nodes(simple_points: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Merge simple intersection points that lie within
    GROUPED_SIMPLE_NODE_DISTANCE_M of one another into a single logical
    node. Isolated points remain individual "simple" nodes; merged
    clusters become "grouped_simple" nodes located at the cluster centroid.
    """
    if simple_points.empty:
        return gpd.GeoDataFrame(
            {"node_id": [], "node_type": [], "member_count": []},
            geometry=[],
            crs=simple_points.crs,
        )

    proximity_buffer = simple_points.copy()
    proximity_buffer["geometry"] = simple_points.geometry.buffer(
        config.GROUPED_SIMPLE_NODE_DISTANCE_M / 2
    )
    proximity_group = utils.group_by_relation(proximity_buffer, relation="intersects")

    working = simple_points.copy()
    working["proximity_group"] = proximity_group

    simple_count = 0
    grouped_count = 0
    records = []

    for _, group in working.groupby("proximity_group"):
        if len(group) == 1:
            simple_count += 1
            records.append(
                {
                    "node_id": f"N_I_{simple_count}",
                    "node_type": "simple",
                    "member_count": 1,
                    "geometry": group.geometry.iloc[0],
                }
            )
        else:
            grouped_count += 1
            records.append(
                {
                    "node_id": f"N_G_{grouped_count}",
                    "node_type": "grouped_simple",
                    "member_count": len(group),
                    "geometry": unary_union(group.geometry.tolist()).centroid,
                }
            )

    return gpd.GeoDataFrame(records, crs=simple_points.crs)


def find_intersecting_arcs(nodes: gpd.GeoDataFrame, arcs: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    For every (polygon) node, list the arcs that touch or cross its
    boundary, excluding arcs that fall entirely inside it (those are
    intersection stubs, not roads passing through the junction).
    """
    touching = gpd.sjoin(arcs, nodes[["geometry"]], how="inner", predicate="touches")
    crossing = gpd.sjoin(arcs, nodes[["geometry"]], how="inner", predicate="crosses")
    combined = pd.concat([touching, crossing])
    combined = combined[~combined.duplicated(subset=["arc_id", "index_right"])]

    combined = combined.merge(
        nodes[["geometry"]], left_on="index_right", right_index=True, suffixes=("", "_node")
    )
    combined = combined[~combined.geometry.within(combined["geometry_node"])]

    arc_lists = combined.groupby("index_right")["arc_id"].agg(list)

    nodes = nodes.copy()
    nodes["intersecting_arc_ids"] = (
        nodes.index.map(arc_lists).map(lambda x: x if isinstance(x, list) else [])
    )
    return nodes


def resolve_traffic_metric_columns() -> list:
    return [config.NODE_TRAFFIC_METRIC.format(year=year) for year in config.YEARS]


def aggregate_node_attributes(nodes: gpd.GeoDataFrame, arcs: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Derive representative road attributes and a traffic summary for each node
    from the arcs associated with it."""
    arcs_by_id = arcs.set_index("arc_id")
    traffic_columns = [c for c in resolve_traffic_metric_columns() if c in arcs.columns]

    records = []
    for arc_ids in nodes["intersecting_arc_ids"]:
        record = {}
        if arc_ids:
            subset = arcs_by_id.loc[arcs_by_id.index.intersection(arc_ids)].reset_index()

            for code_col, label_col, how in CODE_ATTRIBUTE_RULES:
                if code_col in subset.columns and label_col in subset.columns and not subset.empty:
                    extreme = utils.pick_extreme_row(subset, code_col, how=how)
                    record[code_col] = extreme[code_col]
                    record[label_col] = extreme[label_col]

            for metric in traffic_columns:
                max_value, min_value = utils.compute_traffic_range(subset[metric].tolist())
                record[f"max_{metric}"] = max_value
                record[f"min_{metric}"] = min_value

        records.append(record)

    attributes = pd.DataFrame(records, index=nodes.index)
    return pd.concat([nodes, attributes], axis=1)


def flag_arcs_within_nodes(arcs: gpd.GeoDataFrame, nodes: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Mark arcs that lie entirely within a node polygon (intersection stubs)."""
    arcs = arcs.copy()
    arcs["is_within_intersection_buffer"] = False

    spatial_index = arcs.sindex
    within_indices = set()

    for geometry in nodes.geometry:
        candidate_idx = list(spatial_index.intersection(geometry.bounds))
        candidates = arcs.iloc[candidate_idx]
        within_indices.update(candidates[candidates.geometry.within(geometry)].index.tolist())

    arcs.loc[list(within_indices), "is_within_intersection_buffer"] = True
    return arcs


def main():
    arcs = gpd.read_file(config.ARCS_PATH)
    segments = load_intersection_source(config.INTERSECTION_SOURCE_PATH)

    roundabout_nodes = detect_roundabout_nodes(segments)
    simple_points = find_simple_intersection_points(segments, roundabout_nodes)
    simple_nodes = group_nearby_simple_nodes(simple_points)
    simple_nodes["geometry"] = simple_nodes.geometry.buffer(config.SIMPLE_NODE_BUFFER_M)

    nodes = pd.concat([simple_nodes, roundabout_nodes], ignore_index=True)
    nodes = gpd.GeoDataFrame(nodes, geometry="geometry", crs=segments.crs)

    nodes = find_intersecting_arcs(nodes, arcs)
    nodes = aggregate_node_attributes(nodes, arcs)

    arcs = flag_arcs_within_nodes(arcs, nodes)

    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    nodes.to_file(config.NODES_PATH, driver="GPKG")
    arcs.to_file(config.ARCS_PATH, driver="GPKG")

    print(f"Wrote {len(nodes)} intersection nodes to {config.NODES_PATH}")
    print(f"Updated {arcs['is_within_intersection_buffer'].sum()} arcs flagged as intersection stubs")


if __name__ == "__main__":
    main()
