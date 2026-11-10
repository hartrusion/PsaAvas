"""Simulation of the vehicle around the FIAMM A004BR AVAS module of the Corsa-e F.

The AVAS module plays its sound on the bench only if it receives the functional CAN frames of the
other control units with plausible content. This module holds the layout of these input frames, the
time profile of the speed ramp and the thread-safe state that connects the graphical user interface
with the CAN bus worker, which transmits the frames. The frame layouts are taken from the PSA-RE
description of the HS.HS2 bus, and the module plays its sound when it receives this frame set.

LLM code generated with Claude Opus 5.5
"""

import dataclasses
import enum
import threading

# Constant odometer value in km that is reported in the BSI_ODOMETER_DATA frame.
ODOMETER_TOTAL_KM = 1000


class AvasCanFrameId(enum.IntEnum):
    """CAN identifiers of the frames that the program exchanges with the AVAS module.

    The first seven identifiers are the input frames that the module needs to see on the bus before
    it plays its sound. AVAS_STATUS and AVAS_SUPERVISION are status frames of the module, which show
    whether it has accepted the simulated inputs. The last two identifiers carry the UDS diagnostic
    communication of the configuration session.
    """

    VEHICLE_DYNAMICS_WHEELS = 0x38D
    HYBRID_CONTROL_REQUESTS = 0x5D2
    BSI_CABIN_DATA = 0x432
    VCU_POWERTRAIN_STATE = 0x222
    ENGINE_DYNAMIC_DATA_2 = 0x2B2
    ENGINE_HYBRID_DYNAMIC_DATA = 0x408
    BSI_ODOMETER_DATA = 0x552
    AVAS_STATUS = 0x571
    AVAS_SUPERVISION = 0x5D1
    UDS_REQUEST = 0x5B0
    UDS_RESPONSE = 0x5AF


# Transmit period in seconds of each input frame, which follows the periods of the real control
# units on the HS.HS2 bus.
INPUT_FRAME_PERIODS = {
    AvasCanFrameId.VEHICLE_DYNAMICS_WHEELS: 0.040,
    AvasCanFrameId.HYBRID_CONTROL_REQUESTS: 1.000,
    AvasCanFrameId.BSI_CABIN_DATA: 0.050,
    AvasCanFrameId.VCU_POWERTRAIN_STATE: 0.010,
    AvasCanFrameId.ENGINE_DYNAMIC_DATA_2: 0.020,
    AvasCanFrameId.ENGINE_HYBRID_DYNAMIC_DATA: 0.040,
    AvasCanFrameId.BSI_ODOMETER_DATA: 1.000,
}


class VehicleDirection(enum.IntEnum):
    """Values of the VEHICLE_DIRECTION signal in the VEHICLE_DYNAMICS_WHEELS frame."""

    FORWARD = 1
    REARWARD = 2


class VehicleMovingStatus(enum.IntEnum):
    """Values of the VEHICLE_MOVING_STATUS signal in the VEHICLE_DYNAMICS_WHEELS frame."""

    MOVING = 1
    STOPPED = 2


class SimulationPhase(enum.Enum):
    """Phases of the speed simulation, whose values are shown in the status line of the window."""

    STANDSTILL = "standstill"
    CONSTANT_SPEED = "constant tone"
    HOLD_BEFORE = "ramp hold before"
    RAMP_UP = "ramp up"
    RAMP_DOWN = "ramp down"
    HOLD_AFTER = "ramp hold after"


def set_can_signal(frame, bit_range, value):
    """Writes an unsigned value into a signal of a CAN frame payload.

    The bit range uses the PSA-RE notation "b.s-b.e", in which the bytes are numbered from 1 and the
    bits from 7 (most significant) to 0. The signal runs from byte b bit s to byte b bit e in
    big-endian (Motorola) order, so that "1.7-2.0" is the 16-bit value in the first two bytes with
    the most significant byte first. A single bit is given as "b.s". Values that do not fit into the
    signal are cut to the signal width.

    @param frame bytearray with the payload, which is modified in place
    @param bit_range signal position in PSA-RE notation
    @param value unsigned raw signal value
    """
    if "-" in bit_range:
        start_spec, end_spec = bit_range.split("-")
    else:
        start_spec = bit_range
        end_spec = bit_range
    start_byte, start_bit = (int(part) for part in start_spec.split("."))
    end_byte, end_bit = (int(part) for part in end_spec.split("."))
    # Both positions are converted to a bit index that counts from the most significant bit of the
    # first byte, which is the order of transmission.
    first_bit_index = (start_byte - 1) * 8 + (7 - start_bit)
    last_bit_index = (end_byte - 1) * 8 + (7 - end_bit)
    signal_width = last_bit_index - first_bit_index + 1
    value &= (1 << signal_width) - 1
    for i in range(signal_width):
        bit_index = first_bit_index + i
        bit_value = (value >> (signal_width - 1 - i)) & 1
        byte_index = bit_index // 8
        bit_mask = 1 << (7 - bit_index % 8)
        if bit_value:
            frame[byte_index] |= bit_mask
        else:
            frame[byte_index] &= ~bit_mask & 0xFF


def calculate_nibble_checksum(frame, checksum_byte_index, checksum_in_high_nibble):
    """Calculates the 4-bit checksum of the frames VEHICLE_DYNAMICS_WHEELS and VCU_POWERTRAIN_STATE.

    The checksum is 0xF minus the sum of all 4-bit nibbles of the payload, cut to four bits, whereas
    the nibble that holds the checksum itself is counted as zero. This variant of the checksum is
    accepted by the AVAS module.

    @param frame payload whose checksum nibble may still hold an old value
    @param checksum_byte_index index (from 0) of the byte that holds the checksum
    @param checksum_in_high_nibble True if the checksum is the high nibble of that byte
    @return checksum value from 0 to 15
    """
    nibble_sum = 0
    for byte_index, byte_value in enumerate(frame):
        high_nibble = byte_value >> 4
        low_nibble = byte_value & 0xF
        if byte_index == checksum_byte_index:
            if checksum_in_high_nibble:
                high_nibble = 0
            else:
                low_nibble = 0
        nibble_sum += high_nibble + low_nibble
    return (0xF - nibble_sum) & 0xF


class SpeedRampProfile:
    """Time profile of the simulated vehicle speed during a speed ramp.

    The profile consists of a hold phase at 0 km/h, a linear rise to the maximum speed, a linear
    fall back to 0 km/h and a second hold phase at 0 km/h. The first hold phase gives the AVAS
    module time to accept the simulated inputs, which takes about 1.7 s after the frames have
    started, and the second hold phase shows whether the sound stops at standstill. The class
    VehicleSpeedSimulation asks this class for the speed and the phase while a ramp is active.
    """

    def __init__(self, maximum_speed, hold_before_time, ramp_up_time, ramp_down_time, hold_after_time):
        # Peak speed in km/h that is reached at the end of the rise.
        self.maximum_speed = maximum_speed
        self.ramp_up_time = ramp_up_time
        self.ramp_down_time = ramp_down_time
        # Times in seconds from the ramp start at which the rise starts, the maximum is reached,
        # the fall ends and the whole profile ends.
        self.ramp_up_start = hold_before_time
        self.ramp_top = self.ramp_up_start + ramp_up_time
        self.ramp_down_end = self.ramp_top + ramp_down_time
        self.total_time = self.ramp_down_end + hold_after_time

    def get_phase(self, elapsed_time):
        if elapsed_time < self.ramp_up_start:
            return SimulationPhase.HOLD_BEFORE
        if elapsed_time < self.ramp_top:
            return SimulationPhase.RAMP_UP
        if elapsed_time < self.ramp_down_end:
            return SimulationPhase.RAMP_DOWN
        return SimulationPhase.HOLD_AFTER

    def get_speed(self, elapsed_time):
        """Returns the simulated vehicle speed in km/h at the given time in seconds since the ramp start."""
        phase = self.get_phase(elapsed_time)
        if phase == SimulationPhase.RAMP_UP:
            return self.maximum_speed * (elapsed_time - self.ramp_up_start) / self.ramp_up_time
        if phase == SimulationPhase.RAMP_DOWN:
            return self.maximum_speed * (1.0 - (elapsed_time - self.ramp_top) / self.ramp_down_time)
        return 0.0


@dataclasses.dataclass(frozen=True)
class VehicleSimulationState:
    """Snapshot of the speed simulation at one point in time."""

    # True as soon as the input frames shall be transmitted.
    frames_enabled: bool
    # Simulated vehicle speed in km/h.
    speed: float
    direction: VehicleDirection
    phase: SimulationPhase


class VehicleSpeedSimulation:
    """Thread-safe description of what the simulated vehicle currently does.

    The buttons and the slider of the window change this state from the GUI thread, while the CAN
    bus worker reads it at every scheduler pass, which is why all accesses are guarded by a lock.
    The input frames are not transmitted until a ramp or constant tone has been started, and they
    end with stop_input_frames() before a configuration session, because the module refuses
    SecurityAccess with conditionsNotCorrect as long as it receives frames of a switched-on vehicle.
    Between these points, the frames continue at 0 km/h also after the end of a ramp and after a
    stop, so that the module stays ready and the next test starts without the acceptance time of
    about 1.7 s.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.frames_enabled = False
        self.direction = VehicleDirection.FORWARD
        # Active speed ramp, or None if no ramp is running.
        self.ramp_profile = None
        # Monotonic clock time in seconds at which the active ramp has started.
        self.ramp_start_time = 0.0
        # Speed in km/h of the active constant tone, or None if no constant tone is running.
        self.constant_speed = None

    def start_ramp(self, profile, direction, start_time):
        """Replaces the current activity by a speed ramp that starts at the given monotonic clock time."""
        with self.lock:
            self.frames_enabled = True
            self.direction = direction
            self.ramp_profile = profile
            self.ramp_start_time = start_time
            self.constant_speed = None

    def start_constant_speed(self, speed, direction):
        """Replaces the current activity by a constant tone at the given speed in km/h."""
        with self.lock:
            self.frames_enabled = True
            self.direction = direction
            self.ramp_profile = None
            self.constant_speed = speed

    def set_constant_speed(self, speed):
        """Changes the speed of a running constant tone and does nothing if none is running.

        Called from the slider, which may also be moved while no constant tone is active.
        """
        with self.lock:
            if self.constant_speed is not None:
                self.constant_speed = speed

    def stop(self):
        """Returns the simulated speed to 0 km/h at once, whereas the input frames continue."""
        with self.lock:
            self.ramp_profile = None
            self.constant_speed = None

    def stop_input_frames(self):
        """Ends the current activity and the transmission of the input frames, so that the bus becomes silent."""
        with self.lock:
            self.frames_enabled = False
            self.ramp_profile = None
            self.constant_speed = None

    def get_state(self, now):
        """Returns the VehicleSimulationState at the given monotonic clock time and ends a finished ramp."""
        with self.lock:
            if self.ramp_profile is not None:
                elapsed_time = now - self.ramp_start_time
                if elapsed_time < self.ramp_profile.total_time:
                    return VehicleSimulationState(self.frames_enabled, self.ramp_profile.get_speed(elapsed_time),
                                                  self.direction, self.ramp_profile.get_phase(elapsed_time))
                self.ramp_profile = None
            if self.constant_speed is not None:
                return VehicleSimulationState(self.frames_enabled, self.constant_speed, self.direction,
                                              SimulationPhase.CONSTANT_SPEED)
            return VehicleSimulationState(self.frames_enabled, 0.0, self.direction, SimulationPhase.STANDSTILL)


class AvasInputFrameBuilder:
    """Builds the payloads of the input frames that simulate the vehicle around the AVAS module.

    The AVAS module plays its sound only if it receives the frames of the brake control unit, the
    body control unit (BSI) and the powertrain controller with plausible content. This class creates
    these payloads and keeps the state that has to change from frame to frame, which are the 4-bit
    process counters, the odometer that integrates the simulated speed and the vehicle time counter.
    The signal names in the comments are the PSA-RE signal names. The CAN bus worker calls
    update_vehicle_state() at every scheduler pass and build_frame() when a frame is due.
    """

    def __init__(self):
        # Simulated driving direction, which also sets the reverse bit in HYBRID_CONTROL_REQUESTS.
        self.direction = VehicleDirection.FORWARD
        # Current simulated vehicle speed in km/h.
        self.vehicle_speed = 0.0
        # Odometer of the wheel sensors in units of 0.1 m, which integrates the speed.
        self.wheel_odometer = 0.0
        # Vehicle time counter in units of 0.1 s.
        self.vehicle_time_counter = 0.0
        # 4-bit process counters, which are incremented with each sent frame.
        self.process_counters = {
            AvasCanFrameId.VEHICLE_DYNAMICS_WHEELS: 0,
            AvasCanFrameId.BSI_CABIN_DATA: 0,
            AvasCanFrameId.VCU_POWERTRAIN_STATE: 0,
        }

    def update_vehicle_state(self, vehicle_speed, direction, time_step):
        """Sets the speed in km/h and the direction and advances the odometer and the time counter by the time step in seconds."""
        self.vehicle_speed = vehicle_speed
        self.direction = direction
        self.wheel_odometer += vehicle_speed / 3.6 * time_step * 10.0
        self.vehicle_time_counter += time_step * 10.0

    def build_frame(self, frame_id):
        """Builds the payload of the given input frame and advances its process counter.

        @param frame_id one of the input frame identifiers of AvasCanFrameId
        @return payload as bytes
        """
        if frame_id == AvasCanFrameId.VEHICLE_DYNAMICS_WHEELS:
            payload = self.build_vehicle_dynamics_wheels()
        elif frame_id == AvasCanFrameId.HYBRID_CONTROL_REQUESTS:
            payload = self.build_hybrid_control_requests()
        elif frame_id == AvasCanFrameId.BSI_CABIN_DATA:
            payload = self.build_bsi_cabin_data()
        elif frame_id == AvasCanFrameId.VCU_POWERTRAIN_STATE:
            payload = self.build_vcu_powertrain_state()
        elif frame_id == AvasCanFrameId.ENGINE_DYNAMIC_DATA_2:
            payload = self.build_engine_dynamic_data_2()
        elif frame_id == AvasCanFrameId.ENGINE_HYBRID_DYNAMIC_DATA:
            payload = self.build_engine_hybrid_dynamic_data()
        else:
            payload = self.build_bsi_odometer_data()
        if frame_id in self.process_counters:
            self.process_counters[frame_id] = (self.process_counters[frame_id] + 1) & 0xF
        return payload

    def build_vehicle_dynamics_wheels(self):
        """Builds frame 0x38D, which carries the vehicle speed that the AVAS sound follows."""
        if self.vehicle_speed > 0.0:
            moving_status = VehicleMovingStatus.MOVING
        else:
            moving_status = VehicleMovingStatus.STOPPED
        frame = bytearray(8)
        # VEHICLE_SPEED_WHEEL_SENSORS in units of 0.01 km/h.
        set_can_signal(frame, "1.7-2.0", int(round(self.vehicle_speed / 0.01)))
        # ODOMETER_WHEEL_SENSOR in units of 0.1 m, which wraps at 16 bits.
        set_can_signal(frame, "3.7-4.0", int(self.wheel_odometer) & 0xFFFF)
        # LONGITUDINAL_ACCELERATION, whose raw value 0xAF means 0 m/s^2.
        set_can_signal(frame, "5.7-5.0", 0xAF)
        set_can_signal(frame, "6.7-6.4", self.process_counters[AvasCanFrameId.VEHICLE_DYNAMICS_WHEELS])
        # HAZARD_LIGHTS_REQUEST off.
        set_can_signal(frame, "7.7", 0)
        # HILL_ASSIST_STOP_AUTHORIZATION at its default value 1.
        set_can_signal(frame, "7.6", 1)
        set_can_signal(frame, "7.5-7.4", self.direction)
        # VEHICLE_SPEED_UNAVAILABLE_FLAG, whose value 1 means that the speed is available.
        set_can_signal(frame, "7.3-7.2", 1)
        set_can_signal(frame, "7.1-7.0", moving_status)
        # HADC fields, which are not used by the AVAS module.
        set_can_signal(frame, "8.7-8.0", 0)
        set_can_signal(frame, "6.3-6.0", calculate_nibble_checksum(frame, 5, False))
        return bytes(frame)

    def build_hybrid_control_requests(self):
        """Builds frame 0x5D2, which switches the AVAS sound on and signals reverse driving."""
        if self.direction == VehicleDirection.REARWARD:
            reverse_status = 1
        else:
            reverse_status = 0
        frame = bytearray(2)
        # SILENT_VEHICLE_SOUND_COMMAND, whose value 1 requests the sound.
        set_can_signal(frame, "2.7", 1)
        set_can_signal(frame, "2.6", reverse_status)
        return bytes(frame)

    def build_bsi_cabin_data(self):
        """Builds frame 0x432, which reports the main wake-up and ignition on from the BSI."""
        frame = bytearray(8)
        # MAIN_WAKEUP_STATUS, whose value 2 is the main wake-up request.
        set_can_signal(frame, "1.7-1.6", 2)
        # ENGINE_STOP_REQUEST at its default value.
        set_can_signal(frame, "1.1-1.0", 1)
        # ELECTRICAL_SYSTEM_START_INTENT, whose value 1 means that no start is wished.
        set_can_signal(frame, "2.5-2.4", 1)
        # BSI_CALCULATION_PROCESS_COUNTER.
        set_can_signal(frame, "2.3-2.0", self.process_counters[AvasCanFrameId.BSI_CABIN_DATA])
        # PULSE_START_REQUEST, whose value 1 means that no request is present.
        set_can_signal(frame, "3.7-3.6", 1)
        # ADDED_FUEL_VOLUME at its default value.
        set_can_signal(frame, "4.7-4.0", 0xFE)
        # MAIN_STATUS, whose value 1 means ignition on.
        set_can_signal(frame, "7.1-7.0", 1)
        # ELECTRICAL_NETWORK_STATE nominal.
        set_can_signal(frame, "8.7-8.4", 0)
        # ENGINE_START_INTENT_GENERAL, whose value 2 means that no start is intended.
        set_can_signal(frame, "8.1-8.0", 2)
        return bytes(frame)

    def build_vcu_powertrain_state(self):
        """Builds frame 0x222, which reports an active electric powertrain."""
        frame = bytearray(5)
        set_can_signal(frame, "1.3-1.0", self.process_counters[AvasCanFrameId.VCU_POWERTRAIN_STATE])
        # REAR_EMOTOR_SPEED at its default value.
        set_can_signal(frame, "2.7-3.4", 0x640)
        # HYBRID_POWERTRAIN_OPERATING_STATE, whose value 8 means active.
        set_can_signal(frame, "3.3-3.0", 0x8)
        # DRIVER_TORQUE_DEMAND, whose raw value 0x7D means 0 Nm.
        set_can_signal(frame, "4.7-5.7", 0x7D)
        # POWERTRAIN_START_AUTHORIZATION, whose value 1 means authorised.
        set_can_signal(frame, "5.4-5.3", 1)
        set_can_signal(frame, "1.7-1.4", calculate_nibble_checksum(frame, 0, True))
        return bytes(frame)

    def build_engine_dynamic_data_2(self):
        """Builds frame 0x2B2, which reports a running powertrain."""
        frame = bytearray(1)
        # ENGINE_RUNNING, whose value 3 means running.
        set_can_signal(frame, "1.7-1.4", 3)
        # Cruise control and speed limiter inhibited, which is the default value.
        set_can_signal(frame, "1.3-1.2", 3)
        return bytes(frame)

    def build_engine_hybrid_dynamic_data(self):
        """Builds frame 0x408, whose signals are all at their default value 0.

        The content of this frame is not evaluated by the AVAS module as far as it is known, but the
        module plays no sound unless the frame is present.
        """
        return bytes(4)

    def build_bsi_odometer_data(self):
        """Builds frame 0x552, which carries the vehicle time counter and the odometer of the BSI."""
        frame = bytearray(8)
        # VEHICLE_TIME_COUNTER in units of 0.1 s.
        set_can_signal(frame, "1.7-4.0", int(self.vehicle_time_counter) & 0xFFFFFFFF)
        # ODOMETER in km.
        set_can_signal(frame, "5.7-7.0", ODOMETER_TOTAL_KM & 0xFFFFFF)
        set_can_signal(frame, "8.7-8.0", 0xFE)
        return bytes(frame)


class AvasStatusMonitor:
    """Watches the status frames AVAS_STATUS (0x571) and AVAS_SUPERVISION (0x5D1) of the AVAS module.

    Both frames show whether the module has accepted the simulated inputs. After the input frames
    have started, the module clears its "ECU confirmed absent" flags in AVAS_SUPERVISION and its
    diagnostic fault bit in AVAS_STATUS after about 1.7 s, while the AVAS_AVAILABLE_STATUS bit has
    not been seen set even while the sound plays. The monitor keeps the last payload of each frame
    so that the CAN bus worker reports only changes to the log of the window.
    """

    def __init__(self):
        # Last received payload per frame identifier, which is empty until the first frame of that
        # identifier was received.
        self.last_payloads = {}

    def get_last_avas_status_text(self):
        """Returns the last AVAS_STATUS payload as hex text, or "--" if none was received yet."""
        payload = self.last_payloads.get(AvasCanFrameId.AVAS_STATUS)
        if payload is None:
            return "--"
        return payload.hex().upper()

    def process_message(self, frame_id, payload):
        """Stores a received status frame and returns a description if its payload has changed.

        @param frame_id AVAS_STATUS or AVAS_SUPERVISION
        @param payload received payload as bytes
        @return description text of the new payload, or None if the payload is unchanged
        """
        if self.last_payloads.get(frame_id) == payload:
            return None
        if frame_id in self.last_payloads:
            change_text = "changed"
        else:
            change_text = "first seen"
        self.last_payloads[frame_id] = payload
        if frame_id == AvasCanFrameId.AVAS_STATUS:
            return f"0x571 {change_text}: {self.decode_avas_status(payload)}"
        return f"0x5D1 {change_text}: {self.decode_avas_supervision(payload)}"

    def decode_avas_status(self, payload):
        """Decodes the bits AVAS_INHIBITED_STATUS, AVAS_DIAGNOSTIC_STATUS and AVAS_AVAILABLE_STATUS of byte 1."""
        if payload:
            status_byte = payload[0]
        else:
            status_byte = 0
        inhibited = status_byte >> 7 & 1
        fault = status_byte >> 6 & 1
        available = status_byte >> 5 & 1
        return f"inhibited={inhibited} fault={fault} available={available} (raw {status_byte:02X})"

    def decode_avas_supervision(self, payload):
        """Decodes the diagnostic memory state, the ECU confirmed absent flags and the electrical state."""
        # Short payloads are padded with zeros, so that all fields can be decoded.
        padded_payload = payload + bytes(8)
        absent_flags = int.from_bytes(padded_payload[1:5], "big")
        electrical_state = (padded_payload[5] >> 5) & 7
        return (f"fault_memory={padded_payload[0]:02X} absent_flags={absent_flags:08X} "
                f"electrical_state={electrical_state}")
