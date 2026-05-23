#!/usr/bin/env python3
"""Dynamic-DNS updater: fetch current public IP and upsert an A/AAAA record."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.request
from pathlib import Path

from netcupctl import Credentials, NetcupClient, record_payload

log = logging.getLogger("update-ip")

IPIFY_V4 = "https://api.ipify.org?format=json"
IPIFY_V6 = "https://api6.ipify.org?format=json"


def cache_path(domain: str, hostname: str, record_type: str) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "netcupctl"
    return base / f"{domain}_{hostname}_{record_type}.ip"


def read_cached_ip(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def write_cached_ip(path: Path, ip: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ip + "\n", encoding="utf-8")


def fetch_ip(record_type: str, timeout: float = 10.0) -> str:
    url = IPIFY_V6 if record_type == "AAAA" else IPIFY_V4
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))["ip"]


def upsert_record(
    client: NetcupClient,
    domain: str,
    hostname: str,
    record_type: str,
    destination: str,
) -> dict:
    existing = [
        r for r in client.info_dns_records(domain)
        if r["hostname"] == hostname and r["type"] == record_type
    ]

    if existing and existing[0]["destination"] == destination:
        log.info("%s.%s %s already %s — no change", hostname, domain, record_type, destination)
        return existing[0]

    if existing:
        rec = existing[0]
        log.info("updating %s.%s %s: %s → %s",
                 hostname, domain, record_type, rec["destination"], destination)
        payload = [record_payload(
            record_id=str(rec["id"]),
            hostname=hostname,
            record_type=record_type,
            destination=destination,
            priority=int(rec.get("priority") or 0),
        )]
    else:
        log.info("creating %s.%s %s → %s", hostname, domain, record_type, destination)
        payload = [record_payload(
            hostname=hostname,
            record_type=record_type,
            destination=destination,
        )]

    updated = client.update_dns_records(domain, payload)
    match = [r for r in updated
             if r["hostname"] == hostname
             and r["type"] == record_type
             and r["destination"] == destination]
    return match[0] if match else updated[-1]


def send_signal_message(
    api_url: str,
    sender: str,
    recipients: list[str],
    message: str,
    timeout: float = 10.0,
) -> None:
    payload = json.dumps(
        {"message": message, "number": sender, "recipients": recipients}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{api_url.rstrip('/')}/v2/send",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        log.debug("signal response: %s", resp.read().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("domain", help="zone, e.g. example.com")
    p.add_argument("hostname", help="record host, e.g. @ or home")
    p.add_argument("--type", choices=["A", "AAAA"], default="A")
    p.add_argument("--ip", help="override detected IP (skips ipify lookup)")
    p.add_argument("--config", help="credentials ini file")
    p.add_argument("--force", action="store_true",
                   help="ignore the local IP cache and always query the API")
    p.add_argument("-v", "--verbose", action="count", default=0)
    p.add_argument("--signal-url", metavar="URL",
                   help="base URL of the signal-cli-rest-api, e.g. http://localhost:8080")
    p.add_argument("--signal-sender", metavar="NUMBER",
                   help="registered Signal number used as sender, e.g. +4912345")
    p.add_argument("--signal-recipient", metavar="RECIPIENT", action="append",
                   dest="signal_recipients", default=[],
                   help="recipient number or group ID (repeatable), e.g. group.GROUPID")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    ip = args.ip or fetch_ip(args.type)
    log.info("public IP: %s", ip)

    cache_file = cache_path(args.domain, args.hostname, args.type)
    cached = read_cached_ip(cache_file)
    if not args.force and cached == ip:
        log.info("cached IP for %s.%s %s matches — skipping API call",
                 args.hostname, args.domain, args.type)
        return 0

    creds = Credentials.load(args.config)
    with NetcupClient(creds) as c:
        result = upsert_record(c, args.domain, args.hostname, args.type, ip)
    write_cached_ip(cache_file, ip)
    print(json.dumps(result, indent=2, sort_keys=True))

    if args.signal_url and args.signal_sender and args.signal_recipients:
        old_ip = cached or "unknown"
        msg = f"IP updated: {args.hostname}.{args.domain} {args.type} {old_ip} → {ip}"
        try:
            send_signal_message(args.signal_url, args.signal_sender, args.signal_recipients, msg)
            log.info("signal notification sent")
        except Exception as exc:
            log.warning("signal notification failed: %s", exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
