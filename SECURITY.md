# Security policy

## Reporting a vulnerability

Report security problems privately through GitHub's
[private vulnerability reporting](https://github.com/alaraun/homeassistant-eufy-home-security/security/advisories/new)
("Report a vulnerability" on the Security tab), not in public issues. Include the
integration and Home Assistant versions and, if relevant, the device model and firmware,
but **never** credentials, tokens, serial numbers or images from your home.

Problems in the protocol library go to
[eufy-home-security](https://github.com/alaraun/python-eufy-home-security/security).

## What the integration handles

- The eufy password, session, device keys and cloud push registration are kept in Home
  Assistant's `.storage`; treat it like a password file. Diagnostics downloads leave them
  out and shorten serial numbers.
- Each camera's raw stream is served only to Home Assistant's own stream components:
  requests from the Home Assistant host itself, with a per-start random key and no proxy
  header. See [Stream access](https://github.com/alaraun/homeassistant-eufy-home-security/blob/main/docs/network.md#stream-access).
- Event history images and videos are visible to everyone who can open Home Assistant's
  media browser.

This project is not affiliated with Anker or eufy.
