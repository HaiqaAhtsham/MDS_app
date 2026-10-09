import time

import pytest

from app import serial_gps


def nmea(body: str) -> str:
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"${body}*{cs:02X}\r\n"


GGA = nmea("GPGGA,123519,3341.0653,N,07302.8729,E,1,08,0.9,545.4,M,46.9,M,,")
RMC = nmea("GNRMC,123519,A,3341.0653,N,07302.8729,E,021.6,084.4,091026,,,A")
NO_FIX = nmea("GPGGA,123520,,,,,0,00,99.9,,M,,M,,")


def test_parse_gga_and_rmc():
    g = serial_gps.parse(GGA)
    assert g["type"] == "GGA" and g["valid"] and g["sats"] == 8 and g["hdop"] == 0.9
    assert g["lat"] == pytest.approx(33 + 41.0653 / 60) and g["lng"] == pytest.approx(73 + 2.8729 / 60)
    r = serial_gps.parse(RMC)
    assert r["valid"] and r["heading"] == 84.4 and r["speed"] == pytest.approx(21.6 * 0.514444)
    assert serial_gps.parse(NO_FIX)["valid"] is False


def test_parse_rejects_bad_checksum_and_noise():
    assert serial_gps.parse(GGA.replace("*", "0*", 1)) is None
    assert serial_gps.parse("\x00\xff garbage") is None
    assert serial_gps.parse(nmea("GPGSV,3,1,11,03,03,111,00")) == {"type": "GSV"}
    s = serial_gps.parse(nmea("GPRMC,0,A,3341.0653,S,07302.8729,W,0,0,091026,,"))
    assert s["lat"] < 0 and s["lng"] < 0


def test_explains_windows_errors():
    busy = serial_gps.explain("COM5", Exception("could not open port 'COM5': PermissionError(13, 'Access is denied.', None, 5)"))
    assert "in use by another program" in busy
    missing = serial_gps.explain("COM5", Exception("could not open port 'COM5': FileNotFoundError(2, 'The system cannot find the file specified.', None, 2)"))
    assert "not found" in missing


class FakeSerial:
    def __init__(self, port, baud, lines):
        self.baud, self.lines = baud, list(lines)

    def readline(self):
        if self.lines:
            return self.lines.pop(0).encode()
        time.sleep(0.05)
        return b""

    def close(self):
        pass


def test_reads_fixes_into_tracking(client, monkeypatch):
    # Only 4800 baud gives readable data; the reader must find it.
    def fake_open(port, baud):
        if port != "COM5":
            raise Exception("could not open port: FileNotFoundError(2, 'The system cannot find the file specified.')")
        return FakeSerial(port, baud, [NO_FIX, NO_FIX] + [RMC, GGA] * 3 if baud == 4800 else ["\x13\x88garble\n"])

    monkeypatch.setattr(serial_gps, "_open", fake_open)
    monkeypatch.setattr(serial_gps, "PROBE_SECONDS", 0.2)

    r = client.post("/api/gps/serial/start", json={"port": "COM7"})
    assert r.status_code == 400 and "not found" in r.json()["detail"]

    r = client.post("/api/gps/serial/start", json={"port": "COM5"})
    assert r.status_code == 200
    try:
        for _ in range(100):
            s = client.get("/api/gps/serial").json()
            if s["state"] == "live":
                break
            time.sleep(0.05)
        assert s["state"] == "live" and s["baud"] == 4800 and s["running"]
        v = client.get("/api/tracking").json()["vehicle"]
        assert v["lat"] == pytest.approx(33 + 41.0653 / 60)
    finally:
        s = client.post("/api/gps/serial/stop").json()
    assert s["state"] == "stopped" and not s["running"]
