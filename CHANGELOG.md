# Changelog

## 0.3.1 — 2026-11-18

- Added Tab - Help to the receiver footer and a modal shortcut guide.
- Pause held-key tuning while help is open and close help with Tab or Esc.

- Prepared the App Store listing with native screenshots, a 256 px icon, permission declarations, and localized summaries.
- Submitted version 0.3.0 to CardputerZero/packages PR #480 with the device demo. Automated package validation passed; first-release maintainer review is pending.

## 0.3.0 — 2026-11-18

- Selected LSB for the 160/80/40 m band presets and USB for the remaining presets.
- Added live mode, noise reduction, and tuner state readback.
- Added N to toggle noise reduction and T to start the antenna tuner.
- Used PipeWire speaker playback to fix silent first connections through the direct ALSA mixer.
- Changed playback controls to Ent - ▶ and Ent - ■.

## 0.2.1 — 2026-11-18

- Kept the stock Cardputer Fn modifier active across repeated volume key presses.
- Added held-key repeat for Z/C receive tuning.
- Saved volume and mute settings across app restarts, with Fn+A toggling mute.
- Shared the ALSA speaker output and retried temporary audio startup failures automatically.

## 0.2.0 — 2026-11-18

- Displayed the IC-7300MK2's live spectrum and scrolling waterfall with a tuned-frequency marker.
- Added 1 kHz receive tuning with Z and C and an amateur-band preset menu on B.
- Moved sound adjustment to Fn+S and Fn+D and show the volume only while adjusting.

## 0.1.0 — 2026-11-18

- Added receive-only LAN streaming for the Icom IC-7300MK2 with 48 kHz, 16-bit mono PCM.
- Added an APPLaunch screen for radio setup, connection status, an audio level meter, and playback volume.
- Buffered playback to absorb short Wi-Fi delivery gaps.
- Added CardputerZero AppBuilder packaging and an on-device playback diagnostic.
