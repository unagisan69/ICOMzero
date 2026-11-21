#!/usr/bin/env python3
"""Extract native-network radio capabilities from a wfview source checkout."""
import configparser
import json
from pathlib import Path
import subprocess
import sys

source = Path(sys.argv[1])
profiles = []
for path in sorted((source / "rigs").glob("*.rig")):
    config = configparser.ConfigParser(interpolation=None, strict=False)
    config.read(path)
    rig = config["Rig"]
    if not rig.getboolean("HasLAN", fallback=False):
        continue
    commands = {}
    for key, value in rig.items():
        if key.endswith("\\type"):
            commands[value] = rig.get(key[:-4] + "string", "").replace("\\\\x", "\\x")
    bands = []
    for index in range(1, rig.getint("Bands\\size", fallback=0) + 1):
        prefix = f"Bands\\{index}\\"
        start, end = int(rig[prefix + "start"]), int(rig[prefix + "end"])
        if not any(b[1:] == [start, end] for b in bands):
            bands.append([rig[prefix + "name"].strip(), start, end])
    profiles.append({
        "model": rig["Model"],
        "manufacturer": int(rig["Manufacturer"]),
        "civ": rig.getint("CIVAddress"),
        "scope_bins": rig.getint("SpectrumLenMax", fallback=0),
        "scope_parts": rig.getint("SpectrumSeqMax", fallback=0),
        "scope_max": rig.getint("SpectrumAmpMax", fallback=160),
        "nr": bool(commands.get("Noise Reduction")),
        "tuner": bool(commands.get("Tuner/ATU Status")),
        "transmit": rig.getboolean("HasTransmit", fallback=False),
        "bands": bands,
    })
result = {
    "source": "https://github.com/wf-group/wfview",
    "commit": subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip(),
    "radios": profiles,
}
target = Path(__file__).resolve().parents[1] / "app/radios.json"
target.write_text(json.dumps(result, indent=2) + "\n")
print(f"Wrote {len(profiles)} direct-network radio profiles")
