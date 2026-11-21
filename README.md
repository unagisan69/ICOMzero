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
sudo apt install ./icomzero_0.4.1-1_all.deb
```

Open **ICOMzero** from APPLaunch after installation.

## Listen

1. Open **Setup** with **S**. On first launch, the setup screen opens automatically.
2. Enter the radio's IPv4 address or hostname, control port, username, and password. Use **↑**, **↓**, or **Tab** to scroll through setup fields. Press **Enter** to edit a text field and again to save. Press **Enter** on **Radio** to cycle models. **Auto (Icom)** identifies a supported Icom from its name or CI-V address. Select the exact model if the radio uses a custom name and address. Press **Esc** to leave setup.
   - Icom uses control port **50001** by default. Keep its normal negotiated audio ports.
   - TS-890S uses **60000** for TCP control and **60001** for UDP audio. Selecting TS-890S switches the default control port. Set **Login** to **User** or **Admin** to match the account configured on the radio. Its 16 kHz LPCM audio is converted to the app's 48 kHz speaker stream.
3. Press **Enter** to start or stop listening. **Z** and **C** tune down and up in 1 kHz steps. Hold either key to scroll. Press **B** for band presets, select with **Z** or **C**, then press **Enter** to tune to the displayed frequency. The menu shows presets within the connected model’s frequency ranges. Band presets select LSB on 160/80/40 m and USB on the remaining presets.
4. Use **Fn+S** to lower the volume and **Fn+D** to raise it. Hold Fn while pressing S or D repeatedly. **Fn+A** toggles mute. The volume appears briefly as you adjust it, and volume and mute are saved for the next launch.
5. **N** toggles the radio’s noise reduction where supported. **T** starts its antenna tuner, which briefly transmits a tuning carrier. T is inactive on receiver-only radios and models without a tuner command. The top line shows the current mode, NR when enabled, and TUNING during a tuner operation. Press **Esc** to return to the launcher.

The waterfall uses the radio’s scope span and center or fixed-range settings. Icom radios negotiate secondary UDP ports. Kenwood audio arrives on the configured UDP audio port. Keep the radio and Cardputer on the same LAN. If the radio is busy, disconnect the other LAN client and try again. Settings are saved in `~/.config/icomzero/radio.json` (or under `$XDG_CONFIG_HOME`) with user-only permissions. The password is stored in that file as text and encoded using Icom's LAN protocol on the wire.

Press **Tab** on the receiver screen to open the keyboard shortcut guide. **Tab** or **Esc** closes it.

## Build and test

On Debian or the Cardputer itself:

```sh
git clone https://github.com/CardputerZero/AppBuilder.git ../AppBuilder
sudo apt install python3-pil python3-numpy
make check
make test
make package
sudo apt install ./build/icomzero_0.4.1-1_all.deb
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
