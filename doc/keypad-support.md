# Keypad brightness and proximity

Security → a keypad's details provides reported LED brightness and an explicit
Apply action. This slice qualifies only the hub `security-keypad` type with a
finite reported `brightness` in the Ring range 0–1. Halo displays an integer
percentage rounded half up and submits exactly that percentage divided by 100.

Controls start disabled and locked. Enable brightness controls and unlock them
locally before choosing a value and applying it. Permission is stored separately
in `keypad-settings.json`, scoped to the signed-in account and keypad identity.
The file uses hashed keys and mode 0600. The lock gates Halo writes; it does not
enforce a brightness value against Ring, other applications, or physical users.
Saving permission never sends a device command.

The command uses the existing authenticated Alarm socket and the exact CLAP
`DeviceInfoSet` body `[{"zid": "…", "device": {"v1": {"brightness": 0.5}}}]`.
Current owner authorization, exact `base_station_v1` identity, connection epoch, complete
inventory, brightness and communication observations are checked again at socket
handoff. Observations must be at most 120 seconds old. There is one pending
request per keypad, no write replay, and a bounded ten-second confirmation wait.
Only a subsequent matching brightness report confirms the setting; socket
completion alone produces no optimistic change. An uncertain result requires a
new brightness observation before another request. Closing the dialog revokes
an unsent request; it cannot recall a request already handed to the socket.

Proximity is unknown until a live `impulse.v1` entry reports
`impulseType: "keypad.motion"` for a matching `general.v2.zid` and a known current
keypad. Inventory documents and generic motion fields do not establish proximity.
Each event retriggers a local 20-second recent-detection indicator. Expiration
means no event in that window, not a Ring-reported clear signal. Connection loss
or identity replacement restores unknown. Timers and pending operations are
retired with the Alarm service.

Protocol evidence follows the qualified local Halo Server implementation and
the pinned [homebridge-ring brightness adapter](https://github.com/dgreif/ring/blob/3bcc624acc35b6f97c391089a12bff582bb6ed4a/packages/homebridge-ring/brightness-only.ts).
The proximity contract follows the pinned
[ring-mqtt keypad adapter](https://github.com/tsightler/ring-mqtt/blob/59aab41b4afd1692148b1180a6b0920591c86db1/devices/keypad.js).
Ring's [keypad light modes](https://ring.com/support/articles/izp62/Understanding-Ring-Alarm-Keypad-2nd-Generation-Lights-and-Buttons)
are separate from brightness; Halo does not change power-saving or wake modes.

Validation is synthetic protocol, lifecycle, transport and native UI testing.
No live brightness change or physical proximity detection was performed.
