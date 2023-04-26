from __future__ import annotations

from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Optional
from urllib.parse import ParseResult

from cbc_sdk.errors import CredentialError
from cbc_sdk.live_response_api import LiveResponseSession
from cbc_sdk.platform import Device
from cbc_sdk.rest_api import CBCloudAPI
from dissect.target.exceptions import LoaderError, RegistryError, RegistryKeyNotFoundError, RegistryValueNotFoundError
from dissect.target.filesystems.cb import CbFilesystem
from dissect.target.helpers.regutil import RegistryHive, RegistryKey, RegistryValue
from dissect.target.helpers.utils import parse_path_uri
from dissect.target.loader import Loader
from dissect.target.plugins.os.windows.registry import RegistryPlugin

if TYPE_CHECKING:
    from dissect.target.target import Target


class OS(IntEnum):
    WINDOWS = 1
    LINUX = 2
    MAX = 4


class CbLoader(Loader):
    def __init__(self, path: str, parsed_path: ParseResult = None, **kwargs):
        self.host, _, instance = parsed_path.netloc.partition("@")
        super(CbLoader, self).__init__(path)

        # A profile will need to be given as argument to CBCloudAPI
        # e.g. cb://workstation@instance
        try:
            self.cbc_api = CBCloudAPI(profile=instance)
        except CredentialError:
            raise LoaderError("The Carbon Black Cloud API key was not found or has the wrong set of permissions set")

        self.sensor = self.get_device()
        if not self.sensor:
            raise LoaderError("The device was not found within the specified instance")

        self.session = self.sensor.lr_session()

    def get_device(self) -> Optional[Device]:
        host_is_ip = self.host.count(".") == 3 and all([part.isdigit() for part in self.host.split(".")])

        for cbc_sensor in self.cbc_api.select(Device).all():
            if host_is_ip:
                if cbc_sensor.last_internal_ip_address == self.host:
                    return cbc_sensor
            else:
                try:
                    device_name = cbc_sensor.name.lower()
                except AttributeError:
                    continue

                # Sometimes the domain name is included in the device name
                # E.g. DOMAIN\\Hostname
                if "\\" in device_name:
                    device_name = device_name.split("\\")[1]

                if device_name == self.host.lower():
                    return cbc_sensor

        return None

    @staticmethod
    def detect(path: Path) -> bool:
        path_part, _, _ = parse_path_uri(path)
        return path_part == "cb"

    def map(self, target: Target) -> None:
        alt_separator = "\\" if self.session.os_type == OS.WINDOWS else "/"
        case_sensitive = False if self.session.os_type == OS.WINDOWS else True
        for drive in self.session.session_data["drives"]:
            cbfs = CbFilesystem(
                self.cbc_api,
                self.sensor,
                self.session,
                drive,
                alt_separator=alt_separator,
                case_sensitive=case_sensitive,
            )
            target.filesystems.add(cbfs)
            target.fs.mount(drive.lower(), cbfs)
        target.add_plugin(CbRegistry(target, self.session))


class CbRegistry(RegistryPlugin):
    def __init__(self, target: Target, session: LiveResponseSession):
        self.session = session
        super().__init__(target)

    def _init_registry(self) -> None:
        for hive_name, rootkey in self.MAPPINGS.items():
            try:
                hive = CbRegistryHive(self.session, rootkey)
                self.add_hive(hive_name, hive, None)
                self.map_hive(rootkey, hive)
            except RegistryError:
                continue


class CbRegistryHive(RegistryHive):
    def __init__(self, session: LiveResponseSession, rootkey: str):
        self.session = session
        self.rootkey = rootkey

    def key(self, key: str) -> CbRegistryKey:
        key = "\\".join([self.rootkey, key])
        return CbRegistryKey(self, key)


class CbRegistryKey(RegistryKey):
    def __init__(self, hive: str, key: str, data=None):
        super().__init__(hive)
        self.key = key
        self._data = data

    @property
    def data(self) -> dict:
        if not self._data:
            self._data = self.hive.session.list_registry_keys_and_values(self.key)
        return self._data

    @property
    def name(self) -> str:
        return self.key.split("\\")[-1]

    @property
    def path(self) -> str:
        return self.key

    @property
    def timestamp(self) -> None:
        return None

    def subkey(self, subkey: str) -> CbRegistryKey:
        subkey_val = subkey.lower()

        for val in self.data["sub_keys"]:
            if val.lower() == subkey_val:
                return CbRegistryKey(self.hive, "\\".join([self.key, subkey]), None)
        else:
            raise RegistryKeyNotFoundError(subkey)

    def subkeys(self) -> Iterator[CbRegistryKey]:
        return map(self.subkey, self.data["sub_keys"])

    def value(self, value: str) -> str:
        reg_value = value.lower()
        for val in self.values():
            if val.name.lower() == reg_value:
                return val
        else:
            raise RegistryValueNotFoundError(value)

    def values(self) -> Iterator[CbRegistryValue]:
        return (
            CbRegistryValue(self.hive, val["registry_name"], val["registry_data"], val["registry_type"])
            for val in self.data["values"]
        )


class CbRegistryValue(RegistryValue):
    def __init__(self, hive: str, name: str, data: str, type_: str):
        super().__init__(hive)
        self._name = name
        self._type = type_

        if self._type == "pbREG_BINARY":
            self._value = bytes.fromhex(data)
        elif self._type in ("pbREG_DWORD", "pbREG_QWORD"):
            self._value = int(data)
        elif self._type == "pbREG_MULTI_SZ":
            self._value = data.split(",")
        else:
            self._value = data

    @property
    def name(self) -> str:
        return self._name

    @property
    def value(self) -> str:
        return self._value

    @property
    def type(self) -> str:
        return self._type
