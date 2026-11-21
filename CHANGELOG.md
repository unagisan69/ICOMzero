# Changelog

## 0.6.0 — 2026-11-21

- Show eleven smaller FT8 rows with spectral SNR estimates, CQ/own-call filtering, own-call highlighting and stable line selection.
- Support F/X, Fn+F/Fn+X and arrow aliases for scrolling, including held keys; Enter answers the selected decode.
- Replace the FT8 footer with Tab - Menu and add Bands, Call CQ, Settings, Stop Transmit, Logging and Help.
- Add private callsign/grid settings, timed GFSK transmission and standard contact sequencing using RRR.
- Add direct Icom and TS-890S transmit audio, paced/read-back PTT, cancellation, watchdogs, confirmed split/RIT/XIT-off setup and restoration.
- Recover automatically if receive audio is present but initial CAT state never becomes ready.
- Decode near 14 seconds for timely replies, then merge a full-period pass without duplicate rows.
- Require NTP synchronization for TX, show clock status, check reported offset and cancel on clock jumps.
- Save completed QSOs locally as ADIF-backed records and upload independently to Wavelog/QRZ with durable retry queues.
- Fix a control-startup race where unsolicited scope data could suppress the required Icom CI-V open handshake.
- Pass 48 regression tests, including 48 kHz waveform round trips, SNR calibration, both contact roles, incomplete contacts, navigation, clocks, PTT and logging retries.
- Verify live IC-7300MK2 transmission and PTT-off, clock synchronization, and read-only Wavelog/QRZ access. Live contact attempts received no completed exchange; successful-QSO/logging paths are validated in simulation.

## 0.5.1 — 2026-11-21

- Show nine FT8 decodes per page with alternating row and text colors.
- Remove the FT8 heading, timestamps, audio frequencies and last-cycle status row.
- Keep FT8 reception silent without opening the speaker or changing saved Radio volume/mute settings.

## 0.5.0 — 2026-11-21

- Add a Radio / FT8 startup menu and receive-only FT8 message screen.
- Decode locally from UTC-aligned audio cycles before speaker volume/mute, using ft8_lib.
- Start FT8 on 40m (7.074 MHz USB), with B band selection and scrollable UTC/message history.
- Disable receiver NR for FT8 and restore prior frequency, mode and NR on return to the menu.
- Add M to switch USB/LSB in Radio view and update its shortcut guide.
- Package the native decoder and third-party licenses for ARM64.
- Pass 25 tests covering recorded signals, silent input, capture alignment, stale band results, mute and mode controls.
- Decode 75 live messages over five 40m cycles on IC-7300MK2; verify band changes and M mode readback through the device UI.

## 0.4.1 — 2026-11-20

- Recover transient connection failures automatically and retry lost Icom setup requests.
- Request missing control/CAT packets, drain incoming bursts, and repeat disconnects while releasing the radio session.
- Prioritize and pace tuning commands, coalesce held-key targets, retry missing readback, and show the requested frequency immediately.
- Replace Python pixel and audio scaling loops with vectorized operations and allow display updates up to 30 fps.
- Add regressions for lost packets, sequence wraparound, audio sequence resets, automatic reconnect and tuning feedback.


## 0.4.0 — 2026-11-20

- Added direct network profiles for IC-705, IC-7610, IC-7760, IC-7850/7851, IC-905, IC-9700, and IC-R8600 alongside IC-7300MK2.
- Added native TS-890S KNS TCP control and 16 kHz LPCM RTP audio playback.
- Added a scrollable radio selector, Kenwood audio-port and login-type settings, and capability-aware band presets and controls.
- Added 475/689-bin and multipart Icom scopes, including IC-905 six-byte microwave frequencies.
- Added protocol regression tests for all direct-network profiles and an emulated Kenwood session.


## 2026-11-19

- App Store submission #480 was approved and merged. Updated installation instructions to prioritize the App Store and share code ICMZ.

## 0.3.1 — 2026-11-18

- Added Tab - Help to the receiver footer and a modal shortcut guide.
- Pause held-key tuning while help is open and close help with Tab or Esc.

- Prepared the App Store listing with native screenshots, a 256 px icon, permission declarations, and localized summaries.
- Submitted version 0.3.0 to CardputerZero/packages PR #480 with the device demo. Automated package validation passed; approved and merged on 2026-11-19.

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
