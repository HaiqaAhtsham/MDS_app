# MDS Dashboard

Web dashboard for the GBL-131 Mine Dispensing System (MDS). It has four parts:

1. **Vehicle tracking.** You mark a start and an end point on a satellite map. The vehicle drives from wherever it is (point A) to the start point, then along the strip to the end point. The **Minefield Progress** ring shows how much of the start→end path the vehicle has covered. Position comes from the GPS of the device the app is opened on.
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

### GPS needs HTTPS

Browsers only give GPS to pages served over **HTTPS** or from `localhost`. To use a phone or tablet in the vehicle as the tracker, serve the app over HTTPS. One way is a certificate from [mkcert](https://github.com/FiloSottile/mkcert) (install its root CA on the device):

```bash
mkcert 192.168.1.50 localhost
MDS_COOKIE_SECURE=1 uvicorn app.main:app --host 0.0.0.0 --port 8443 \
  --ssl-keyfile 192.168.1.50+1-key.pem --ssl-certfile 192.168.1.50+1.pem
```

Another way is a reverse proxy with a real certificate (nginx, Caddy).

## Using it

### Tracking

1. Click **Set start**, then click the map. It switches to the end point, so click again for the end.
2. On the device in the vehicle, sign in and press **Use this device's GPS**. It sends a fix every 2 s.
3. Press **Start mission**. The vehicle's current position becomes point A.
   - During the approach phase, the panel shows the distance to the start point, and a dashed white line runs from the vehicle to the start.
   - Once the vehicle is within 25 m of the start, the laying phase begins. Progress is the vehicle's position projected onto the start→end line. The ring never goes backwards on GPS jitter, and the panel warns if the vehicle drifts more than 50 m off the centreline.
   - Within 25 m of the end point, the mission is marked complete.
4. Everyone else signed in sees the vehicle move, because the dashboard refreshes every 3 s.

**Simulate** drives a pretend vehicle along the route, so you can try this without going outside.

### Drone detection

1. Put a few pictures of the object in `reference/`, or upload them under **Drone video detection**. Tight, top-down crops work best.
2. Upload the video with its `.srt` file. Useful options:
   - **Object size (m):** makes matching faster and more accurate.
   - **Min. frames per object:** set it to 2 or more to ignore one-frame false hits.
   - **Camera HFOV:** the camera's horizontal field of view. The default of 73.7° fits many DJI cameras; check yours.
3. Each object that is found becomes one orange marker. Hits of the same object across frames are merged within 3 m. A marker's popup shows the matched image crop, the match score and the self-destruct countdown. When there are three or more markers, a red dashed perimeter with its corner coordinates is drawn around them.

About the `.srt` files: they are **not training data**. DJI drones write the GPS position, altitude and heading for every frame into that subtitle file, and the app uses them to turn a pixel in the frame into a latitude and longitude. The calculation assumes the camera points straight down (gimbal pitch −90°). Oblique footage will place markers less accurately.

The built-in detector compares each frame with your reference pictures at several sizes and 4 rotations. That is enough to get started, but it is sensitive to lighting, scale and background. For field use, train a real detector:

```bash
python tools/extract_frames.py flights/*.mp4 --every 0.5 --out dataset/images
# label the frames (CVAT / Label Studio, YOLO format), then:
pip install ultralytics
yolo detect train data=dataset.yaml model=yolov8n.pt imgsz=1280 epochs=100
cp runs/detect/train/weights/best.pt models/detector.pt   # the app uses it automatically
```

### Self-destruct time

The **Set self-destruct time** dropdown in the Control Module applies to new marks. If you tick **Also apply to objects already marked**, existing marks are re-timed to *marked time + new value*. Anything older than that is removed at once. Expired marks disappear from every open dashboard within a second and are deleted from the database within a minute.

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
| `MDS_ARRIVAL_RADIUS_M` / `MDS_OFF_ROUTE_M` | `25` / `50` | Tracking thresholds |

## Layout

```
app/main.py        HTTP API + static files
app/tracking.py    mission state machine and progress
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

- The satellite tiles come from Esri World Imagery and need internet access. On an offline network, point the tile URL in `static/app.js` to a local tile server.
- `MDS.pdf` is marked **RESTRICTED**. Keep this repository private, and read the `claude` mode row above before enabling it.
