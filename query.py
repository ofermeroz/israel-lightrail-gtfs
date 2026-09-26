#!/usr/bin/env python3
"""Query a built schedule.sqlite for direct trips between two stations.

Usage: query.py FROM_STATION_ID TO_STATION_ID "YYYY-MM-DD HH:MM" [--db out/schedule.sqlite] [--limit 6]

This is the reference query the app's TripOptionsRequest mirrors. A service day can run past
midnight (times >= 24:00:00), so trips are searched on the requested service day and on the
previous one, shifted by 24 hours.
"""

import argparse
import datetime as dt
import sqlite3

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

QUERY = """
SELECT l.shortName, o.departure - :offset AS departure, d.arrival - :offset AS arrival, s.nameHe
FROM stopTimes o
JOIN platforms op ON op.id = o.platformID
JOIN stopTimes d ON d.tripID = o.tripID AND d.sequence > o.sequence
JOIN platforms dp ON dp.id = d.platformID
JOIN trips t ON t.id = o.tripID
JOIN lines l ON l.id = t.lineID
JOIN services v ON v.id = t.serviceID
JOIN stations s ON s.id = t.destinationStationID
WHERE op.stationID = :origin AND dp.stationID = :destination
  AND o.canBoard = 1 AND d.canAlight = 1
  AND v.{weekday} = 1 AND :serviceDate BETWEEN v.startDate AND v.endDate
  AND o.departure - :offset >= :time
"""


def trips(db, origin, destination, when, limit):
    time = when.hour * 3600 + when.minute * 60
    results = []
    for offset_days in (0, 1):
        service_day = when.date() - dt.timedelta(days=offset_days)
        results += db.execute(
            QUERY.format(weekday=DAYS[service_day.weekday()]),
            {
                "origin": origin,
                "destination": destination,
                "serviceDate": int(service_day.strftime("%Y%m%d")),
                "offset": offset_days * 86400,
                "time": time,
            },
        ).fetchall()
    return sorted(results, key=lambda row: row[1])[:limit]


def clock(seconds):
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("origin", type=int)
    parser.add_argument("destination", type=int)
    parser.add_argument("when")
    parser.add_argument("--db", default="out/schedule.sqlite")
    parser.add_argument("--limit", type=int, default=6)
    args = parser.parse_args()
    db = sqlite3.connect(args.db)
    when = dt.datetime.strptime(args.when, "%Y-%m-%d %H:%M")
    rows = trips(db, args.origin, args.destination, when, args.limit)
    if not rows:
        print("no direct trips")
    for line, departure, arrival, headsign in rows:
        print(f"line {line}  {clock(departure)} -> {clock(arrival)}  ({(arrival - departure) // 60} min)  toward {headsign}")


if __name__ == "__main__":
    main()
