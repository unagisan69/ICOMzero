# ICOMzero

Listen to an Icom **IC-7300MK2** on a **Cardputer Zero** running the stock M5Stack Debian image. ICOMzero connects directly to the radio's LAN interface and plays 48 kHz, 16-bit mono PCM through the Cardputer's built-in speaker. The LCD shows the radio’s live spectrum and waterfall. ICOMzero supports receive tuning with band-appropriate sideband selection, noise reduction, and antenna-tuner control.

## Install

Install **ICOMzero** from the **App Store** on your Cardputer Zero.

1. Open **App Store** from the launcher.
2. Find **ICOMzero**, or enter the share code **ICMZ**.
3. Select **Install**, then open **ICOMzero** from APPLaunch.

Connect the Cardputer to a network that can reach the radio and enable the IC-7300MK2's network control with a configured user account.

For manual installation, download the `.deb` from [GitHub Releases](https://github.com/unagisan69/ICOMzero/releases), then install it on the Cardputer.

```sh
sudo apt install ./icomzero_0.3.1-1_all.deb
```

Open **ICOMzero** from APPLaunch after installation.

## Listen

1. Open **Setup** with **S**. On first launch, the setup screen opens automatically.
2. Enter the radio's IPv4 address or hostname, control port (default **50001**), username, and password. Use **↑**, **↓**, or **Tab** to select a field. Press **Enter** to edit and again to save. Press **Esc** to leave setup.
3. Press **Enter** to start or stop listening. **Z** and **C** tune down and up in 1 kHz steps. Hold either key to scroll. Press **B** for band presets, select with **Z** or **C**, then press **Enter** to tune to the displayed frequency. Band presets select LSB on 160/80/40 m and USB on the remaining presets.
4. Use **Fn+S** to lower the volume and **Fn+D** to raise it. Hold Fn while pressing S or D repeatedly. **Fn+A** toggles mute. The volume appears briefly as you adjust it, and volume and mute are saved for the next launch.
5. **N** toggles the radio’s noise reduction. **T** starts its antenna tuner, which briefly transmits a tuning carrier. The top line shows the current mode, NR when enabled, and TUNING during a tuner operation. Press **Esc** to return to the launcher.

The waterfall uses the radio’s scope span and center or fixed-range settings. The radio negotiates the secondary UDP ports. Keep the radio and Cardputer on the same LAN. If the radio is busy, disconnect the other LAN client and try again. Settings are saved in `~/.config/icomzero/radio.json` (or under `$XDG_CONFIG_HOME`) with user-only permissions. The password is stored in that file as text and encoded using Icom's LAN protocol on the wire.

Press **Tab** on the receiver screen to open the keyboard shortcut guide. **Tab** or **Esc** closes it.

## Build and test

On Debian or the Cardputer itself:

```sh
git clone https://github.com/CardputerZero/AppBuilder.git ../AppBuilder
make check
make package
sudo apt install ./build/icomzero_0.3.1-1_all.deb
```

Use `make package APPBUILDER=/path/to/AppBuilder` for another checkout location. The package includes a foreground APPLaunch entry and declares its Python, Pillow, font, ALSA, and PipeWire dependencies.

To test the installed client over SSH, run `icomzero --configure RADIO_HOST RADIO_USER` and enter the radio password at the prompt. Then run `icomzero --test-audio 10`. The diagnostic plays through the on-device speaker and prints the number of received packets and bytes passed to ALSA's playback process.

## Credits

The Icom LAN protocol implementation was developed using [wfview](https://github.com/wf-group/wfview/tree/d8828dd531776b997f1a345456df7c034f0ea241) as a reference, including its credential substitution table. Packaging uses [CardputerZero AppBuilder](https://github.com/CardputerZero/AppBuilder). ICOMzero is distributed under the GNU General Public License version 3. See [LICENSE](LICENSE) and the [changelog](CHANGELOG.md).
