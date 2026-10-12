# Contributing to Spooler

Thank you for helping. Bug reports, test results from your own printer, ideas and pull requests are all welcome, and the smallest ones (a typo, a firmware version that works for you) are as useful as the big ones.

## Reporting a problem or sharing a test result

- In Spooler, use **Report bug** in the footer. It prepares an issue with your Spooler version, printer types and firmware, and recent log lines, with IP addresses, serial numbers and access codes removed.
- Or open an issue and pick a template: bug report, feature request, or *Works (or not) on my printer*.
- Please never post IP addresses, serial numbers, access codes, passwords or API keys.

## Running Spooler for development

```bash
git clone https://github.com/tharje/spooler-v2.git
cd spooler-v2
git checkout dev
python3 -m venv venv
venv/bin/pip install -r requirements-dev.txt
venv/bin/pytest
```

To run Spooler itself without Docker, use `./setup.sh` (see the README). The tests run offline against fixtures: you do not need a printer or a Spoolman instance. The same suite runs on every pull request, and the Docker image is only built once the tests pass. The frontend is plain HTML, CSS and JavaScript with no build step.

## Pull requests

1. Branch from **`dev`** and open the pull request against `dev`. `main` only holds released versions.
2. Keep a pull request to one change. A small, focused change is reviewed and merged faster.
3. Add or update tests for what you change (`tests/`), and make sure `pytest` passes.
4. Write the commit message as a short sentence about what changes for the user, for example *Layer notification: once per print*.
5. Say in the pull request how you tested it, and on which printer and firmware if it touches the printer code.
6. Do not put personal data in the repository: use `example.com` and `192.0.2.x` as example addresses, never real IP addresses, serial numbers, e-mail addresses or tokens.
7. By contributing you agree that your work is released under the project's license, [AGPL-3.0](LICENSE).

If you are unsure whether an idea fits, open an issue first and ask.

## Printers and firmware

Spooler talks to printers with protocols that are not officially documented, so changes can break when a manufacturer updates its firmware.

- A firmware version that works for you can be added to [`printers/tested_firmware.json`](printers/tested_firmware.json) in a pull request. Say how you tested it.
- Please do not add support for a protocol command to the printer code unless it has been tried on a real printer. Mark anything you could not verify as unverified.
- Notes on the protocols are in `CC2_INTEGRATION.md` and in the README.

## Good first contributions

- Test Spooler on a printer or firmware that is not in the tested list and report the result.
- Documentation: unclear steps, missing screenshots, typos.
- Printer error messages that are missing or worded unclearly (`printers/error_codes.py`).
- Issues labelled `good first issue` or `help wanted`.
