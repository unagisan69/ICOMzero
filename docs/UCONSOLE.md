# ICOMzero on uConsole

[Project overview](../README.md) · [Full user guide](USER_GUIDE.md) · [Releases](https://github.com/unagisan69/ICOMzero/releases/latest)

ICOMzero supports the **ClockworkPi uConsole running ARM64 Debian with a graphical desktop**, alongside the original Cardputer Zero build. The desktop frontend shares the same radio protocols, CW decoder, FT8 engine, logbook and settings. It presents the compact application screens in a resizable window, scaled to fit the display; it does not require the Cardputer LCD or keypad devices.

The port is **keyboard-first**. Trackball clicks and the on-screen navigation buttons are optional. The desktop window works through X11/XWayland; the tested device runs Debian 13, labwc/Wayland and a 1280×720 screen.

## Install

Download **`icomzero-desktop_0.18.0-1_arm64.deb`** and **SHA256SUMS** from [GitHub Releases](https://github.com/unagisan69/ICOMzero/releases/latest). On the uConsole, check the package and install it:

```sh
sha256sum --check --ignore-missing SHA256SUMS
sudo apt install ./icomzero-desktop_0.18.0-1_arm64.deb
```

Use the **desktop** package on uConsole. `icomzero_VERSION-1_arm64.deb` is the Cardputer build; the two variants have the same Debian package name and are alternatives, not separate apps installed together.

Open **ICOMzero** from the desktop application menu, or run:

```sh
icomzero
```

For a fullscreen launch:

```sh
icomzero --desktop --fullscreen
```

The package installs its own launcher, icon, Python modules and native FT8 helpers. APT installs the Tk/Pillow/NumPy and ALSA/PipeWire dependencies. Run the app as your normal desktop user, not root. Launch it within the graphical session; a plain SSH shell without access to the desktop display cannot create the window.

## Keyboard controls

| Key | Action |
| --- | --- |
| **↑ / ↓** | Navigate menus, browse Log, select FT8 decodes, or scroll CW history |
| **← / →** | Choose a main-menu card; tune down/up in Radio (hold to repeat) |
| **Enter** | Open/confirm; in Radio start/stop listening; in FT8 reply to a selected decode |
| **Tab** | Open the current screen’s menu/help; close a selection menu |
| **Esc** | Back; leaving Radio/FT8 stops that session |
| **Ctrl+↑ / Ctrl+↓** | Zoom Radio spectrum/waterfall in/out, 1×–8× |
| **Ctrl+← / Ctrl+→** | Radio volume down/up |
| **Ctrl+M** | Mute/unmute Radio audio |
| **Ctrl+Enter** or **F11** | Toggle fullscreen |
| **Ctrl+Q** | Quit from any screen, with normal receiver/transmitter cleanup |
| **S** | Radio settings |
| **B** | Select a band in Radio or FT8 |
| **M** | USB/LSB toggle in Radio |
| **W** | CW decoder toggle in Radio |
| **N** | Noise reduction, if supported |
| **T** | Antenna tuner, if supported; **briefly transmits** |

F/X and Z/C remain available as secondary shortcuts. Ctrl also supplies the original Fn modifier, but it is not required for ordinary arrow-key navigation. F1–F4 select the main-menu cards when available on your keyboard.

In text-entry forms, type punctuation normally; no Cardputer SYM layer is needed. **←/→**, **Home/End**, **Backspace**, **Delete** (clear the field), and **Ctrl+V** paste are supported by the shared settings forms. Enter saves and Esc cancels. FT8’s compact inline fields retain their append/backspace editor and also accept ordinary desktop text and paste. Key releases are handled across focus changes so moving to another window does not leave a tuning key held.

## Settings and existing data

Use **Settings → Radios** to add/select a radio. Use **Settings → Station & logging** for station identity and Wavelog/QRZ credentials. These are the same settings used by FT8 and Log; all features described in the [user guide](USER_GUIDE.md) remain available.

Data lives in **`~/.config/icomzero/`** (or under `$XDG_CONFIG_HOME`), matching the Cardputer format:

- `radio.json`: named radio connections and app preferences.
- `ft8.json`: station identity, FT8 preferences and provider credentials.
- `qsos.sqlite3`: onboard contacts and durable upload queues.
- `worked.sqlite3`: cached worked-station history.

To migrate from another device, close the app on both devices before copying these files, or use SQLite’s backup API for a consistent database copy. Copy databases rather than only their main file while a live WAL transaction may exist. Preserve mode **0600** on files and **0700** on the private directory. Do not put credentials or private logs in the GitHub repository.

After copying profiles, select the desired connection with **Use this radio**. The log copies then evolve independently; use the configured provider sync to bring later contacts across devices.

## Audio

The desktop build uses the desktop’s default PulseAudio/PipeWire output rather than forcing the Cardputer speaker sink. Select speakers/headphones through the uConsole’s system audio controls. FT8 reception remains intentionally silent; Radio and optional QSO notifications use system playback.

For an explicit ALSA output override:

```sh
icomzero --audio-device pulse
```

`ICOMZERO_AUDIO_DEVICE` is also supported. If you explicitly set `PULSE_SINK`, the desktop build respects it. The default `pulse` device needs `libasound2-plugins`, included in the package dependencies.

## Updates

Use **Settings → Check for updates**. The desktop updater selects the **icomzero-desktop** asset and verifies SHA-256; the Cardputer updater continues to select its own asset. The desktop installer also checks the package’s platform marker before installation. Settings and logs are preserved. Fully close/reopen the app after an update.

## Build on ARM64 Debian

```sh
sudo apt install build-essential python3-tk python3-pil python3-pil.imagetk python3-numpy fonts-dejavu-core
make check
make test
make package-desktop
sudo apt install ./build/icomzero-desktop_0.18.0-1_arm64.deb
```

The existing `make package APPBUILDER=/path/to/AppBuilder` target still builds the Cardputer package. Desktop packaging does not require AppBuilder or install APPLaunch files.

## Validation and limits

The uConsole port has been checked with native keyboard-driven menu/log navigation and text entry, live Icom receive audio, frequency/scope telemetry, spectrum zoom and disconnect cleanup. DSP/protocol and FT8 waveform checks run in the automated suite. No over-the-air test transmission was needed for the port; entering FT8 normally can start its existing automatic tuner sequence, so use the same antenna/clock/RF precautions as on Cardputer.

Other ARM64 Debian desktop systems may work but have not received the same physical-device validation. The interface currently keeps the shared compact layout rather than adding extra decode rows just because the window is larger.
