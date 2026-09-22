# alexa-wipe

One-shot Python script that bulk-deletes everything in your Amazon Alexa smart
home setup — devices, groups, scenes — so you can rebuild from scratch.

Built in April 2026 after Amazon retired the Alexa web UI, which means bulk
deletion through the app or website is no longer possible at all (see
[The Ambient](https://www.the-ambient.com/how-to/delete-smart-home-devices-alexa-2174/),
[Hubitat thread](https://community.hubitat.com/t/amazon-really-broke-alexa-no-web-portal/129056)).

## What it does

Deletes, in order (to minimise reference errors):

1. **Routines** (`/api/behaviors/v2/automations/{id}`)
2. **Scenes** (`/api/phoenix/appliance/{id}`)
3. **Groups / rooms** (`/api/phoenix/group/{id}`)
4. **Smart home devices / appliances** (`/api/phoenix/appliance/{id}`)

It leaves untouched:
- Echo speakers / registration
- Enabled skills (the Hue / Nanoleaf / Bosch skill stays on — only the
  imported entities are purged)
- Amazon account settings / household members

## The 2026 Alexa API reality

A few things worth knowing if you want to adapt this to another region or
extend it:

- **`alexa.amazon.<tld>` returns HTTP 401 to any browser request** — the web UI
  is fully dead. You can only hit the backend API, and only with the cookies an
  authenticated mobile app would carry.
- **Scripted email+password login doesn't work** if your Amazon account has a
  passkey. Amazon's OAuth page only offers WebAuthn, and HTML form parsers
  can't navigate it.
- **The working login method is a local HTTP proxy**
  ([alexapy.AlexaProxy](https://alexapy.readthedocs.io/en/stable/alexapy/alexaproxy.html),
  same approach [Home Assistant's alexa_media_player](https://github.com/alandtse/alexa_media_player)
  uses). The script starts a proxy on `127.0.0.1:<random-port>`, opens your
  browser to it, transparently forwards to Amazon's real signin, and captures
  the OAuth authorization code from the redirect. Your real browser does the
  login (passkey → fails on origin mismatch → falls back to password form
  which the proxy auto-fills). The proxy then exchanges the code for Alexa API
  cookies valid for ~14 days.
- **`/api/phoenix` is dead** on at least some accounts (returns HTTP 299 with
  an empty body). The current entity source is
  `/api/behaviors/entities?skillId=amzn1.ask.1p.smarthome`, which returns a
  flat JSON array classified by `providerData.categoryType` (`APPLIANCE`,
  `SCENE`, `GROUP`).
- **`/api/phoenix/group` still works** for listing groups with their legacy
  `amzn1.HomeAutomation.ApplianceGroup.<acct>.<uuid>` identifiers — still the
  right shape for `DELETE`.
- **`DELETE /api/phoenix/appliance/{uuid}` also still works** for both
  appliances and scenes using the new UUIDs. Surprising but confirmed.
- **SSL cert bundling**: Python.org Python on macOS ships without populated CA
  trust, and `authcaptureproxy` builds its `ssl.create_default_context()` at
  import time, so the script monkey-patches `create_default_context` to load
  `certifi`'s bundle before importing `alexapy`. Without this every proxy call
  to `amazon.com` fails with `CERTIFICATE_VERIFY_FAILED`.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp setup.example.yaml setup.yaml
# edit setup.yaml with your email / password / TOTP secret / region
```

`setup.yaml` is gitignored. Password and TOTP are used only to auto-fill the
login form inside the proxy — you still complete the sign-in yourself.

`email`, `password` and `region` are **required**: `load_config` exits if any of
them is missing or empty. Only `totp_secret` may be omitted — leave it as
unquoted `null` (or blank, or delete the line) when 2FA is off. Quoting it as
`"null"` makes it a literal four-character secret.

### setup.yaml

```yaml
email: your.amazon.email@example.com
password: "your-amazon-password"
totp_secret: "ABCDEFGHIJKLMNOP"   # base32 from the 2FA QR; null if no 2FA
region: de                          # de | com | co.uk
```

## Usage

### Recommended: disable skills first

Skills like Philips Hue re-import their devices to Alexa within seconds of
deletion if the skill is still enabled and the bridge is reachable. For a true
clean slate, open the Alexa app → Mehr → Skills → deactivate Hue / Nanoleaf /
Bosch / etc. **before** running the script.

### Dry run (default — nothing is deleted)

```bash
python wipe_alexa.py
```

Starts the proxy, opens your browser, lists every entity it would delete.
Ends with `DRY RUN — re-run with --yes to delete.`

### Actually delete

```bash
python wipe_alexa.py --yes
```

Same login flow, then 5-second countdown, then deletions in the order above.
Each item prints `✓ deleted <name>` or `✗ failed <name>: <status>`.

### Subset

```bash
python wipe_alexa.py --only scenes,devices --yes
```

Valid kinds: `devices`, `scenes`, `groups`, `routines`.

## Known limitations

- **Routines delete doesn't work.** `DELETE /api/behaviors/v2/automations/{id}`
  returns non-2xx — Amazon appears to have moved routine management to a
  different endpoint that we haven't mapped yet. The script reports failures
  and moves on. Delete routines manually in the Alexa app, or patch
  `endpoints.py::delete_routine` if you find the right URL.
- **Passkey-only accounts need a password fallback.** The proxy sees the
  browser origin as `127.0.0.1`, which WebAuthn refuses. The script relies on
  Amazon's "Use password instead" link on the signin page. If your account
  enforces passkey with no fallback, this won't work without disabling the
  passkey requirement in Amazon security settings first.
- **Skill-owned entities respawn** if the upstream skill is enabled. See
  "disable skills first" above.

## Files

| File | Purpose |
|---|---|
| `wipe_alexa.py` | CLI entry point — arg parsing, dry-run output, deletion loop |
| `alexa_client.py` | Proxy-based login, SSL cert patch, authenticated `requests.Session` |
| `endpoints.py` | `list_*` / `delete_*` for the four entity kinds |
| `diagnose.py` | Probe tool: `python diagnose.py <entity_uuid>` tries 6 DELETE endpoint shapes. Used to find the working delete URL. |
| `setup.example.yaml` | Template for `setup.yaml` |
| `requirements.txt` | `alexapy`, `requests`, `pyyaml`, `certifi` |
| `.gitignore` | Keeps `setup.yaml`, `.venv`, `__pycache__` out of git |

## Credits

- [alexapy](https://github.com/alandtse/alexapy) by Alan D. Tse — the proxy
  login flow comes from here.
- [alexa_media_player](https://github.com/alandtse/alexa_media_player) —
  Home Assistant integration whose login approach this script mirrors.
- [alexa-cookie](https://github.com/Apollon77/alexa-cookie) by Apollon77 —
  original Node.js implementation of the proxy+OAuth idea.
- [Python-Delete-Alexa-Devices](https://github.com/Pezmc/Python-Delete-Alexa-Devices) —
  pointed at the (now-dead) `/api/phoenix/appliance/{id}` DELETE shape which
  happily still works for the new UUIDs.

## Disclaimer

Not affiliated with Amazon. Uses undocumented endpoints that can change any
time. Deletions are irreversible. Use at your own risk.
