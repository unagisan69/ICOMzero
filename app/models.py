"""Capabilities of wfview radios with native LAN/Wi-Fi interfaces."""
import json
from pathlib import Path

CATALOG = json.loads(Path(__file__).with_name("radios.json").read_text())
PROFILES = {radio["model"]: radio for radio in CATALOG["radios"]}
RADIO_CHOICES = ("Auto (Icom)", *PROFILES)
DEFAULT_MODEL = "IC-7300MK2"
DEFAULT_PROFILE = PROFILES[DEFAULT_MODEL]
MIN_HZ, MAX_HZ = 1, 10_500_000_000


def normalized(name):
    return "".join(c for c in name.upper() if c.isalnum())


def find_icom(name, address, selected="Auto (Icom)"):
    if selected != "Auto (Icom)":
        profile = PROFILES.get(selected)
        return profile if profile and profile["manufacturer"] == 0 else None
    aliases = {"IC7850": "IC-785x", "IC7851": "IC-785x"}
    model = aliases.get(normalized(name), name)
    for profile in PROFILES.values():
        if profile["manufacturer"] == 0 and normalized(profile["model"]) == normalized(model):
            return profile
    candidates = [p for p in PROFILES.values() if p["manufacturer"] == 0 and p["civ"] == address]
    return candidates[0] if len(candidates) == 1 else None


def presets(profile, cw=False):
    customary = (
        ("160m", 1_900_000), ("80m", 3_750_000), ("60m", 5_357_000),
        ("40m", 7_150_000), ("30m", 10_125_000), ("20m", 14_200_000),
        ("17m", 18_130_000), ("15m", 21_250_000), ("12m", 24_950_000),
        ("10m", 28_400_000), ("6m", 50_125_000), ("4m", 70_200_000),
        ("2m", 144_200_000), ("70cm", 432_200_000), ("23cm", 1_296_200_000),
        ("13cm", 2_400_200_000), ("6cm", 5_760_200_000), ("3cm", 10_368_200_000),
    )
    if cw:
        cw_hz = {"160m":1825000,"80m":3525000,"60m":5357000,"40m":7025000,
                 "30m":10110000,"20m":14025000,"17m":18080000,"15m":21025000,
                 "12m":24895000,"10m":28025000,"6m":50090000,"4m":70090000,
                 "2m":144050000,"70cm":432050000}
        customary = tuple((name,cw_hz.get(name,hz)) for name,hz in customary)
    ranges = profile["bands"]
    return tuple((name, hz) for name, hz in customary
                 if any(start <= hz <= end for _, start, end in ranges)) or customary[:11]
