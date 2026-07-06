# XTB Bridge add-on

This add-on runs the Chromium/Playwright part of the unofficial XTB login flow outside Home Assistant Core.

It exposes a local HTTP API on port `8765`. The Home Assistant integration starts login with the XTB login and password. If XTB requires OTP, the add-on keeps the pending login challenge for a few minutes while Home Assistant asks the user for the current one-time code.

The add-on is read-only. It does not expose trading endpoints.
