"""External GPS receiver on a serial (COM) port.

Browsers cannot open COM ports, so a USB/serial GPS is read here, on the
server, and its fixes go into tracking exactly like phone GPS fixes. The
receiver must be plugged into the computer that runs the server.

GPS receivers send NMEA 0183 text: GGA (fix, satellites, HDOP) and RMC
(fix, speed, course) sentences, from any talker (GP, GN, GL, GA, BD).
"""

import logging
import threading
import time

from . import config, tracking

log = logging.getLogger(__name__)

try:
    import serial  # pyserial
    from serial.tools import list_ports as _list_ports
except ImportError:  # pragma: no cover - reported to the user instead
    serial = None
    _list_ports = None

# Usual receiver speeds, most common first. 4800 is the NMEA standard;
# most USB pucks (u-blox, SiRF) default to 9600.
BAUD_RATES = (9600, 4800, 38400, 115200, 19200, 57600)
PROBE_SECONDS = 2.5  # listening time per baud rate when searching
RECONNECT_S = 3.0
KNOT_MS = 0.514444
UERE_M = 5.0  # metres of error per unit of HDOP (rough accuracy estimate)

_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()
_status: dict = {"state": "stopped"}


# ------------------------------------------------------------ NMEA parsing

def checksum_ok(line: str) -> bool:
    if not line.startswith("$"):
        return False
    body, star, given = line[1:].partition("*")
    if not star:
        return True  # some receivers omit the checksum
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    try:
        return calc == int(given[:2], 16)
    except ValueError:
        return False


def _coord(value: str, hemi: str) -> float | None:
    """ddmm.mmmm / dddmm.mmmm plus N/S/E/W -> signed decimal degrees."""
    if not value or not hemi:
        return None
    try:
        v = float(value)
    except ValueError:
        return None
    deg = int(v // 100)
    dec = deg + (v - deg * 100) / 60
    return -dec if hemi in ("S", "W") else dec


def _num(value: str) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


def parse(line: str) -> dict | None:
    """One NMEA sentence -> {'type', 'lat', 'lng', 'valid', ...} or None."""
    line = line.strip()
    if not checksum_ok(line):
        return None
    f = line[1:].split("*")[0].split(",")
    kind = f[0][-3:]
    if kind == "GGA" and len(f) >= 9:
        quality = int(f[6]) if f[6].isdigit() else 0
        return {
            "type": "GGA",
            "lat": _coord(f[2], f[3]),
            "lng": _coord(f[4], f[5]),
            "valid": quality > 0,
            "sats": int(f[7]) if f[7].isdigit() else None,
            "hdop": _num(f[8]),
        }
    if kind == "RMC" and len(f) >= 9:
        knots = _num(f[7])
        return {
            "type": "RMC",
            "lat": _coord(f[3], f[4]),
            "lng": _coord(f[5], f[6]),
            "valid": f[2] == "A",
            "speed": knots * KNOT_MS if knots is not None else None,
            "heading": _num(f[8]),
        }
    return {"type": kind}  # a valid sentence we don't use (GSV, GSA, VTG...)


# ------------------------------------------------------------ ports

def ports() -> list[dict]:
    if _list_ports is None:
        return []
    return [
        {"device": p.device, "description": p.description or "", "hwid": p.hwid or ""}
        for p in sorted(_list_ports.comports(), key=lambda p: p.device)
    ]


def explain(port: str, err: Exception) -> str:
    """Turn pyserial's open errors into something an operator can act on."""
    text = str(err)
    low = text.lower()
    if "access is denied" in low or "permissionerror" in low or "errno 13" in low or "permission denied" in low:
        if "errno 13" in low or "permission denied" in low:
            return f"{port}: permission denied. On Linux add this user to the 'dialout' group (sudo usermod -aG dialout $USER) and log in again."
        return (f"{port} is in use by another program. Close any GPS software (u-center, GPS Info, "
                "a terminal such as PuTTY, another MDS server) and try again. If Windows Location is "
                "using the receiver through a sensor driver, disable that driver or Location.")
    if "filenotfound" in low or "cannot find the file" in low or "errno 2" in low or "no such file" in low:
        found = ", ".join(p["device"] for p in ports()) or "none"
        return (f"{port} was not found on the server. Ports seen: {found}. The receiver must be plugged "
                "into the computer running the MDS server; check Device Manager > Ports (COM & LPT).")
    if "semaphore" in low or "device not functioning" in low or "attached to the system is not functioning" in low:
        return f"{port} stopped responding. Unplug the receiver, plug it back in and connect again."
    return f"{port}: {text}"


def _open(port: str, baud: int):
    return serial.Serial(port, baud, timeout=1)


# ------------------------------------------------------------ reader

def status() -> dict:
    with _lock:
        s = dict(_status)
    s["running"] = _thread is not None and _thread.is_alive()
    s["available"] = serial is not None
    s["ports"] = ports()
    return s


def _set(**kw) -> None:
    with _lock:
        _status.update(kw)


def _readline(ser) -> str:
    raw = ser.readline()
    return raw.decode("ascii", "ignore") if isinstance(raw, bytes) else raw


def _probe(ser, seconds: float) -> bool:
    """True if valid NMEA arrives within `seconds`."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not _stop.is_set():
        if parse(_readline(ser)):
            return True
    return False


def _connect(port: str, baud: int | None):
    """Open the port, finding the baud rate if not given. Raises on failure."""
    rates = (baud,) if baud else BAUD_RATES
    for rate in rates:
        if _stop.is_set():
            return None, None
        _set(state="searching", message=f"Listening on {port} at {rate} baud…")
        ser = _open(port, rate)
        if _probe(ser, PROBE_SECONDS * (2 if baud else 1)):
            return ser, rate
        ser.close()
    tried = ", ".join(str(r) for r in rates)
    raise RuntimeError(
        f"{port} opened, but no GPS (NMEA) data arrived at {tried} baud. Check that this is the "
        "GPS's port (Device Manager), that the receiver has power, and that it outputs NMEA, not a "
        "binary protocol (set it to NMEA in its configuration tool)."
    )


def _run(port: str, baud: int | None, user_id) -> None:
    interval = config.GPS_INTERVAL_S
    while not _stop.is_set():
        ser = None
        try:
            ser, rate = _connect(port, baud)
            if ser is None:
                break
            _set(state="no_fix", baud=rate, error=None,
                 message=f"Connected to {port} at {rate} baud. Waiting for a satellite fix (go outdoors, clear sky)…")
            extra: dict = {}
            last_rec = 0.0
            while not _stop.is_set():
                line = _readline(ser)
                if not line:
                    continue
                s = parse(line)
                if not s:
                    continue
                with _lock:
                    _status["sentences"] = _status.get("sentences", 0) + 1
                if s["type"] == "GGA":
                    extra.update(sats=s["sats"], hdop=s["hdop"])
                if s["type"] not in ("GGA", "RMC"):
                    continue
                if s["type"] == "RMC":
                    extra.update(speed=s["speed"], heading=s["heading"])
                if not s["valid"] or s["lat"] is None or s["lng"] is None:
                    if _status.get("state") != "no_fix":
                        _set(state="no_fix", message="Receiver connected but has lost its satellite fix.")
                    continue
                hdop = extra.get("hdop")
                _set(state="live", message=f"Live from {port}",
                     fix={"lat": s["lat"], "lng": s["lng"], "sats": extra.get("sats"),
                          "hdop": hdop, "speed": extra.get("speed"), "ts": time.time()})
                now = time.monotonic()
                if now - last_rec < interval:
                    continue  # receivers send several sentences per second
                last_rec = now
                tracking.record_position(
                    s["lat"], s["lng"],
                    accuracy=hdop * UERE_M if hdop else None,
                    heading=extra.get("heading"),
                    speed=extra.get("speed"),
                    user_id=user_id,
                )
                with _lock:
                    _status["fixes"] = _status.get("fixes", 0) + 1
        except Exception as err:  # serial errors, unplugged receiver, no data
            msg = str(err) if isinstance(err, RuntimeError) else explain(port, err)
            log.warning("serial GPS: %s", msg)
            _set(state="error", error=msg, message=msg)  # then retry: it may be replugged
        finally:
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
        _stop.wait(RECONNECT_S)
    _set(state="stopped", message="Disconnected")


def start(port: str, baud: int | None = None, user_id=None) -> dict:
    global _thread
    if serial is None:
        raise RuntimeError("Serial GPS needs pyserial on the server: pip install pyserial")
    port = port.strip()
    if not port:
        raise ValueError("Choose a COM port")
    stop()
    # Open once here so the operator sees an "in use" / "not found" error at once.
    try:
        _open(port, baud or BAUD_RATES[0]).close()
    except Exception as err:
        msg = explain(port, err)
        with _lock:
            _status.clear()
            _status.update(state="error", port=port, error=msg, message=msg)
        raise RuntimeError(msg) from err
    _stop.clear()
    with _lock:
        _status.clear()
        _status.update(state="connecting", port=port, baud=baud, message=f"Opening {port}…",
                       sentences=0, fixes=0, fix=None, error=None, started=time.time())
    _thread = threading.Thread(target=_run, args=(port, baud, user_id), daemon=True, name="serial-gps")
    _thread.start()
    return status()


def stop() -> dict:
    global _thread
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=5)
        _thread = None
    with _lock:
        _status.update(state="stopped", message="Disconnected", error=None)
    return status()
