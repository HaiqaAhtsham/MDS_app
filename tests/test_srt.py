from app import srt

NEW = """1
00:00:00,000 --> 00:00:00,033
<font size="28">FrameCnt: 1, DiffTime: 33ms
2024-05-01 10:00:00.000
[iso: 100] [shutter: 1/1000.0] [fnum: 2.8] [ev: 0] [focal_len: 24.00] [latitude: 33.684422] [longitude: 73.047882] [rel_alt: 50.000 abs_alt: 560.123] [ct: 5500] </font>

2
00:00:01,000 --> 00:00:01,033
<font size="28">FrameCnt: 31, DiffTime: 33ms
[latitude: 33.684500] [longitude: 73.047900] [rel_alt: 51.000 abs_alt: 561.0] [gb_yaw: 90.0 gb_pitch: -89.9 gb_roll: 0.0]</font>
"""

MINI = """1
00:00:00,000 --> 00:00:01,000
[latitude : 33.1] [longtitude : 73.2] [altitude: 30.5]
"""

OLD = """1
00:00:00,000 --> 00:00:01,000
HOME(73.0000,33.0000) 2019.01.01 10:00:00
GPS(73.0478,33.6844,19) ISO:100 Shutter:60 EV: F-Num:2.2
F.PRY (0.0, 0.0, 0.0), G.PRY (-90.0, 0.0, 0.0) BAROMETER:40.2
"""


def test_new_dji_format():
    fixes = srt.parse(NEW)
    assert len(fixes) == 2
    assert fixes[0].lat == 33.684422 and fixes[0].lng == 73.047882 and fixes[0].alt == 50.0
    assert fixes[1].yaw == 90.0 and fixes[1].pitch == -89.9
    assert srt.fix_at(fixes, 0.9).alt == 51.0
    assert srt.fix_at(fixes, 0.2).alt == 50.0


def test_mini_misspelled_longitude():
    (fix,) = srt.parse(MINI)
    assert (fix.lat, fix.lng, fix.alt) == (33.1, 73.2, 30.5)


def test_old_gps_format():
    (fix,) = srt.parse(OLD)
    assert (fix.lat, fix.lng, fix.alt) == (33.6844, 73.0478, 40.2)
