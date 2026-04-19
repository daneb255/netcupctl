#!/usr/bin/env python3
"""
Netcup domain-reselling API client (DNS, domains, handles, polling, pricing).

Endpoint: https://ccp.netcup.net/run/webservice/servers/endpoint.php

Credentials (first hit wins):
  1. env vars NETCUP_API_KEY, NETCUP_API_PASSWORD, NETCUP_CUSTOMER_NUMBER
  2. ini file ~/.config/ncdapi/credentials (chmod 600), section [netcup]:
         api_key = ...
         api_password = ...
         customer_number = ...

Stdlib only. Requires Python 3.9+.
"""

from __future__ import annotations

import argparse
import configparser
import dataclasses
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ENDPOINT = "https://ccp.netcup.net/run/webservice/servers/endpoint.php?JSON"
DEFAULT_CONFIG = Path.home() / ".config" / "ncdapi" / "credentials"
USER_AGENT = "ncdapi-py/1.0"

VALID_RECORD_TYPES = frozenset({
    "A", "AAAA", "CAA", "CNAME", "MX", "NS", "PTR", "SRV", "TLSA", "TXT",
})

log = logging.getLogger("ncdapi")


class NetcupError(RuntimeError):
    """API returned status != success."""

    def __init__(self, payload: dict[str, Any]):
        self.status = payload.get("status", "unknown")
        self.shortmessage = payload.get("shortmessage", "")
        self.longmessage = payload.get("longmessage", "")
        self.statuscode = payload.get("statuscode")
        self.raw = payload
        super().__init__(
            f"{self.statuscode} {self.status}: {self.shortmessage} — {self.longmessage}"
        )


@dataclasses.dataclass(frozen=True)
class Credentials:
    api_key: str
    api_password: str
    customer_number: str

    @classmethod
    def load(cls, config_path: Path | None = None) -> "Credentials":
        key = os.environ.get("NETCUP_API_KEY")
        pw = os.environ.get("NETCUP_API_PASSWORD")
        cid = os.environ.get("NETCUP_CUSTOMER_NUMBER")

        path = config_path or DEFAULT_CONFIG
        if not (key and pw and cid) and path.exists():
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                raise RuntimeError(
                    f"{path} has permissions {oct(mode)}; chmod 600 first"
                )
            cfg = configparser.ConfigParser()
            cfg.read(path)
            section = cfg["netcup"] if cfg.has_section("netcup") else cfg["DEFAULT"]
            key = key or section.get("api_key")
            pw = pw or section.get("api_password")
            cid = cid or section.get("customer_number")

        missing = [n for n, v in (
            ("NETCUP_API_KEY", key),
            ("NETCUP_API_PASSWORD", pw),
            ("NETCUP_CUSTOMER_NUMBER", cid),
        ) if not v]
        if missing:
            raise RuntimeError(
                f"Missing credentials: {', '.join(missing)}. "
                f"Set env vars or populate {path}."
            )
        return cls(api_key=key, api_password=pw, customer_number=str(cid))


class NetcupClient:
    """Session-aware client. Use as `with NetcupClient(creds) as c: ...`."""

    def __init__(
        self,
        creds: Credentials,
        endpoint: str = ENDPOINT,
        timeout: float = 30.0,
        retries: int = 3,
        backoff: float = 1.5,
        client_request_id: str | None = None,
    ):
        self.creds = creds
        self.endpoint = endpoint
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.client_request_id = client_request_id
        self._session_id: str | None = None

    def __enter__(self) -> "NetcupClient":
        self.login()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.logout()
        except Exception as e:
            log.warning("logout failed: %s", e)

    def login(self) -> None:
        data = self._raw_call("login", {
            "apikey": self.creds.api_key,
            "apipassword": self.creds.api_password,
            "customernumber": self.creds.customer_number,
        })
        self._session_id = data["responsedata"]["apisessionid"]
        log.debug("login ok")

    def logout(self) -> None:
        if not self._session_id:
            return
        try:
            self._raw_call("logout", self._auth_params())
        finally:
            self._session_id = None

    def call(self, action: str, **params: Any) -> dict[str, Any]:
        if not self._session_id:
            raise RuntimeError("not logged in")
        merged = {**params, **self._auth_params()}  # auth params always win
        if self.client_request_id is not None:
            merged.setdefault("clientrequestid", self.client_request_id)
        return self._raw_call(action, merged)

    def _auth_params(self) -> dict[str, Any]:
        return {
            "apikey": self.creds.api_key,
            "apisessionid": self._session_id,
            "customernumber": self.creds.customer_number,
        }

    def _raw_call(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps({"action": action, "param": params}).encode("utf-8")
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                req = urllib.request.Request(
                    self.endpoint,
                    data=body,
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "User-Agent": USER_AGENT,
                    },
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                log.debug("← %s: %s", action, payload.get("shortmessage"))
                if payload.get("status") != "success":
                    raise NetcupError(payload)
                return payload
            except urllib.error.HTTPError as e:
                last_exc = e
                if 500 <= e.code < 600 and attempt < self.retries:
                    time.sleep(self.backoff ** attempt)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError) as e:
                last_exc = e
                if attempt < self.retries:
                    log.warning("network error on %s (%s); retrying", action, e)
                    time.sleep(self.backoff ** attempt)
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    # -- domains ---------------------------------------------------------

    def list_all_domains(self) -> list[dict[str, Any]]:
        return self.call("listallDomains")["responsedata"]

    def info_domain(self, domainname: str, registry_info: bool = False) -> dict[str, Any]:
        return self.call(
            "infoDomain",
            domainname=domainname,
            registryinformationflag=int(registry_info),
        )["responsedata"]

    def get_authcode(self, domainname: str) -> dict[str, Any]:
        return self.call("getAuthcodeDomain", domainname=domainname)["responsedata"]

    def cancel_domain(self, domainname: str) -> dict[str, Any]:
        return self.call("cancelDomain", domainname=domainname)["responsedata"]

    # -- DNS -------------------------------------------------------------

    def info_dns_records(self, domainname: str) -> list[dict[str, Any]]:
        return self.call("infoDnsRecords", domainname=domainname)["responsedata"]["dnsrecords"]

    def info_dns_zone(self, domainname: str) -> dict[str, Any]:
        return self.call("infoDnsZone", domainname=domainname)["responsedata"]

    def update_dns_zone(self, domainname: str, zone: dict[str, Any]) -> dict[str, Any]:
        return self.call("updateDnsZone", domainname=domainname, dnszone=zone)["responsedata"]

    def update_dns_records(
        self, domainname: str, records: Iterable[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        payload = self.call(
            "updateDnsRecords",
            domainname=domainname,
            dnsrecordset={"dnsrecords": list(records)},
        )
        return payload["responsedata"]["dnsrecords"]

    # -- handles ---------------------------------------------------------

    def list_all_handles(self) -> list[dict[str, Any]]:
        return self.call("listallHandle")["responsedata"]

    def info_handle(self, handle_id: int) -> dict[str, Any]:
        return self.call("infoHandle", handle_id=handle_id)["responsedata"]

    def create_handle(self, handle: dict[str, Any]) -> dict[str, Any]:
        return self.call("createHandle", **handle)["responsedata"]

    def update_handle(self, handle_id: int, handle: dict[str, Any]) -> dict[str, Any]:
        handle = {k: v for k, v in handle.items() if k != "handle_id"}
        return self.call("updateHandle", handle_id=handle_id, **handle)["responsedata"]

    def delete_handle(self, handle_id: int) -> dict[str, Any]:
        return self.call("deleteHandle", handle_id=handle_id)["responsedata"]

    # -- poll / price ----------------------------------------------------

    def poll(self, messagecount: int) -> dict[str, Any]:
        return self.call("poll", messagecount=messagecount)["responsedata"]

    def ack_poll(self, apilogid: int) -> dict[str, Any]:
        return self.call("ackpoll", apilogid=apilogid)["responsedata"]

    def price_tld(self, topleveldomain: str) -> dict[str, Any]:
        return self.call("priceTopleveldomain", topleveldomain=topleveldomain)["responsedata"]


# --------------------------------------------------------------------- helpers


def record_payload(
    *,
    hostname: str,
    record_type: str,
    destination: str,
    priority: int = 0,
    record_id: str = "",
    delete: bool = False,
    state: str = "yes",
) -> dict[str, Any]:
    rt = record_type.upper()
    if rt not in VALID_RECORD_TYPES:
        raise ValueError(f"unsupported record type {record_type!r}")
    return {
        "id": record_id,
        "hostname": hostname,
        "type": rt,
        "priority": str(priority),
        "destination": destination,
        "deleterecord": "true" if delete else "false",
        "state": state,
    }


def backup_zone(client: NetcupClient, domain: str, out_dir: Path) -> Path:
    zone = client.info_dns_zone(domain)
    records = client.info_dns_records(domain)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"backup-{domain}-{ts}.json"
    prev_umask = os.umask(0o077)
    try:
        path.write_text(json.dumps({"zone": zone, "records": records}, indent=2))
    finally:
        os.umask(prev_umask)
    return path


def restore_zone(client: NetcupClient, backup_file: Path, *, keep_existing: bool = False) -> None:
    """Restore a zone atomically — deletes and adds go in a single updateDnsRecords call."""
    data = json.loads(backup_file.read_text())
    zone = data.get("zone") or data.get("soa")  # accept legacy bash-tool backups
    if not zone:
        raise ValueError(f"{backup_file}: missing 'zone' or 'soa' section")
    backup_records = data["records"]
    domain = zone["name"]

    client.update_dns_zone(domain, {
        "name": domain,
        "ttl": zone["ttl"],
        "serial": "",
        "refresh": zone["refresh"],
        "retry": zone["retry"],
        "expire": zone["expire"],
        "dnssecstatus": zone["dnssecstatus"],
    })

    payload: list[dict[str, Any]] = []
    if not keep_existing:
        for rec in client.info_dns_records(domain):
            payload.append(record_payload(
                hostname=rec["hostname"],
                record_type=rec["type"],
                destination=rec["destination"],
                priority=int(rec.get("priority") or 0),
                record_id=str(rec["id"]),
                delete=True,
            ))
    for rec in backup_records:
        payload.append(record_payload(
            hostname=rec["hostname"],
            record_type=rec["type"],
            destination=rec["destination"],
            priority=int(rec.get("priority") or 0),
        ))
    if payload:
        client.update_dns_records(domain, payload)


# ------------------------------------------------------------------------- CLI


def _make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ncdapi", description=__doc__.split("\n\n", 1)[0])
    p.add_argument("-v", "--verbose", action="count", default=0)
    p.add_argument("--config", type=Path, help="credentials ini file")
    p.add_argument("--request-id", help="clientrequestid sent with every call")
    p.add_argument("--json", action="store_true", help="emit JSON to stdout")
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("records").add_subparsers(dest="rcmd", required=True)
    r.add_parser("list").add_argument("domain")
    a = r.add_parser("add")
    a.add_argument("domain"); a.add_argument("hostname"); a.add_argument("type")
    a.add_argument("destination"); a.add_argument("--priority", type=int, default=0)
    m = r.add_parser("update")
    m.add_argument("domain"); m.add_argument("id"); m.add_argument("hostname")
    m.add_argument("type"); m.add_argument("destination")
    m.add_argument("--priority", type=int, default=0)
    d = r.add_parser("delete"); d.add_argument("domain"); d.add_argument("id")

    z = sub.add_parser("zone").add_subparsers(dest="zcmd", required=True)
    z.add_parser("info").add_argument("domain")
    zs = z.add_parser("set")
    zs.add_argument("domain")
    zs.add_argument("--ttl", type=int, required=True)
    zs.add_argument("--refresh", type=int, required=True)
    zs.add_argument("--retry", type=int, required=True)
    zs.add_argument("--expire", type=int, required=True)
    zs.add_argument("--dnssec", choices=["true", "false"], default="false")

    dom = sub.add_parser("domains").add_subparsers(dest="dcmd", required=True)
    dom.add_parser("list")
    di = dom.add_parser("info"); di.add_argument("domain")
    di.add_argument("--registry", action="store_true")
    dom.add_parser("authcode").add_argument("domain")
    dom.add_parser("cancel").add_argument("domain")

    b = sub.add_parser("backup"); b.add_argument("domain")
    b.add_argument("--out-dir", type=Path, default=Path("."))
    rs = sub.add_parser("restore"); rs.add_argument("file", type=Path)
    rs.add_argument("--keep-existing", action="store_true")

    h = sub.add_parser("handles").add_subparsers(dest="hcmd", required=True)
    h.add_parser("list")
    h.add_parser("info").add_argument("id", type=int)
    h.add_parser("delete").add_argument("id", type=int)
    h.add_parser("create").add_argument("json_file", type=Path)
    hu = h.add_parser("update"); hu.add_argument("id", type=int)
    hu.add_argument("json_file", type=Path)

    sub.add_parser("poll").add_argument("--count", type=int, default=100)
    sub.add_parser("ack-poll").add_argument("apilogid", type=int)
    sub.add_parser("price-tld").add_argument("tld")

    return p


def _emit(data: Any, as_json: bool) -> None:
    if data is None:
        return
    if as_json or not isinstance(data, (str, int, float)):
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(data)


def _dispatch(c: NetcupClient, args: argparse.Namespace) -> Any:
    cmd = args.cmd
    if cmd == "records":
        if args.rcmd == "list":
            return c.info_dns_records(args.domain)
        if args.rcmd == "add":
            created = c.update_dns_records(args.domain, [record_payload(
                hostname=args.hostname, record_type=args.type,
                destination=args.destination, priority=args.priority,
            )])
            target_type = args.type.upper()
            match = [r for r in created
                     if r["hostname"] == args.hostname
                     and r["type"] == target_type
                     and r["destination"] == args.destination]
            return match or created
        if args.rcmd == "update":
            return c.update_dns_records(args.domain, [record_payload(
                record_id=args.id, hostname=args.hostname,
                record_type=args.type, destination=args.destination,
                priority=args.priority,
            )])
        if args.rcmd == "delete":
            current = next(
                (r for r in c.info_dns_records(args.domain) if str(r["id"]) == args.id),
                None,
            )
            if current is None:
                raise ValueError(f"record id {args.id} not found in {args.domain}")
            return c.update_dns_records(args.domain, [record_payload(
                record_id=args.id,
                hostname=current["hostname"],
                record_type=current["type"],
                destination=current["destination"],
                priority=int(current.get("priority") or 0),
                delete=True,
            )])
    if cmd == "zone":
        if args.zcmd == "info":
            return c.info_dns_zone(args.domain)
        if args.zcmd == "set":
            return c.update_dns_zone(args.domain, {
                "name": args.domain,
                "ttl": str(args.ttl),
                "serial": "",
                "refresh": str(args.refresh),
                "retry": str(args.retry),
                "expire": str(args.expire),
                "dnssecstatus": args.dnssec,
            })
    if cmd == "domains":
        if args.dcmd == "list":
            return c.list_all_domains()
        if args.dcmd == "info":
            return c.info_domain(args.domain, registry_info=args.registry)
        if args.dcmd == "authcode":
            return c.get_authcode(args.domain)
        if args.dcmd == "cancel":
            return c.cancel_domain(args.domain)
    if cmd == "backup":
        path = backup_zone(c, args.domain, args.out_dir)
        log.info("wrote %s", path)
        return str(path)
    if cmd == "restore":
        restore_zone(c, args.file, keep_existing=args.keep_existing)
        log.info("restored from %s", args.file)
        return None
    if cmd == "handles":
        if args.hcmd == "list":
            return c.list_all_handles()
        if args.hcmd == "info":
            return c.info_handle(args.id)
        if args.hcmd == "delete":
            return c.delete_handle(args.id)
        if args.hcmd == "create":
            return c.create_handle(json.loads(args.json_file.read_text()))
        if args.hcmd == "update":
            return c.update_handle(args.id, json.loads(args.json_file.read_text()))
    if cmd == "poll":
        return c.poll(args.count)
    if cmd == "ack-poll":
        return c.ack_poll(args.apilogid)
    if cmd == "price-tld":
        return c.price_tld(args.tld)
    raise SystemExit(f"unhandled command: {cmd}")


def main(argv: list[str] | None = None) -> int:
    print("argv:", sys.argv)
    parser = _make_parser()
    args = parser.parse_args(argv)
    print("parsed:", vars(args))
    if not args.cmd:
        parser.print_help()
        return 0
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    try:
        creds = Credentials.load(args.config)
    except Exception as e:
        log.error("%s", e)
        return 2

    try:
        with NetcupClient(creds, client_request_id=args.request_id) as c:
            result = _dispatch(c, args)
    except NetcupError as e:
        log.error("API error: %s", e)
        if args.verbose:
            log.debug("response: %s", json.dumps(e.raw, indent=2))
        return 1
    except (ValueError, FileNotFoundError) as e:
        log.error("%s", e)
        return 2
    except urllib.error.URLError as e:
        log.error("network error: %s", e)
        return 3

    _emit(result, args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
