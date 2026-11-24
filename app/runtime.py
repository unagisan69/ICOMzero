"""Small platform boundary shared by playback and release installation."""

import os
from pathlib import Path


def target():
    # Installed desktop marker also survives sudo's environment filtering.
    return (
        "desktop"
        if (
            Path(__file__).with_name("desktop-platform").exists()
            or os.environ.get("ICOMZERO_PLATFORM") == "desktop"
        )
        else "cardputer"
    )


def audio_device():
    return os.environ.get("ICOMZERO_AUDIO_DEVICE", "pulse")


def playback_env():
    env = dict(os.environ)
    if target() == "cardputer":
        env["PULSE_SINK"] = "alsa_output.platform-sound.stereo-fallback"
    return env


def release_asset(version, platform=None):
    prefix = "icomzero-desktop" if (platform or target()) == "desktop" else "icomzero"
    return f"{prefix}_{version}-1_arm64.deb"
