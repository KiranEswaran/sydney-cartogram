#!/usr/bin/env python3
"""Build compact Sydney public-transport cartogram data from static TfNSW GTFS."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
import time
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"

BOUNDARIES_PATH = DATA_DIR / "sydney_lga_boundaries.geojson"
PARKS_PATH = DATA_DIR / "parks_open_space.geojson"
STREETS_PATH = DATA_DIR / "osm_major_streets.geojson"
GTFS_PATH = DATA_DIR / "tfnsw_gtfs_complete.zip"
OUTPUT_PATH = ROOT / "site" / "data" / "commute_map_data.json"

SYDNEY_BBOX = {
    "min_lon": 150.45,
    "min_lat": -34.25,
    "max_lon": 151.45,
    "max_lat": -33.35,
}

GRID_COLS = 160
GRID_ROWS = 160
MIN_PARK_AREA = 70_000.0
MAX_SHAPES_PER_ROUTE_DIRECTION = 1
MAX_SHAPE_POINTS = 140

WALK_METERS_PER_MINUTE = 80.0
MAX_WALK_TO_TRANSIT_METERS = 900.0
MAX_TRANSFER_WALK_METERS = 450.0
MAX_TRANSFER_NEIGHBOURS_PER_STOP = 8
MAX_ORIGIN_ACCESS_STOPS = 20
CELL_NEAREST_STOPS = 12
DEFAULT_TRANSFER_PENALTY_MINUTES = 5.0
DEFAULT_BOARD_WAIT = 0.0
STATION_ACCESS_PENALTY = 0.0

# TfNSW's current static bundle uses several extended route_type values.
# 700 is regular bus. 712 school buses and 714 temporary replacement buses are
# excluded from this representative "typical access" v1 graph.
ROUTE_TYPE_TO_MODE = {
    "0": "light_rail",
    "1": "metro",
    "2": "rail",
    "3": "bus",
    "4": "ferry",
    "11": "bus",
    "401": "metro",
    "700": "bus",
    "900": "light_rail",
}

WAIT_PENALTY_BY_MODE = {
    "light_rail": 5.0,
    "metro": 5.0,
    "rail": 5.0,
    "bus": 7.0,
    "ferry": 10.0,
}

MODE_LABELS = {
    "rail": "Rail",
    "metro": "Metro",
    "light_rail": "Light rail",
    "bus": "Bus",
    "ferry": "Ferry",
}

MODE_FALLBACK_COLORS = {
    "rail": "F99D1C",
    "metro": "168388",
    "light_rail": "DD1E25",
    "bus": "00B5EF",
    "ferry": "5AB031",
}

Point = tuple[float, float]
Ring = list[Point]
Polygon = list[Ring]
MultiPolygon = list[Polygon]


def load_json(path: Path) -> dict | list:
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv_from_zip(gtfs_path: Path, member: str) -> Iterable[dict[str, str]]:
    with zipfile.ZipFile(gtfs_path) as archive:
        with archive.open(member) as handle:
            reader = csv.DictReader(line.decode("utf-8-sig") for line in handle)
            yield from reader


def lonlat_to_xy(lon: float, lat: float, lat0: float) -> Point:
    meters_per_deg_lat = 111_320.0
    meters_per_deg_lon = meters_per_deg_lat * math.cos(math.radians(lat0))
    return lon * meters_per_deg_lon, lat * meters_per_deg_lat


def round_point(point: Point) -> list[float]:
    return [round(point[0], 1), round(point[1], 1)]


def round_path(points: Sequence[Point]) -> list[list[float]]:
    return [round_point(point) for point in points]


def point_in_sydney_bbox(lon: float, lat: float) -> bool:
    return (
        SYDNEY_BBOX["min_lon"] <= lon <= SYDNEY_BBOX["max_lon"]
        and SYDNEY_BBOX["min_lat"] <= lat <= SYDNEY_BBOX["max_lat"]
    )


def bbox_world(lat0: float) -> tuple[float, float, float, float]:
    min_x, min_y = lonlat_to_xy(SYDNEY_BBOX["min_lon"], SYDNEY_BBOX["min_lat"], lat0)
    max_x, max_y = lonlat_to_xy(SYDNEY_BBOX["max_lon"], SYDNEY_BBOX["max_lat"], lat0)
    return min_x, min_y, max_x, max_y


def ring_area(ring: Sequence[Point]) -> float:
    area = 0.0
    for index, point in enumerate(ring):
        nxt = ring[(index + 1) % len(ring)]
        area += point[0] * nxt[1] - nxt[0] * point[1]
    return area / 2.0


def polygon_centroid(ring: Sequence[Point]) -> Point:
    area = ring_area(ring) or 1.0
    cx = 0.0
    cy = 0.0
    factor = 1.0 / (6.0 * area)
    for index, point in enumerate(ring):
        nxt = ring[(index + 1) % len(ring)]
        cross = point[0] * nxt[1] - nxt[0] * point[1]
        cx += (point[0] + nxt[0]) * cross
        cy += (point[1] + nxt[1]) * cross
    return cx * factor, cy * factor


def simplify_polyline(points: Sequence[Point], min_distance: float) -> list[Point]:
    if len(points) <= 2:
        return list(points)
    simplified = [points[0]]
    for point in points[1:-1]:
        if math.hypot(point[0] - simplified[-1][0], point[1] - simplified[-1][1]) >= min_distance:
            simplified.append(point)
    if points[-1] != simplified[-1]:
        simplified.append(points[-1])
    return simplified


def simplify_ring(ring: Sequence[Point], min_distance: float) -> Ring:
    if len(ring) <= 4:
        return list(ring)
    core = list(ring[:-1]) if ring[0] == ring[-1] else list(ring)
    simplified = [core[0]]
    for point in core[1:]:
        if math.hypot(point[0] - simplified[-1][0], point[1] - simplified[-1][1]) >= min_distance:
            simplified.append(point)
    if len(simplified) < 3:
        simplified = core[:3]
    simplified.append(simplified[0])
    return simplified


def bounds_of_points(points: Sequence[Point]) -> tuple[float, float, float, float]:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def bounds_of_multipolygon(multipolygon: MultiPolygon) -> tuple[float, float, float, float]:
    return bounds_of_points([point for polygon in multipolygon for ring in polygon for point in ring])


def bbox_intersects(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def point_in_ring(point: Point, ring: Sequence[Point]) -> bool:
    x, y = point
    inside = False
    j = len(ring) - 1
    for i, item in enumerate(ring):
        xi, yi = item
        xj, yj = ring[j]
        intersects = (yi > y) != (yj > y)
        if intersects:
            x_hit = (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
            if x < x_hit:
                inside = not inside
        j = i
    return inside


def point_in_polygon(point: Point, polygon: Polygon) -> bool:
    if not polygon or not point_in_ring(point, polygon[0]):
        return False
    return not any(point_in_ring(point, hole) for hole in polygon[1:])


def point_in_multipolygon(point: Point, multipolygon: MultiPolygon) -> bool:
    return any(point_in_polygon(point, polygon) for polygon in multipolygon)


def geojson_polygons(geometry: dict) -> list:
    if not geometry:
        return []
    if geometry["type"] == "Polygon":
        return [geometry["coordinates"]]
    if geometry["type"] == "MultiPolygon":
        return geometry["coordinates"]
    return []


def iter_lon_lat(payload: dict) -> Iterable[tuple[float, float]]:
    for feature in payload.get("features", []):
        for polygon in geojson_polygons(feature.get("geometry")):
            for ring in polygon:
                for lon, lat, *_ in ring:
                    yield float(lon), float(lat)


def average_geojson_latitude(payload: dict) -> float:
    total = 0.0
    count = 0
    for _lon, lat in iter_lon_lat(payload):
        total += lat
        count += 1
    if count:
        return total / count
    return (SYDNEY_BBOX["min_lat"] + SYDNEY_BBOX["max_lat"]) / 2


def feature_name(feature: dict) -> str:
    props = feature.get("properties") or {}
    for key in ("GCCSA_NAME_2021", "LGA_NAME_2024", "LGA_NAME_2023", "LGA_NAME_2021", "name", "Name"):
        if props.get(key):
            return str(props[key])
    return "Greater Sydney"


def extract_boundaries(lat0: float) -> tuple[list[dict], MultiPolygon, bool]:
    if not BOUNDARIES_PATH.exists():
        min_x, min_y, max_x, max_y = bbox_world(lat0)
        ring = [(min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y), (min_x, min_y)]
        polygon = [ring]
        return (
            [{"name": "Greater Sydney", "label": round_point(polygon_centroid(ring)), "polygons": [[round_path(ring)]]}],
            [polygon],
            False,
        )

    payload = load_json(BOUNDARIES_PATH)
    areas = []
    all_polygons: MultiPolygon = []
    for feature in payload.get("features", []):
        multipolygon: MultiPolygon = []
        for polygon_coords in geojson_polygons(feature.get("geometry")):
            polygon: Polygon = []
            for ring_coords in polygon_coords:
                ring = [lonlat_to_xy(float(lon), float(lat), lat0) for lon, lat, *_ in ring_coords]
                if len(ring) >= 4:
                    polygon.append(simplify_ring(ring, 160.0))
            if polygon:
                multipolygon.append(polygon)
                all_polygons.append(polygon)
        if not multipolygon:
            continue
        largest_polygon = max(multipolygon, key=lambda polygon: abs(ring_area(polygon[0])))
        areas.append(
            {
                "name": feature_name(feature),
                "label": round_point(polygon_centroid(largest_polygon[0])),
                "polygons": [[round_path(ring) for ring in polygon] for polygon in multipolygon],
            }
        )
    return areas, all_polygons, True


def extract_parks(lat0: float, bbox: tuple[float, float, float, float]) -> tuple[list, bool]:
    if not PARKS_PATH.exists():
        return [], False
    payload = load_json(PARKS_PATH)
    parks = []
    for feature in payload.get("features", []):
        props = feature.get("properties") or {}
        try:
            area = float(props.get("shape_area") or props.get("area") or props.get("AREA") or 0)
        except (TypeError, ValueError):
            area = 0.0
        for polygon_coords in geojson_polygons(feature.get("geometry")):
            polygon: Polygon = []
            for ring_coords in polygon_coords:
                ring = [lonlat_to_xy(float(lon), float(lat), lat0) for lon, lat, *_ in ring_coords]
                if len(ring) >= 4:
                    polygon.append(simplify_ring(ring, 120.0))
            if not polygon or not bbox_intersects(bounds_of_points(polygon[0]), bbox):
                continue
            if area and area < MIN_PARK_AREA:
                continue
            parks.append([round_path(ring) for ring in polygon])
    return parks, bool(parks)


def extract_streets(lat0: float, bbox: tuple[float, float, float, float]) -> tuple[list, bool]:
    if not STREETS_PATH.exists():
        return [], False
    payload = load_json(STREETS_PATH)
    streets = []
    allowed = {"motorway", "trunk", "primary", "secondary"}
    for feature in payload.get("features", []):
        props = feature.get("properties") or {}
        kind = props.get("highway") or props.get("class") or props.get("type")
        if kind not in allowed:
            continue
        geometry = feature.get("geometry") or {}
        lines = []
        if geometry.get("type") == "LineString":
            lines = [geometry.get("coordinates") or []]
        elif geometry.get("type") == "MultiLineString":
            lines = geometry.get("coordinates") or []
        for line in lines:
            points = [lonlat_to_xy(float(lon), float(lat), lat0) for lon, lat, *_ in line]
            if len(points) < 2 or not bbox_intersects(bounds_of_points(points), bbox):
                continue
            streets.append(
                {
                    "kind": kind,
                    "name": props.get("name") or "",
                    "points": round_path(simplify_polyline(points, 220.0)),
                }
            )
    return streets, bool(streets)


def normalize_color(value: str, mode: str, route_id: str) -> str:
    raw = (value or "").strip().strip("#")
    if len(raw) == 6 and all(char in "0123456789abcdefABCDEF" for char in raw):
        return f"#{raw.upper()}"
    fallback = MODE_FALLBACK_COLORS.get(mode, "808183")
    digest = hashlib.md5(route_id.encode("utf-8")).hexdigest()
    # Blend mode color with a stable route hash so missing colors are distinct.
    base = tuple(int(fallback[i : i + 2], 16) for i in (0, 2, 4))
    tint = tuple(int(digest[i : i + 2], 16) for i in (0, 2, 4))
    mixed = tuple(round(base[index] * 0.72 + tint[index] * 0.28) for index in range(3))
    return "#" + "".join(f"{value:02X}" for value in mixed)


def parse_gtfs_time(value: str) -> int | None:
    try:
        hours, minutes, seconds = (int(part) for part in value.split(":"))
    except (AttributeError, ValueError):
        return None
    return hours * 3600 + minutes * 60 + seconds


def load_routes() -> tuple[dict[str, dict], Counter]:
    routes = {}
    skipped = Counter()
    for row in read_csv_from_zip(GTFS_PATH, "routes.txt"):
        raw_type = row.get("route_type") or ""
        mode = ROUTE_TYPE_TO_MODE.get(raw_type)
        if not mode:
            skipped[raw_type] += 1
            continue
        route_id = row["route_id"]
        label = row.get("route_short_name") or row.get("route_long_name") or route_id
        routes[route_id] = {
            "route_id": route_id,
            "agency_id": row.get("agency_id") or "",
            "route_short_name": row.get("route_short_name") or "",
            "route_long_name": row.get("route_long_name") or "",
            "route_type": raw_type,
            "mode": mode,
            "mode_label": MODE_LABELS[mode],
            "route_color": normalize_color(row.get("route_color") or "", mode, route_id),
            "route_text_color": f"#{(row.get('route_text_color') or 'FFFFFF').strip('#')[:6] or 'FFFFFF'}",
            "label": label,
        }
    return routes, skipped


def load_stops(lat0: float) -> tuple[dict[str, dict], dict[str, dict], dict[str, str], set[str]]:
    raw_stops = {}
    parent_rows = {}
    inside_stop_ids = set()
    for row in read_csv_from_zip(GTFS_PATH, "stops.txt"):
        try:
            lat = float(row["stop_lat"])
            lon = float(row["stop_lon"])
        except (KeyError, TypeError, ValueError):
            continue
        stop_id = row["stop_id"]
        info = {
            "id": stop_id,
            "name": row.get("stop_name") or stop_id,
            "lat": lat,
            "lon": lon,
            "point": lonlat_to_xy(lon, lat, lat0),
            "location_type": row.get("location_type") or "",
            "parent_station": row.get("parent_station") or None,
            "wheelchair_boarding": row.get("wheelchair_boarding") or "",
            "platform_code": row.get("platform_code") or "",
        }
        raw_stops[stop_id] = info
        if info["location_type"] == "1":
            parent_rows[stop_id] = info
        if point_in_sydney_bbox(lon, lat):
            inside_stop_ids.add(stop_id)

    node_for_stop = {}
    for stop_id in inside_stop_ids:
        stop = raw_stops[stop_id]
        parent_id = stop.get("parent_station")
        if parent_id and parent_id in raw_stops:
            node_for_stop[stop_id] = parent_id
        else:
            node_for_stop[stop_id] = stop_id
    return raw_stops, parent_rows, node_for_stop, inside_stop_ids


def load_trips(routes: dict[str, dict]) -> tuple[dict[str, dict], dict[tuple[str, str], Counter[str]], int]:
    trips = {}
    shape_counts: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for row in read_csv_from_zip(GTFS_PATH, "trips.txt"):
        route_id = row.get("route_id") or ""
        if route_id not in routes:
            continue
        trip_id = row["trip_id"]
        trips[trip_id] = {
            "route_id": route_id,
            "shape_id": row.get("shape_id") or "",
            "direction_id": row.get("direction_id") or "0",
            "service_id": row.get("service_id") or "",
        }
        shape_id = row.get("shape_id") or ""
        if shape_id:
            shape_counts[(route_id, row.get("direction_id") or "0")][shape_id] += 1
    return trips, shape_counts, len(trips)


def stop_node_info(node_id: str, raw_stops: dict[str, dict], children_by_parent: dict[str, list[str]]) -> dict:
    source = raw_stops[node_id]
    child_ids = children_by_parent.get(node_id) or [node_id]
    child_points = [raw_stops[child_id]["point"] for child_id in child_ids if child_id in raw_stops]
    if source.get("location_type") == "1" and child_points:
        point = (
            sum(point[0] for point in child_points) / len(child_points),
            sum(point[1] for point in child_points) / len(child_points),
        )
    else:
        point = source["point"]
    return {
        "id": node_id,
        "name": source["name"],
        "point": point,
        "stop_ids": sorted(child_ids),
        "routes": set(),
        "modes": set(),
    }


def build_transit_edges(
    trips: dict[str, dict],
    routes: dict[str, dict],
    raw_stops: dict[str, dict],
    node_for_stop: dict[str, str],
) -> tuple[dict[tuple[str, str, str], list[float]], dict[str, dict], Counter, int]:
    durations_by_edge: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    parse_failures = Counter()
    node_route_membership: dict[str, dict] = defaultdict(lambda: {"routes": set(), "modes": set()})
    children_by_parent: dict[str, list[str]] = defaultdict(list)
    for stop_id, node_id in node_for_stop.items():
        children_by_parent[node_id].append(stop_id)

    current_trip_id = None
    current_rows: list[dict[str, str]] = []
    trips_with_edges = 0

    def process_trip(trip_id: str | None, rows: list[dict[str, str]]) -> None:
        nonlocal trips_with_edges
        if not trip_id or len(rows) < 2:
            return
        trip = trips.get(trip_id)
        if not trip:
            return
        route_id = trip["route_id"]
        route = routes[route_id]
        ordered = sorted(rows, key=lambda item: int(item.get("stop_sequence") or 0))
        local_edges = 0
        for row in ordered:
            node_id = node_for_stop.get(row.get("stop_id") or "")
            if not node_id:
                continue
            node_route_membership[node_id]["routes"].add(route_id)
            node_route_membership[node_id]["modes"].add(route["mode"])
        for prev, nxt in zip(ordered, ordered[1:]):
            from_node = node_for_stop.get(prev.get("stop_id") or "")
            to_node = node_for_stop.get(nxt.get("stop_id") or "")
            if not from_node or not to_node or from_node == to_node:
                continue
            departure = parse_gtfs_time(prev.get("departure_time") or prev.get("arrival_time") or "")
            arrival = parse_gtfs_time(nxt.get("arrival_time") or nxt.get("departure_time") or "")
            if departure is None or arrival is None:
                parse_failures["bad_time"] += 1
                continue
            duration_seconds = arrival - departure
            if duration_seconds < 0:
                parse_failures["negative_duration"] += 1
                continue
            if not 10 <= duration_seconds <= 90 * 60:
                parse_failures["implausible_duration"] += 1
                continue
            durations_by_edge[(from_node, to_node, route_id)].append(duration_seconds / 60.0)
            local_edges += 1
        if local_edges:
            trips_with_edges += 1

    for row in read_csv_from_zip(GTFS_PATH, "stop_times.txt"):
        trip_id = row.get("trip_id")
        if trip_id not in trips:
            continue
        if current_trip_id is None:
            current_trip_id = trip_id
        if trip_id != current_trip_id:
            process_trip(current_trip_id, current_rows)
            current_trip_id = trip_id
            current_rows = []
        if row.get("stop_id") in node_for_stop:
            current_rows.append(row)
    process_trip(current_trip_id, current_rows)

    nodes = {
        node_id: stop_node_info(node_id, raw_stops, children_by_parent)
        for node_id in node_route_membership
        if node_id in raw_stops
    }
    for node_id, membership in node_route_membership.items():
        if node_id not in nodes:
            continue
        nodes[node_id]["routes"].update(membership["routes"])
        nodes[node_id]["modes"].update(membership["modes"])

    return durations_by_edge, nodes, parse_failures, trips_with_edges


def build_route_shapes(
    lat0: float,
    bbox: tuple[float, float, float, float],
    routes: dict[str, dict],
    shape_counts: dict[tuple[str, str], Counter[str]],
) -> list[dict]:
    selected_shape_ids = {}
    for (route_id, _direction), counter in shape_counts.items():
        if route_id not in routes:
            continue
        for shape_id, _count in counter.most_common(MAX_SHAPES_PER_ROUTE_DIRECTION):
            selected_shape_ids[shape_id] = route_id

    points_by_shape: dict[str, list[tuple[int, Point]]] = defaultdict(list)
    for row in read_csv_from_zip(GTFS_PATH, "shapes.txt"):
        shape_id = row.get("shape_id") or ""
        route_id = selected_shape_ids.get(shape_id)
        if not route_id:
            continue
        try:
            lat = float(row["shape_pt_lat"])
            lon = float(row["shape_pt_lon"])
            sequence = int(float(row["shape_pt_sequence"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not (
            SYDNEY_BBOX["min_lon"] - 0.05 <= lon <= SYDNEY_BBOX["max_lon"] + 0.05
            and SYDNEY_BBOX["min_lat"] - 0.05 <= lat <= SYDNEY_BBOX["max_lat"] + 0.05
        ):
            continue
        points_by_shape[shape_id].append((sequence, lonlat_to_xy(lon, lat, lat0)))

    shapes = []
    seen = set()
    for shape_id, route_id in selected_shape_ids.items():
        points = [point for _sequence, point in sorted(points_by_shape.get(shape_id, []))]
        if len(points) < 2:
            continue
        route = routes[route_id]
        simplify_distance = 450.0 if route["mode"] == "bus" else 120.0
        points = simplify_polyline(points, simplify_distance)
        if len(points) > MAX_SHAPE_POINTS:
            stride = math.ceil(len(points) / MAX_SHAPE_POINTS)
            points = points[::stride]
            if points[-1] != points_by_shape[shape_id][-1][1]:
                points.append(points_by_shape[shape_id][-1][1])
        if len(points) < 2 or not bbox_intersects(bounds_of_points(points), bbox):
            continue
        key = (route_id, tuple(round_point(points[0])), tuple(round_point(points[-1])))
        if key in seen:
            continue
        seen.add(key)
        shapes.append(
            {
                "routeId": route_id,
                "mode": route["mode"],
                "color": route["route_color"],
                "textColor": route["route_text_color"],
                "label": route["label"],
                "points": round_path(points),
            }
        )
    return shapes


def build_graph(
    nodes_by_id: dict[str, dict],
    durations_by_edge: dict[tuple[str, str, str], list[float]],
    routes: dict[str, dict],
) -> tuple[list[dict], list[list[int]], list[list[list[float]]], dict[str, float], int]:
    active_node_ids = sorted(nodes_by_id, key=lambda node_id: nodes_by_id[node_id]["name"])
    node_index_by_id = {node_id: index for index, node_id in enumerate(active_node_ids)}
    stations = [nodes_by_id[node_id] for node_id in active_node_ids]

    route_waits = {
        route_id: WAIT_PENALTY_BY_MODE[routes[route_id]["mode"]]
        for route_id in routes
    }

    route_states = []
    state_index_by_key = {}
    station_states: list[list[int]] = [[] for _ in stations]
    for station_index, station in enumerate(stations):
        for route_id in sorted(station["routes"]):
            state_index_by_key[(station_index, route_id)] = len(route_states)
            route_states.append({"stationIndex": station_index, "routeId": route_id})
            station_states[station_index].append(len(route_states) - 1)

    adjacency_maps: list[dict[int, float]] = [dict() for _ in route_states]

    def upsert(from_state: int, to_state: int, weight: float) -> None:
        existing = adjacency_maps[from_state].get(to_state)
        if existing is None or weight < existing:
            adjacency_maps[from_state][to_state] = round(weight, 2)

    for (from_node, to_node, route_id), durations in durations_by_edge.items():
        if from_node not in node_index_by_id or to_node not in node_index_by_id:
            continue
        from_station = node_index_by_id[from_node]
        to_station = node_index_by_id[to_node]
        from_state = state_index_by_key.get((from_station, route_id))
        to_state = state_index_by_key.get((to_station, route_id))
        if from_state is None or to_state is None:
            continue
        upsert(from_state, to_state, statistics.median(durations))

    for station_index, states in enumerate(station_states):
        for from_state in states:
            for to_state in states:
                if from_state == to_state:
                    continue
                to_route = route_states[to_state]["routeId"]
                upsert(from_state, to_state, DEFAULT_TRANSFER_PENALTY_MINUTES + route_waits[to_route])

    walking_transfer_edges = add_walking_transfers(stations, station_states, route_states, route_waits, adjacency_maps)
    adjacency = [
        [[to_index, weight] for to_index, weight in sorted(edges.items())]
        for edges in adjacency_maps
    ]
    return route_states, station_states, adjacency, route_waits, walking_transfer_edges


def add_walking_transfers(
    stations: list[dict],
    station_states: list[list[int]],
    route_states: list[dict],
    route_waits: dict[str, float],
    adjacency_maps: list[dict[int, float]],
) -> int:
    bucket_size = MAX_TRANSFER_WALK_METERS
    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, station in enumerate(stations):
        x, y = station["point"]
        buckets[(math.floor(x / bucket_size), math.floor(y / bucket_size))].append(index)

    def upsert(from_state: int, to_state: int, weight: float) -> None:
        existing = adjacency_maps[from_state].get(to_state)
        if existing is None or weight < existing:
            adjacency_maps[from_state][to_state] = round(weight, 2)

    walking_edges = 0
    for source_index, station in enumerate(stations):
        sx, sy = station["point"]
        bx = math.floor(sx / bucket_size)
        by = math.floor(sy / bucket_size)
        candidates = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for target_index in buckets.get((bx + dx, by + dy), []):
                    if target_index == source_index:
                        continue
                    tx, ty = stations[target_index]["point"]
                    meters = math.hypot(tx - sx, ty - sy)
                    if meters <= MAX_TRANSFER_WALK_METERS:
                        candidates.append((meters, target_index))
        candidates.sort(key=lambda item: item[0])
        for meters, target_index in candidates[:MAX_TRANSFER_NEIGHBOURS_PER_STOP]:
            walk_minutes = meters / WALK_METERS_PER_MINUTE
            for from_state in station_states[source_index]:
                for to_state in station_states[target_index]:
                    to_route = route_states[to_state]["routeId"]
                    upsert(
                        from_state,
                        to_state,
                        walk_minutes + DEFAULT_TRANSFER_PENALTY_MINUTES + route_waits[to_route],
                    )
                    walking_edges += 1
    return walking_edges


def nearest_station_indexes(
    point: Point,
    stations: list[dict],
    buckets: dict[tuple[int, int], list[int]],
    radius: float,
    limit: int,
) -> list[int]:
    bx = math.floor(point[0] / radius)
    by = math.floor(point[1] / radius)
    candidates = []
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for station_index in buckets.get((bx + dx, by + dy), []):
                station = stations[station_index]
                meters = math.hypot(station["point"][0] - point[0], station["point"][1] - point[1])
                if meters <= radius:
                    candidates.append((meters, station_index))
    candidates.sort(key=lambda item: item[0])
    return [station_index for _meters, station_index in candidates[:limit]]


def build_grid_cells(polygons: MultiPolygon, stations: list[dict], bbox: tuple[float, float, float, float]) -> tuple[list, list]:
    min_x, min_y, max_x, max_y = bbox
    cell_w = (max_x - min_x) / GRID_COLS
    cell_h = (max_y - min_y) / GRID_ROWS
    bucket_radius = MAX_WALK_TO_TRANSIT_METERS
    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, station in enumerate(stations):
        x, y = station["point"]
        buckets[(math.floor(x / bucket_radius), math.floor(y / bucket_radius))].append(index)

    cells = []
    mask = []
    for row in range(GRID_ROWS):
        for col in range(GRID_COLS):
            point = (min_x + (col + 0.5) * cell_w, min_y + (row + 0.5) * cell_h)
            if not point_in_multipolygon(point, polygons):
                mask.append(-1)
                continue
            access = nearest_station_indexes(point, stations, buckets, bucket_radius, CELL_NEAREST_STOPS)
            cells.append(
                {
                    "col": col,
                    "row": row,
                    "point": round_point(point),
                    "access": [[station_index, 0] for station_index in access],
                }
            )
            mask.append(len(cells) - 1)
    return cells, mask


def output_route_styles(routes: dict[str, dict]) -> dict[str, dict]:
    return {
        route_id: {
            "agencyId": route["agency_id"],
            "routeShortName": route["route_short_name"],
            "routeLongName": route["route_long_name"],
            "routeType": route["route_type"],
            "mode": route["mode"],
            "color": route["route_color"],
            "textColor": route["route_text_color"],
            "label": route["label"],
        }
        for route_id, route in routes.items()
    }


def file_size_label(path: Path) -> str:
    size = path.stat().st_size
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024
    return f"{size:.1f} GB"


def main() -> None:
    started = time.perf_counter()
    if not GTFS_PATH.exists():
        raise FileNotFoundError(f"Missing TfNSW GTFS ZIP at {GTFS_PATH}")

    boundary_payload = load_json(BOUNDARIES_PATH) if BOUNDARIES_PATH.exists() else {"features": []}
    lat0 = average_geojson_latitude(boundary_payload)
    areas, all_polygons, boundary_loaded = extract_boundaries(lat0)
    bbox = bounds_of_multipolygon(all_polygons) if boundary_loaded else bbox_world(lat0)
    parks, parks_loaded = extract_parks(lat0, bbox)
    streets, streets_loaded = extract_streets(lat0, bbox)

    routes, skipped_route_types = load_routes()
    raw_stops, _parent_rows, node_for_stop, _inside_stop_ids = load_stops(lat0)
    trips, shape_counts, selected_trip_count = load_trips(routes)
    durations_by_edge, nodes_by_id, parse_failures, trips_with_edges = build_transit_edges(
        trips, routes, raw_stops, node_for_stop
    )

    # Retain only routes that actually contribute to in-bbox Sydney graph nodes.
    active_route_ids = {route_id for node in nodes_by_id.values() for route_id in node["routes"]}
    routes = {route_id: route for route_id, route in routes.items() if route_id in active_route_ids}
    route_shapes = build_route_shapes(lat0, bbox, routes, shape_counts)
    route_states, station_states, adjacency, route_waits, walking_transfer_edges = build_graph(
        nodes_by_id,
        durations_by_edge,
        routes,
    )
    stations = [
        {
            "id": station["id"],
            "name": station["name"],
            "point": round_point(station["point"]),
            "routes": sorted(route_id for route_id in station["routes"] if route_id in routes),
            "modes": sorted(station["modes"]),
            "stopIds": station["stop_ids"],
        }
        for station in sorted(nodes_by_id.values(), key=lambda item: item["name"])
    ]
    cells, mask = build_grid_cells(all_polygons, stations, bbox)

    modes_present = sorted({route["mode"] for route in routes.values()})
    routes_by_mode = Counter(route["mode"] for route in routes.values())
    output = {
        "metadata": {
            "city": "Sydney",
            "source": "TfNSW Timetables Complete GTFS",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "static_not_realtime": True,
            "schedule_aware": False,
            "modes_included": modes_present,
            "default_max_time_minutes": 90,
            "max_supported_time_minutes": 120,
            "route_type_mapping": ROUTE_TYPE_TO_MODE,
            "excluded_route_types": dict(skipped_route_types),
            "default_origin": {"name": "Central Station", "lat": -33.8826, "lon": 151.2069},
        },
        "meta": {
            "lat0": round(lat0, 6),
            "bounds": [round(value, 1) for value in bbox],
            "gridCols": GRID_COLS,
            "gridRows": GRID_ROWS,
            "walkMetersPerMinute": WALK_METERS_PER_MINUTE,
            "accessWalkMetersPerMinute": WALK_METERS_PER_MINUTE,
            "maxWalkToTransitMeters": MAX_WALK_TO_TRANSIT_METERS,
            "stationAccessPenalty": STATION_ACCESS_PENALTY,
            "originStationCount": MAX_ORIGIN_ACCESS_STOPS,
            "cellNearestStations": CELL_NEAREST_STOPS,
            "defaultBoardWait": DEFAULT_BOARD_WAIT,
            "transferPenalty": DEFAULT_TRANSFER_PENALTY_MINUTES,
            "interComplexTransferPenalty": DEFAULT_TRANSFER_PENALTY_MINUTES,
        },
        "boroughs": areas,
        "externalLand": [],
        "parks": parks,
        "streets": streets,
        "routes": route_shapes,
        "stations": stations,
        "routeStates": route_states,
        "stationStates": station_states,
        "routeWaits": route_waits,
        "adjacency": adjacency,
        "cells": cells,
        "mask": mask,
        "routeStyles": output_route_styles(routes),
        "modes": {
            mode: {
                "label": MODE_LABELS[mode],
                "color": f"#{MODE_FALLBACK_COLORS[mode]}",
                "waitPenaltyMinutes": WAIT_PENALTY_BY_MODE[mode],
            }
            for mode in modes_present
        },
        "buildReport": {},
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, separators=(",", ":")), encoding="utf-8")
    build_seconds = time.perf_counter() - started
    output["buildReport"] = {
        "generatedJsonSize": file_size_label(OUTPUT_PATH),
        "buildTimeSeconds": round(build_seconds, 2),
        "totalStops": len(stations),
        "totalParentStationsOrComplexes": sum(1 for station in stations if len(station.get("stopIds") or []) > 1),
        "routesByMode": dict(sorted(routes_by_mode.items())),
        "totalTripsParsed": trips_with_edges,
        "selectedTrips": selected_trip_count,
        "transitEdges": len(durations_by_edge),
        "walkingTransferEdges": walking_transfer_edges,
        "gridCells": len(cells),
        "gtfsParseFailures": dict(parse_failures),
        "boundaryLayerLoaded": boundary_loaded,
        "streetLayerLoaded": streets_loaded,
        "parksLayerLoaded": parks_loaded,
    }
    OUTPUT_PATH.write_text(json.dumps(output, separators=(",", ":")), encoding="utf-8")

    print(f"Wrote {OUTPUT_PATH}")
    print(f"Generated JSON size: {file_size_label(OUTPUT_PATH)}")
    print(f"Build time: {build_seconds:.2f}s")
    print(f"Total stops: {len(stations)}")
    print(f"Total parent stations/complexes: {output['buildReport']['totalParentStationsOrComplexes']}")
    print(f"Total routes by mode: {dict(sorted(routes_by_mode.items()))}")
    print(f"Total trips parsed: {trips_with_edges}")
    print(f"Total transit edges: {len(durations_by_edge)}")
    print(f"Total walking-transfer edges: {walking_transfer_edges}")
    print(f"GTFS parse failures/skipped rows: {dict(parse_failures)}")
    print(f"Boundary layer loaded: {'yes' if boundary_loaded else 'no'}")
    print(f"Street layer loaded: {'yes' if streets_loaded else 'no'}")
    print(f"Parks layer loaded: {'yes' if parks_loaded else 'no'}")


if __name__ == "__main__":
    main()
