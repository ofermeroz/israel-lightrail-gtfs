# israel-lightrail-gtfs

A small, app-ready SQLite schedule for the Tel Aviv light rail Red Line, rebuilt nightly from
Israel's national public-transport GTFS feed.

The national feed is ~170 MB zipped. This repo extracts only the Red Line (agency `22`, Tevel)
into a typed ~3.5 MB SQLite database (~0.9 MB gzipped) that mobile apps can download daily.

## Published files

- [`manifest.json`](manifest.json) — the current version: feed date, validity window, download
  URL, sha256 and size. Clients read this first and download only when `feedDate` changes.
- `schedule.sqlite.gz` — attached to the release named in the manifest's `url`. Releases are
  immutable per feed version; the last 7 are kept.

## Schema (version 1)

| Table | Contents |
|---|---|
| `stations` | one row per station (`id` = GTFS `stop_code`), names in Hebrew/English/Arabic, coordinates, `sortOrder` north to south |
| `platforms` | GTFS stops (one per platform) mapped to their station |
| `lines` | GTFS routes (`shortName` 1/2/3) |
| `services` | weekday flags and `startDate`/`endDate` (`yyyymmdd`) |
| `trips` | line, service, direction, and destination station |
| `stopTimes` | per-trip platform visits; `arrival`/`departure` in seconds since service-day midnight (may exceed 86 400), `canBoard`/`canAlight` |
| `stationOrder` | each line's station sequence |
| `metadata` | `schemaVersion`, `feedDate`, `validFrom`, `validTo` |

`query.py` is a reference query for direct trips between two stations, including trips after
midnight that belong to the previous service day:

```sh
python3 build_schedule.py --out out
python3 query.py 20713 36307 "2026-10-11 08:00"
```

## Source and terms

Data: [Israel Ministry of Transport — National Public Transport Authority GTFS feed](https://www.gov.il/he/pages/gtfs_general_transit_feed_specifications).
Use of the data is subject to the Ministry's terms of use. The Ministry provides it without any
guarantee of accuracy or completeness, and so does this repository. This project is not
affiliated with the Ministry of Transport, NTA, Tevel, or Dankal.
