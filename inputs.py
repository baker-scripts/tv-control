"""TV inputs: read from the device, named the way people say them, and resolved one way for every verb.

No input list lives in code. Discovery reads:
- LG webOS: ssap tv/getExternalInputList (aiowebostv WebOsClient.get_inputs). Each entry carries
  id, label, port, connected, and a subList with the HDMI-CEC osdName or the set-top brand.
- Android TV and Fire OS TVs: `dumpsys tv_input` (TvInputManagerService.dump). TvInputInfo ids,
  TvInputHardwareInfo hdmi_port (printed only for HDMI hardware), and HDMI-CEC device names.
  A streaming device (Shield, Fire TV Stick) has no hardware inputs, so it lists none.

devices.yml can only add names: inputs (key -> label), input_ids, input_keys, and input_aliases
(spoken name -> input id or another name). A name that discovery does not list is refused.
"""

import re
from typing import Any

from apppolicy import normalize
from inventory import DeviceError

MAX_CANDIDATES = 10

# One grep over the dump: the input map, the hardware list, and the HDMI-CEC device list.
ANDROID_READ = (
    "dumpsys tv_input | grep -E 'info: TvInputInfo|^ *TvInputHardwareInfo |CEC: '"
)
ANDROID_INPUT_RE = re.compile(
    r"TvInputInfo\{id=([^,\s]+), pkg=[^,]*, service=[^}]*\}; state: (\d+)"
)
ANDROID_HW_RE = re.compile(r"^\s*TvInputHardwareInfo \{id=(\d+), type=(\d+)(.*)\}")
HDMI_PORT_RE = re.compile(r"hdmi_port=(\d+)")
CEC_RE = re.compile(
    r"CEC: logical_address: 0x([0-9A-Fa-f]{2}) .*?display_name: (.*?) power_status: "
    r".*?physical_address: 0x([0-9A-Fa-f]{4})"
)
HW_ID_RE = re.compile(r"/HW(\d+)$")
HDMI_ID_RE = re.compile(r"/HDMI([0-9A-Fa-f]{4})([0-9A-Fa-f]{2})$")
# TvInputManager.INPUT_STATE_*.
ANDROID_STATES = {0: "connected", 1: "standby", 2: "disconnected"}


class InputRefusal(DeviceError):
    """The input request was refused; candidates (id and label) help the caller pick again."""

    def __init__(
        self, message: str, candidates: list[dict[str, Any]] | None = None
    ) -> None:
        super().__init__(message)
        self.candidates = candidates or []

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = dict(super().body())
        if self.candidates:
            body["candidates"] = self.candidates[:MAX_CANDIDATES]
        return body


def compact(text: str) -> str:
    """normalize() without spaces, so 'HDMI 2', 'hdmi2', and 'HDMI_2' are one name."""
    return normalize(text).replace(" ", "")


def _entry(
    input_id: str, port: int | None, device: str | None, state: str | None
) -> dict[str, Any]:
    label = f"HDMI {port}" if port else input_id
    return {
        "id": input_id,
        "label": device or label,
        "port": port,
        "device": device,
        "state": state,
        "names": [n for n in (label, device) if n],
    }


def parse_android(dump: str) -> list[dict[str, Any]]:
    """HDMI inputs from the dumpsys tv_input grep, one per port, named by port and CEC device.

    A port input (/HW<n>) takes its port from the hardware entry with id n. A CEC device input
    (/HDMI<address><logical>) folds into the port input when one exists, else stands alone.
    """
    ports: dict[int, int] = {}
    cec: dict[str, str] = {}
    found: list[tuple[str, str]] = []
    for line in dump.splitlines():
        hw = ANDROID_HW_RE.match(line)
        info = ANDROID_INPUT_RE.search(line)
        cec_dev = CEC_RE.search(line)
        if hw:
            hdmi_port = HDMI_PORT_RE.search(hw.group(3))
            if hdmi_port:
                ports[int(hw.group(1))] = int(hdmi_port.group(1))
        elif info:
            found.append((info.group(1), info.group(2)))
        elif cec_dev and cec_dev.group(3) != "0000":
            key = (cec_dev.group(3) + cec_dev.group(1)).upper()
            cec[key] = cec_dev.group(2).strip()
    by_port: dict[int, dict[str, Any]] = {}
    loose: list[dict[str, Any]] = []
    for input_id, raw_state in found:
        state = ANDROID_STATES.get(int(raw_state))
        hw_id = HW_ID_RE.search(input_id)
        hdmi_id = HDMI_ID_RE.search(input_id)
        if hw_id:
            port = ports.get(int(hw_id.group(1)))
            if port:
                by_port[port] = _entry(input_id, port, None, state)
        elif hdmi_id:
            address = hdmi_id.group(1).upper()
            name = cec.get(address + hdmi_id.group(2).upper())
            loose.append(_entry(input_id, int(address[0], 16) or None, name, state))
    for extra in loose:
        port_input = by_port.get(extra["port"] or 0)
        if port_input and extra["device"]:
            port_input["device"] = extra["device"]
            port_input["label"] = extra["device"]
            port_input["names"].append(extra["device"])
            port_input["cec_id"] = extra["id"]
        elif not port_input:
            by_port[extra["port"] or -len(by_port) - 1] = extra
    return [by_port[p] for p in sorted(by_port, key=lambda p: (p < 0, abs(p)))]


def from_webos(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """webOS getExternalInputList devices -> inputs named by label, port, CEC name, and set-top brand."""
    out = []
    for item in raw:
        if not item.get("id"):
            continue
        port = item.get("port") if isinstance(item.get("port"), int) else None
        label = str(item.get("label") or item["id"])
        raw_subs = item.get("subList")
        subs: list[Any] = raw_subs if isinstance(raw_subs, list) else []
        devices = [
            str(s.get(k))
            for s in subs
            if isinstance(s, dict)
            for k in ("osdName", "labelName", "brandName")
            if s.get(k)
        ]
        names = [label, *([f"HDMI {port}"] if port else []), *devices]
        out.append(
            {
                "id": str(item["id"]),
                "label": label,
                "appId": str(item.get("appId", "")),
                "port": port,
                "device": devices[0] if devices else None,
                "connected": item.get("connected"),
                "names": list(dict.fromkeys(names)),
            }
        )
    return out


def configured_keys(dev: dict[str, Any]) -> list[str]:
    """Every input key named in devices.yml (inputs, input_ids, input_keys), in file order."""
    keys: list[str] = []
    for field in ("inputs", "input_ids", "input_keys"):
        block = dev.get(field)
        if isinstance(block, dict):
            keys += [str(k) for k in block if str(k) not in keys]
    return keys


def _match_key(
    dev: dict[str, Any], key: str, found: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The discovered input a configured key means: its input_ids id, its own id, or its port name."""
    ids = dev.get("input_ids")
    pinned = ids.get(key) if isinstance(ids, dict) else None
    for want in (pinned, key):
        if want:
            hit = next((i for i in found if i["id"] == want), None)
            if hit:
                return hit
    return next(
        (i for i in found if any(compact(n) == compact(key) for n in i["names"])),
        None,
    )


def with_config(
    dev: dict[str, Any], found: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Discovered inputs plus the devices.yml names; a configured label replaces the shown label.

    A configured key that matches no discovered input is listed with an error, never switchable.
    """
    out = [{**i, "names": list(i.get("names") or [i["label"]])} for i in found]
    raw = dev.get("inputs")
    labels: dict[str, Any] = raw if isinstance(raw, dict) else {}
    extra = []
    for key in configured_keys(dev):
        hit = _match_key(dev, key, out)
        label = labels.get(key)
        if hit is None:
            extra.append(
                {
                    "id": key,
                    "label": str(label or key),
                    "error": f"Unknown configured input {key}; the device does not list it.",
                }
            )
            continue
        hit["key"] = key
        hit["names"] += [n for n in (key, label) if n and n not in hit["names"]]
        if label:
            hit["label"] = str(label)
    return out + extra


def _candidates(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"id": i["id"], "label": i["label"]} for i in found]


def resolve(
    dev: dict[str, Any], query: str, found: list[dict[str, Any]]
) -> dict[str, Any]:
    """The discovered input a spoken name or id means on dev (found is with_config output).

    Order: exact id or configured key, input_aliases entry (to an id or a name), then any name.
    Unknown, ambiguous, and configured-but-not-discovered names raise InputRefusal.
    """
    raw = query.strip()
    live = [i for i in found if "error" not in i]
    if not raw:
        raise InputRefusal(
            "unknown_input: no input named; pass a name or id", _candidates(live)
        )
    if not live:
        hint = f"; switch {dev['paired_tv']} instead" if dev.get("paired_tv") else ""
        raise InputRefusal(f"no_inputs: {dev['name']} lists no switchable inputs{hint}")
    exact = [
        i
        for i in live
        if raw.casefold() in (i["id"].casefold(), str(i.get("key", "")).casefold())
    ]
    if len(exact) == 1:
        return exact[0]
    want = compact(raw)
    aliases = dev.get("input_aliases")
    if isinstance(aliases, dict):
        target = next(
            (str(v) for k, v in aliases.items() if compact(str(k)) == want), None
        )
        if target is not None:
            hit = next((i for i in live if i["id"] == target), None)
            if hit:
                return hit
            want = compact(target)
    hits = [i for i in live if any(compact(n) == want for n in i["names"])]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise InputRefusal(
            f"ambiguous_input: {raw!r} matches {len(hits)} inputs; pass the input id",
            _candidates(hits),
        )
    stale = any("error" in i and compact(i["id"]) == want for i in found)
    why = " (configured, but the device does not list it)" if stale else ""
    raise InputRefusal(
        f"unknown_input: no input matches {raw!r}{why}", _candidates(live)
    )
