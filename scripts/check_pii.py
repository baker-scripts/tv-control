"""Fail when a tracked file carries personal or site-specific data. Public repo: nothing personal.

Built-in checks are generic: private, CGNAT, and public IPv4 addresses (documentation and loopback
ranges pass), MAC addresses, tailnet and internal host names, email addresses, phone numbers, street
addresses, and the operator's service domain. Real names, device labels, serials, and hosts are
matched from a list that never lives in this repo: the PII_TERMS env var (one term per line, for a
CI secret) or the file named by PII_TERMS_FILE, default ~/.config/pii-guard/terms.txt. A line
starting with `re:` is a regex; any other line is a case-insensitive whole-token match. Without a
list only the generic checks run, and the output says so.
"""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_NAMES = {"uv.lock", "LICENSE"}

IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
# Tuples, and split words below, so this file does not match its own patterns.
PRIVATE_NETS = [
    ipaddress.ip_network(".".join(map(str, net)) + f"/{bits}")
    for net, bits in (
        ((10, 0, 0, 0), 8),
        ((172, 16, 0, 0), 12),
        ((192, 168, 0, 0), 16),
        ((100, 64, 0, 0), 10),
        ((169, 254, 0, 0), 16),
    )
]
MAC = re.compile(
    r"(?<![0-9A-Fa-f:-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:-])"
)
MAC_OK = re.compile(
    r"^(?:00[:-]00[:-]5e[:-]00[:-]53[:-][0-9a-f]{2}|(?:aa[:-]bb[:-]cc[:-]dd[:-]ee[:-]ff)|(?:00[:-]){5}00|(?:ff[:-]){5}ff)$",
    re.IGNORECASE,
)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
EMAIL_OK = re.compile(
    r"@(?:[a-z0-9-]+\.)*(?:example\.(?:com|org|net|test)|users\.noreply\.github\.com)$|^noreply@",
    re.IGNORECASE,
)
PHONE = re.compile(r"\(\d{3}\)\s?\d{3}[-\s]?\d{4}\b|(?<![\d.])\d{3}-\d{3}-\d{4}\b")
STREET = re.compile(
    r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s+){1,3}(?:Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|Road|Rd|Court|Ct|Terrace|Way)\b"
)
WORDS = [
    ("service domain", re.compile("baker" + "flix", re.IGNORECASE)),
    ("internal path", re.compile(re.escape("/opt/" + "dockergit"))),
    ("internal host", re.compile(r"\b[a-z0-9-]+\.inter" + r"nal\b", re.IGNORECASE)),
    ("tailnet name", re.compile(r"\btail[0-9a-f]{4,}\.ts\.net\b", re.IGNORECASE)),
]


def load_terms() -> tuple[list[tuple[str, re.Pattern[str]]], bool]:
    raw = os.environ.get("PII_TERMS", "")
    path = Path(
        os.environ.get("PII_TERMS_FILE") or Path.home() / ".config/pii-guard/terms.txt"
    )
    if not raw.strip() and path.is_file():
        raw = path.read_text()
    out: list[tuple[str, re.Pattern[str]]] = []
    for line in raw.splitlines():
        term = line.strip()
        if not term or term.startswith("#"):
            continue
        if term.startswith("re:"):
            out.append(("listed term", re.compile(term[3:], re.IGNORECASE)))
        else:
            out.append(
                (
                    "listed term",
                    re.compile(
                        rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])",
                        re.IGNORECASE,
                    ),
                )
            )
    return out, bool(out)


def ip_hits(line: str) -> list[str]:
    hits = []
    for raw in IPV4.findall(line):
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if any(ip in net for net in PRIVATE_NETS) or ip.is_global:
            hits.append("private or public IPv4")
    return hits


def line_hits(line: str, terms: list[tuple[str, re.Pattern[str]]]) -> list[str]:
    hits = ip_hits(line)
    hits += ["MAC address" for m in MAC.finditer(line) if not MAC_OK.match(m.group(0))]
    hits += [
        "email address" for m in EMAIL.finditer(line) if not EMAIL_OK.search(m.group(0))
    ]
    hits += ["phone number" for _ in PHONE.finditer(line)]
    hits += ["street address" for _ in STREET.finditer(line)]
    for label, pat in WORDS + terms:
        hits += [label for _ in pat.finditer(line)]
    return hits


def tracked() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout
    names = [n for n in out.decode().split("\0") if n]
    return [ROOT / n for n in names if Path(n).name not in SKIP_NAMES]


def scan(paths: list[Path], terms: list[tuple[str, re.Pattern[str]]]) -> list[str]:
    findings = []
    for path in paths:
        try:
            text = path.read_text()
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for no, line in enumerate(text.splitlines(), 1):
            for label in sorted(set(line_hits(line, terms))):
                findings.append(f"{path.relative_to(ROOT)}:{no}: {label}")
    return findings


def main(argv: list[str]) -> int:
    terms, have_list = load_terms()
    paths = [Path(a).resolve() for a in argv] if argv else tracked()
    paths = [p for p in paths if p.name not in SKIP_NAMES]
    findings = scan(paths, terms)
    for f in findings:
        print(f, file=sys.stderr)
    if not have_list:
        print(
            "pii-guard: no PII_TERMS list found; generic checks only", file=sys.stderr
        )
    if findings:
        print(
            f"pii-guard: {len(findings)} finding(s). Use documentation values (192.0.2.x, example.com).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
