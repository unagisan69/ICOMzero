"""Private named radio connections and app-wide preferences."""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import uuid

FIELDS = (
    "Host",
    "Control port",
    "Username",
    "Password",
    "Radio",
    "Audio port",
    "Login",
)
DEFAULTS = ("", "50001", "", "", "Auto (Icom)", "60001", "User")
STEPS = (1000, 500, 100)


def config_path():
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "icomzero/radio.json"
    )


class ProfileStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else config_path()

    def read(self):
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as error:
            raise ValueError("Could not read radio settings") from error
        if not isinstance(data, dict):
            raise ValueError("Invalid radio settings")
        if "radios" not in data:
            # Migrate in memory first. The original file is replaced only on save.
            connection = {k: str(data.pop(k, v)) for k, v in zip(FIELDS, DEFAULTS)}
            data["radios"] = [
                {
                    "id": "legacy",
                    "name": "Radio 1",
                    "connection": connection,
                    "step": 1000,
                }
            ]
            data["active_radio"] = "legacy"
        radios = data["radios"]
        if not isinstance(radios, list) or not radios or len(radios) > 32:
            raise ValueError("Invalid radio profiles")
        ids = set()
        for radio in radios:
            if not isinstance(radio, dict) or not radio.get("id") or radio["id"] in ids:
                raise ValueError("Invalid radio profile")
            ids.add(radio["id"])
            connection = radio.get("connection")
            if not isinstance(connection, dict):
                raise ValueError("Invalid radio connection")
            radio["connection"] = {
                k: str(connection.get(k, v)) for k, v in zip(FIELDS, DEFAULTS)
            }
            radio["name"] = self.name(radio.get("name", "Radio"))
            if radio.get("step") not in STEPS:
                radio["step"] = 1000
        if data.get("active_radio") not in ids:
            data["active_radio"] = radios[0]["id"]
        data["schema"] = 2
        return data

    @staticmethod
    def name(value):
        value = str(value).strip()
        if not value or len(value) > 32 or any(ord(c) < 32 for c in value):
            raise ValueError("Use a radio name of 1–32 characters")
        return value

    def write(self, data):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=self.path.parent, delete=False
        ) as temp:
            try:
                json.dump(data, temp)
                temp.flush()
                os.fsync(temp.fileno())
                os.replace(temp.name, self.path)
            finally:
                Path(temp.name).unlink(missing_ok=True)

    def active(self, data=None):
        data = self.read() if data is None else data
        return next(r for r in data["radios"] if r["id"] == data["active_radio"])

    def config(self):
        data = self.read()
        active = self.active(data)
        return {
            **data,
            **active["connection"],
            "step": active["step"],
            "radio_name": active["name"],
        }

    def save_connection(self, values, volume=None, muted=None):
        data = self.read()
        self.active(data)["connection"].update(zip(FIELDS, values))
        if volume is not None:
            data["volume"] = volume
        if muted is not None:
            data["muted"] = muted
        self.write(data)

    def update(self, profile_id, *, name=None, connection=None, step=None):
        data = self.read()
        radio = next(r for r in data["radios"] if r["id"] == profile_id)
        if name is not None:
            name = self.name(name)
            if any(
                r["id"] != profile_id and r["name"].casefold() == name.casefold()
                for r in data["radios"]
            ):
                raise ValueError("That radio name is already used")
            radio["name"] = name
        if connection is not None:
            radio["connection"].update(
                {k: str(v) for k, v in connection.items() if k in FIELDS}
            )
        if step is not None:
            if step not in STEPS:
                raise ValueError("Choose 1000, 500 or 100 Hz")
            radio["step"] = step
        self.write(data)

    def add(self, source=None):
        data = self.read()
        if len(data["radios"]) >= 32:
            raise ValueError("Maximum 32 radios")
        radio = (
            deepcopy(next(r for r in data["radios"] if r["id"] == source))
            if source
            else {
                "connection": dict(zip(FIELDS, DEFAULTS)),
                "step": 1000,
            }
        )
        radio["id"] = uuid.uuid4().hex
        base = (radio["name"][:22] + " copy") if source else "Radio"
        name = base
        index = 2
        while any(r["name"].casefold() == name.casefold() for r in data["radios"]):
            name = f"{base} {index}"
            index += 1
        radio["name"] = name
        data["radios"].append(radio)
        self.write(data)
        return radio["id"]

    def select(self, profile_id):
        data = self.read()
        if not any(r["id"] == profile_id for r in data["radios"]):
            raise ValueError("Radio profile no longer exists")
        data["active_radio"] = profile_id
        self.write(data)

    def delete(self, profile_id):
        data = self.read()
        if len(data["radios"]) == 1:
            raise ValueError("Keep at least one radio profile")
        data["radios"] = [r for r in data["radios"] if r["id"] != profile_id]
        if data["active_radio"] == profile_id:
            data["active_radio"] = data["radios"][0]["id"]
        self.write(data)

    def preference(self, key, value):
        if key not in ("check_updates",):
            raise ValueError("Unknown app preference")
        data = self.read()
        data[key] = bool(value)
        self.write(data)
