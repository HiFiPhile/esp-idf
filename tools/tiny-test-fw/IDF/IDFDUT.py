# Copyright 2015-2017 Espressif Systems (Shanghai) PTE LTD
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http:#www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

""" DUT for IDF applications """
import os
import os.path
import sys
import re
import subprocess
import functools
import random
import tempfile
import time

from serial.tools import list_ports

from collections import namedtuple

import DUT

try:
    import esptool
except ImportError:  # cheat and use IDF's copy of esptool if available
    idf_path = os.getenv("IDF_PATH")
    if not idf_path or not os.path.exists(idf_path):
        raise
    sys.path.insert(0, os.path.join(idf_path, "components", "esptool_py", "esptool"))
    import esptool


class IDFToolError(OSError):
    pass


def _uses_esptool(func):
    """ Suspend listener thread, connect with esptool,
    call target function with esptool instance,
    then resume listening for output
    """
    @functools.wraps(func)
    def handler(self, *args, **kwargs):
        self.stop_receive()

        settings = self.port_inst.get_settings()

        rom = esptool.ESP32ROM(self.port_inst)
        rom.connect('hard_reset')
        esp = rom.run_stub()

        ret = func(self, esp, *args, **kwargs)

        self.port_inst.apply_settings(settings)
        self.start_receive()
        return ret
    return handler


class IDFDUT(DUT.SerialDUT):
    """ IDF DUT, extends serial with esptool methods

    (Becomes aware of IDFApp instance which holds app-specific data)
    """

    # /dev/ttyAMA0 port is listed in Raspberry Pi
    # /dev/tty.Bluetooth-Incoming-Port port is listed in Mac
    INVALID_PORT_PATTERN = re.compile(r"AMA|Bluetooth")
    # if need to erase NVS partition in start app
    ERASE_NVS = True

    def __init__(self, name, port, log_file, app, **kwargs):
        super(IDFDUT, self).__init__(name, port, log_file, app, **kwargs)

    @classmethod
    def get_mac(cls, app, port):
        """
        get MAC address via esptool

        :param app: application instance (to get tool)
        :param port: serial port as string
        :return: MAC address or None
        """
        try:
            esp = esptool.ESP32ROM(port)
            esp.connect()
            return esp.read_mac()
        except RuntimeError as e:
            return None
        finally:
            esp._port.close()

    @classmethod
    def confirm_dut(cls, port, app, **kwargs):
        return cls.get_mac(app, port) is not None

    @_uses_esptool
    def start_app(self, esp, erase_nvs=ERASE_NVS):
        """
        download and start app.

        :param: erase_nvs: whether erase NVS partition during flash
        :return: None
        """
        flash_files = [ (offs, open(path, "rb")) for (offs, path) in self.app.flash_files ]

        if erase_nvs:
            address = self.app.partition_table["nvs"]["offset"]
            size = self.app.partition_table["nvs"]["size"]
            nvs_file = tempfile.TemporaryFile()
            nvs_file.write(b'\xff' * size)
            nvs_file.seek(0)
            flash_files.append( (int(address, 0), nvs_file) )

        # fake flasher args object, this is a hack until
        # esptool Python API is improved
        Flash_Args = namedtuple('write_flash_args',
                                ['flash_size',
                                 'flash_mode',
                                 'flash_freq',
                                 'addr_filename',
                                 'no_stub',
                                 'compress',
                                 'verify',
                                 'encrypt'])

        flash_args = Flash_Args(
            self.app.flash_settings["flash_size"],
            self.app.flash_settings["flash_mode"],
            self.app.flash_settings["flash_freq"],
            flash_files,
            False,
            True,
            False,
            False
        )

        try:
            for baud_rate in [ 921600, 115200 ]:
                try:
                    esp.change_baud(baud_rate)
                    esptool.write_flash(esp, flash_args)
                    break
                except RuntimeError:
                    continue
            else:
                raise IDFToolError()
        finally:
            for (_,f) in flash_files:
                f.close()

    @_uses_esptool
    def reset(self, esp):
        """
        hard reset DUT

        :return: None
        """
        esp.hard_reset()

    @_uses_esptool
    def erase_partition(self, esp, partition):
        """
        :param partition: partition name to erase
        :return: None
        """
        raise NotImplementedError()  # TODO: implement this
        address = self.app.partition_table[partition]["offset"]
        size = self.app.partition_table[partition]["size"]
        # TODO can use esp.erase_region() instead of this, I think
        with open(".erase_partition.tmp", "wb") as f:
            f.write(chr(0xFF) * size)

    @_uses_esptool
    def dump_flush(self, esp, output_file, **kwargs):
        """
        dump flush

        :param output_file: output file name, if relative path, will use sdk path as base path.
        :keyword partition: partition name, dump the partition.
                            ``partition`` is preferred than using ``address`` and ``size``.
        :keyword address: dump from address (need to be used with size)
        :keyword size: dump size (need to be used with address)
        :return: None
        """
        if os.path.isabs(output_file) is False:
            output_file = os.path.relpath(output_file, self.app.get_log_folder())
        if "partition" in kwargs:
            partition = self.app.partition_table[kwargs["partition"]]
            _address = partition["offset"]
            _size = partition["size"]
        elif "address" in kwargs and "size" in kwargs:
            _address = kwargs["address"]
            _size = kwargs["size"]
        else:
            raise IDFToolError("You must specify 'partition' or ('address' and 'size') to dump flash")

        content = esp.read_flash(_address, _size)
        with open(output_file, "wb") as f:
            f.write(content)

    @classmethod
    def list_available_ports(cls):
        ports = [x.device for x in list_ports.comports()]
        espport = os.getenv('ESPPORT')
        if not espport:
            # It's a little hard filter out invalid port with `serial.tools.list_ports.grep()`:
            # The check condition in `grep` is: `if r.search(port) or r.search(desc) or r.search(hwid)`.
            # This means we need to make all 3 conditions fail, to filter out the port.
            # So some part of the filters will not be straight forward to users.
            # And negative regular expression (`^((?!aa|bb|cc).)*$`) is not easy to understand.
            # Filter out invalid port by our own will be much simpler.
            return [x for x in ports if not cls.INVALID_PORT_PATTERN.search(x)]

        # On MacOs with python3.6: type of espport is already utf8
        if type(espport) is type(u''):
            port_hint = espport
        else:
            port_hint = espport.decode('utf8')

        # If $ESPPORT is a valid port, make it appear first in the list
        if port_hint in ports:
            ports.remove(port_hint)
            return [port_hint] + ports

        # On macOS, user may set ESPPORT to /dev/tty.xxx while
        # pySerial lists only the corresponding /dev/cu.xxx port
        if sys.platform == 'darwin' and 'tty.' in port_hint:
            port_hint = port_hint.replace('tty.', 'cu.')
            if port_hint in ports:
                ports.remove(port_hint)
                return [port_hint] + ports

        return ports
