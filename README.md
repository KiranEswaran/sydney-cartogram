# Sydney Transit Time Cartogram

Interactive Greater Sydney public-transport accessibility map forked from
[`AntCas/nyc-cartogram`](https://github.com/AntCas/nyc-cartogram).

The app renders a static, browser-only heatmap/cartogram from preprocessed TfNSW GTFS data. It
includes trains, metro, light rail, regular buses, and ferries.

## Data Sources

- TfNSW Timetables Complete GTFS: <https://opendata.transport.nsw.gov.au/dataset/timetables-complete-gtfs>
- ABS ASGS Greater Sydney boundary via ABS ArcGIS services: <https://geo.abs.gov.au/>
- Optional cosmetic layers:
  - `data/osm_major_streets.geojson`
  - `data/parks_open_space.geojson`

The raw GTFS ZIP is expected at:

```bash
data/tfnsw_gtfs_complete.zip
```

Raw GTFS is ignored by Git. The generated compact bundle is:

```bash
site/data/commute_map_data.json
```

## API Keys

The preferred v1 workflow is a one-off local GTFS download followed by local preprocessing. No TfNSW
API key is required by the browser app, and no key should be committed.

If a future workflow needs an API key, store it only in `.env`:

```bash
TFNSW_API_KEY=replace_with_real_key
```

Do not reference that key from client-side JavaScript.

## Build Data

```bash
python3 build_commute_site_data.py
```

or:

```bash
npm run build:data
```

The build prints JSON size, build time, stop counts, routes by mode, trip counts, transit edges,
walking-transfer edges, parse failures, and optional layer status.

## Local Preview

```bash
python3 -m http.server 8000
```

or:

```bash
npm run dev
```

Open:

```text
http://localhost:8000/site/
```

Address search uses OpenStreetMap Nominatim at runtime with a Sydney/Australia bias.

## Static Build

```bash
npm run build
```

This copies `site/` to `dist/`.

## Vercel

This project is configured as a static Vercel app with `vercel.json`:

- build command: `npm run build`
- output directory: `dist`
- route rewrites for `/sydney/@lat,lon` deep links

For v1, generate `site/data/commute_map_data.json` locally and deploy the static bundle. Avoid
processing the full TfNSW GTFS ZIP during Vercel builds unless you explicitly accept the build-time
cost and configure secrets in Vercel environment variables.

## Project Layout

- `build_commute_site_data.py`: converts TfNSW static GTFS and optional geography layers into the compact site bundle
- `site/index.html`: app shell and metadata
- `site/app.js`: interactive map, routing, search, sharing, heatmap, and warp rendering
- `site/styles.css`: app styles
- `site/data/commute_map_data.json`: generated Sydney dataset
- `vercel.json`: static Vercel deployment config

## Limitations

This is an approximate static public-transport accessibility visualisation.
It is not realtime.
It is not time-of-day-aware.
It does not account for service disruptions, trackwork, cancellations, or live delays.
Travel times are derived from static GTFS and simplified transfer/walking assumptions.

Additional implementation notes:

- Regular bus routes are included. School-only and temporary replacement bus route types are excluded from the representative graph.
- Wait time is approximated by fixed mode penalties, not exact timetable transfer optimisation.
- Walking transfers are capped by distance and neighbour count to keep the browser graph responsive.
- The static SVG cartogram generator from the original NYC project is retained but not ported for Sydney v1.
