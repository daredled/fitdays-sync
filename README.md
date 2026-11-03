# fitdays-sync

Download your own weigh-ins from the Fitdays (ICOMON) smart-scale cloud and keep them in a CSV,
one row per weigh-in, with body composition.

Fitdays has no public API. This tool speaks the Android app's own protocol, as documented by
[AboveColin/fitdays](https://github.com/AboveColin/fitdays) (Python) and
[roquerodrigo/fitdays-api](https://github.com/roquerodrigo/fitdays-api) (TypeScript).
It may stop working if Fitdays changes its app. It only reads; it never writes to your account.

- Standard library only, no runtime dependencies.
- Follows the region redirect for accounts outside Europe and remembers the host.
- Never logs in on its own: see [One session per account](#one-session-per-account).
- Re-running a sync updates the weigh-ins it downloads and never duplicates rows.

## 1. Credentials

Put your Fitdays login in a `.env` file. Real environment variables also work.

```
FITDAYS_EMAIL=...
FITDAYS_PASSWORD=...
# optional
FITDAYS_COUNTRY=US
FITDAYS_LANGUAGE=en
```

The login endpoint takes `MD5(MD5(password + "hx"))`, so the plaintext is never sent. That digest is still
password-equivalent for this API. The session file stores only the token, the user id, the host and a client id,
with `0600` permissions, at `~/.config/fitdays-sync/session.json` (change it with `--session-file`).

## 2. Sync

```bash
fitdays-sync sync --days 30 --csv fitdays.csv --raw-dir fitdays_raw/
```

`sync` uses the saved session only. If there is none, or Fitdays rejects it, it stops with an error;
add `--login` to log in (or run `fitdays-sync auth`), knowing that it signs the phone app out.

| Option | Default | Meaning |
|---|---|---|
| `--days` | 30 | How many days back to download |
| `--csv` | `fitdays.csv` | CSV to create or update |
| `--raw-dir` | – | Also save the raw API response as JSON, without the `account` block (token, email) |
| `--login` | off | Log in if the saved session is missing or rejected. Signs the phone app out |

Columns: `date, time` (local time of this machine), `weight_kg, bmi, body_fat_pct, fat_mass_kg, fat_free_mass_kg,
muscle_pct, muscle_kg, skeletal_muscle_pct, skeletal_muscle_kg, protein_pct, protein_kg, water_pct, water_kg,
bone_kg, subcutaneous_fat_pct, visceral_fat, bmr, body_age, score, whr, impedance`, ten segment columns, `suid, data_id`.

The API sends composition as percentages of weight; the `_kg` columns are derived from them and can differ from
the app by 0.1 kg because of rounding. `fat_free_mass_kg` follows the app: muscle + bone.

Scales with 8 electrodes also send segment masses (`fat_*_kg`, `muscle_*_kg` for each arm, the trunk and each leg),
taken from the impedance record linked to each weigh-in.

### One session per account

Fitdays keeps only the token of the last login on an account, whatever the device or platform: a new login
makes the previous token answer `code 10000` ("token无效", invalid token). Logging in from this tool therefore
signs the phone app out. The app does not seem to notice: it keeps measuring and showing weigh-ins, but they stop reaching
the cloud. After a login here, log out and back in on the app. The same was found independently in
[Kinetrail](https://github.com/ErikaAlk/Kinetrail/blob/main/research/FITDAYSPLUS.md), which also applies to Fitdays+.

So this tool logs in only when asked to. Once the app logs in again, the token saved here stops working, and `sync`
fails until you choose to log in with `--login`.
