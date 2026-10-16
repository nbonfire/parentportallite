# Edulog Parent Portal Lite — school-bus GPS (CLI + Home Assistant)

Tools for reading **live school-bus GPS** from KarrosTech's
**Edulog Parent Portal Lite** (`com.karrostech.parentportallite.android`,
v1.15.9), the *Lite* companion app. Reverse-engineered from the APK; see
[FLOW.md](FLOW.md) for the verified protocol notes.

> **Product note:** this is for *Edulog Parent Portal **Lite*** only. The
> full "Edulog Parent Portal" is a different product with a different
> account/auth flow and is *not* supported by these tools.

Everything is scoped to **your own parent account**: the CLI and the
integration can only see the districts (tenants) your account is
subscribed to, and the buses your account has followed.

## Repo layout

| Path | What it is |
|---|---|
| `bus_client.py` | Standalone Python CLI: `login` / `district` / `follow` / `stream` |
| `custom_components/edulog_lite/` | **Home Assistant HACS integration** — per-bus distance/GPS/bearing sensors + an "arriving" binary sensor per district |
| `FLOW.md` | Full reverse-engineering notes + live-verified API contract |
| `AGENTS.md` | Conventions for agents working in this repo |

## Home Assistant (HACS) install

Requires HA ≥ 2026.1. No extra Python packages (uses stdlib + `requests`,
both already in HA core).

1. HACS → ⋮ menu → **Custom repositories** → add this repository's URL
   (category: **Integration**).
2. Search **Edulog Parent Portal Lite** in HACS → *Install*.
3. HA → Settings → **Devices & Services** → *Add integration* →
   **Edulog Parent Portal Lite** → sign in with your parent-portal
   account's email/password → **pick your district** from the list.
   One entry per district (add another entry for a second district).
4. Options: poll interval (default 30 s), arrival threshold in meters
   (default 500), and an optional watch-location override (defaults to
   your HA home location).

Each followed bus gets devices under the district containing:

- `… / Distance` — meters to the watch location (unknown when the bus has
  no fresh GPS fix)
- `… / GPS last update` — timestamp of the last fix
- `… / Bearing to watch location` — degrees
- `… / Arriving` — binary sensor on when the bus is inside the threshold
  (unknown rather than off when there's no fresh fix)
- `… / GPS` — the bus on HA's map (a `device_tracker`; state is
  `home` / `not_home` / the name of any zone the bus is inside)

### Your bus on the map

```yaml
# any dashboard, add a card
type: map
entities:
  - device_tracker.m14_gps           # one per followed bus
  - device_tracker.fram_m31_gps
```

The icon plots wherever the bus's latest fresh fix is. Buses without a
recent fix disappear from the map (state `unknown`) rather than sitting
at a stale position. Want arrival semantics at a *place* instead of a
distance? Create a [zone](https://www.home-assistant.io/integrations/zone)
around the school or your street with a ~150–300 m radius — the tracker's
state changes to that zone's name while the bus is inside it, which makes
a clean automation trigger.

### "Bus is close to arrival" notification

```yaml
# configuration.yaml → automations
trigger:
  - trigger: state
    entity_id: binary_sensor.bus_a_arriving      # the "Arriving" sensor
    from: "off"
    to: "on"
    for: "00:00:15"           # debounce GPS jitter
action:
  - action: notify.mobile_app_phone
    data:
      message: >
        {{ trigger.to_state.name }} is
        {{ state('sensor.bus_a_distance') | round(0) }} m away
      data:
        tag: edulog_arrival
        ttl: 0
        priority: high
        channel: Bus arriving
```

The `for: 15s` is what makes this a real "arriving" signal: a single GPS
blip won't fire it, and it stays `on` until the bus leaves the threshold.

## CLI (`bus_client.py`)

Python 3.14 venv with `requests` only (`.venv/` is pre-created via uv).

```sh
.venv/bin/python bus_client.py login                   # EMAIL/PASSWORD from .env
.venv/bin/python bus_client.py district [code|tenantId]
.venv/bin/python bus_client.py follow <vehicleId>
.venv/bin/python bus_client.py stream [code|tenantId] [poll_sec]
```

`.env` (gitignored) holds `EMAIL` / `PASSWORD`; the token cache
`.bus_client_cache.json` is written `0600` and gitignored.

## Notes & etiquette

- Keep the poll interval ≥ 30 s — this talks to a third-party production
  service on the app's own endpoints. Aggressive polling risks rate
  limits or account flags.
- The `ApiKey` / `clientSecret` constants in the code are the app's
  **public client identifiers**, embedded in the distributed APK — not
  personal credentials. No personal data (email, passwords, tenant/vehicle
  IDs, coordinates) is stored in this repository.
- Buses without a recent GPS fix show as **unknown**, not as a position —
  treat missing as "no location" in your automations.
