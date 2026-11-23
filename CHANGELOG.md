# Changelog

## Unreleased

- Add a Simplified Chinese setup tutorial covering installation, native-network radio profiles, listening/CW, FT8, logging, remote access and troubleshooting. Link it from the README and English user guide.

## 0.16.7 — 2026-11-23

- Put Auto CQ first in the FT8 menu, above Bands. Keep selected-decode replies and active automation stop labels attached to the correct actions.
- Keep RR73 and other acknowledgment tokens in the bright message text instead of treating RR73 as a Maidenhead grid locator. Actual grid locators and the smaller signal report remain subdued.
- Add acknowledgment-styling regressions and update menu-routing checks.

## 0.16.6 — 2026-11-23

- Add Fn+F/Fn+X Radio spectrum/waterfall zoom at 1×, 2×, 4× and 8×, centered on the tuned frequency and bounded by the received scope span. The original view remains the widest setting.
- Rescale both displays together, reset differently scaled waterfall history and show the active zoom beside the mode. No radio tuning or scope commands are sent. Plain F/X retains CW scrollback; FT8 controls are unchanged.
- Update keyboard help and operating instructions; cover zoom limits, driver-translated keys, edge clamping, retuning and narrow-bin rendering.

## 0.16.5 — 2026-11-23

- Simplify the FT8 footer with compact rounded controls; show TX and SWR only while armed or transmitting. Preserve its height and red transmit progress.
- Put decode signal strength directly after the Maidenhead grid locator in a smaller font; dim the locator and signal details so the message remains prominent. Keep country/state separately on the right.
- Give the RRR acknowledgment stage four unanswered attempts, including hunted stations, before moving on. Continue immediately when the other station sends 73.
- Re-send a final 73 when a recently completed station repeats RRR/RR73, then resume the interrupted exchange without resetting its attempt count or creating another QSO log. Bound acknowledgment history and queued replies; normal TX safety checks still apply.


- Recover initial radio setup across transient discovery/login failures within a 90-second startup budget, with capped backoff, cancellation and visible retry progress. Disarm the startup deadline once audio and control are ready; retain the existing established-session reconnect and TX-stop behavior.
- Retry a lost Icom capability request without a new login. Distinguish discovery, control handshake, login and capability response timeouts.
- Show unexpected connection-worker failures on screen instead of leaving the previous setup label frozen, while excluding exception input details from private diagnostics.

## 0.16.4 — 2026-11-22

- Refresh the README feature overview, navigation and screenshots; move detailed operating instructions into a linked user guide and update the App Store listing.

- Preserve CW timing through late but contiguous audio. Icom and Kenwood RX sequence gaps now explicitly resynchronize CW; remove the wall-clock delay reset and enlarge the bounded worker queue to absorb packet bursts.
- Learn letter and word gap groups separately from character speed, including stretched/Farnsworth spacing. Keep early spacing decisions provisional and cache the timing model to reduce repeated work.
- Add F/X and Fn+F/Fn+X CW scrollback, including hold repeat. Stay on the reading position while new text arrives; scrolling to the newest line resumes automatic following.
- Keep up to 16,384 characters during a Radio session. Retunes, reconnects, tone changes and audio resynchronization separate segments with a line break instead of clearing the visible history. Use a monospaced CW font, a history-position header and scrollbar while browsing.
- Add regressions for delayed delivery versus actual loss, sequence wraparound, burst handling, stretched spacing, keyboard routing, bounded scrollback and returning to live follow.

## 0.16.3 — 2026-11-22

- Stabilize CW tone acquisition with a bounded spectral history, keyed-energy selection, sub-bin pitch tracking and a short confirmation/lookback. Avoid latching onto a nearby steady carrier; reacquiring a tone resets its speed estimate.
- Measure keying every 5 ms using a local noise floor and narrow tone checks. Remove the fixed peak-level gate that erased fading marks; retain receiver-noise rejection.
- Repair invalid Morse groups using measured gaps across the word, while preserving already-valid letter boundaries; do not use an English dictionary or callsign guesses. Adapt word spacing and avoid premature boundaries while a rising edge is still being confirmed. In-progress words can revise as more Morse arrives.
- Add receive-only regressions for weighted/compressed hand timing, fading, nearby interference, manual off-bin selection, tone/speed changes and receiver noise after a station stops.

## 0.16.2 — 2026-11-22

- Make W toggle CW decoding without changing the radio’s demodulation mode, dial frequency or receive filter. Add an independent CW decoder toggle to Radio settings; native CW/CW-R remain explicit radio mode choices.
- Extend audio-tone search to 200–2800 Hz for CW heard in SSB, and evaluate local tone concentration so another signal elsewhere in the passband does not mask it. Add manual CW tone selection for crowded passbands.
- Add regressions for preserving LSB/frequency on W, higher-pitched CW with another tone, and manual tone selection with a louder interfering carrier.

## 0.16.1 — 2026-11-22

- Tighten CW tone concentration thresholds to reject 250/500 Hz filtered receiver noise without losing the generated noisy 8–35 WPM test messages. Add a filtered-noise regression.

## 0.16.0 — 2026-11-22

- Add receive-only CW/CW-R Morse decoding to Radio. W enters/leaves CW, and S → Mode selects LSB/USB/CW/CW-R. Start decoding only after the radio confirms CW mode; pause on disconnect, tuning or PTT.
- Display live decoded text, automatic tone and estimated speed beneath the waterfall. Use raw audio before volume/mute, a bounded background worker, adaptive spectral/envelope detection and Morse timing. Reset partial symbols across tuning, audio gaps and overruns.
- Add CW receive band presets while preserving CW/CW-R on band changes. Icom CW modes request filter 2 and immediate mode readback; existing voice/FT8 controls remain unchanged.

## 0.15.0 — 2026-11-22

- Redesign the startup menu with Radio, FT8 and Settings icon cards, the active radio name and app version.
- Move connection setup to Settings → Radios. Add named radio profiles with selection, editing, cloning and deletion; migrate the legacy connection privately without discarding volume/mute or logging preferences. Pin each live connection to its selected profile.
- Make Radio → S a radio-only menu for Band and 1000/500/100 Hz tuning steps. Persist the step per profile and apply it to tap/hold tuning.
- Add bounded background GitHub checks at launch, an optional startup-check preference, manual updates, download/installation progress, checksum and package validation, administrator-password entry and explicit restart. Adapt the MIT-licensed MeshCore Zero updater and retain its license.
- Consume nested-menu input events individually so keystrokes are not lost across screen transitions. Preserve radio/FT8 RF protections.

## 0.14.2 — 2026-11-22

- Fix #1: handle the stock Cardputer symbol-layer keycodes in radio setup and FT8 text fields. SYM+B enters a period; punctuation in radio credentials and logging URLs follows the shipped keymap.
- Allow S to open radio settings while receiving, from the band chooser or from Help. Close the receive stream before editing connection settings; retain Fn+S volume control. Add an S - Settings footer hint.

## 0.14.1 — 2026-11-22

- Detect a changed local route address and reopen the radio transport instead of keeping sockets from the previous address. Cancel ongoing transmission through the existing disconnect path.
- Wait up to 90 seconds for a refused Icom stream to become available, using fresh requests on one authenticated session and a capped eight-second retry interval instead of four rapid failures and repeated logins.
- Preserve explicit restart requirements and PTT/RF guards; recovery does not automatically restart Auto CQ or tuning.

## 0.14.0 — 2026-11-22

- Add Settings / Logging → Sync Log for private, incremental Wavelog or QRZ worked-history downloads. Keep imported history separate from contact upload queues.
- Add Hide previous contacts, limited to FT8 on the current band. Filter the decode view and unsolicited automated CQ targets while preserving active exchanges and own transmitted messages. Auto CQ still answers or queues previously worked stations that call you. New local QSOs update the index immediately.
- Use paged background sync with resumable cursors, account-scoped indexes and cached local lookups. Publish the operator-validated log-sync update to the App Store.

## 0.13.0 — 2026-11-22

- Publish the validated FT8 reliability fixes to the App Store: continuous 12 kHz audio without empty tracked audio packets, 300 ms TX buffering with matching PTT drain, complete retransmission-range repair, and bounded CI-V recovery.
- Recognize fast confirmed tuner completion and use continuous readiness deadlines after successful transmissions; preserve RF safeguards and private diagnostics.
- Remove AUTO and POTA text labels from the FT8 status bar. Keep the armed dot, TX/SWR/power indicators and menu controls.
- Verify continued operation and QSO logging on the IC-7300MK2; other radio profiles remain pending physical validation.

## 0.12.5 — 2026-11-22

- Repair every packet in inclusive Icom retransmission ranges, including sequence wrap, rather than only their endpoints. Deduplicate overlapping ranges, bound repair bursts, and preserve idle placeholders for commands deliberately removed from replay history.
- Measure readiness timeouts from the last confirmed ready state, allowing brief CI-V recovery after successful transmissions without reusing an expired startup deadline. Keep an eight-second continuous readiness limit and log readiness failures privately with source/mode/control state.
- Retain 300 ms TX buffering, matching PTT drain, CI-V recovery and all RF guards.

## 0.12.4 — 2026-11-22

- Negotiate a 300 ms Icom FT8 transmit buffer and hold PTT for its matching drain plus a 50 ms margin after the final samples. Preserve UTC packet scheduling and existing transmit watchdog limits.
- Reopen a stalled CI-V bridge after two seconds rather than immediately replacing the whole session at five seconds. Require fresh frequency and mode readbacks before permitting new TX; retain the original five-second failure deadline and RF safeguards.
- Keep audio-channel idle packets disabled. Add buffered-drain and real-UDP control-recovery regressions. On-air validation remains pending.

## 0.12.3 — 2026-11-22

- Do not inject tracked empty idle packets into the audio channel. Retain audio-channel discovery and ping keepalives; the tracked stream contains audio payloads during TX.
- Restart control/CAT idle deadlines after real tracked payloads, matching reference-client idle semantics.
- Advance ping sequence numbers on matching replies instead of repeatedly sending sequence zero.
- Retain RF safeguards and the fast-tuner completion correction. Experimental on-air validation is still required.

## 0.12.2 — 2026-11-22

- End tuner telemetry protection on a confirmed completed cycle even when it finishes within 600 ms. Require fresh tuner and PTT/RX readbacks, support either reply order, and keep the guard active when RX is only cached.
- Start tracked UDP sequence numbers at one, matching wfview and FT8TW discovery/session conventions.
- Keep the experimental 12 kHz audio and staggered polling configuration. On-air audio reliability remains unresolved; local waveform decoding does not establish successful radio modulation.

## 0.12.1 — 2026-11-21

- Use native 12 kHz mono PCM for Icom FT8 receive/transmit and 20 ms TX packets; preserve 48 kHz radio listening. Decode incoming 12 kHz audio directly and encode TX at the negotiated rate.
- Stagger TX meter/state queries at 100 ms intervals (500 ms per meter), reduce pending PTT queries to 100 ms, and allow four polling rounds before a missing FT8 meter stops TX. Keep high SWR, excessive ALC, PTT, clock and connection protection.
- Pace TX sample delivery against a monotonic deadline while retaining UTC clock-jump detection and stall shutdown.
- Experimental reliability update: hardware RF output and sustained contact performance still require operator validation.

## 0.12.0 — 2026-11-21

- Preserve an existing designated FT8 dial on entry; otherwise require band selection before setup/tuning/TX. Show the active band beside power and in the menu. Revalidate after reconnection without automatically resuming RF.
- Use IC-7300MK2 USB-D/FIL1 and DATA MOD/LAN with explicit mode/filter/source readback. Disable split/RIT/XIT during receive setup and preserve original settings for restoration.
- Reconnect after DHCP/address-change socket errors, request RX on recovery, retain latched RF faults, and preserve underlying network/channel-close reasons. Add channel/CIV reply-age diagnostics.
- Add offline USA-XX registered-state labels from a minimized FCC callsign/state database; keep unknown states explicit and preserve foreign operating entities. Include source provenance and a reproducible extraction tool.

## 0.11.0 — 2026-11-21

- Change Auto CQ to one own CQ, then up to five distinct strong recent CQ targets with two calls each before no-response stop.
- Add POTA Hunt: monitor tagged POTA/SOTA CQs, send a direct R-report at the opposite UTC period/audio offset, allow three unanswered attempts per stage, then rest and continue monitoring. Require actual exchanged reports for logging.
- Clear latched RF warnings with Enter after confirmed receive state; remove the Tab menu clearance step and prevent the same key from starting a reply.
- Move receive filtering/capture off the network pacing thread, bound packet-drain work, and reset capture alignment on queue overruns. Cache unchanged decode rows while refreshing live TX/meters.
- Add private transport-loop gap diagnostics. Retain all existing SWR/ALC/telemetry, clock, PTT and pacing safety limits.

## 0.10.0 — 2026-11-21

- Queue fresh callers who address the station during another exchange; refresh/deduplicate entries, expire stale callers, and prioritize queued contacts after completion or abandonment.
- Replace four own CQs with two CQs, then up to two distinct strong recent CQ targets with two initial calls each. Keep opposite-slot timing, Auto audio frequency, per-stage exchange retries and existing RF/clock safety stops.
- Avoid completed stations, unsupported compound/hashed targets and stale/busy CQ candidates; prepare target transitions before encoding, and journal queue/ranking/expiry decisions.
- Center and wrap notifications and RF warning cards; highlight tagged POTA/SOTA CQ rows in green.
- Bundle licensed offline BIG CTY country/territory lookup with exact exceptions, portable prefixes, compact decode labels, selected-decode entity display and optional local CTY.DAT import.

## 0.9.0 — 2026-11-21

- Add explicitly started Auto CQ: four unanswered CQs, four unanswered attempts per contact stage, then resume CQ after a completed contact or an abandoned attempt. Wait for the final receive opportunity before stopping.
- Show AUTO while running; Stop Auto CQ, Stop Transmit, exit, band/settings changes and RF/clock/connection faults cancel the session.
- Keep private per-session JSONL journals of received decodes, transmitted messages, timing, frequencies, reports, state decisions, meters, logging status and stop reasons.
- Play a short completion beep for newly saved QSOs, with a saved QSO beep setting, and a distinct sound when Auto CQ stops after four unanswered calls. FT8 receive audio remains muted.

## 0.8.0 — 2026-11-21

- Reveal local TX rows after the complete 15-second period, show a red armed dot, and animate the UTC progress bar in red while transmitting.
- Continue highlighted contacts from their latest grid/report/acknowledgment stage, including R-report → RRR, with retained known reports and opposite-slot timing.
- Default TX audio frequency to Auto: follow the selected station’s offset, or use 1500 Hz for an unanswered own CQ. Numeric manual settings remain supported.
- Add selected-decode Record QSO review/save with required exchanged reports and duplicate protection; reuse the existing Wavelog/QRZ outboxes.
- Show a green QSO-saved popup after a new local save.
- Smooth and round the watt readout while retaining raw safety meters and private steady-power diagnostics.

## 0.7.5 — 2026-11-21

- End RF telemetry assessment when RX is confirmed, keeping any real fault latched. Begin a new episode only when the next PTT-on is sent.
- Fix false SWR telemetry-loss faults between CQ cycles when command pacing briefly delays the next PTT request.
- Apply matching RX confirmation to Kenwood as well, so an early RX readback cannot end a newly requested TX episode.
- Add a deterministic regression reproducing a 30-second CQ gap and deferred PTT dispatch; retain all active-RF SWR/ALC and telemetry-loss limits.

## 0.7.4 — 2026-11-21

- Prepare FT8 waveforms before the transmit slot and queue frames within its first 200 ms. Request PTT up to 500 ms before the unchanged UTC slot +0.5-second audio start.
- Require PTT readback to match the requested TX/RX state and poll pending confirmation every 50 ms. Keep tuner-stop commands ahead of confirmation polls.
- Retain confirmed-TX gating and late/stalled-frame cancellation; distinguish a return to RX during a frame from initial PTT confirmation failure.
- Include PTT timing and audio-start state in private failure diagnostics.

## 0.7.3 — 2026-11-21

- Allow a bounded three-second meter pause during internal antenna tuning; retain the 1.2-second missing-telemetry deadline for FT8 frames.
- Keep immediate shutdown for fresh SWR above 2.0 during both tuning and FT8 transmission.
- Disable unused network spectrum output in FT8 and restore its prior state on exit, without changing the radio’s front-panel scope display.

## 0.7.2 — 2026-11-21

- Retry temporarily refused or zero-port Icom stream requests as fresh transactions within the authenticated session, with bounded backoff.
- Give login and secondary UDP stream startup independent eight-second budgets; start CI-V readback timing when the command channel opens.
- Preserve the underlying connection failure reason on screen and in bounded, private connection diagnostics.
- Add UDP radio-emulator regressions for busy replies, explicit refusals and first audio arriving after the old three-second limit.

## 0.7.1 — 2026-11-21

- Preserve a successfully tuned antenna when opening Settings, stopping FT8, or clearing a warning; stop/bypass only an active tuner.
- Keep RX/tuner confirmation polling alive after a fault. Clear warning now waits for fresh receive readback instead of dropping a request against stale TX state.
- Stop PTT-off retries once receive is confirmed, so they cannot interrupt a subsequent tuner cycle.
- Judge tuner completion from the actual command-send timestamp and observed tuning state; do not declare failure from an early bypass readback.
- Reduce configuration polling during RF to leave bandwidth for safety meters. Keep the existing SWR/ALC and telemetry-loss cutoffs.
- Record private, bounded fault diagnostics with meter ages, without credentials or decoded messages.

## 0.7.0 — 2026-11-21

- Clear decode selection after replying or scrolling to the newest row; resume following new decodes.
- Distinguish TX armed from active RF, show fresh calibrated SWR/output watts, and add blue local TX records to history.
- Add private nominal RF power settings (10 W default), confirmed application and prior-power restoration.
- Start the antenna tuner on FT8 entry/band changes after frequency and power confirmation; add Tune antenna and Clear warning menu actions.
- Stop and latch a warning for SWR above 2.0, excessive/sustained ALC, or missing telemetry. Internal tuner carriers use SWR protection, with no external-audio ALC test.
- Prevent stale tuner-start replay and prevent transmission while a fault or tuning operation is active.
- Remove the home tagline.

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
