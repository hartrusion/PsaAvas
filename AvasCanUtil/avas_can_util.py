#!/usr/bin/env python3
"""AvasCanUtil tests and configures the FIAMM A004BR AVAS module of the Corsa-e F on the bench.

The module is connected directly to one CANable adapter with 120 Ohm termination, and no other part
of the vehicle is needed. The program simulates the CAN frames of the vehicle, so that the module
plays its sound either along an adjustable speed ramp or as a constant tone at a selectable speed.
"Start Config" ends the simulated CAN frames, because the module accepts SecurityAccess only at
standstill, and opens the UDS extended session with SecurityAccess after 5 s of silence, so that the
volumes and the sound selection of the module can be changed and written to the data identifier 2101.

Preparation and start on Linux, where the CANable is activated as a socketcan interface:

    sudo ./can-up.sh
    ./avas_can_util.py
    sudo ./can-up.sh down

Start on Windows, where the script finds the COM port of the CANable and starts the program:

    powershell -ExecutionPolicy Bypass -File can-up.ps1

The dependencies are python-can, pyserial (only for the slcan backend) and tkinter, which is part of
most Python installations and is the package python3-tk on Debian.

LLM code generated with Claude Opus 5.5
"""

import argparse
import os
import sys
import tkinter as tk

import can

# The worker modules lie beside this file, also if the program is started from another folder.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from avas_can_util_window import AvasCanUtilWindow  # noqa: E402
from can_bus_worker import (BUS_TYPE_SLCAN, BUS_TYPE_SOCKETCAN, check_socketcan_interface_ready,  # noqa: E402
                            open_can_bus)

# Socketcan interface name that can-up.sh creates.
DEFAULT_SOCKETCAN_CHANNEL = "can0"


def main():
    # The unrelated PyPI package "can" provides a module with the same name as python-can, which
    # would otherwise only fail later with an AttributeError at the first use of the bus.
    if not hasattr(can, "Bus"):
        sys.exit("ERROR: The installed Python module 'can' is not python-can. Replace it with:\n"
                 f"  {sys.executable} -m pip uninstall can\n"
                 f"  {sys.executable} -m pip install python-can")
    if sys.platform == "win32":
        default_bus_type = BUS_TYPE_SLCAN
    else:
        default_bus_type = BUS_TYPE_SOCKETCAN
    parser = argparse.ArgumentParser(description="Speed simulation and configuration of the AVAS module on the bench.")
    parser.add_argument("--bus", dest="bus_type", choices=(BUS_TYPE_SOCKETCAN, BUS_TYPE_SLCAN), default=default_bus_type,
                        help="way to reach the CANable (default: %(default)s)")
    parser.add_argument("--channel", help="socketcan interface name or serial port, for example can0 or COM5")
    arguments = parser.parse_args()

    channel = arguments.channel
    if arguments.bus_type == BUS_TYPE_SOCKETCAN:
        if channel is None:
            channel = DEFAULT_SOCKETCAN_CHANNEL
        interface_problem = check_socketcan_interface_ready(channel)
        if interface_problem is not None:
            sys.exit(f"ERROR: {interface_problem}")
    elif channel is None:
        sys.exit("ERROR: The slcan backend needs the serial port. On Windows, start the program with can-up.ps1.")

    try:
        bus = open_can_bus(arguments.bus_type, channel)
    except (can.CanError, OSError, ValueError, ImportError) as error:
        sys.exit(f"ERROR: The CANable on {channel} could not be opened: {error}")
    try:
        root = tk.Tk()
        AvasCanUtilWindow(root, bus)
        root.mainloop()
    finally:
        bus.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
