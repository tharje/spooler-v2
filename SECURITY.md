# Security

## Reporting a vulnerability

Please report security problems **privately**, not in a public issue.

Use **Security → Report a vulnerability** on the
[repository page](https://github.com/tharje/spooler-v2/security). If that button is not there,
open a public issue that says only that you have a security report and how to reach you, with
no details, and it will be taken from there.

This is a hobby project with one maintainer. Expect a first reply within about a week, and
please give a reasonable time to fix a problem before you publish it.

## Supported versions

Fixes go into the latest release. Pre-releases (`beta`) are tested by volunteers and are
fixed forward, not patched separately.

## What Spooler assumes

Spooler is built for a **trusted home network**. It controls printers and shows their camera,
so it should not be exposed directly to the internet.

- **Reaching it from outside:** use a VPN such as [Tailscale](https://tailscale.com/kb/1312/serve)
  rather than opening a port on your router.
- **Login:** on first start you create a username and password. Failed logins from one address
  are rate limited. You can run without a password on a network you fully trust (see the
  README), and then anyone who can reach the port has full control.
- **HTTPS:** a self-signed certificate is served on port 8443. Behind a reverse proxy or
  Tailscale serve you get a valid one.
- **Printers:** the printers' own protocols (SDCP, MQTT, Moonraker, PrusaLink) are whatever
  the printers offer, and Spooler cannot add security to them. Keep the printers on the same
  trusted network.
- **Stored secrets:** printer access codes, notification tokens and the Spoolman credentials
  are kept as files in the data folder (`DATA_DIR`, the `/data` volume in Docker), and are not
  encrypted at rest. Protect the volume and your backups the same way you would the printers.
  Diagnostics and settings downloads leave secrets out unless you choose to include them.
- **Notifications and Obico:** if you switch on push, ntfy, Telegram, Discord, a webhook or a
  spaghetti detector, Spooler sends data (and, if you choose, camera pictures) to the address
  you gave it.

## Out of scope

Reports that need access to the server's data folder or an already logged-in session, or that
assume the app is exposed to the internet without a VPN or reverse proxy, are welcome as
hardening ideas but are not treated as vulnerabilities.
