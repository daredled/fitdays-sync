"""Client for the Fitdays (ICOMON) smart-scale cloud (standard library only).

There is no public API: this speaks the Android app's own protocol, as documented by
github.com/AboveColin/fitdays (Python) and github.com/roquerodrigo/fitdays-api (TypeScript).

- Every call is a JSON POST with signed query parameters (see `signed_query`).
- Errors come back as HTTP 200 with a non-zero `code` in the JSON body.
- Accounts registered in another region answer `code 302` with the host to use.
- There is no OAuth: the login takes the email and a double-MD5 of the password.
- The server keeps only the token of the last login on the account. Logging in here signs the phone app out,
  and the app then stops uploading weigh-ins without warning. So this client never logs in on its own.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_HOST = "https://online-eu.fitdays.cn"
APP_VERSION = "1.27.6"
SIGN_SALT = "hxsign"
PASSWORD_SALT = "hx"
DEVICE_MODEL = "fitdays-sync"
USER_AGENT = "okhttp/4.9.1"
SUCCESS_CODES = (0, 200)
REGION_REDIRECT_CODE = 302
AUTH_ERROR_CODES = {401, 1002, 1003, 10000, 10001}
LOGIN_WARNING = (
    "Fitdays keeps one session per account: logging in here signs the phone app out, and the app "
    "then stops uploading weigh-ins without warning. Run again with --login if you accept that, "
    "then log out and back in on the app."
)

COLUMNS = [
    "date", "time", "weight_kg", "bmi", "body_fat_pct", "fat_mass_kg", "fat_free_mass_kg",
    "muscle_pct", "muscle_kg", "skeletal_muscle_pct", "skeletal_muscle_kg", "protein_pct",
    "protein_kg", "water_pct", "water_kg", "bone_kg", "subcutaneous_fat_pct", "visceral_fat",
    "bmr", "body_age", "score", "whr", "impedance",
    "fat_left_arm_kg", "fat_right_arm_kg", "fat_trunk_kg", "fat_left_leg_kg", "fat_right_leg_kg",
    "muscle_left_arm_kg", "muscle_right_arm_kg", "muscle_trunk_kg", "muscle_left_leg_kg",
    "muscle_right_leg_kg", "suid", "data_id",
]

# Segment columns <- fields of the `impedance_list` record linked by `imp_data_id`
# (only 8-electrode scales send them).
SEGMENTS = {
    "fat_left_arm_kg": "left_arm_kg", "fat_right_arm_kg": "right_arm_kg",
    "fat_trunk_kg": "all_body_kg", "fat_left_leg_kg": "left_leg_kg",
    "fat_right_leg_kg": "right_leg_kg",
    "muscle_left_arm_kg": "left_arm_muscle_kg", "muscle_right_arm_kg": "right_arm_muscle_kg",
    "muscle_trunk_kg": "all_body_muscle_kg", "muscle_left_leg_kg": "left_leg_muscle_kg",
    "muscle_right_leg_kg": "right_leg_muscle_kg",
}


class FitdaysError(RuntimeError):
    """Any failure talking to Fitdays (HTTP error, API code, bad credentials)."""


class FitdaysAuthError(FitdaysError):
    """The token was rejected."""


@dataclass(frozen=True)
class Credentials:
    email: str
    password_hash: str
    country: str = "US"
    language: str = "en"

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> Credentials:
        """Read FITDAYS_EMAIL / FITDAYS_PASSWORD (optional FITDAYS_COUNTRY / FITDAYS_LANGUAGE).

        Real environment variables win over values in `env_file`. Only the password digest is kept.
        """
        values = _read_env_file(env_file) if env_file and env_file.exists() else {}
        values.update({k: v for k, v in os.environ.items() if k.startswith("FITDAYS_")})
        missing = [k for k in ("FITDAYS_EMAIL", "FITDAYS_PASSWORD") if not values.get(k)]
        if missing:
            raise FitdaysError(f"Missing {', '.join(missing)} (environment or {env_file})")
        return cls(
            values["FITDAYS_EMAIL"],
            hash_password(values["FITDAYS_PASSWORD"]),
            values.get("FITDAYS_COUNTRY") or cls.country,
            values.get("FITDAYS_LANGUAGE") or cls.language,
        )


def _read_env_file(path: Path) -> dict[str, str]:
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def default_session_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "fitdays-sync" / "session.json"


# ---- signing -----------------------------------------------------------------

def _md5(value: str) -> str:
    return hashlib.md5(value.encode()).hexdigest()


def hash_password(plaintext: str) -> str:
    """MD5(MD5(password + "hx")), both uppercase hex: the second MD5 is fed the uppercase digest."""
    return _md5(_md5(plaintext + PASSWORD_SALT).upper()).upper()


def signed_query(token: str, uid: int | str, client_id: str, country: str, language: str,
                 timestamp: int | None = None, request_id: str | None = None) -> dict[str, str]:
    """Public query parameters plus `sign`.

    sign = MD5(form-urlencode("k1=v1&k2=v2..." sorted by key + "hxsign")), lowercase.
    `capp_ver` goes after `sign` and is not signed.
    """
    params = {
        "app_ver": APP_VERSION,
        "client_id": client_id,
        "country": country,
        "device_model": DEVICE_MODEL,
        "language": language,
        "os_type": "0",
        "request_id": request_id or _md5(str(uuid.uuid4())),
        "source": "0",
        "timestamp": str(int(time.time() if timestamp is None else timestamp)),
        "token": token or "",
        "uid": str(uid or 0),
    }
    params["sign"] = sign(params)
    params["capp_ver"] = APP_VERSION
    return params


def sign(params: dict[str, str]) -> str:
    joined = "&".join(f"{k}={params[k]}" for k in sorted(params)) + SIGN_SALT
    return _md5(urllib.parse.quote_plus(joined, safe="*"))


# ---- transport ---------------------------------------------------------------

def _post_json(url: str, body: dict, retries: int = 3) -> dict:
    data = json.dumps(body).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json;charset=UTF-8",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if (e.code == 429 or e.code >= 500) and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise FitdaysError(f"HTTP {e.code} from {url.split('?')[0]}") from e
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise FitdaysError(f"Cannot reach {url.split('?')[0]}: {e.reason}") from e
        except json.JSONDecodeError as e:
            raise FitdaysError(f"Non-JSON response from {url.split('?')[0]}") from e
    raise AssertionError("unreachable")


def _host_from_domain(domain: str) -> str:
    """The 302 payload's `domain` may or may not carry a scheme or a trailing /api."""
    domain = domain.rstrip("/")
    if not domain.startswith("http"):
        domain = "https://" + domain
    return domain.removesuffix("/api")


class FitdaysClient:
    """The session (token, uid, host, client id) is stored on disk; re-login uses the credentials."""

    def __init__(self, credentials: Credentials, session_file: Path | None = None):
        self.creds = credentials
        self.session_file = session_file or default_session_file()
        self.session = self._load_session()

    def _load_session(self) -> dict:
        if self.session_file.exists():
            return json.loads(self.session_file.read_text())
        # One client id per installation, so the traffic looks like a single app install.
        return {"host": DEFAULT_HOST, "client_id": _md5(str(uuid.uuid4())).upper()}

    def _save_session(self) -> None:
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.session_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.session, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.session_file)

    def _request(self, path: str, body: dict, authenticated: bool = True) -> dict:
        """Signed POST; follows one region redirect and returns `data`."""
        for _ in range(2):
            params = signed_query(
                self.session.get("token", "") if authenticated else "",
                self.session.get("uid", 0) if authenticated else 0,
                self.session["client_id"], self.creds.country, self.creds.language,
            )
            url = f"{self.session['host']}/api/{path}?{urllib.parse.urlencode(params)}"
            resp = _post_json(url, body)
            code = resp.get("code")
            data = resp.get("data")
            if code == REGION_REDIRECT_CODE and isinstance(data, dict) and data.get("domain"):
                host = _host_from_domain(data["domain"])
                if host != self.session["host"]:
                    self.session["host"] = host
                    continue
            break
        if code in SUCCESS_CODES:
            return data if isinstance(data, dict) else {}
        msg = resp.get("msg") or ""
        if code in AUTH_ERROR_CODES or any(w in msg.lower() for w in ("token", "login", "登录")):
            raise FitdaysAuthError(f"Fitdays rejected the credentials or token: code {code} {msg}".strip())
        raise FitdaysError(f"Fitdays {path} failed: code {code} {msg}".strip())

    def login(self) -> None:
        data = self._request("users/login", {"email": self.creds.email,
                                             "password": self.creds.password_hash},
                             authenticated=False)
        account = data.get("account") or {}
        token = data.get("token") or account.get("token")
        uid = account.get("uid") or data.get("uid")
        if not token or uid is None:
            raise FitdaysAuthError("Fitdays login returned no token or uid")
        self.session.update(token=token, uid=uid)
        self._save_session()

    def sync_from_server(self, days: int, login: bool = False) -> dict:
        """Everything recorded in the last `days` days.

        Uses the saved token only. A login signs the phone app out (see the module docstring), so it
        happens only with `login=True`, and then only if there is no token or the token is rejected.
        The API takes `start_time` as the newest bound and `end_time` as the oldest.
        """
        now = int(time.time())
        body = {"start_time": now, "end_time": now - days * 86400}
        if not self.session.get("token"):
            if not login:
                raise FitdaysAuthError(f"No saved session. {LOGIN_WARNING}")
            self.login()
        try:
            data = self._request("sync/syncFromServer", body)
        except FitdaysAuthError:
            if not login:
                raise FitdaysAuthError("The saved session is no longer valid (the Fitdays app has "
                                       f"probably logged in since). {LOGIN_WARNING}") from None
            self.login()
            data = self._request("sync/syncFromServer", body)
        self._save_session()  # keeps a host learned from a region redirect
        return data


# ---- parsing -----------------------------------------------------------------

def _num(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _pos(value) -> float | None:
    """The API uses 0 for "not measured"."""
    n = _num(value)
    return n if n else None


def _parse_ext(value) -> dict:
    """`ext_data` arrives as a JSON string, occasionally as a dict or null."""
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value) if value else {}
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _fmt(value: float | None, digits: int = 1) -> str | float | int:
    if value is None:
        return ""
    return round(value) if digits == 0 else round(value, digits)


def measurement_rows(data: dict) -> dict[str, dict]:
    """One row per non-deleted weigh-in in `weight_list`, keyed by data_id.

    The API sends body composition as percentages of weight; masses are derived here.
    Fat-free mass follows the app's definition, muscle + bone (not weight - fat).
    Segment masses come from the linked `impedance_list` record.
    Times are converted to the machine's local timezone (the API sends only unix seconds).
    """
    impedances = {i.get("data_id"): i for i in data.get("impedance_list") or [] if not i.get("is_deleted")}
    rows = {}
    for w in data.get("weight_list") or []:
        if w.get("is_deleted"):
            continue
        grams = _num(w.get("weight_g"))
        weight = grams / 1000 if grams else _num(w.get("weight_kg"))
        if not weight:
            continue
        ext = _parse_ext(w.get("ext_data"))

        def mass(pct):
            return None if pct is None else pct * weight / 100

        fat_pct = _pos(w.get("bfr"))
        muscle_pct, sm_pct = _pos(w.get("rom")), _pos(w.get("rosm"))
        protein_pct, water_pct = _pos(w.get("pp")), _pos(w.get("vwc"))
        muscle_kg, bone_kg = mass(muscle_pct), _pos(w.get("bm"))
        imp = impedances.get(w.get("imp_data_id")) or {}
        when = datetime.fromtimestamp(int(w["measured_time"])).astimezone()
        data_id = str(w.get("data_id") or w.get("id"))
        rows[data_id] = {
            "date": when.strftime("%Y-%m-%d"),
            "time": when.strftime("%H:%M:%S"),
            "weight_kg": _fmt(weight, 2),
            "bmi": _fmt(_pos(w.get("bmi"))),
            "body_fat_pct": _fmt(fat_pct),
            "fat_mass_kg": _fmt(mass(fat_pct)),
            "fat_free_mass_kg": _fmt(None if muscle_kg is None or bone_kg is None else muscle_kg + bone_kg),
            "muscle_pct": _fmt(muscle_pct),
            "muscle_kg": _fmt(muscle_kg),
            "skeletal_muscle_pct": _fmt(sm_pct),
            "skeletal_muscle_kg": _fmt(mass(sm_pct)),
            "protein_pct": _fmt(protein_pct),
            "protein_kg": _fmt(mass(protein_pct)),
            "water_pct": _fmt(water_pct),
            "water_kg": _fmt(mass(water_pct)),
            "bone_kg": _fmt(bone_kg),
            "subcutaneous_fat_pct": _fmt(_pos(w.get("sfr"))),
            "visceral_fat": _fmt(_pos(w.get("uvi"))),
            "bmr": _fmt(_pos(w.get("bmr")), 0),
            "body_age": _fmt(_pos(w.get("bodyage")), 0),
            "score": _fmt(_pos(ext.get("bodyScore")), 0),
            "whr": _fmt(_pos(ext.get("whr")), 2),
            "impedance": _fmt(_pos(w.get("adc"))),
            **{col: _fmt(_pos(imp.get(field))) for col, field in SEGMENTS.items()},
            "suid": str(w.get("suid") or ""),
            "data_id": data_id,
        }
    return rows


def redact(data: dict) -> dict:
    """Drop the `account` block (it carries the token and email) before saving raw data."""
    return {k: v for k, v in data.items() if k != "account"}
