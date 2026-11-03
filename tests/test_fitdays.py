import json
import os
import time

import pytest

from fitdays_sync import client as fc
from fitdays_sync.cli import merge_csv
from fitdays_sync.client import (COLUMNS, Credentials, FitdaysAuthError, FitdaysClient,
                                 hash_password, measurement_rows, redact, sign, signed_query)


@pytest.fixture(autouse=True)
def fixed_tz(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.delenv("TZ")
    time.tzset()


def test_sign_matches_reference_vector():
    # Vector from roquerodrigo/fitdays-api src/tests/sign.test.ts
    params = {
        "app_ver": "1.21.0", "client_id": "DC156F861A09E36508B5AF9EBBFD49F1", "country": "US",
        "device_model": "AndroidSDKbuiltforarm64-6.0", "language": "en", "os_type": "0",
        "request_id": "1fbbaba4e2de090c8a39df5c0a0f8163", "source": "0",
        "timestamp": "1777683482", "token": "", "uid": "0",
    }
    assert sign(params) == "d201da49271424d1593163b9355f99bc"


def test_password_hash_matches_reference_vector():
    # Vector from roquerodrigo/fitdays-api src/tests/crypto.test.ts
    assert hash_password("abc") == "C3C92D8A2EE88EEA2FCADE3737066C00"


def test_capp_ver_is_appended_after_sign_and_not_signed():
    q = signed_query("tok", 42, "C" * 32, "US", "en", timestamp=1756000000, request_id="r" * 32)
    assert list(q)[-2:] == ["sign", "capp_ver"]
    assert q["sign"] == sign({k: v for k, v in q.items() if k not in ("sign", "capp_ver")})


def record(data_id, ts, weight_g=80000, bfr=20.0, **kw):
    return {"data_id": data_id, "measured_time": ts, "weight_g": weight_g, "weight_kg": weight_g / 1000,
            "bfr": bfr, "rom": 70.0, "rosm": 40.0, "pp": 15.0, "vwc": 55.0, "bm": 4.0, "sfr": 15.0,
            "uvi": 6, "bmi": 25.0, "bmr": 1700, "bodyage": 35, "adc": 500, "suid": 7, "is_deleted": 0,
            "imp_data_id": f"imp-{data_id}", "ext_data": json.dumps({"bodyScore": 85, "whr": 0.85}), **kw}


def impedance(data_id):
    return {"data_id": f"imp-{data_id}", "is_deleted": 0, "left_arm_kg": 1.21, "right_arm_kg": 1.23,
            "all_body_kg": 8.04, "left_leg_kg": 2.52, "right_leg_kg": 2.54,
            "left_arm_muscle_kg": 3.11, "right_arm_muscle_kg": 3.13, "all_body_muscle_kg": 26.07,
            "left_leg_muscle_kg": 9.52, "right_leg_muscle_kg": 9.54}


def test_rows_derive_masses_and_use_local_time():
    # 2025-01-15 15:20:30 UTC = 10:20:30 in New York
    row = measurement_rows({"weight_list": [record("a", 1736954430)], "impedance_list": [impedance("a")]})["a"]
    assert (row["date"], row["time"]) == ("2025-01-15", "10:20:30")
    assert row["weight_kg"] == 80.0
    assert (row["fat_mass_kg"], row["fat_free_mass_kg"]) == (16.0, 60.0)  # muscle + bone
    assert (row["muscle_kg"], row["skeletal_muscle_kg"], row["bone_kg"]) == (56.0, 32.0, 4.0)
    assert (row["score"], row["whr"], row["visceral_fat"], row["bmr"]) == (85, 0.85, 6, 1700)
    assert isinstance(row["bmr"], int)
    assert (row["fat_left_arm_kg"], row["fat_trunk_kg"], row["fat_right_leg_kg"]) == (1.2, 8.0, 2.5)
    assert (row["muscle_right_arm_kg"], row["muscle_trunk_kg"], row["muscle_left_leg_kg"]) == (3.1, 26.1, 9.5)


def test_deleted_and_weight_only_records():
    rows = measurement_rows({"weight_list": [
        record("del", 1736954430, is_deleted=1),
        record("w", 1736954430, bfr=0, rom=0, adc=0, ext_data=None),
    ]})
    assert list(rows) == ["w"]
    assert rows["w"]["body_fat_pct"] == "" and rows["w"]["fat_free_mass_kg"] == ""
    assert rows["w"]["fat_trunk_kg"] == ""  # no linked impedance record


def test_merge_csv_upserts_by_data_id(tmp_path):
    path = tmp_path / "f.csv"
    merge_csv(path, measurement_rows({"weight_list": [record("b", 1736954430), record("a", 1736868030)]}))
    merge_csv(path, measurement_rows({"weight_list": [record("b", 1736954430, weight_g=81000)]}))
    lines = path.read_text().splitlines()
    assert lines[0] == ",".join(COLUMNS) and len(lines) == 3
    assert lines[2].startswith("2025-01-15,10:20:30,81.0,")


def test_redact_drops_account():
    assert redact({"account": {"token": "x"}, "weight_list": []}) == {"weight_list": []}


def test_region_redirect_and_relogin(tmp_path, monkeypatch):
    calls = []
    replies = [
        {"code": 302, "data": {"domain": "online-us.fitdays.cn/api"}},
        {"code": 0, "data": {"token": "t1", "account": {"uid": 5}}},
        {"code": 1003, "msg": "token expired"},
        {"code": 0, "data": {"token": "t2", "account": {"uid": 5}}},
        {"code": 0, "data": {"weight_list": []}},
    ]
    monkeypatch.setattr(fc, "_post_json", lambda url, body: (calls.append(url), replies.pop(0))[1])
    client = FitdaysClient(Credentials("e@x", "H"), tmp_path / "s.json")
    assert client.sync_from_server(7, login=True) == {"weight_list": []}
    assert calls[1].startswith("https://online-us.fitdays.cn/api/users/login?")
    assert calls[-1].startswith("https://online-us.fitdays.cn/api/sync/syncFromServer?")
    saved = json.loads((tmp_path / "s.json").read_text())
    assert (saved["token"], saved["uid"], saved["host"]) == ("t2", 5, "https://online-us.fitdays.cn")
    assert oct(os.stat(tmp_path / "s.json").st_mode & 0o777) == "0o600"


def test_sync_never_logs_in_without_permission(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(fc, "_post_json", lambda url, body: (calls.append(url), {"code": 0, "data": {}})[1])
    with pytest.raises(FitdaysAuthError, match="--login"):
        FitdaysClient(Credentials("e@x", "H"), tmp_path / "s.json").sync_from_server(7)
    assert calls == []


def test_rejected_token_is_not_renewed_without_permission(tmp_path, monkeypatch):
    session = tmp_path / "s.json"
    session.write_text(json.dumps({"host": fc.DEFAULT_HOST, "client_id": "C", "token": "old", "uid": 5}))
    calls = []
    reply = {"code": 10000, "msg": "token无效"}
    monkeypatch.setattr(fc, "_post_json", lambda url, body: (calls.append(url), reply)[1])
    with pytest.raises(FitdaysAuthError, match="no longer valid"):
        FitdaysClient(Credentials("e@x", "H"), session).sync_from_server(7)
    assert len(calls) == 1 and "/api/sync/syncFromServer?" in calls[0]
    assert json.loads(session.read_text())["token"] == "old"


def test_auth_error_detection(tmp_path, monkeypatch):
    monkeypatch.setattr(fc, "_post_json", lambda url, body: {"code": 10001, "msg": "bad password"})
    with pytest.raises(FitdaysAuthError):
        FitdaysClient(Credentials("e@x", "H"), tmp_path / "s.json").login()
