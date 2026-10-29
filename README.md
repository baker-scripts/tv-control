# tv-control

A web panel and MCP server for a small fleet of TVs: NVIDIA Shields and Fire TVs over ADB, LG webOS TVs over [aiowebostv](https://github.com/home-assistant-libs/aiowebostv) and Wake-on-LAN. One container serves both, driven by one `devices.yml`.

## Features

- Remote control per device: D-pad, media, volume, and home keys; app launch with quick-launch buttons; on-screen toasts.
- Fast keys on Shields: after a one-time pairing, keys go over the Android TV Remote v2 protocol ([androidtvremote2](https://github.com/tronikos/androidtvremote2)) in milliseconds instead of about 1 s per `adb shell input`. Unpaired Shields and any Remote v2 failure fall back to ADB.
- Live status over Server-Sent Events (`/events`, one update per device every 10 s), so cards refresh without polling.
- Installable PWA: web manifest and icons, content-hashed static assets served `immutable`.
- Power, wake (WoL through an SSH relay on the TV's LAN), input switching, and sleep timers. LG webOS inputs switch through aiowebostv; Fire TV inputs switch through a passthrough intent (needs `input_ids`, does not wake the TV).
- Shield "find my remote" (locator), device status, screenshots, and an admin screen preview.
- Health checks, including paired TVs: a Shield with `paired_tv` reports `standby` when its TV is off, and a fault only when the TV is unreachable while the Shield is awake and playing.
- Site LAN scan for new ADB or webOS devices, with an admin approve step.
- MCP server (streamable HTTP) with the same verbs, for an AI agent or a script. `python3 /app/app.py call <verb> key=value` calls it from inside the container.
- An app denylist: `denied_apps` entries are never launched, by the panel or by MCP.

## Security model

The panel is meant to sit behind a reverse proxy with forward auth (built and tested with [Authentik](https://goauthentik.io/) proxy providers).

- Every panel route except `/healthz` needs the header `X-TV-Proxy-Key` equal to `TV_PROXY_KEY`. Only the reverse proxy sets it, so a client that reaches the port directly cannot forge the user headers. Without `TV_PROXY_KEY` the panel answers 503.
- The user comes from `X-authentik-username`, and groups from `X-authentik-groups`.
- Group `tv-admin` sees and drives every device. Group `tv-site-<site>` sees only that site's devices and can use key, launch, sleep timer, power, wake, input, and toast.
- Reboot, pair (webOS pairing and Shield fast-key pairing), baseline, lockdown, scan, approve, screenshot, and screen preview are admin-only. A denial returns 403 with `error`, `cause`, and `next_step`.
- POST routes need `X-TV-Panel: 1`, which a cross-site form cannot send. Real baseline and lockdown runs need the device name typed as confirmation; MCP can only dry-run them.
- Power and input changes: at most one per 5 s per device (429). Every POST writes one audit line to stdout.
- Responses carry a strict CSP (`script-src 'self'`, no inline script or style, `frame-ancestors 'none'`) and `X-Frame-Options: DENY`.
- MCP on `MCP_PORT` requires `Authorization: Bearer $TV_CONTROL_MCP_TOKEN`; without a token it answers 503.
- tv-control never disconnects devices from the shared ADB server, powers a Shield off, or wipes or removes apps. Static tests guard this.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `TV_PROXY_KEY` | (unset) | Shared secret the reverse proxy sends as `X-TV-Proxy-Key`. Required for the panel. |
| `TV_CONTROL_MCP_TOKEN` | (unset) | Bearer token for `/mcp`. Required for MCP. |
| `TV_PANEL_URL` | `the panel URL` | Public panel URL, shown in error messages. |
| `TS_IP` | (unset) | Extra bind address (for example a Tailscale IP) next to 127.0.0.1. Bound with `IP_FREEBIND`, so it may come up later. |
| `PANEL_PORT` | `8011` | Panel and `/healthz`. |
| `MCP_PORT` | `8012` | MCP streamable HTTP at `/mcp`. |
| `DEVICES_FILE` | `/scripts/devices.yml` | Device inventory. |
| `SCRIPTS_DIR` | `/scripts` | Optional `deploy-config.sh`, `firetv-settings.sh`, and `lockdown.sh` for the baseline and lockdown verbs. Not shipped here. |
| `HOST_CONFIG_DIR` | `/config` | Host path of the `/config` volume, printed for CLI screenshots. |
| `REMOTE_DIR` | `/config/remote` | Android TV Remote v2 client certificates and pairing markers, one set per Shield. |
| `APP_CACHE_FILE`, `SLEEP_TIMERS_FILE`, `UNDO_DIR`, `ENROLL_DIR`, `SCREENSHOT_DIR`, `WEBOS_KEYS_FILE`, `WOL_KEY_FILE` | under `/config` | State files. |

tv-control runs `adb` against the server on `127.0.0.1:5037`. If another container shares that server, keep both on the same adb version, or each client restarts the server.

## devices.yml

See [`devices.example.yml`](devices.example.yml). Keep the real file outside this repo and mount it read-only.

| Key | Purpose |
| --- | --- |
| `devices[].name` | Unique device name, used by every verb. |
| `devices[].type` | `shield`, `firestick`, `firetv`, or `webos`. |
| `devices[].site` | Site key; must exist under `sites`. Drives `tv-site-<site>` access. |
| `devices[].ip`, `port` | Primary ADB (5555) or webOS address. |
| `devices[].tailnet_fqdn` | Fallback address, tried when `ip` fails. |
| `devices[].capabilities` | Any of `find_remote`, `wake`, `power`, `inputs`, `sleep_timer`. |
| `devices[].label`, `mac`, `tv_control`, `status` | Display name, WoL MAC, `best-effort` CEC, `pending` (skipped until it has an address). |
| `devices[].inputs` | Input key to button label, for example `HDMI_1: Shield`. |
| `devices[].input_ids` | Fire TV only: input key to the Android TvInputInfo id (from `adb shell dumpsys tv_input`). An input without an id is shown as unavailable. |
| `devices[].paired_tv` | The device name of the TV a Shield feeds; drives the paired-TV health status. |
| `denied_apps` | Substrings of package names or titles that are never launched. |
| `quick_apps` | Quick-launch buttons: `label`, `id` list, optional `title` list. |
| `sites.<site>.cidr` | Private LAN that the scan probes. |
| `sites.<site>.wol_relay` | `user@host` that sends WoL for that site over SSH. |
| `type_profiles.<type>` | Read only by the optional `SCRIPTS_DIR` scripts: `debloat_lists`, `cec_on` (Global settings set to 1), and `keep_apps` (never disabled). tv-control ignores it. |

## Compose example

```yaml
services:
  tv-control:
    image: ghcr.io/baker-scripts/tv-control:latest
    container_name: tv-control
    network_mode: host
    user: "1000:1000"
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    tmpfs: [/tmp]
    environment:
      TV_PROXY_KEY: ${TV_PROXY_KEY}
      TV_CONTROL_MCP_TOKEN: ${TV_CONTROL_MCP_TOKEN}
      TV_PANEL_URL: https://tv.example.com
      HOME: /config
    volumes:
      - ./devices.yml:/scripts/devices.yml:ro
      - ./config:/config
    restart: unless-stopped
```

`HOME=/config` keeps the ADB key (`/config/.android/adbkey`) on the volume. Point the reverse proxy at `127.0.0.1:8011` (or `TS_IP:8011`) and have it set `X-TV-Proxy-Key`.

## Pair fast keys (Shield)

1. Open the panel as a `tv-admin` member. The Shield must be reachable on its `ip` (Remote v2 uses TCP 6466 and 6467).
2. On the Shield card, press "Pair fast keys". The Shield shows a code on screen.
3. Type the code into the prompt. The certificate is saved under `REMOTE_DIR`, and later key presses use Remote v2.

Wake, sleep, and power stay on ADB, so they work while the Shield's remote service is down. To re-pair, delete that Shield's files under `REMOTE_DIR` and repeat the steps.

## Development

```bash
uv sync --locked
uv run --locked ruff check . && uv run --locked ruff format --check .
uv run --locked mypy .
uv run --locked pytest             # unit tests, 80% coverage gate
uv run --locked playwright install chromium
uv run --locked pytest -m e2e --no-cov   # Playwright smoke at 375 px and 1280 px
```

Tests need `yq` (mikefarah/yq) on `PATH`. Set `DEVICES_FILE` to lint your own inventory with `tests/test_lint.py`.

## License

GPL-3.0. See [LICENSE](LICENSE).
