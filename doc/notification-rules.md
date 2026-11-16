# Notification rules

Open **Settings → Events → Notification Rules → Configure** to set quiet hours,
snooze routine notifications, or change notification preferences for individual
cameras, Intercoms, Alarm locations and Alarm devices.

- Quiet hours use this computer's local 24-hour clock and can cross midnight.
  Start and end must differ; the end time resumes routine notifications.
- Snooze lasts 15 minutes, 30 minutes, one hour or two hours. **Resume now**
  cancels it. Existing snoozes keep their original expiry when saving other rules.
- Camera motion and doorbell notifications can be disabled independently.
- An Alarm location rule applies to its routine events; individual device rules
  can further suppress those events.
- Enabled critical Alarm alerts bypass quiet hours, snooze and per-device rules.
  The existing master notification and Alarm alert switches still apply.
- **Reset device rules** clears saved device rules when **Save** is pressed.
  Closing without saving discards edits. A failed save leaves the dialog open.

These preferences affect Halo's desktop notifications only. They do not change
Ring subscriptions, device motion detection, recording, lights or Alarm modes.
They use cached device inventory and make no remote requests. Preferences are
stored locally; routine alert suppression also applies to already queued events
before enrichment and again before delivery. An alert already displayed is not
withdrawn by snoozing. Desktop notification service policies may further suppress
alerts, so Halo is not a substitute for Ring's emergency notifications.

Validation uses synthetic events, clock boundaries, simulated save failures and
the actual native dialog on an isolated display. No real Alarm event is generated.
