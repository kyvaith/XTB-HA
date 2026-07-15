# XTB Bridge add-on

This add-on runs the Chromium/Playwright part of the unofficial XTB login flow outside Home Assistant Core.

It exposes a local HTTP API on port `8765`. The Home Assistant integration starts login with the XTB login and password. If XTB requires OTP, the add-on keeps the pending login challenge for a few minutes while Home Assistant asks the user for the current one-time code.

Background polling does not start a fresh password login when the cached XTB TGT expires. New OTP challenges are created only after a manual Home Assistant login or reauthentication confirmation, which avoids overnight OTP SMS loops. Shortly before TGT expiry, the bridge reconnects active xStation clients with the still-valid TGT to keep the data WebSocket fresh without triggering OTP. If an active WebSocket dies after the cached TGT has expired, the bridge closes that client and asks Home Assistant for reauthentication instead of letting the underlying xStation library retry CAS login on its own.

After a successful OTP login, the add-on keeps persistent Playwright browser profiles in `/data/sessions/browser_profiles`. Reauthentication for an existing Home Assistant entry includes the selected account number, so accounts can keep separate trusted-browser profiles when needed.

Retirement account data, such as IKZE, uses a separate XTB endpoint. The add-on caches the last successful retirement balance for up to 7 days and uses it as a stale fallback when the trading WebSocket is still usable but the retirement endpoint cannot refresh.

The add-on is read-only. It does not expose trading endpoints.
