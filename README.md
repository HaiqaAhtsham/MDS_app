# MDS Dashboard

Web dashboard for the GBL-131 Mine Dispensing System (MDS). It has four parts:

1. **Laying missions and navigation.** You enter a start point and an end point as latitude/longitude, or pick them on the map. You pick the mine type, density and time interval from the density-frontage chart; the app looks up the frontage and the required speed. **Start** then gives turn-by-turn directions from the vehicle to the start point, and along the strip it warns when the speed is outside tolerance. Position comes from the GPS of the device the app is opened on, and the app works offline.
2. **Drone video detection.** You upload a drone video together with its `.srt` telemetry file. The app finds the object from your reference pictures in the video frames, works out each object's GPS position, and marks it on the map. The **self-destruct time** selector (1–272 h, the MLCU range) controls when marks are removed automatically.
3. **RAG chatbot.** Answers questions about MDS from `MDS.pdf` and any other PDFs you add. Every answer cites page numbers.
4. **Accounts.** There is one admin account. The admin can add, disable, reset and delete operator accounts.

## Run it

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
MDS_ADMIN_PASSWORD='choose-a-strong-one' uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 and sign in as `admin`. If you don't set `MDS_ADMIN_PASSWORD`, the first start generates a password and prints it in the server log. You can change it later under **Settings**.

### Running fully offline

The app needs no internet once two things are installed: map tiles for your area and a road-routing server.

**1. Map tiles.** Put an MBTiles file at `tiles/map.mbtiles`, or a folder of `tiles/{z}/{x}/{y}.jpg|png`. You can make one with QGIS (*Processing → Generate XYZ tiles (MBTiles)*) from your satellite imagery or maps. The app serves the file itself. While no tiles are installed, the browser falls back to online Esri imagery; set `MDS_ONLINE_TILES=0` to switch that off.

**2. Road routing (OSRM).** Prepare the road data once, while you have internet:

```bash
wget https://download.geofabrik.de/asia/pakistan-latest.osm.pbf
docker run -t -v "$PWD:/data" ghcr.io/project-osrm/osrm-backend osrm-extract -p /opt/car.lua /data/pakistan-latest.osm.pbf
docker run -t -v "$PWD:/data" ghcr.io/project-osrm/osrm-backend osrm-partition /data/pakistan-latest.osrm
docker run -t -v "$PWD:/data" ghcr.io/project-osrm/osrm-backend osrm-customize /data/pakistan-latest.osrm
```

Then run the server (no internet needed) and point the app at it:

```bash
docker run -d -p 5000:5000 -v "$PWD:/data" ghcr.io/project-osrm/osrm-backend osrm-routed --algorithm mld /data/pakistan-latest.osrm
MDS_OSRM_URL=http://localhost:5000 uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Without `MDS_OSRM_URL`, or if the server is down, navigation shows a straight line with a compass heading and a clear "no road routing" warning.

OSRM snaps the start point to the nearest road. When the start point is in a field, the directions end with *"Leave the road: head east (90°) for 120 m"*.

### GPS needs HTTPS

Browsers only give GPS to pages served over **HTTPS** or from `localhost`. To use a phone or tablet in the vehicle as the tracker, serve the app over HTTPS. One way is a certificate from [mkcert](https://github.com/FiloSottile/mkcert) (install its root CA on the device):

```bash
mkcert 192.168.1.50 localhost
MDS_COOKIE_SECURE=1 uvicorn app.main:app --host 0.0.0.0 --port 8443 \
  --ssl-keyfile 192.168.1.50+1-key.pem --ssl-certfile 192.168.1.50+1.pem
```

Another way is a reverse proxy with a real certificate (nginx, Caddy).

## Using it

### Laying mission: start point → parameters → end point → Start

1. **Start point.** Type the latitude and longitude in decimal degrees. Bad input is rejected with a message. **Pick on map** lets you click the point instead.
2. **Mine parameters.** This dialog opens by itself after the start point is set.
   - *Anti-tank only:* choose one of the chart's AT densities.
   - *Mixed:* choose an ATM density. The chart pairs every ATM density with exactly one APM density, so the APM box shows that pair.
   - Time interval: 1, 2 or 5 s, the only rows in the chart.

   The dialog shows the **frontage** and the **required vehicle speed** from the chart, the chart rows they came from, and a hand check (see below).
3. **End point.** By default the end point is **calculated from the frontage**: give the direction the minefield runs, either as a bearing from the start point (0–360°) or with **Pick direction on map** (click anywhere that way; only the direction is used). The app puts the end point exactly one chart frontage from the start, so the strip length always matches the chart.
   - If the start point or the minefield parameters change later, the end point moves with them, keeping the same direction.
   - **Type lat / long** is still available. If a typed end point differs from the frontage by more than 1% (or 10 m), the Minefield Progress panel says how much shorter or longer the strip is and offers **Snap end to frontage**, which keeps the direction and corrects the length.
4. **Start.** Opens the navigation panel:
   - the road route from the vehicle to the start point, then the strip to the end point;
   - turn-by-turn directions, distance to the next turn, distance remaining and ETA to the start point;
   - a new route is calculated if the vehicle is more than 50 m off the route for two fixes in a row;
   - during laying, current speed against the required speed. It warns when the speed is outside the tolerance. The tolerance is ±10% by default; the admin can change it under **Settings**.

Progress on the strip works as before. The **Minefield Progress** ring shows how much of the start→end line has been covered, and other signed-in users see the vehicle move. **Simulate** drives a pretend vehicle along the route and lays at the required speed.

#### Density-frontage chart

`app/density.py` holds Annexure A (p. 120) exactly as printed. Nothing is interpolated, and selections that are not in the chart are rejected. Speeds are treated as km/h. The chart itself gives no unit, but the relationship below only holds in km/h.

Every result shows two **hand checks**. They are relationships that hold across the chart; the result itself is always the chart's printed value.

- `vehicle speed = space interval ÷ time gap × 3.6`
- `frontage = space interval × 288 canisters`

The app flags these chart values (they are still the values used):

| Chart cell | Printed | Check gives |
| --- | --- | --- |
| AT 0.5 / mixed 0.4–0.3, 5 s | 6.1 km/h | 7.2 km/h |
| AT 0.6 / mixed 0.48–0.36, 5 s | 5.6 km/h | 5.98 km/h |
| AT 0.2 / mixed 0.16–0.12, 1 s | 90 km/h | above the vehicle's 81 km/hr maximum (p. 7) |

### Drone detection

1. Put a few pictures of the object in `reference/`, or upload them under **Drone video detection**. Tight, top-down crops work best.
2. Upload the video with its `.srt` file. Useful options:
   - **Object size (m):** makes matching faster and more accurate.
   - **Min. frames per object:** set it to 2 or more to ignore one-frame false hits.
   - **Camera HFOV:** the camera's horizontal field of view. The default of 73.7° fits many DJI cameras; check yours.
3. Choose the **marker type** (ATM or APM) before uploading. Each object that is found becomes one marker of that type. Hits of the same object across frames are merged within 3 m. A marker's popup shows the matched image crop, the match score and the self-destruct countdown. When there are three or more markers, a red dashed perimeter with its corner coordinates is drawn around them.

About the `.srt` files: they are **not training data**. DJI drones write the GPS position, altitude and heading for every frame into that subtitle file, and the app uses them to turn a pixel in the frame into a latitude and longitude. The calculation assumes the camera points straight down (gimbal pitch −90°). Oblique footage will place markers less accurately.

The built-in detector compares each frame with your reference pictures at several sizes and 4 rotations. That is enough to get started, but it is sensitive to lighting, scale and background. For field use, train a real detector:

```bash
python tools/extract_frames.py flights/*.mp4 --every 0.5 --out dataset/images
# label the frames (CVAT / Label Studio, YOLO format), then:
pip install ultralytics
yolo detect train data=dataset.yaml model=yolov8n.pt imgsz=1280 epochs=100
cp runs/detect/train/weights/best.pt models/detector.pt   # the app uses it automatically
```

### Markers by hand

**Add marker** opens a dialog for the marker type (ATM or APM) and its latitude/longitude. **Pick on map** lets you click the spot instead; the dialog then reopens with the coordinates filled in. You can pick anywhere, including inside the red marked area. The Control Module shows how many ATM and APM markers are on the map.

### Ammunition

**Edit** on the Ammunition Status card asks for:

- **Total rockets:** GBL-240 and GBL-243 in stock;
- **Total rockets loaded:** GBL-240 and GBL-243 in the launchers. Loaded can't exceed the total, and the launchers hold 288 in all.

The handout (p. 27) gives the contents: GBL-240 holds 5 ATM and is used for ATM-only minefields; GBL-243 holds 4 ATM + 3 APM and is used for mixed minefields.

**Each route needs its own ammunition entry.** Pressing **Start** (or **Simulate**) opens the dialog as *Ammunition for this route*; the route starts when it is saved (**Save & start route**). If the minefield's rocket type has none loaded (GBL-240 for ATM-only, GBL-243 for mixed), the route doesn't start and the dialog says why. Changing the start point, end point or parameters means entering the ammunition again.

When a route ends (end point reached, or changed / reset before that), the rockets it fired are taken off the total and loaded numbers. The next route's dialog therefore starts from what is actually left, and its fired count starts at 0. The card shows what the last route fired.

**Route history** (clock icon in the sidebar) lists every route: when it started and ended, who started it, start/end points, strip length, minefield parameters, how much was laid, rockets loaded and rockets fired of each type, and whether it was completed or abandoned.

While the vehicle lays the strip, the app counts one rocket of that type as fired every **time gap** set in the minefield parameters. Counting starts when the vehicle reaches the start point and stops at the end point, or when that type's loaded rockets run out. Counting is by time, so it keeps going if the GPS drops out mid-strip. The dialog can reset the fired count.

### Self-destruct time

The **Set self-destruct time** dropdown in the Control Module applies to new marks. **Custom** asks for a time in hrs or mins, from 1 minute up to 272 hrs. If you tick **Also apply to objects already marked**, existing marks are re-timed to *marked time + new value*. Anything older than that is removed at once. Expired marks disappear from every open dashboard within a second and are deleted from the database within a minute.

### Chatbot

`MDS_RAG_MODE` sets how answers are produced:

| Mode | How answers are made | Where the document text goes |
| --- | --- | --- |
| `local` (default) | BM25 search over ~900-character chunks, then the best-matching sentences are quoted with page numbers | Stays on the server |
| `claude` | Same search, then the top 6 excerpts go to the Claude API, which writes the answer from them only and cites pages | **Excerpts are sent to Anthropic's API** |

For `claude` mode, set `ANTHROPIC_API_KEY`. `MDS_CLAUDE_MODEL` defaults to `claude-opus-5-5`. If the API call fails, the answer falls back to `local` mode. The admin can add more PDFs under **Documents**, or you can drop them into `docs/`.

## Configuration

| Variable | Default | |
| --- | --- | --- |
| `MDS_ADMIN_USER` / `MDS_ADMIN_PASSWORD` | `admin` / generated | Admin account created on first start |
| `MDS_DATA_DIR` | `./data` | SQLite database, uploads, crops, RAG cache |
| `MDS_REFERENCE_DIR` | `./reference` | Reference pictures of the object |
| `MDS_YOLO_WEIGHTS` | `./models/detector.pt` | Optional trained detector |
| `MDS_RAG_MODE` | `local` | `local` or `claude` |
| `MDS_SESSION_HOURS` | `12` | Sign-in lifetime |
| `MDS_COOKIE_SECURE` | `0` | Set to `1` when serving over HTTPS |
| `MDS_ARRIVAL_RADIUS_M` / `MDS_OFF_ROUTE_M` | `25` / `50` | Arrival radius; off-route distance that triggers rerouting |
| `MDS_OSRM_URL` / `MDS_OSRM_PROFILE` | unset / `driving` | Self-hosted OSRM server for road routing |
| `MDS_MBTILES` / `MDS_TILE_DIR` | `tiles/map.mbtiles` / `tiles/` | Offline map tiles |
| `MDS_ONLINE_TILES` | `1` | `0` = never load online imagery |

## Layout

```
app/main.py        HTTP API + static files
app/tracking.py    mission state machine and progress
app/density.py     density-frontage chart (Annexure A) and lookups
app/ammo.py        canister stock and automatic firing count
app/routing.py     OSRM client, turn-by-turn text, straight-line fallback
app/geo.py         distance / projection helpers
app/srt.py         DJI .srt telemetry parser
app/detection.py   detector, pixel→GPS, clustering, background jobs
app/rag.py         PDF chunking, BM25 retrieval, answers
app/auth.py        scrypt passwords, cookie sessions, admin bootstrap
static/            dashboard (Leaflet map, vendored so it works offline)
tools/             frame extraction for training a detector
tests/             pytest suite (includes an end-to-end synthetic drone video)
```

Run the tests with `python -m pytest -q`.

## Notes

- `MDS.pdf` is marked **RESTRICTED**. Keep this repository private, and read the `claude` mode row above before enabling it.
