# netcupctl

A single-file Python 3 CLI for the [netcup Domain Reselling API](https://ccp.netcup.net/run/webservice/servers/endpoint.php) — DNS records, zones, domains, handles, polling, and pricing.

Inspired by [linxside/ncdapi](https://github.com/linxside/ncdapi) (Bash) and rewritten in Python with a focus on safety, correctness, and full API coverage.

## Features

- **Full API coverage** — DNS, domain, handle, poll, and pricing endpoints.
- **Safe JSON** — all payloads built via `json.dumps`, no string escaping hacks (CAA works out of the box).
- **Single session per invocation** — one `login` up front, automatic `logout` on exit or error.
- **Atomic zone restore** — deletes and adds go in a single `updateDnsRecords` call so the zone is never wiped mid-operation.
- **Retries with backoff** on network errors and 5xx responses.
- **Credentials from env or chmod 600 ini file** — never hard-coded, never on `argv`.
- **Stdlib only** — Python 3.9+, no `pip install` required.

## Requirements

- Python 3.9 or newer
- Network access to `ccp.netcup.net`
- A netcup Domain Reselling account with API credentials (key, password, customer number)

## Installation

```bash
git clone https://github.com/YOUR_USER/netcupctl.git
cd netcupctl
chmod +x netcupctl.py
# optional: put it on your PATH
ln -s "$PWD/netcupctl.py" ~/.local/bin/netcupctl
```

## Configuration

Credentials are read in this order:

1. Environment variables:
   ```bash
   export NETCUP_API_KEY=...
   export NETCUP_API_PASSWORD=...
   export NETCUP_CUSTOMER_NUMBER=...
   ```
2. Ini file at `~/.config/ncdapi/credentials` (must be `chmod 600`):
   ```ini
   [netcup]
   api_key = ...
   api_password = ...
   customer_number = ...
   ```

Override the ini path with `--config /path/to/file`.

## Usage

```
netcupctl [-v] [--config FILE] [--request-id ID] [--json] <command> ...
```

Add `--json` to any command to print the full response as pretty-printed JSON.

### DNS records

```bash
netcupctl records list example.com
netcupctl records add    example.com @    A     127.0.0.1
netcupctl records add    example.com @    MX    mail.example.com --priority 10
netcupctl records add    example.com @    CAA   "0 issue letsencrypt.org"
netcupctl records update example.com 1234567 www A 192.0.2.1
netcupctl records delete example.com 1234567
```

### DNS zone (SOA)

```bash
netcupctl zone info example.com
netcupctl zone set  example.com --ttl 3600 --refresh 28800 --retry 7200 --expire 1209600 --dnssec true
```

### Domains

```bash
netcupctl domains list
netcupctl domains info     example.com
netcupctl domains info     example.com --registry
netcupctl domains authcode example.com
netcupctl domains cancel   example.com
```

### Backup & restore

```bash
netcupctl backup  example.com --out-dir ./backups
netcupctl restore ./backups/backup-example.com-20260418T120000Z.json
netcupctl restore ./backups/backup-example.com-20260418T120000Z.json --keep-existing
```

Backup files are JSON (`{"zone": ..., "records": [...]}`) and created with `umask 077`.

### Handles (contacts)

```bash
netcupctl handles list
netcupctl handles info   12345
netcupctl handles create handle.json
netcupctl handles update 12345 handle.json
netcupctl handles delete 12345
```

Example `handle.json`:

```json
{
  "type": "person",
  "name": "Jane Doe",
  "organisation": "",
  "street": "Example Street 1",
  "postalcode": "12345",
  "city": "Berlin",
  "countrycode": "DE",
  "telephone": "+49.301234567",
  "email": "jane@example.com"
}
```

### Polling & pricing

```bash
netcupctl poll --count 100
netcupctl ack-poll 98765
netcupctl price-tld de
```

## Dynamic DNS (`update_ip.py`)

Companion script that fetches the host's current public IP from
[ipify](https://www.ipify.org/) and upserts an `A` (or `AAAA`) record via the
netcup API. Pure stdlib, no `curl`/`jq` required — reuses `NetcupClient` from
`netcupctl.py`.

```bash
python3 update_ip.py example.com home              # A record for home.example.com
python3 update_ip.py example.com @ --type AAAA     # AAAA record for the apex
python3 update_ip.py example.com home --ip 1.2.3.4 # skip ipify lookup
```

Behaviour:

- If the record already points at the detected IP → no API write.
- If it exists with a different value → updated in place (record id reused).
- If it does not exist → created.

### Cron example

Run every 5 minutes. Credentials must be reachable from cron's environment —
either point `--config` at the ini file or export the `NETCUP_*` env vars in
the crontab.

```cron
*/5 * * * * /usr/bin/python3 /opt/netcupctl/update_ip.py example.com home --config /root/.config/ncdapi/credentials >> /var/log/update-ip.log 2>&1
```

For IPv6 add a second line with `--type AAAA`.

## Exit codes

| Code | Meaning                   |
|------|---------------------------|
| 0    | success                   |
| 1    | API returned an error     |
| 2    | input / config problem    |
| 3    | network error             |

## Notes

- Record-delete looks up the current record and sends the full payload with `deleterecord: true`, as required by the API.
- The restore command accepts backups in the new format (`zone`/`records`) as well as legacy `ncdapi.sh` backups (`soa`/`records`).
- DNSSEC status in `zone set` is passed through as a string (`"true"` / `"false"`).

## License

MIT — see `LICENSE` for details.
