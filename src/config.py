"""
Central configuration for the road-network arc & intersection processing pipeline.

Edit the paths and constants below before running any step. No real data is
distributed with this repository (the source road-network data belongs to a
third party); point INPUT_* at your own copies of a similarly-structured
dataset.

Expected input schema
----------------------
The pipeline was designed around a road-network layer shaped like Italy's
regional BDTRE geo-topographic database: a LineString per road segment, a
unique identifier per segment, and a handful of descriptive attribute codes
(functional class, surface, carriageway type, grade separation, operational
status, road type). If your own dataset uses different column names, adjust
RAW_SEGMENT_COLUMN_MAP below rather than editing the step scripts.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Paths (placeholders -- replace with your own files; none are included here)
# ---------------------------------------------------------------------------
DATA_DIR = Path("data")
OUTPUT_DIR = Path("output")

# Step 1 input: raw road-segment layer (LineString per segment)
RAW_SEGMENTS_PATH = DATA_DIR / "road_segments.gpkg"

# Step 1 output / steps 2-4 input-output: one row per merged arc
ARCS_PATH = OUTPUT_DIR / "arcs.gpkg"

# Step 2 input: public-transport route layer (e.g. exported from GTFS shapes)
TRANSIT_ROUTES_PATH = DATA_DIR / "public_transport_routes.shp"

# Step 3 input: digital elevation raster covering the study area
ELEVATION_RASTER_PATH = DATA_DIR / "elevation.tif"

# Step 5 input: raw road-segment layer used to detect roundabouts and
# intersections (can be the same file as RAW_SEGMENTS_PATH)
INTERSECTION_SOURCE_PATH = DATA_DIR / "road_segments.gpkg"

# Step 5 output: one row per classified intersection node
NODES_PATH = OUTPUT_DIR / "intersection_nodes.gpkg"

# ---------------------------------------------------------------------------
# Column names on the raw input segment layer -> generalized English names
# used throughout this codebase. Adjust the keys (left side) to match your
# own source data; the values (right side) are the names every script here
# expects to find.
# ---------------------------------------------------------------------------
RAW_SEGMENT_COLUMN_MAP = {
    "uuid": "segment_id",
    "el_str_cf": "road_class",
    "cod_el_str_cf": "road_class_code",
    "el_str_fon": "road_surface",
    "cod_el_str_fon": "road_surface_code",
    "el_str_sed": "carriageway_type",
    "cod_el_str_sed": "carriageway_type_code",
    "el_str_liv": "grade_separation",
    "cod_el_str_liv": "grade_separation_code",
    "el_str_sta": "operational_status",
    "cod_el_str_sta": "operational_status_code",
    "el_str_ty": "road_type",
}

# Attribute codes used to identify roundabout segments in the raw layer
# (see step5). Adjust to match the value used by your own data source.
ROUNDABOUT_ROAD_TYPE_VALUE = "roundabout"

# ---------------------------------------------------------------------------
# Optional per-year numeric indicators (e.g. proprietary annual traffic
# metrics) that should simply be carried through and aggregated when
# segments are merged into arcs, and later summarized per intersection node.
# Populate YEARS with whatever years your own indicator columns cover, or
# leave empty if you have none.
# ---------------------------------------------------------------------------
YEARS = [2018, 2019, 2020, 2021, 2022, 2023]

# Column-name templates for the optional annual indicators described above.
# {year} is substituted for each entry in YEARS.
ANNUAL_METRIC_TEMPLATES = {
    "traffic_length_sum_{year}": "mfwl_sum_{year}",
    "traffic_point_sum_{year}": "mfwp_sum_{year}",
    "traffic_volume_{year}": "mfw_sum_{year}",
    "traffic_point_pct_{year}": "mfwp_perc_{year}",
}

# The single annual indicator used later to summarize traffic per
# intersection node (step 5). Must be one of the generalized names above.
NODE_TRAFFIC_METRIC = "traffic_volume_{year}"

# Column on the raw layer used to keep only the current edition of the
# dataset (set to None to skip this filter).
CURRENT_EDITION_FLAG_COLUMN = None  # e.g. "valid_2023"

# ---------------------------------------------------------------------------
# Geometric parameters
# ---------------------------------------------------------------------------
CRS_METRIC = "EPSG:32632"  # projected CRS used for all distance/buffer math

MIN_INTERSECTION_DEGREE = 3       # endpoints shared by >= this many segments
ARC_TRIM_BUFFER_M = 1             # buffer used to trim segments at intersections
TRANSIT_BUFFER_M = 15             # buffer around transit routes to find candidate arcs
TRANSIT_CANDIDATE_BBOX_BUFFER_M = 50  # coarse bbox buffer for spatial pre-filtering

# Because arcs are trimmed apart by ARC_TRIM_BUFFER_M at every real
# intersection (step 1), two different arcs meeting at the same
# intersection no longer share an exact endpoint -- each one stops a
# little short, on its own side of the trim buffer. Two such endpoints can
# be up to 2 * ARC_TRIM_BUFFER_M apart and still represent the same
# junction, so the endpoint-connectivity check in step 2 needs a tolerance
# at least that large to recognize them as connected.
ENDPOINT_CONNECTIVITY_TOLERANCE_M = 2 * ARC_TRIM_BUFFER_M + 0.1

ROUNDABOUT_BUFFER_M = 25          # buffer used to dissolve roundabout segments into a node polygon
SIMPLE_NODE_BUFFER_M = 25         # buffer used to find arcs touching a simple-node point
GROUPED_SIMPLE_NODE_DISTANCE_M = 25  # max spacing between simple nodes merged into one logical node
