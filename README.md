# ICOMzero

Listen to a network-connected radio on a **Cardputer Zero** running the stock M5Stack Debian image. ICOMzero connects directly to the radio over LAN or Wi-Fi and plays receive audio through the Cardputer speaker, with a live spectrum/waterfall, tuning, band presets, noise reduction, and antenna-tuner controls where the radio supports them.

## Direct network radios

| Manufacturer | Models |
| --- | --- |
| Icom | IC-705, IC-7300MK2, IC-7610, IC-7760, IC-7850 / IC-7851, IC-905, IC-9700, IC-R8600 |
| Kenwood | TS-890S |

These profiles use the native network protocols and capabilities from wfview's radio definitions. **IC-7300MK2 has been tested on physical hardware. The added models have protocol and emulator coverage and still need physical-radio validation.**

The IC-7300 original model and the USB/serial-only wfview radios require a computer bridge and are outside this direct-network release. The IC-7850/7851 share the **IC-785x** profile.


## Install

Install **ICOMzero** from the **App Store** on your Cardputer Zero.

1. Open **App Store** from the launcher.
2. Find **ICOMzero**, or enter the share code **ICMZ**.
3. Select **Install**, then open **ICOMzero** from APPLaunch.

Connect the Cardputer to a network that can reach the radio and enable the radio's network control with a configured user account.

For manual installation, download the `.deb` from [GitHub Releases](https://github.com/unagisan69/ICOMzero/releases), then install it on the Cardputer.

```sh
sudo apt install ./icomzero_0.7.4-1_arm64.deb
```

Open **ICOMzero** from APPLaunch after installation.

## Listen

1. Choose **Radio** from the startup menu. Open **Setup** with **S**. Setup opens automatically when no connection is configured.
2. Enter the radio's IPv4 address or hostname, control port, username, and password. Use **↑**, **↓**, or **Tab** to scroll through setup fields. Press **Enter** to edit a text field and again to save. Press **Enter** on **Radio** to cycle models. **Auto (Icom)** identifies a supported Icom from its name or CI-V address. Select the exact model if the radio uses a custom name and address. Press **Esc** to leave setup.
   - Icom uses control port **50001** by default. Keep its normal negotiated audio ports.
   - TS-890S uses **60000** for TCP control and **60001** for UDP audio. Selecting TS-890S switches the default control port. Set **Login** to **User** or **Admin** to match the account configured on the radio. Its 16 kHz LPCM audio is converted to the app's 48 kHz speaker stream.
3. Press **Enter** to start or stop listening. **Z** and **C** tune down and up in 1 kHz steps. Hold either key to scroll. Press **B** for band presets, select with **Z** or **C**, then press **Enter** to tune to the displayed frequency. The menu shows presets within the connected model’s frequency ranges. Band presets select LSB on 160/80/40 m and USB on the remaining presets.
4. Use **Fn+S** to lower the volume and **Fn+D** to raise it. Hold Fn while pressing S or D repeatedly. **Fn+A** toggles mute. The volume appears briefly as you adjust it, and volume and mute are saved for the next launch.
5. **N** toggles the radio’s noise reduction where supported. **T** starts its antenna tuner, which briefly transmits a tuning carrier. T is inactive on receiver-only radios and models without a tuner command. The top line shows the current mode, NR when enabled, and TUNING during a tuner operation. Press **M** to switch USB/LSB. Press **Esc** to return to the main menu.

The waterfall uses the radio’s scope span and center or fixed-range settings. Icom radios negotiate secondary UDP ports. Kenwood audio arrives on the configured UDP audio port. Keep the radio and Cardputer on the same LAN. Temporarily refused/zero-port stream replies trigger fresh stream transactions with bounded backoff, using the existing login. Login, secondary-channel setup and CI-V startup have separate time budgets. Connection failures retain their underlying reason on screen and in a bounded private `~/.config/icomzero/connection-diagnostics.jsonl` file (no credentials or packet payloads). If the radio is busy, disconnect the other LAN client and try again. Settings are saved in `~/.config/icomzero/radio.json` (or under `$XDG_CONFIG_HOME`) with user-only permissions. The password is stored in that file as text and encoded using Icom's LAN protocol on the wire.

Press **Tab** on the receiver screen to open the keyboard shortcut guide. **Tab** or **Esc** closes it.

## FT8 contacts

Choose **FT8** from the startup menu. Reception starts silently on **40m, 7.074 MHz USB**. The screen displays eleven smaller decode lines, prefixed with estimated signal reports in dB relative to 2500 Hz. Only CQ calls, messages involving your configured callsign, and your own transmitted frames appear. Blue **TX** rows are local transmit records, not received decodes or proof the other station heard you. Gold rows indicate your callsign; the outlined row is your selection.

- **F / Fn+F** selects the previous decode; **X / Fn+X** selects the next. Hold to move through history one line at a time. Selection stays on the same decode when new messages arrive. Scrolling down to the newest line clears selection and resumes automatic scrolling.
- **Enter** answers the highlighted recent CQ or initial addressed call. Selection clears after replying and the display returns to the newest decodes.
- **Tab – Menu** opens Bands, Call CQ, Settings, Stop Transmit, Logging, Tune antenna, Clear warning and Help.
- **Esc** returns to the main menu and stops transmission.

In **Settings**, enter your callsign and Maidenhead grid (four or six characters), choose your CQ period, TX audio frequency audio level (10–80%, default 50%), and nominal RF power in watts (default 10 W; TS-890S minimum 5 W). Replies use this configured TX audio frequency, independently of the selected station’s receive frequency. Adjust transmit audio for little/no ALC using the radio’s meter. **Call CQ** begins your own CQ sequence; Enter on a selected CQ answers that station. A standard contact exchanges grids and signal reports, uses **RRR** (not RR73) for acknowledgment, and finishes with 73. Incoming RR73 is accepted. Only one explicitly started contact is active at a time; there is no unattended search for additional contacts. Five unanswered repetitions or a three-minute limit stops the sequence without creating a completed contact.

The device must report NTP synchronization before transmission. The menu shows clock health; a reported offset over 250 ms blocks TX. Clock checks repeat during a sequence, and clock jumps, pacing stalls, failed PTT confirmation, disconnects, band changes and Stop Transmit cancel transmission. Waveforms are prepared before the slot. PTT is requested up to 500 ms before the fixed audio start at UTC slot +0.5 seconds; frames are queued only in the first 200 ms of their chosen slot. Audio waits for confirmed TX. PTT readbacks must match the requested state, and a pending state is queried every 50 ms. PTT-off confirmation is required before a transmission counts as completed. Split, RIT and XIT are disabled and read back before TX so the intended VFO frequency is used; their previous settings are restored on a normal exit. RF power is applied and read back before tuning or FT8 TX. Icom power settings use a nominal fraction of the model’s rated power; actual watts depend on band and hardware limits. The live RF meter reports calibrated output, and the prior power control is restored on normal exit. Receive-only IC-R8600 cannot transmit. Network transmit paths cover the supported Icom models and TS-890S; physical validation is on IC-7300MK2, and other transmit models still need hardware validation.

**Automatic antenna tuning briefly transmits a carrier** when FT8 opens and after each band change, on models with a supported tuner. It waits for band and power confirmation; Call CQ/reply waits for tuning to finish. Stop Transmit cancels active tuning but preserves an already completed tuner match. Opening Settings also preserves that match. The main menu no longer displays the tagline.

The footer distinguishes green **ARM** (contact/CQ sequence enabled, including receive periods) from red **TX** (radio transmitting, including tuner carriers). Beside TX are fresh SWR and actual watts; receive periods show the configured wattage. Meter calibration is taken from the pinned wfview radio profiles. Samples older than 1.2 seconds are hidden.

**RF protection:** SWR strictly above 2.0 stops FT8 audio and requests PTT-off/tuner-stop, with a latched warning. ALC at the model’s redline, or above 20% of that redline for 0.3 seconds, also stops FT8; reduce TX audio level before restarting. Missing SWR/ALC telemetry after a 1.2-second startup grace stops FT8 transmission. Internal tuner cycles have a three-second SWR telemetry startup/staleness limit to accommodate tuning pauses; a fresh SWR above 2.0 still stops immediately. The radio’s internal tuner carrier uses the SWR guard but is exempt from external-audio ALC checks. Consequently automatic tuning can abort when the initial match exceeds 2.0. Correct the cause, then choose **Clear warning** and explicitly restart tuning or the contact. Clearing waits for fresh receive/tuner readback and does not restart transmission. RF faults write bounded, private meter-age diagnostics to `~/.config/icomzero/rf-diagnostics.jsonl`; credentials and decoded traffic are excluded. These network-polled guards supplement the radio’s own protection and cannot guarantee instantaneous RF cutoff if connectivity fails.

Unused network spectrum output is disabled during FT8 and restored on exit, leaving the radio’s own scope display untouched. Speaker audio stays disabled in FT8. Raw audio reaches the local decoder before volume/mute processing. Reception stays aligned to 15-second periods. An early decode pass near 14 seconds prepares timely replies; a full-period pass can add late signals without duplicating rows. Receiver NR is disabled for FT8; keep noise blanker and notch filters off for best decoding. The app restores prior frequency, mode, NR and transmit modulation settings on a normal exit. It avoids restoring transmit inputs while PTT remains unconfirmed.

### Logging

Completed contacts are saved first to a private local SQLite log with ADIF data. **Logging → Local log** shows completed contacts. Enabled services receive independent queued uploads; offline or rejected uploads stay pending and can be retried with **Sync pending**, automatically once per minute, or at the next launch. An incomplete or cancelled contact is not logged.

- **Wavelog:** set the HTTPS API base URL (including `index.php` if your installation uses it), a read/write key and station profile ID. **Test connections** finds the station matching your callsign/grid when the ID is empty. Uploads use the [Wavelog QSO API](https://docs.wavelog.org/developer/api/#apiqso); Wavelog does not forward API-created QSOs to QRZ, so configure QRZ separately if desired.
- **QRZ:** enter the logbook API key and enable the service. The [QRZ Logbook API](https://www.qrz.com/docs/logbook/QRZLogbookAPI.html) requires the appropriate subscription for status and insertion. Uploads do not request replacement of existing confirmed contacts.

Connection tests only read station/logbook information and never create test QSOs. Credentials and station identity live in `~/.config/icomzero/ft8.json` with mode 0600; the local log is `qsos.sqlite3` in the same private directory. No credentials or personal station defaults are bundled in the public app.

The decoder/encoder uses [ft8_lib](https://github.com/kgoba/ft8_lib). Reports are spectral SNR estimates, not the synchronization score; real-world values can differ from WSJT-X. Sensitivity is lower than a full WSJT-X decoder, and unresolved hashed/nonstandard calls cannot be selected for automatic replies. This release handles standard FT8 contacts rather than contest, Fox/Hound or multi-stream operation.

## Build and test

On Debian or the Cardputer itself:

```sh
git clone https://github.com/CardputerZero/AppBuilder.git ../AppBuilder
sudo apt install build-essential python3-pil python3-numpy
make check
make test
make package
sudo apt install ./build/icomzero_0.7.4-1_arm64.deb
```

Use `make package APPBUILDER=/path/to/AppBuilder` for another checkout location. The package includes a foreground APPLaunch entry and declares its Python, Pillow, NumPy, font, ALSA, and PipeWire dependencies.

To configure an Icom over SSH, run `icomzero --configure RADIO_HOST RADIO_USER` and enter the radio password at the prompt. Then run `icomzero --test-audio 10`. The diagnostic plays through the on-device speaker and prints the number of received packets and bytes passed to ALSA's playback process.

For a TS-890S account, use:

```sh
icomzero --configure RADIO_HOST RADIO_USER --model TS-890S --port 60000 --audio-port 60001
```

Add `--admin` for the administrator account. Passwords are entered at the prompt. The package does not include account credentials.

The radio capability snapshot is pinned in `app/radios.json`. Regenerate it with `python3 tools/update_radio_profiles.py /path/to/wfview`. It includes only wfview profiles that advertise native LAN support.

## Credits

The Icom LAN and Kenwood KNS/RTP implementations were developed using [wfview](https://github.com/wf-group/wfview/tree/8e4172c18bddc2efc6ba93fdbd33736ab0939bdd) as a reference, including its credential substitution table. Packaging uses [CardputerZero AppBuilder](https://github.com/CardputerZero/AppBuilder). ICOMzero is distributed under the GNU General Public License version 3. See [LICENSE](LICENSE) and the [changelog](CHANGELOG.md).
