"""Access to the CANable adapter and the thread that does all CAN communication of AvasCanUtil.

LLM code generated with Claude Opus 5.5
"""

import json
import os
import queue
import subprocess
import threading
import time

import can

from avas_vehicle_simulation import (AvasCanFrameId, AvasInputFrameBuilder, AvasStatusMonitor,
                                     INPUT_FRAME_PERIODS, SimulationPhase, VehicleDirection,
                                     VehicleSimulationState)

# Bitrate in bit/s of the CAN bus of the AVAS module.
AVAS_CAN_BITRATE = 500000

# python-can interface names of the two supported ways to reach the CANable.
BUS_TYPE_SOCKETCAN = "socketcan"
BUS_TYPE_SLCAN = "slcan"

# Time in seconds that the worker sleeps between two scheduler passes, which limits the CPU load
# while still meeting the shortest frame period of 10 ms.
SCHEDULER_SLEEP_TIME = 0.001

# Bit of the file /sys/class/net/<interface>/flags that is set while the interface is up.
NETWORK_INTERFACE_FLAG_UP = 0x1


def check_socketcan_interface_ready(interface_name):
    """Returns a description of the problem if the socketcan interface cannot transmit, or None if it can.

    The check is done before the bus is opened, because python-can would otherwise fail with a less
    clear error message or would not be able to transmit.
    """
    bring_up_hint = "Bring up the CANable first: sudo ./can-up.sh"
    interface_directory = f"/sys/class/net/{interface_name}"
    if not os.path.isdir(interface_directory):
        return f"The CAN interface '{interface_name}' does not exist.\n{bring_up_hint}"
    try:
        with open(f"{interface_directory}/flags") as flags_file:
            interface_flags = int(flags_file.read().strip(), 16)
    except OSError:
        interface_flags = 0
    if not interface_flags & NETWORK_INTERFACE_FLAG_UP:
        return f"The CAN interface '{interface_name}' exists but is down.\n{bring_up_hint}"
    try:
        ip_result = subprocess.run(["ip", "-d", "-j", "link", "show", "dev", interface_name],
                                   capture_output=True, text=True, timeout=1)
        link_info = json.loads(ip_result.stdout)[0].get("linkinfo", {}).get("info_data", {})
        control_modes = link_info.get("ctrlmode") or []
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        # The listen-only check is optional, because older ip versions do not support JSON output.
        return None
    if "LISTEN-ONLY" in control_modes:
        return (f"The CAN interface '{interface_name}' is in listen-only mode and cannot transmit.\n"
                f"Restart it in normal mode: sudo ./can-up.sh down && sudo ./can-up.sh")
    return None


def open_can_bus(bus_type, channel):
    """Opens the CANable with python-can and returns the bus object.

    On Linux, the script can-up.sh has already created the socketcan interface, which only has to be
    opened. On Windows, the program talks the slcan text protocol through the COM port by itself. A
    CANable whose slcan firmware was last used in the listen-only mode stays in the silent mode until
    it is unplugged, so that the normal mode is requested explicitly here. The firmware accepts the
    mode command only while the channel is closed.

    @param bus_type BUS_TYPE_SOCKETCAN or BUS_TYPE_SLCAN
    @param channel socketcan interface name or serial port name
    @return opened can.BusABC object
    """
    if bus_type == BUS_TYPE_SOCKETCAN:
        return can.Bus(interface=bus_type, channel=channel)
    bus = can.Bus(interface=bus_type, channel=channel, bitrate=AVAS_CAN_BITRATE)
    bus.close()
    bus.serialPortOrig.write(b"M0\r")
    bus.open()
    return bus


class CanBusWorker(threading.Thread):
    """Thread that is the only user of the CAN bus object and serves all CAN traffic of the program.

    The slcan backend of python-can cannot be shared between a transmitting and a receiving thread,
    and on Windows a COM port cannot be opened twice, so that one thread has to do everything. At
    every scheduler pass it reads the state of the VehicleSpeedSimulation, transmits the input
    frames of AvasInputFrameBuilder that are due and the frames that the diagnostic communication
    has queued, and receives all waiting frames. Received status frames are passed to the
    AvasStatusMonitor, whereas received UDS frames are put into a queue for the ISO-TP channel of
    the configuration session, which runs in its own thread because its requests block while they
    wait for an answer.
    """

    def __init__(self, bus, simulation, report_log):
        """Creates the worker.

        @param bus opened python-can bus
        @param simulation VehicleSpeedSimulation that defines the content of the input frames
        @param report_log function that takes a text for the log of the window, callable from any thread
        """
        super().__init__(name="CanBusWorker", daemon=True)
        self.bus = bus
        self.simulation = simulation
        self.report_log = report_log
        self.frame_builder = AvasInputFrameBuilder()
        self.status_monitor = AvasStatusMonitor()
        # Payloads of UDS_REQUEST frames that the ISO-TP channel wants to have transmitted.
        self.diagnostic_transmit_queue = queue.Queue()
        # Payloads of received UDS_RESPONSE frames for the ISO-TP channel.
        self.diagnostic_receive_queue = queue.Queue()
        self.stop_event = threading.Event()
        # Latest state of the simulation, which the window reads for its status line.
        self.current_state = VehicleSimulationState(False, 0.0, VehicleDirection.FORWARD, SimulationPhase.STANDSTILL)
        # Monotonic clock time at which each input frame is sent next, in which zero means at once.
        self.next_send_times = {frame_id: 0.0 for frame_id in INPUT_FRAME_PERIODS}
        # True after a transmit error was reported, so that a lasting error is logged only once.
        self.transmit_error_reported = False

    def stop(self):
        self.stop_event.set()

    def run(self):
        last_loop_time = time.monotonic()
        while not self.stop_event.is_set():
            now = time.monotonic()
            state = self.simulation.get_state(now)
            self.current_state = state
            if state.frames_enabled:
                self.frame_builder.update_vehicle_state(state.speed, state.direction, now - last_loop_time)
                self.transmit_due_input_frames(now)
            last_loop_time = now
            self.transmit_queued_diagnostic_frames()
            self.receive_waiting_frames()
            time.sleep(SCHEDULER_SLEEP_TIME)

    def transmit_due_input_frames(self, now):
        """Sends every input frame whose time has come and keeps its period without catching up after a stall."""
        for frame_id, period in INPUT_FRAME_PERIODS.items():
            if now >= self.next_send_times[frame_id]:
                self.transmit_frame(frame_id, self.frame_builder.build_frame(frame_id))
                self.next_send_times[frame_id] += period
                if self.next_send_times[frame_id] < now:
                    self.next_send_times[frame_id] = now + period

    def transmit_queued_diagnostic_frames(self):
        while True:
            try:
                payload = self.diagnostic_transmit_queue.get_nowait()
            except queue.Empty:
                return
            self.transmit_frame(AvasCanFrameId.UDS_REQUEST, payload)

    def transmit_frame(self, frame_id, payload):
        """Transmits one frame, whereas an error is only logged, so that the program survives a bus without the module."""
        message = can.Message(arbitration_id=frame_id, data=payload, is_extended_id=False)
        try:
            self.bus.send(message)
        except can.CanError as error:
            if not self.transmit_error_reported:
                self.report_log(f"CAN transmit error, is the AVAS module connected and powered? ({error})")
                self.transmit_error_reported = True
            return
        self.transmit_error_reported = False

    def receive_waiting_frames(self):
        """Reads all waiting frames without blocking and passes them on according to their identifier."""
        while True:
            try:
                message = self.bus.recv(timeout=0)
            except (can.CanError, ValueError, IndexError) as error:
                # The slcan backend raises ValueError or IndexError on a damaged text line.
                self.report_log(f"CAN receive error: {error}")
                return
            if message is None:
                return
            payload = bytes(message.data)
            if message.arbitration_id == AvasCanFrameId.UDS_RESPONSE:
                self.diagnostic_receive_queue.put(payload)
            elif message.arbitration_id in (AvasCanFrameId.AVAS_STATUS, AvasCanFrameId.AVAS_SUPERVISION):
                change_description = self.status_monitor.process_message(message.arbitration_id, payload)
                if change_description is not None:
                    self.report_log(change_description)
