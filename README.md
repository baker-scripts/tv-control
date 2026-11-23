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
- One app policy for every verb that opens an app (`launch_app`, `open_app`, `play_title`, panel launch and open): `denied_apps` (id and label) are never launched, a device with `allowed_apps` opens only those ids, and app stores, Settings, and account sign-in apps are never launch targets. Apps are named by package id or by name (see "App names and policy").
- On-screen messages under a policy in `devices.yml` (`messages:`): who may send, when, how long, and a per-TV history. Android messages go through [TvOverlay](https://github.com/gugutab/TvOverlay).
- Screen state and guarded navigation on Android: `current_screen`, `what_is_on`, `open_app`, `go_home`, `play_title` (Plex deep link), `navigate`, and optional on-screen narration of each step.
- A plain `POST /message` on the MCP port (bearer token) for a Home Assistant `rest_command` operator push.

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
| `TAUTULLI_URL` | (unset) | Tautulli base URL, including any `http_root`. With `TAUTULLI_API_KEY`, status of a Plex client adds `now_playing` from the Plex server's sessions. |
| `TAUTULLI_API_KEY` | (unset) | Tautulli API key, sent in the `X-Api-Key` header only. |
| `HOST_CONFIG_DIR` | `/config` | Host path of the `/config` volume, printed for CLI screenshots. |
| `TV_CONTROL_URL` | `http://127.0.0.1:$MCP_PORT/mcp` | MCP endpoint for `app.py call`, so a CLI on another host can call a remote instance. |
| `TV_CONTROL_COMMIT_URL` | this repo's `/commit/` URL | Prefix for the commit link in the panel footer. |
| `TV_CONTROL_VERSION`, `TV_CONTROL_GIT_SHA`, `TV_CONTROL_GIT_DATE` | `unknown` | Panel footer. Set as build args by CI; `BUILD_DATE` is written at image build. |
| `MESSAGE_LOG_DIR` | `/config/messages` | Per-TV message history, one JSON file per device. |
| `REMOTE_DIR` | `/config/remote` | Android TV Remote v2 client certificates and pairing markers, one set per Shield. |
| `APP_CACHE_FILE`, `SLEEP_TIMERS_FILE`, `UNDO_DIR`, `ENROLL_DIR`, `SCREENSHOT_DIR`, `WEBOS_KEYS_FILE`, `WOL_KEY_FILE` | under `/config` | State files. |

Image build args: `TV_UID` and `TV_GID` (default `1000`) name the `tvcontrol` user. ssh (the WoL relay) refuses a uid with no passwd entry, so run the container as that uid, or rebuild with your own, or mount a passwd entry for it. `ANDROID_TOOLS` pins the adb version.

tv-control runs `adb` against the server on `127.0.0.1:5037`. If another container shares that server, keep both on the same adb version, or each client restarts the server.

## devices.yml

See [`devices.example.yml`](devices.example.yml). Keep the real file outside this repo and mount it read-only.

| Key | Purpose |
| --- | --- |
| `devices[].name` | Unique device name, used by every verb. |
| `devices[].type` | `shield`, `firestick`, `firetv`, or `webos`. |
| `devices[].site` | Site key; must exist under `sites`. Drives `tv-site-<site>` access. |
| `devices[].ip`, `port` | Primary ADB (5555) or webOS address. |
| `devices[].tailnet_ip`, `tailnet_fqdn` | Fallback addresses, tried in that order when `ip` fails. |
| `devices[].household` | Groups TVs for the message policy: narration depth and which household an assistant reply may reach. |
| `devices[].narration` | `every_step`, `start_end`, or `off`; overrides the household's narration depth. |
| `devices[].overlay_port` | TvOverlay REST port on the device (default 5001). |
| `devices[].volume_via` | Name of a webOS TV that takes this device's volume and mute keys. |
| `devices[].capabilities` | Any of `find_remote`, `wake`, `power`, `inputs`, `sleep_timer`. |
| `devices[].label`, `mac`, `tv_control`, `status` | Display name, WoL MAC, `best-effort` CEC, `pending` (skipped until it has an address). |
| `devices[].inputs` | Input key to button label, for example `HDMI_1: Shield`. |
| `devices[].input_keys` | Fire TV only: input key to an Android keyevent name, tried before `input_ids`. |
| `devices[].input_ids` | Fire TV only: input key to the Android TvInputInfo id (from `adb shell dumpsys tv_input`). An input without an id is shown as unavailable. |
| `devices[].plex_player` | Optional Plex player name for this device. A Plex session matches when its player equals the device `name`, `label`, `plex_player`, or Android device name (case and punctuation ignored). |
| `devices[].paired_tv` | The device name of the TV a Shield feeds; drives the paired-TV health status, and an agent's input switch or launch on that TV is refused while the device plays or is open in an app. |
| `devices[].allowed_apps` | Optional list of package (or webOS app) ids. When present, automation opens only these ids on the device; an empty list opens nothing. Absent keeps every installed app. |
| `devices[].app_aliases` | Optional spoken name to id (or list of ids) for this device; overrides the top-level `app_aliases`. |
| `denied_apps` | Substrings of package names or titles that are never launched. |
| `quick_apps` | Quick-launch buttons: `label`, `id` list, optional `title` list. |
| `app_aliases` | Optional spoken name to id (or list of ids; the first one installed wins). |
| `messages` | On-screen message policy; required for `toast`, `narrate`, and `narrate=true`. See below. |
| `sites.<site>.cidr` | Private LAN that the scan probes. |
| `sites.<site>.wol_relay` | `user@host` that sends WoL for that site over SSH. |
| `type_profiles.<type>` | Read only by the optional `SCRIPTS_DIR` scripts: `debloat_lists`, `cec_on` (Global settings set to 1), and `keep_apps` (never disabled). tv-control ignores it. |

## On-screen message policy

The `messages:` block in `devices.yml` is the only source; tv-control re-reads it on every call.

| Key | Effect |
| --- | --- |
| `title`, `corner` | TvOverlay title and corner on every Android message. |
| `duration` | `ceil(words * seconds_per_5_words / 5)` seconds, clamped to `floor_s`..`cap_s`. |
| `quiet_hours` | Device-local `start` and `end` (HH:MM). An unrequested message inside the window, or to a sleeping TV, is dropped with `shown: false`. |
| `unrequested_kinds` | The only kinds a sender may push without a request: `media_ready`, `reply_waiting`, `operator_push`. |
| `narration.default`, `narration.households` | Narration depth: per device, then per household, then the default. |
| `assistant.name`, `assistant.sender` | The narration intro reads "Hi, this is <name>. I will navigate now."; `sender` is the id the assistant passes to `toast`. |

Senders: `sender=operator` reaches any TV. The assistant sender needs `household=<the TV's household>` and either `requested=true` or a named kind (never `operator_push`); otherwise `wrong_household` or `not_allowed`. `sender` is declared, not authenticated: the MCP bearer token is the trust boundary.

An Android message is `shown: true` only after HTTP 200 from TvOverlay and a visible overlay window. `image` takes an `http(s)` URL or `mdi:<name>`. The panel lists the last 50 messages per TV (`GET /api/messages/<device>`).

```text
toast device=<tv> message="Your movie is ready." sender=assistant kind=media_ready household=<household>
```

```bash
curl -sS -X POST "http://<TS_IP>:8012/message" -H "Authorization: Bearer $TV_CONTROL_MCP_TOKEN" \
  -H "Content-Type: application/json" -d '{"device":"<tv>","message":"Dinner is ready."}'
```

## Screen state and navigation (Android)

| Verb | Behavior |
| --- | --- |
| `current_screen` | Read-only: app and activity in front, focus window, wakefulness, media sessions, and the focused view. `focus=false` skips the `uiautomator` dump. |
| `what_is_on` | Read-only: app in front, playback state, and the media-session title. |
| `open_app`, `go_home` | Wake the device if needed, act, then confirm the app in front (`not_confirmed` otherwise). `go_home` opens Projectivy on a Shield and presses HOME on Fire OS. |
| `play_title` | Plex deep link from a server `machine_id` and a `rating_key`, then wait for Plex's media session to play. An optional `view_offset_ms` adds `viewOffset` to the link so playback starts at that point. tv-control holds no Plex token. |
| `navigate` | Up to 10 keys; checks `expect_app` (and `expect_focus` before the first key) before every key and stops with `screen_mismatch`. |
| `narrate` | Shows the intro, then each `|`-separated step, 6 s apart, at the device's narration depth. |

The in-use refusal skips a Shield whose app in front was confirmed by `open_app` or `go_home` in the last 120 s while nothing plays, so a multi-step flow runs without `force`.

## App names and policy

`launch_app` and `open_app` take a package id or a name. A name is casefolded with punctuation dropped, then matched in this order against the device's cached app list (`list_apps`):

1. exact label, 2. exact id, 3. an `app_aliases` entry, 4. a package id as given, 5. a unique label prefix.

On a device with `allowed_apps`, an exact label shared by several apps prefers the allowed one, and a prefix matches only allowed apps. An ambiguous or unknown name is refused with `ambiguous_app` or `unknown_app` and a `candidates` list. The resolved id and every known label then pass the policy: `denied_apps` (`denylist`), protected targets (`protected_target`), and `allowed_apps` (`allowlist`). `force=true` skips only the in-use refusal, never the app policy.

The allow list never depends on the device's app list: Android's `cmd package query-activities` (used by `list_apps`) omits disabled and hidden packages, and the policy checks the resolved id itself.

In-use refusal (agent verbs): a Shield or Fire OS device that is awake and playing, or open in an app other than its home screen, TvOverlay, or (Fire OS) an HDMI input; an LG TV whose media pipeline reports `playing` or `paused`. Not readable: an LG app with no media session (a menu or store page), and what plays on an HDMI input with no paired device.

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
      MESSAGE_LOG_DIR: /config/messages
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

Tests need `yq` (mikefarah/yq) on `PATH`. They read `devices.example.yml`; set `DEVICES_FILE` to lint your own inventory with `tests/test_lint.py`.

This is a public repo: nothing personal goes in it. `scripts/check_pii.py` runs as a pre-commit hook (`pre-commit install`), in CI, and from `tests/test_leaks.py`. It fails on private, CGNAT, or public IPv4 addresses, MAC addresses, emails, phone numbers, street addresses, `*.internal` and `tail<hex>.ts.net` names, and deploy-specific paths in any tracked file. Real names, device labels, serials, and hosts are matched from a list kept outside the repo: set `PII_TERMS` (one term per line, the CI secret of the same name) or `PII_TERMS_FILE` (default `~/.config/pii-guard/terms.txt`); a line starting with `re:` is a regex. Without a list only the generic checks run. Keep site values in `devices.yml` and env; examples use 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, example.com, and neutral names.

## Releases

A push to `main` that passes CI gets a calver tag (`yyyy.mm.dd`), then an image `ghcr.io/baker-scripts/tv-control:<tag>` built with the version, commit SHA, and commit date. The `bump-deploy-repo` job opens a digest-pin PR in a deploy repo; it runs only when the repo variable `DOCKERGIT_BUMP_ENABLED` is `true` and `DOCKERGIT_BUMP_REPO`, `DOCKERGIT_BUMP_BASE`, `DOCKERGIT_BUMP_FILE`, and the secret `DOCKERGIT_BUMP_TOKEN` are set.

## License

GPL-3.0. See [LICENSE](LICENSE).
