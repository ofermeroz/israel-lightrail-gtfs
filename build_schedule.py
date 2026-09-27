#!/usr/bin/env python3
"""Build a compact SQLite schedule for the Tel Aviv Red Line from Israel's national GTFS feed.

Outputs (in --out):
  schedule.sqlite       the typed, app-oriented schedule database
  schedule.sqlite.gz    the published artifact (deterministic gzip)
  manifest.json         metadata the app reads before downloading
  release_tag           the GitHub release tag for this feed version
"""

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import urllib.request
import zipfile
from zoneinfo import ZoneInfo

FEED_URL = "https://gtfs.mot.gov.il/gtfsfiles/israel-public-transportation.zip"
SCHEMA_VERSION = 2
# Schema changes are additive, so older apps can import a newer database: their importer copies
# only the tables and columns they know. Each version gets its own manifest pointing at one file.
COMPATIBLE_SCHEMA_VERSIONS = [1, 2]
DEFAULT_LINE_COLOR = "D0112B"
# Map paths are simplified to this many meters, which is invisible at street zoom.
PATH_TOLERANCE_METERS = 4.0
ISRAEL = ZoneInfo("Asia/Jerusalem")
MIN_STATIONS = 30

# Stations whose name has no standalone entry in translations.txt; values taken from the feed's
# own translations of compound stop names that contain them.
TRANSLATION_OVERRIDES = {
    'כ"ט בנובמבר': {"EN": "Kaf Tet BeNovember", "AR": "كات بنوڤمبر"},
}

SCHEMA = """
CREATE TABLE "stations" (
  "id" INTEGER PRIMARY KEY NOT NULL,
  "nameHe" TEXT NOT NULL,
  "nameEn" TEXT NOT NULL,
  "nameAr" TEXT NOT NULL,
  "latitude" REAL NOT NULL,
  "longitude" REAL NOT NULL,
  "sortOrder" INTEGER NOT NULL
) STRICT;
CREATE TABLE "platforms" (
  "id" INTEGER PRIMARY KEY NOT NULL,
  "stationID" INTEGER NOT NULL,
  "code" INTEGER NOT NULL
) STRICT;
CREATE TABLE "lines" (
  "id" INTEGER PRIMARY KEY NOT NULL,
  "shortName" TEXT NOT NULL,
  "longName" TEXT NOT NULL,
  "color" TEXT NOT NULL
) STRICT;
CREATE TABLE "services" (
  "id" INTEGER PRIMARY KEY NOT NULL,
  "sunday" INTEGER NOT NULL,
  "monday" INTEGER NOT NULL,
  "tuesday" INTEGER NOT NULL,
  "wednesday" INTEGER NOT NULL,
  "thursday" INTEGER NOT NULL,
  "friday" INTEGER NOT NULL,
  "saturday" INTEGER NOT NULL,
  "startDate" INTEGER NOT NULL,
  "endDate" INTEGER NOT NULL
) STRICT;
CREATE TABLE "trips" (
  "id" TEXT PRIMARY KEY NOT NULL,
  "lineID" INTEGER NOT NULL,
  "serviceID" INTEGER NOT NULL,
  "directionID" INTEGER NOT NULL,
  "destinationStationID" INTEGER NOT NULL
) STRICT;
CREATE TABLE "stopTimes" (
  "tripID" TEXT NOT NULL,
  "platformID" INTEGER NOT NULL,
  "sequence" INTEGER NOT NULL,
  "arrival" INTEGER NOT NULL,
  "departure" INTEGER NOT NULL,
  "canBoard" INTEGER NOT NULL,
  "canAlight" INTEGER NOT NULL,
  PRIMARY KEY ("tripID", "sequence")
) STRICT;
CREATE INDEX "index_stopTimes_on_platformID_departure" ON "stopTimes"("platformID", "departure");
CREATE TABLE "stationOrder" (
  "lineShortName" TEXT NOT NULL,
  "stationID" INTEGER NOT NULL,
  "position" INTEGER NOT NULL,
  PRIMARY KEY ("lineShortName", "stationID")
) STRICT;
CREATE TABLE "linePaths" (
  "lineID" INTEGER NOT NULL,
  "sequence" INTEGER NOT NULL,
  "latitude" REAL NOT NULL,
  "longitude" REAL NOT NULL,
  PRIMARY KEY ("lineID", "sequence")
) STRICT;
CREATE TABLE "metadata" (
  "key" TEXT PRIMARY KEY NOT NULL,
  "value" TEXT NOT NULL
) STRICT;
"""


def rows(archive, name):
    with archive.open(name) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))


def seconds(gtfs_time):
    h, m, s = (int(part) for part in gtfs_time.split(":"))
    return h * 3600 + m * 60 + s


def translation_key(name):
    # stops.txt writes gershayim as two apostrophes; translations.txt uses a double quote.
    return name.replace("''", '"').strip()


def display_name(name):
    # The feed writes Hebrew gershayim as two apostrophes (e.g. הבעש''ט).
    return name.replace("''", "״").strip()


def hebrew_display_name(name):
    # ...and the geresh as one apostrophe (e.g. אהרונוביץ').
    return display_name(name).replace("'", "׳")


def platform_code(stop_desc):
    # e.g. "רחוב: ... רציף: 2   קומה: " → 2; 0 when the feed doesn't say.
    match = re.search(r"רציף:\s*(\d+)", stop_desc)
    return int(match.group(1)) if match else 0


def simplified(points, tolerance):
    """Douglas–Peucker on (lat, lon) points, measuring distance in meters."""
    if len(points) < 3:
        return points
    lat0 = math.radians(points[0][0])
    xy = [(lon * 111_320 * math.cos(lat0), lat * 110_540) for lat, lon in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        (x1, y1), (x2, y2) = xy[start], xy[end]
        length = math.hypot(x2 - x1, y2 - y1) or 1e-9
        farthest, distance = None, tolerance
        for i in range(start + 1, end):
            x0, y0 = xy[i]
            d = abs((x2 - x1) * (y1 - y0) - (x1 - x0) * (y2 - y1)) / length
            if d > distance:
                farthest, distance = i, d
        if farthest is not None:
            keep[farthest] = True
            stack += [(start, farthest), (farthest, end)]
    return [p for p, k in zip(points, keep) if k]


def download(url, destination):
    request = urllib.request.Request(url, headers={"User-Agent": "israel-lightrail-gtfs"})
    with urllib.request.urlopen(request, timeout=600) as response, open(destination, "wb") as out:
        shutil.copyfileobj(response, out)


def feed_timestamp(archive):
    year, month, day, hour, minute, second = archive.getinfo("stop_times.txt").date_time
    return dt.datetime(year, month, day, hour, minute, second, tzinfo=ISRAEL)


def merged_station_order(patterns):
    """Merge per-line stop patterns into one north-to-south order.

    Patterns are placed longest first; stations missing from the order so far (e.g. a branch)
    are inserted just before the first already-placed station that follows them.
    """
    order = []
    for pattern in sorted(patterns, key=len, reverse=True):
        pending = []
        for station in pattern:
            if station in order:
                index = order.index(station)
                order[index:index] = pending
                pending = []
            else:
                pending.append(station)
        order.extend(pending)
    return order


def build(archive, agency_id, database_path):
    routes = [r for r in rows(archive, "routes.txt") if r["agency_id"] == agency_id]
    route_ids = {r["route_id"] for r in routes}
    trips = [t for t in rows(archive, "trips.txt") if t["route_id"] in route_ids]
    trip_ids = {t["trip_id"] for t in trips}
    service_ids = {t["service_id"] for t in trips}

    stop_times = [st for st in rows(archive, "stop_times.txt") if st["trip_id"] in trip_ids]
    stop_ids = {st["stop_id"] for st in stop_times}
    stops = {s["stop_id"]: s for s in rows(archive, "stops.txt") if s["stop_id"] in stop_ids}
    services = [c for c in rows(archive, "calendar.txt") if c["service_id"] in service_ids]
    shape_of_route = {t["route_id"]: t["shape_id"] for t in trips if t["shape_id"]}
    shape_ids = set(shape_of_route.values())
    shapes = {}
    for point in rows(archive, "shapes.txt"):
        if point["shape_id"] in shape_ids:
            shapes.setdefault(point["shape_id"], []).append(
                (int(point["shape_pt_sequence"]), float(point["shape_pt_lat"]), float(point["shape_pt_lon"]))
            )

    keys = {translation_key(s["stop_name"]) for s in stops.values()}
    translations = {key: dict(value) for key, value in TRANSLATION_OVERRIDES.items() if key in keys}
    for t in rows(archive, "translations.txt"):
        key = translation_key(t["trans_id"])
        if key in keys and key not in TRANSLATION_OVERRIDES:
            translations.setdefault(key, {})[t["lang"].upper()] = t["translation"]

    station_of_stop = {stop_id: int(s["stop_code"]) for stop_id, s in stops.items()}

    by_trip = {}
    for st in stop_times:
        by_trip.setdefault(st["trip_id"], []).append(st)
    for sequence in by_trip.values():
        sequence.sort(key=lambda st: int(st["stop_sequence"]))

    line_of_route = {r["route_id"]: r["route_short_name"] for r in routes}
    longest_pattern = {}
    for trip in trips:
        if trip["direction_id"] != "0":
            continue
        pattern = [station_of_stop[st["stop_id"]] for st in by_trip[trip["trip_id"]]]
        line = line_of_route[trip["route_id"]]
        if len(pattern) > len(longest_pattern.get(line, [])):
            longest_pattern[line] = pattern
    order = merged_station_order(longest_pattern.values())

    stations = {}
    for stop_id, stop in stops.items():
        station_id = station_of_stop[stop_id]
        entry = stations.setdefault(station_id, {"name": stop["stop_name"], "lats": [], "lons": []})
        entry["lats"].append(float(stop["stop_lat"]))
        entry["lons"].append(float(stop["stop_lon"]))

    if os.path.exists(database_path):
        os.remove(database_path)
    db = sqlite3.connect(database_path)
    db.executescript(SCHEMA)

    station_rows = []
    for station_id, entry in stations.items():
        names = translations.get(translation_key(entry["name"]), {})
        hebrew = hebrew_display_name(entry["name"])
        station_rows.append((
            station_id,
            hebrew,
            display_name(names.get("EN", hebrew)),
            display_name(names.get("AR", hebrew)),
            sum(entry["lats"]) / len(entry["lats"]),
            sum(entry["lons"]) / len(entry["lons"]),
            order.index(station_id) if station_id in order else len(order),
        ))
    db.executemany("INSERT INTO stations VALUES (?,?,?,?,?,?,?)", station_rows)
    db.executemany(
        "INSERT INTO platforms VALUES (?,?,?)",
        [(int(stop_id), station_of_stop[stop_id], platform_code(s["stop_desc"])) for stop_id, s in stops.items()],
    )
    db.executemany(
        "INSERT INTO lines VALUES (?,?,?,?)",
        [
            (
                int(r["route_id"]),
                r["route_short_name"],
                r["route_long_name"],
                (r.get("route_color") or DEFAULT_LINE_COLOR).upper(),
            )
            for r in routes
        ],
    )
    for route_id, shape_id in shape_of_route.items():
        points = [(lat, lon) for _, lat, lon in sorted(shapes.get(shape_id, []))]
        db.executemany(
            "INSERT INTO linePaths VALUES (?,?,?,?)",
            [
                (int(route_id), index, round(lat, 6), round(lon, 6))
                for index, (lat, lon) in enumerate(simplified(points, PATH_TOLERANCE_METERS))
            ],
        )
    days = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
    db.executemany(
        "INSERT INTO services VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (int(c["service_id"]), *(int(c[d]) for d in days), int(c["start_date"]), int(c["end_date"]))
            for c in services
        ],
    )
    db.executemany(
        "INSERT INTO trips VALUES (?,?,?,?,?)",
        [
            (
                t["trip_id"],
                int(t["route_id"]),
                int(t["service_id"]),
                int(t["direction_id"]),
                station_of_stop[by_trip[t["trip_id"]][-1]["stop_id"]],
            )
            for t in trips
        ],
    )
    db.executemany(
        "INSERT INTO stopTimes VALUES (?,?,?,?,?,?,?)",
        [
            (
                st["trip_id"],
                int(st["stop_id"]),
                int(st["stop_sequence"]),
                seconds(st["arrival_time"]),
                seconds(st["departure_time"]),
                0 if st["pickup_type"] == "1" else 1,
                0 if st["drop_off_type"] == "1" else 1,
            )
            for st in stop_times
        ],
    )
    db.executemany(
        "INSERT INTO stationOrder VALUES (?,?,?)",
        [
            (line, station_id, position)
            for line, pattern in longest_pattern.items()
            for position, station_id in enumerate(pattern)
        ],
    )

    valid_from = min(int(c["start_date"]) for c in services)
    valid_to = max(int(c["end_date"]) for c in services)
    return db, len(stations), len(trips), valid_from, valid_to


def manifest_name(version):
    # v1 apps read manifest.json; later versions read manifest-v<N>.json.
    return "manifest.json" if version == 1 else f"manifest-v{version}.json"


def iso_date(yyyymmdd):
    text = str(yyyymmdd)
    return f"{text[:4]}-{text[4:6]}-{text[6:]}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--zip", help="use a local GTFS zip instead of downloading")
    parser.add_argument("--out", default="out")
    parser.add_argument("--agency", default="22")
    parser.add_argument(
        "--release-base",
        default="https://github.com/ofermeroz/israel-lightrail-gtfs/releases/download",
    )
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = args.zip
        if not zip_path:
            zip_path = os.path.join(tmp, "gtfs.zip")
            print(f"Downloading {FEED_URL}")
            download(FEED_URL, zip_path)

        with zipfile.ZipFile(zip_path) as archive:
            fed_at = feed_timestamp(archive)
            database_path = os.path.join(args.out, "schedule.sqlite")
            db, station_count, trip_count, valid_from, valid_to = build(archive, args.agency, database_path)

    today = int(dt.datetime.now(ISRAEL).strftime("%Y%m%d"))
    covering_today = db.execute(
        "SELECT count(*) FROM services WHERE ? BETWEEN startDate AND endDate", (today,)
    ).fetchone()[0]
    problems = []
    if station_count < MIN_STATIONS:
        problems.append(f"only {station_count} stations")
    if trip_count == 0:
        problems.append("no trips")
    unnumbered = db.execute("SELECT count(*) FROM platforms WHERE code = 0").fetchone()[0]
    if unnumbered:
        problems.append(f"{unnumbered} platforms without a number")
    if db.execute("SELECT count(DISTINCT lineID) FROM linePaths").fetchone()[0] == 0:
        problems.append("no line paths")
    if covering_today == 0:
        problems.append(f"no services cover {today}")
    if problems:
        db.close()
        sys.exit("Sanity check failed: " + ", ".join(problems))

    feed_date = fed_at.isoformat()
    tag = "feed-" + fed_at.strftime("%Y%m%d-%H%M") + f"-s{SCHEMA_VERSION}"
    metadata = {
        "schemaVersion": str(SCHEMA_VERSION),
        "feedDate": feed_date,
        "validFrom": iso_date(valid_from),
        "validTo": iso_date(valid_to),
    }
    db.executemany("INSERT INTO metadata VALUES (?,?)", metadata.items())
    db.commit()
    db.execute("VACUUM")
    db.close()

    with open(database_path, "rb") as raw:
        compressed = gzip.compress(raw.read(), compresslevel=9, mtime=0)
    gz_path = database_path + ".gz"
    with open(gz_path, "wb") as out:
        out.write(compressed)

    generated_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    for version in COMPATIBLE_SCHEMA_VERSIONS:
        manifest = {
            "schemaVersion": version,
            "feedDate": feed_date,
            "validFrom": metadata["validFrom"],
            "validTo": metadata["validTo"],
            "generatedAt": generated_at,
            "url": f"{args.release_base}/{tag}/schedule.sqlite.gz",
            "sha256": hashlib.sha256(compressed).hexdigest(),
            "size": len(compressed),
        }
        with open(os.path.join(args.out, manifest_name(version)), "w") as out:
            json.dump(manifest, out, indent=2, ensure_ascii=False)
            out.write("\n")
    with open(os.path.join(args.out, "release_tag"), "w") as out:
        out.write(tag + "\n")

    print(
        f"{station_count} stations, {trip_count} trips, valid {metadata['validFrom']}..{metadata['validTo']}, "
        f"{len(compressed) / 1e6:.2f} MB gzipped, tag {tag}"
    )


if __name__ == "__main__":
    main()
