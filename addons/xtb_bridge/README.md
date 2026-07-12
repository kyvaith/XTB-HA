# XTB Bridge add-on

This add-on runs the Chromium/Playwright part of the unofficial XTB login flow outside Home Assistant Core.

It exposes a local HTTP API on port `8765`. The Home Assistant integration starts login with the XTB login and password. If XTB requires OTP, the add-on keeps the pending login challenge for a few minutes while Home Assistant asks the user for the current one-time code.

Background polling does not start a fresh password login when the cached XTB TGT expires. New OTP challenges are created only after a manual Home Assistant login or reauthentication confirmation, which avoids overnight OTP SMS loops.

After a successful OTP login, the add-on keeps a persistent Playwright browser profile in `/data/sessions/browser_profiles`. This lets later session refreshes reuse XTB's trusted-browser state instead of treating every refresh as a brand-new device.

The add-on is read-only. It does not expose trading endpoints.
