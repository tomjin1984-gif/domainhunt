# Dynadot DomainBot

Production-oriented Dynadot multi-domain monitoring and automatic registration daemon.

This project does not use WHOIS, RDAP, Selenium, Playwright, browser automation, or third-party availability APIs. Real-time availability and registration both go through Dynadot official APIs.

## Dynadot API Notes

Checked against Dynadot official docs on 2026-08-15:

- Legacy commands: [Dynadot API Commands](https://www.dynadot.com/domain/api-commands)
- Current RESTful docs: [Dynadot API Document](https://www.dynadot.com/domain/api-document)
- FAQ / setup: [Dynadot API FAQ](https://www.dynadot.com/domain/api-faq)

This project defaults to Dynadot Legacy `api3.json` because the RESTful v2 docs currently mark the REST API as Beta and warn against critical production reliance.

Confirmed defaults:

- Live base URL: `https://api.dynadot.com/api3.json`
- Sandbox base URL: `https://api-sandbox.dynadot.com/api3.json`
- Auth: `key=<DYNADOT_API_KEY>` request parameter
- Search: `command=search`, `domain0=<domain>`, `show_price=1`
- Register: `command=register`, `domain=<domain>`, `duration=<years>`
- Premium: register with `premium=1` only when accepted by price policy
- Sequential rule: the daemon sends exactly one Dynadot API request at a time
- Default rate limit setting: `DYNADOT_RATE_LIMIT_RPM=60`, so 5-second polling is safe by default
- IP whitelist: configure the VPS fixed public IP in Dynadot API settings and allow propagation time before testing

If Dynadot changes an endpoint, parameter, or account-specific rate limit, update `.env` and verify with `domainbot test-api` before live mode.

## Quick Deploy On Ubuntu VPS

```bash
git clone <your-repo-url> domainbot
cd domainbot
cp .env.example .env
nano .env
sudo ./scripts/install.sh
```

Edit `/opt/domainbot/.env`:

```bash
DYNADOT_API_KEY=your_key_here
DYNADOT_TEST_MODE=true
```

Then test:

```bash
/opt/domainbot/.venv/bin/domainbot test-api
sudo systemctl status domainbot --no-pager
```

Switch to live only after sandbox/API checks pass:

```bash
sudo nano /opt/domainbot/.env
# set DYNADOT_TEST_MODE=false
sudo systemctl restart domainbot
```

## Add Domains

```bash
domainbot add localfab.ai \
  --start "2026-08-20 14:00:00" \
  --duration 20m \
  --interval 5 \
  --timezone Asia/Singapore \
  --max-price 200
```

Daily:

```bash
domainbot add abc.com \
  --start "14:00:00" \
  --duration 20m \
  --interval 5 \
  --timezone Asia/Singapore \
  --schedule-type daily \
  --max-price 50
```

CSV import:

```csv
domain,start_at,duration,interval,timezone,max_price,registration_years,schedule_type
localfab.ai,2026-08-20 14:00:00,20m,5,Asia/Singapore,200,1,once
example.ai,2026-08-20 14:05:00,20m,5,Asia/Singapore,200,1,once
abc.com,14:00:00,20m,5,Asia/Singapore,50,1,daily
```

```bash
domainbot import domains.csv
```

## CLI

```bash
domainbot init
domainbot add DOMAIN
domainbot import FILE
domainbot remove DOMAIN [DOMAIN...]
domainbot remove --file remove.txt
domainbot pause DOMAIN
domainbot resume DOMAIN
domainbot list
domainbot status DOMAIN
domainbot run DOMAIN
domainbot stop DOMAIN
domainbot daemon
domainbot daemon --dry-run
domainbot test-api
domainbot logs
```

`--dry-run` allows Search. If a domain is available, it records `SIMULATED_REGISTER` and never sends Register.

## Runtime Behavior

The scheduler always chooses the enabled active domain with the earliest `next_check_at`.

Example:

```text
14:00:00 SEARCH localfab.ai
14:00:01 SEARCH another.ai
14:00:05 SEARCH localfab.ai
14:00:06 SEARCH another.ai -> AVAILABLE
14:00:06 REGISTER another.ai
```

After successful registration:

- `status = registered`
- `registered_at` is saved
- `next_check_at = null`
- future searches skip the domain forever

Register timeout/network uncertainty:

- no immediate duplicate Register
- status becomes `registration_pending_confirmation`
- the daemon queries registration/order/domain status first
- if still unknown, it stays pending confirmation

## Dynadot Setup Checklist

1. Use a VPS with a fixed public IPv4/IPv6 address.
2. Enable Dynadot API access in your account.
3. Add the VPS public IP to Dynadot API whitelist.
4. Wait for whitelist propagation.
5. Put the API key in `.env`.
6. Keep `DYNADOT_TEST_MODE=true` for initial validation.
7. Run `domainbot test-api`.
8. Start with `domainbot daemon --dry-run`.
9. Switch `DYNADOT_TEST_MODE=false` only when ready.

Do not put real API keys in source control.

## Logs

Default log file:

```text
logs/domainbot.log
```

Rotation:

- 10 MB per file
- 10 backups

Example events:

```text
DOMAIN=localfab.ai ACTION=SEARCH STATUS=UNAVAILABLE HTTP_STATUS=200 API_CODE=0 DURATION=213ms
DOMAIN=localfab.ai ACTION=SEARCH STATUS=AVAILABLE HTTP_STATUS=200 API_CODE=0 DURATION=188ms
DOMAIN=localfab.ai ACTION=REGISTER_SUCCESS STATUS=REGISTERED ORDER_ID=xxxxx
```

API keys are not logged.

## Telegram

Set:

```bash
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Notifications include watching start, available, registering, success, failure, timeout, and price exceeded.

## TLD And Premium Notes

The implementation sends generic Dynadot Register parameters: `domain`, `duration`, `currency`, and optional `premium=1`.

Some TLDs may have registry-specific rules or optional parameters in Dynadot's current docs. Validate `.ai`, `.com`, and any other target TLD in sandbox/test mode before live operation. If Search cannot return a reliable premium price, `max_price` cannot fully protect against unknown registry-side pricing; start with dry-run and low-risk domains.

## Development

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest
```

Current test suite covers scheduling fairness, single Dynadot concurrency, register priority, intervals/windows, daily/once behavior, pause/registered skips, registration success, timeout confirmation, duplicate prevention, restart recovery, max price, dry-run, network failures, and state transitions.

