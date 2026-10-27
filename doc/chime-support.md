# Chime support

The Devices page shows Chimes separately from cameras. Opening the page reads
the Chime inventory; opening a device also reads its available Wi-Fi health.
Refreshing a hidden page only reads cached state. There is no background Chime
polling, and these reads do not start camera streams.

## Controls

- **Volume:** recognized legacy Chime and Chime Pro models expose Ring's 0–11
  volume range when the account reports ownership, an explicit volume, and an
  online device. Moving the control stays local until Apply. Halo performs one
  write, then reads the reported volume before calling it confirmed.
- **Sound tests:** Ring and Motion are separate explicit requests. Acceptance
  means Ring accepted the request; it does not prove playback or completion.
  After an admitted request, volume and further tests stay locked until the user
  confirms **The sound has stopped**. The lock survives closing and reopening a
  device dialog within the same running account session.
- **Read-only details:** account-reported model, connection, volume and available
  firmware/health values remain distinct from missing or stale data. Missing
  volume does not mean muted. Unknown models and ownership remain read-only.

Controls are experimental and have been tested with synthetic HTTP responses,
not a physical Chime. The recognized API kinds are `chime`, `chime_v2`,
`chime_pro`, and `chime_pro_v2`. This does not establish support for third
generation Chimes, Chime Plus, separate Ring/Motion volume controls, or future
hardware that reuses an older kind.

## Failures and session changes

Writes are never automatically replayed after admission, including on token
errors, timeout, cancellation or server failure. An uncertain result may still
have applied remotely. Refresh to inspect volume; listen before acknowledging a
sound test. Closing a view cancels its pending intent before admission, while
already admitted operations remain owned by the account service.

Logout prevents new admissions and retires private cached data. A request
already handed to the HTTP client may still reach Ring. The local sound lock
cannot detect tests started elsewhere and does not survive an application crash
or a new account session. No command journal or automatic recovery replay is
persisted.

Inventory has a 128-device limit. Invalid, duplicate or incomplete responses do
not authoritatively remove cached devices. Manual Reload bypasses the normal
five-second inventory and 60-second health caches. Command preflight uses fresh
inventory with a 30-second maximum age, measured across suspend.

The client-owned JSON transport shares two active slots between Alarm and
Chime, reserving recovery priority and allowing one non-recovery request at a
time. Camera SDK calls, media and OAuth token requests have separate budgets.
Reads are bounded to 1 MiB, JSON depth 32 and 50,000 nodes; write replies are
bounded to 64 KiB. Chime HTTP requests have a ten-second total budget, with
30 seconds for a complete volume operation and 15 seconds for a sound request.
Alarm history retains its existing 30-second page budget.

## Not implemented

Linked-camera editing, snooze changes, tone catalog selection, nightlight,
network setup, provisioning, rename and newer hardware-specific controls are
outside this feature. Linked-camera data has no verified response schema, so
Halo does not infer that an empty or unparsed response means no linked cameras.
Doorbell mechanical/digital chime settings are a separate existing camera
feature.

The adapter uses the existing authenticated Ring session and fixed Chime
routes. It does not replace the backend, introduce another login flow or add
Node/MQTT dependencies. Real account reads, writes and audible tests require a
separate supervised validation before declaring live support.
