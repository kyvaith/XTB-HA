# XTB Bridge add-on

This add-on runs the Chromium/Playwright part of the unofficial XTB login flow outside Home Assistant Core.

It exposes a local HTTP API on port `8765`. The Home Assistant integration posts login, password and OTP secret to this local API and receives a normalized portfolio snapshot.

The add-on is read-only. It does not expose trading endpoints.
