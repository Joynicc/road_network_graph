"""
Build arcs from raw road segments and enrich them with public-transport
coverage, altitude, and tortuosity.

An "arc" is one or more consecutive road segments that connect two real
intersections, with no intersection in between. Working at the arc level
instead of the raw segment level simplifies the road graph: segments that
only exist because the source data happened to split a road at an
arbitrary point are merged back together, while segments that meet at a
genuine junction (three or more roads converging) remain distinct.

This script runs four stages in sequence and writes one arc dataset:

1. build_arcs        - detect intersections, trim segments around them,
                        merge connected segments into arcs, and aggregate
                        their attributes.
2. add_public_transport_flag - flag arcs that carry a public-transport line.
3. add_average_altitude      - sample a digital elevation raster at each
                        arc's endpoints and average the result.
4. add_tortuosity            - ratio of an arc's real length to the
                        straight-line distance between its endpoints.

Run directly (`python build_arcs.py`) using the paths in config.py, or
import the individual functions to use as a library.
"""

import warnings

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from shapely.geometry import LineString, Point
from shapely.ops import linemerge

import config
import utils


# ---------------------------------------------------------------------------
# 1. Build arcs from raw segments
# ---------------------------------------------------------------------------

def load_segments(path) -> gpd.GeoDataFrame:
    """Load the raw segment layer and rename columns to the generalized schema."""
    gdf = gpd.read_file(path)

    # ANNUAL_METRIC_TEMPLATES maps generalized name -> raw name, per year
    annual_metric_map = {
        raw.format(year=year): name.format(year=year)
        for name, raw in config.ANNUAL_METRIC_TEMPLATES.items()
        for year in config.YEARS
    }
    gdf = gdf.rename(columns={**config.RAW_SEGMENT_COLUMN_MAP, **annual_metric_map})
    gdf = gdf.to_crs(config.CRS_METRIC)

    if config.CURRENT_EDITION_FLAG_COLUMN and config.CURRENT_EDITION_FLAG_COLUMN in gdf.columns:
        gdf = gdf[gdf[config.CURRENT_EDITION_FLAG_COLUMN] == 1]

    return gdf


def annual_metric_columns() -> list:
    """Resolve the configured annual-metric column names that are present."""
    return [
        name.format(year=year)
        for name in config.ANNUAL_METRIC_TEMPLATES
        for year in config.YEARS
    ]


def split_and_trim_at_intersections(segments: gpd.GeoDataFrame) -> tuple:
    """
    Detect intersections and trim segment geometries around them.

    Returns (trimmed_single_or_no_junction, untouched_multi_junction):
    the first group needs connectivity grouping to be merged into arcs,
    the second group already forms a complete arc bounded by junctions
    on both ends.
    """
    intersections = utils.find_high_degree_points(
        segments, min_degree=config.MIN_INTERSECTION_DEGREE
    )
    trimmed, multi_junction = utils.trim_at_intersection_buffers(
        segments, intersections, buffer_m=config.ARC_TRIM_BUFFER_M
    )
    return trimmed, multi_junction


def assign_arc_groups(trimmed: gpd.GeoDataFrame, multi_junction: gpd.GeoDataFrame) -> pd.DataFrame:
    """Assign an `arc_group_id` to every segment."""
    trimmed = trimmed.copy()
    trimmed["arc_group_id"] = utils.group_by_shared_endpoints(trimmed)

    existing_ids = trimmed["arc_group_id"].str.extract(r"ARC_(\d+)").astype(int)
    next_id = int(existing_ids.max().iloc[0]) + 1 if not existing_ids.empty else 1

    multi_junction = multi_junction.copy()
    multi_junction["arc_group_id"] = [
        f"ARC_{i}" for i in range(next_id, next_id + len(multi_junction))
    ]

    return pd.concat([trimmed, multi_junction]).sort_index()


def aggregate_arc_attributes(segments: pd.DataFrame, metric_columns: list) -> pd.DataFrame:
    """Collapse each `arc_group_id` group of segments into a single arc record."""
    numeric_columns = [c for c in metric_columns if c in segments.columns]
    for col in numeric_columns:
        segments[col] = pd.to_numeric(segments[col], errors="coerce")

    code_attribute_rules = [
        ("road_class_code", "road_class", "min"),
        ("operational_status_code", "operational_status", "min"),
        ("road_surface_code", "road_surface", "min"),
        ("grade_separation_code", "grade_separation", "min"),
        ("carriageway_type_code", "carriageway_type", "max"),
    ]

    rows = []
    for arc_id, group in segments.groupby("arc_group_id"):
        row = {
            "arc_id": arc_id,
            "geometry": linemerge(group.geometry.tolist()),
            "source_segment_ids": group["segment_id"].tolist(),
        }

        for code_col, label_col, how in code_attribute_rules:
            if code_col in group.columns and label_col in group.columns:
                extreme = utils.pick_extreme_row(group, code_col, how=how)
                row[code_col] = extreme[code_col]
                row[label_col] = extreme[label_col]

        if numeric_columns:
            row.update(group[numeric_columns].max().to_dict())

        rows.append(row)

    return pd.DataFrame(rows)


def build_arcs(segments_path=None) -> gpd.GeoDataFrame:
    segments = load_segments(segments_path or config.RAW_SEGMENTS_PATH)
    trimmed, multi_junction = split_and_trim_at_intersections(segments)
    grouped = assign_arc_groups(trimmed, multi_junction)
    arcs = aggregate_arc_attributes(grouped, annual_metric_columns())
    return gpd.GeoDataFrame(arcs, geometry="geometry", crs=config.CRS_METRIC)


# ---------------------------------------------------------------------------
# 2. Public transport (TPL) flag
# ---------------------------------------------------------------------------
#
# The public-transport network (e.g. bus routes exported from GTFS shapes)
# rarely lines up exactly with the road network geometry, so a simple
# buffer-and-intersect join over-matches: any buffer wide enough to catch
# the real host road also catches side streets and alleys running
# alongside it, even though only the main road actually carries the line.
#
# To filter those false positives out, each transit route is buffered and
# the candidate arcs inside that buffer are checked for how they connect
# to one another: a side road typically has only one of its two endpoints
# touching the rest of the local network, while the road that actually
# carries the transit line is connected at both ends. Only arcs connected
# at both ends are marked as carrying public transport.

def load_transit_routes(path, target_crs) -> gpd.GeoDataFrame:
    routes = gpd.read_file(path)
    return routes.to_crs(target_crs)


def add_public_transport_flag(arcs: gpd.GeoDataFrame, routes: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    arcs = arcs.copy()
    arcs["has_public_transport"] = False
    spatial_index = arcs.sindex

    for _, route in routes.iterrows():
        route_buffer = route.geometry.buffer(config.TRANSIT_BUFFER_M, cap_style="flat")
        candidate_bbox = route.geometry.buffer(config.TRANSIT_CANDIDATE_BBOX_BUFFER_M)

        candidate_idx = list(spatial_index.intersection(candidate_bbox.bounds))
        candidates = arcs.iloc[candidate_idx].copy()
        intersecting = candidates[candidates.geometry.intersects(route_buffer)].copy()

        if intersecting.empty:
            continue

        intersecting["endpoints"] = intersecting.geometry.apply(utils.get_endpoint_points)

        for arc_idx, arc in intersecting.iterrows():
            if arcs.loc[arc_idx, "has_public_transport"]:
                continue

            endpoints = arc["endpoints"]
            if endpoints is None:
                continue

            other_endpoints = [
                pt
                for other_idx, pts in intersecting["endpoints"].items()
                if other_idx != arc_idx and pts is not None
                for pt in pts
            ]

            both_ends_connected = all(
                utils.is_endpoint_connected(
                    endpoint, other_endpoints, config.ENDPOINT_CONNECTIVITY_TOLERANCE_M
                )
                for endpoint in endpoints
            )

            if both_ends_connected:
                arcs.loc[arc_idx, "has_public_transport"] = True

    return arcs


# ---------------------------------------------------------------------------
# 3. Altitude
# ---------------------------------------------------------------------------
#
# The altitude at an arc's first and last point is sampled from a digital
# elevation raster and averaged. This is a lightweight proxy for the
# arc's overall elevation and avoids the cost of sampling every vertex
# along the line.

def sample_altitude(point: Point, raster) -> float:
    try:
        # masked=True masks the raster's nodata value and points outside it
        value = next(raster.sample([(point.x, point.y)], masked=True))[0]
    except (ValueError, StopIteration, IndexError):
        return None

    if np.ma.is_masked(value) or not np.isfinite(value):
        return None
    return float(value)


def get_arc_endpoints(geometry):
    """Return the (first, last) points of a LineString, or of the first
    part of a MultiLineString, for altitude sampling."""
    if geometry is None:
        return None

    if geometry.geom_type == "MultiLineString":
        geometry = list(geometry.geoms)[0]

    if geometry.geom_type != "LineString":
        return None

    return Point(geometry.coords[0]), Point(geometry.coords[-1])


def add_average_altitude(arcs: gpd.GeoDataFrame, raster_path) -> gpd.GeoDataFrame:
    arcs = arcs.copy()
    averages = []

    with rasterio.open(raster_path) as raster:
        # sample in the raster's own CRS, which may differ from CRS_METRIC
        geometries = arcs.geometry.to_crs(raster.crs) if raster.crs else arcs.geometry
        for geometry in geometries:
            endpoints = get_arc_endpoints(geometry)
            if endpoints is None:
                averages.append(None)
                continue

            first_altitude = sample_altitude(endpoints[0], raster)
            last_altitude = sample_altitude(endpoints[1], raster)

            if first_altitude is None or last_altitude is None:
                averages.append(None)
            else:
                averages.append((first_altitude + last_altitude) / 2)

    arcs["avg_altitude_m"] = averages
    arcs["avg_altitude_m"] = arcs["avg_altitude_m"].replace([np.inf, -np.inf], np.nan)
    return arcs


# ---------------------------------------------------------------------------
# 4. Tortuosity
# ---------------------------------------------------------------------------
#
# Tortuosity is the ratio of an arc's actual length to the straight-line
# (Euclidean) distance between its start and end points. A value of 1
# means the arc is perfectly straight; higher values indicate a more
# winding path.

MIN_STRAIGHT_LINE_DISTANCE_M = 0.01  # avoids division by zero for closed loops


def compute_tortuosity(geometry) -> float:
    if not isinstance(geometry, LineString):
        return None

    start = Point(geometry.coords[0])
    end = Point(geometry.coords[-1])

    straight_line_distance = start.distance(end)
    if straight_line_distance == 0:
        straight_line_distance = MIN_STRAIGHT_LINE_DISTANCE_M

    return geometry.length / straight_line_distance


def add_tortuosity(arcs: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    arcs = arcs.copy()
    arcs["tortuosity"] = arcs.geometry.apply(compute_tortuosity)
    return arcs


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

def main():
    arcs = build_arcs()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=UserWarning)
        routes = load_transit_routes(config.TRANSIT_ROUTES_PATH, arcs.crs)
    arcs = add_public_transport_flag(arcs, routes)

    arcs = add_average_altitude(arcs, config.ELEVATION_RASTER_PATH)
    arcs = add_tortuosity(arcs)

    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    arcs.to_file(config.ARCS_PATH, driver="GPKG")
    arcs.to_csv(config.ARCS_PATH.with_suffix(".csv"), index=False)

    print(f"Wrote {len(arcs)} arcs to {config.ARCS_PATH}")
    print(f"  - {arcs['has_public_transport'].sum()} carry public transport")
    print(f"  - {arcs['avg_altitude_m'].isna().sum()} missing an altitude sample")


if __name__ == "__main__":
    main()
