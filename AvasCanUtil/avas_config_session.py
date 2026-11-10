"""UDS configuration session of the AVAS module over a direct CAN connection.

The AVAS module holds its forward volume, its reverse volume and its sound selection in the
telecoding record with the UDS data identifier (DID) 0x2101. Writing this record needs the extended
diagnostic session and SecurityAccess, for which this module implements the ISO-TP transport, the
UDS requests and their sequence directly on the CANable adapter.

LLM code generated with Claude Opus 5.5
"""

import enum
import queue
import threading
import time

from psa_seed_key import PSA_SEED_KEY_LENGTH, compute_psa_key

# Time in seconds that the module may take for a UDS response before the request has failed. The
# module announces a limit of 200 ms in its session response, which is extended here for the USB
# latency of the adapter.
UDS_RESPONSE_TIMEOUT = 2.0

# Time in seconds that the module may take after it has answered with responsePending.
UDS_PENDING_RESPONSE_TIMEOUT = 5.0

# Time in seconds that one ISO-TP partner may take for its next frame within a message.
ISO_TP_FRAME_TIMEOUT = 1.0

# Time in seconds without a request after which a TesterPresent request keeps the session open. A
# session ends by itself after about 5 s of silence, and 3 s is the period of the real testers.
TESTER_PRESENT_PERIOD = 3.0

# Time in seconds that the session thread waits for a command before it checks the TesterPresent period.
COMMAND_POLL_TIME = 0.1

# Time in seconds without input frames before the extended session is opened, so that the module
# has recognised the standstill of the vehicle and accepts SecurityAccess.
BUS_SILENCE_TIME = 5

# Length of a classic CAN frame in bytes, to which the requests are padded with zero bytes, because
# the module accepts this padding.
CAN_FRAME_LENGTH = 8

# Number of payload bytes that fit into an ISO-TP single frame.
ISO_TP_SINGLE_FRAME_CAPACITY = 7

# Number of payload bytes that fit into an ISO-TP first frame.
ISO_TP_FIRST_FRAME_CAPACITY = 6

# Number of payload bytes that fit into an ISO-TP consecutive frame.
ISO_TP_CONSECUTIVE_FRAME_CAPACITY = 7

# Value that a positive UDS response adds to the service identifier of its request.
UDS_POSITIVE_RESPONSE_OFFSET = 0x40

# Service byte that starts a negative UDS response.
UDS_NEGATIVE_RESPONSE = 0x7F

# Sub-function of the seed request of the AVAS module, whose key is sent with the next value.
AVAS_SECURITY_LEVEL = 0x03

# 16-bit constant of the PSA seed/key algorithm for the AVAS module, taken from PSA-RE.
AVAS_SECURITY_CONSTANT = 0xC504

# Data identifier of the telecoding record.
TELECODING_DID = 0x2101

# Gain value of full level (0 dB), which is also the highest volume that the module accepts.
VOLUME_FULL_LEVEL = 0x4000

# Factory settings of the telecoding record, which correspond to -10 dB forward and -8 dB reverse.
DEFAULT_FORWARD_VOLUME_GAIN = 0x143D
DEFAULT_REAR_VOLUME_GAIN = 0x197A
DEFAULT_SOUND = 1


class UdsServiceId(enum.IntEnum):
    """Service identifiers of the UDS requests of the configuration session."""

    DIAGNOSTIC_SESSION_CONTROL = 0x10
    READ_DATA_BY_IDENTIFIER = 0x22
    SECURITY_ACCESS = 0x27
    WRITE_DATA_BY_IDENTIFIER = 0x2E
    TESTER_PRESENT = 0x3E


class UdsSessionType(enum.IntEnum):
    """Sub-functions of DiagnosticSessionControl."""

    DEFAULT = 0x01
    EXTENDED = 0x03


class UdsNegativeResponseCode(enum.IntEnum):
    """Negative response codes that the third byte of a negative UDS response can hold."""

    GENERAL_REJECT = 0x10
    SERVICE_NOT_SUPPORTED = 0x11
    SUB_FUNCTION_NOT_SUPPORTED = 0x12
    INCORRECT_MESSAGE_LENGTH = 0x13
    CONDITIONS_NOT_CORRECT = 0x22
    REQUEST_SEQUENCE_ERROR = 0x24
    REQUEST_OUT_OF_RANGE = 0x31
    SECURITY_ACCESS_DENIED = 0x33
    INVALID_KEY = 0x35
    EXCEEDED_NUMBER_OF_ATTEMPTS = 0x36
    REQUIRED_TIME_DELAY_NOT_EXPIRED = 0x37
    GENERAL_PROGRAMMING_FAILURE = 0x72
    RESPONSE_PENDING = 0x78
    SERVICE_NOT_SUPPORTED_IN_ACTIVE_SESSION = 0x7F


class IsoTpFrameType(enum.IntEnum):
    """Values of the high nibble of the first byte of an ISO-TP frame."""

    SINGLE = 0
    FIRST = 1
    CONSECUTIVE = 2
    FLOW_CONTROL = 3


class IsoTpFlowStatus(enum.IntEnum):
    """Values of the low nibble of the first byte of an ISO-TP flow control frame."""

    CONTINUE = 0
    WAIT = 1
    OVERFLOW = 2


class ConfigSessionState(enum.Enum):
    """States of the configuration session, which decide which buttons the window enables."""

    # No unlocked session exists, so that only "Start Config" is possible.
    NOT_STARTED = enum.auto()
    # A request is running, so that no further action is possible.
    BUSY = enum.auto()
    # The module is unlocked and the telecoding record is known, so that it can be written.
    UNLOCKED = enum.auto()


class AvasDiagnosticError(Exception):
    """A UDS request has failed, whose message text is meant for the log of the window."""


class UdsNegativeResponseError(AvasDiagnosticError):
    """The module has refused a UDS request with a negative response."""


def describe_negative_response_code(code):
    """Returns the name of a negative response code with its value as hex text."""
    try:
        return f"{UdsNegativeResponseCode(code).name} ({code:02X})"
    except ValueError:
        return f"UNKNOWN ({code:02X})"


# Meaning of the known UDS requests for the log of the window as (first bytes of the request, meaning
# of the request, meaning of its positive response).
UDS_MESSAGE_MEANINGS = (
    (bytes([UdsServiceId.DIAGNOSTIC_SESSION_CONTROL, UdsSessionType.DEFAULT]),
     "DiagnosticSessionControl: open default session", "default session opened"),
    (bytes([UdsServiceId.DIAGNOSTIC_SESSION_CONTROL, UdsSessionType.EXTENDED]),
     "DiagnosticSessionControl: open extended session", "extended session opened"),
    (bytes([UdsServiceId.SECURITY_ACCESS, AVAS_SECURITY_LEVEL]),
     "SecurityAccess: request seed", "SecurityAccess: seed received"),
    (bytes([UdsServiceId.SECURITY_ACCESS, AVAS_SECURITY_LEVEL + 1]),
     "SecurityAccess: send key", "SecurityAccess: key accepted, module unlocked"),
    (bytes([UdsServiceId.READ_DATA_BY_IDENTIFIER]) + TELECODING_DID.to_bytes(2, "big"),
     "ReadDataByIdentifier: read telecoding record 2101", "telecoding record 2101 received"),
    (bytes([UdsServiceId.WRITE_DATA_BY_IDENTIFIER]) + TELECODING_DID.to_bytes(2, "big"),
     "WriteDataByIdentifier: write telecoding record 2101", "telecoding record 2101 written"),
    (bytes([UdsServiceId.TESTER_PRESENT, 0x00]), "TesterPresent: keep the session open", "TesterPresent confirmed"),
)


def describe_uds_request(request):
    """Returns the meaning of a UDS request for the log of the window."""
    for request_start, request_meaning, _ in UDS_MESSAGE_MEANINGS:
        if request.startswith(request_start):
            return request_meaning
    return "unknown request"


def describe_uds_response(response):
    """Returns the meaning of a positive or negative UDS response for the log of the window."""
    if len(response) >= 3 and response[0] == UDS_NEGATIVE_RESPONSE:
        if response[2] == UdsNegativeResponseCode.RESPONSE_PENDING:
            return "module is still working, RESPONSE_PENDING (78)"
        try:
            service_name = UdsServiceId(response[1]).name
        except ValueError:
            service_name = f"service {response[1]:02X}"
        return f"refused: {service_name}, {describe_negative_response_code(response[2])}"
    for request_start, _, response_meaning in UDS_MESSAGE_MEANINGS:
        positive_response_start = bytes([request_start[0] + UDS_POSITIVE_RESPONSE_OFFSET]) + request_start[1:]
        if response.startswith(positive_response_start):
            return response_meaning
    return "unknown response"


def decode_separation_time(separation_time_code):
    """Returns the time in seconds that an ISO-TP flow control frame demands between two consecutive frames."""
    if separation_time_code <= 0x7F:
        return separation_time_code / 1000.0
    if 0xF1 <= separation_time_code <= 0xF9:
        return (separation_time_code - 0xF0) / 10000.0
    # All other codes are reserved, for which ISO 15765-2 demands the longest time.
    return 0.127


class AvasTelecodingRecord:
    """The six data bytes of the telecoding record 0x2101 of the AVAS module.

    UDS has no service that writes a single setting, because the module returns and accepts the
    record only as a whole. The configuration session therefore reads the record first, and the
    window creates a changed copy with with_settings(), so that the telecoding index in the first
    byte is written back unchanged. Each volume is a linear gain of 16 bits with the low byte first
    and 14 fraction bits (Q14), so that 0x4000 is full level, which measurements on the module have
    shown in contradiction to the description in PSA-RE. The sound selection in the last byte is
    counted from 1 to 6, whereas the module refuses the value 0.
    """

    # Number of data bytes of the record.
    LENGTH = 6
    # Byte offsets of the settings in the data bytes.
    FORWARD_VOLUME_OFFSET = 1
    REAR_VOLUME_OFFSET = 3
    SOUND_OFFSET = 5

    def __init__(self, data):
        if len(data) != self.LENGTH:
            raise ValueError(f"The telecoding record has {len(data)} bytes instead of {self.LENGTH}.")
        self.data = bytes(data)

    def get_forward_volume_gain(self):
        return int.from_bytes(self.data[self.FORWARD_VOLUME_OFFSET:self.FORWARD_VOLUME_OFFSET + 2], "little")

    def get_rear_volume_gain(self):
        return int.from_bytes(self.data[self.REAR_VOLUME_OFFSET:self.REAR_VOLUME_OFFSET + 2], "little")

    def get_sound(self):
        return self.data[self.SOUND_OFFSET]

    def with_settings(self, forward_volume_gain, rear_volume_gain, sound):
        """Returns a copy of this record with the given Q14 gains and sound and all other bytes unchanged."""
        new_data = bytearray(self.data)
        new_data[self.FORWARD_VOLUME_OFFSET:self.FORWARD_VOLUME_OFFSET + 2] = forward_volume_gain.to_bytes(2, "little")
        new_data[self.REAR_VOLUME_OFFSET:self.REAR_VOLUME_OFFSET + 2] = rear_volume_gain.to_bytes(2, "little")
        new_data[self.SOUND_OFFSET] = sound
        return AvasTelecodingRecord(new_data)

    def format_hex(self):
        return self.data.hex(" ").upper()


class IsoTpChannel:
    """ISO-TP transport of the UDS messages between the program and the AVAS module.

    A UDS message that does not fit into one CAN frame has to be split, which is the case for the
    response of the telecoding record and for its write request. The kernel ISO-TP sockets exist
    only on Linux, so that the program implements the transport by itself for both platforms. The
    frames are exchanged through the two queues of the CanBusWorker. Transmitted frames are padded
    to 8 bytes with zeros, whereas the module sends its own frames without padding.
    """

    def __init__(self, transmit_queue, receive_queue):
        """Creates the channel.

        @param transmit_queue queue that takes the payloads of the frames to the module
        @param receive_queue queue that delivers the payloads of the frames from the module
        """
        self.transmit_queue = transmit_queue
        self.receive_queue = receive_queue

    def clear_receive_queue(self):
        """Removes late frames of an earlier request, which would otherwise be taken as the next response."""
        while True:
            try:
                self.receive_queue.get_nowait()
            except queue.Empty:
                return

    def transmit_padded_frame(self, payload):
        self.transmit_queue.put(payload.ljust(CAN_FRAME_LENGTH, b"\x00"))

    def receive_frame(self, timeout):
        """Returns the next non-empty received frame and raises AvasDiagnosticError after the timeout in seconds."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                frame = self.receive_queue.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                raise AvasDiagnosticError("The AVAS module did not answer in time.") from None
            if frame:
                return frame

    def send_message(self, payload):
        """Transmits one UDS message as a single frame or as a first frame with consecutive frames."""
        if len(payload) <= ISO_TP_SINGLE_FRAME_CAPACITY:
            self.transmit_padded_frame(bytes([IsoTpFrameType.SINGLE << 4 | len(payload)]) + payload)
            return
        self.transmit_padded_frame(bytes([IsoTpFrameType.FIRST << 4 | len(payload) >> 8, len(payload) & 0xFF])
                                   + payload[:ISO_TP_FIRST_FRAME_CAPACITY])
        remaining_payload = payload[ISO_TP_FIRST_FRAME_CAPACITY:]
        sequence_number = 1
        while remaining_payload:
            block_size, separation_time = self.wait_for_flow_control()
            frames_in_block = 0
            while remaining_payload and (block_size == 0 or frames_in_block < block_size):
                self.transmit_padded_frame(bytes([IsoTpFrameType.CONSECUTIVE << 4 | sequence_number])
                                           + remaining_payload[:ISO_TP_CONSECUTIVE_FRAME_CAPACITY])
                remaining_payload = remaining_payload[ISO_TP_CONSECUTIVE_FRAME_CAPACITY:]
                sequence_number = (sequence_number + 1) & 0xF
                frames_in_block += 1
                if remaining_payload:
                    time.sleep(separation_time)

    def wait_for_flow_control(self):
        """Waits for the permission of the module to continue and returns its block size and separation time in seconds."""
        while True:
            frame = self.receive_frame(ISO_TP_FRAME_TIMEOUT)
            if frame[0] >> 4 != IsoTpFrameType.FLOW_CONTROL or len(frame) < 3:
                raise AvasDiagnosticError(f"Flow control frame expected, received {frame.hex(' ').upper()}.")
            flow_status = frame[0] & 0xF
            if flow_status == IsoTpFlowStatus.CONTINUE:
                return frame[1], decode_separation_time(frame[2])
            if flow_status != IsoTpFlowStatus.WAIT:
                raise AvasDiagnosticError(f"The AVAS module has refused the message with flow status {flow_status}.")

    def receive_message(self, timeout):
        """Receives one UDS message and allows the module the given time in seconds for its first frame."""
        frame = self.receive_frame(timeout)
        frame_type = frame[0] >> 4
        if frame_type == IsoTpFrameType.SINGLE:
            return frame[1:1 + (frame[0] & 0xF)]
        if frame_type != IsoTpFrameType.FIRST or len(frame) < 2:
            raise AvasDiagnosticError(f"Unexpected ISO-TP frame received: {frame.hex(' ').upper()}.")
        message_length = (frame[0] & 0xF) << 8 | frame[1]
        message = bytearray(frame[2:])
        self.transmit_padded_frame(bytes([IsoTpFrameType.FLOW_CONTROL << 4 | IsoTpFlowStatus.CONTINUE, 0, 0]))
        sequence_number = 1
        while len(message) < message_length:
            frame = self.receive_frame(ISO_TP_FRAME_TIMEOUT)
            if frame[0] != (IsoTpFrameType.CONSECUTIVE << 4 | sequence_number):
                raise AvasDiagnosticError(f"Consecutive frame {sequence_number} expected, "
                                          f"received {frame.hex(' ').upper()}.")
            message += frame[1:]
            sequence_number = (sequence_number + 1) & 0xF
        return bytes(message[:message_length])


class AvasConfigSession(threading.Thread):
    """Thread that runs the UDS configuration session with the AVAS module.

    The requests block while they wait for the module and therefore must not run in the GUI thread.
    The window hands its actions to this thread with request_start() and request_write() and gets the
    results through the report functions, which it makes safe for calls from another thread. All UDS
    requests are sent from this thread only, so that the TesterPresent request cannot interfere with
    an action. A new DiagnosticSessionControl request would lock the module again, so that the session
    is opened once per "Start Config" and kept open with TesterPresent afterwards. The module accepts
    SecurityAccess only at standstill of the vehicle, so that the window ends the input frames first
    and this thread waits for BUS_SILENCE_TIME before it opens the session.
    """

    def __init__(self, channel, report_log, report_state, report_record):
        """Creates the session thread.

        @param channel IsoTpChannel to the module
        @param report_log function that takes a text for the log of the window
        @param report_state function that takes the new ConfigSessionState
        @param report_record function that takes an AvasTelecodingRecord that was read from the module
        """
        super().__init__(name="AvasConfigSession", daemon=True)
        self.channel = channel
        self.report_log = report_log
        self.report_state = report_state
        self.report_record = report_record
        # Actions of the window as (function, arguments), in which None ends the thread.
        self.command_queue = queue.Queue()
        # Set when the program ends, so that the wait for a silent bus does not delay the exit.
        self.shutdown_event = threading.Event()
        self.state = ConfigSessionState.NOT_STARTED
        # Monotonic clock time of the last request, from which the TesterPresent period is counted.
        self.last_request_time = 0.0

    def request_start(self):
        """Asks the thread to open the extended session, to unlock the module and to read the record."""
        self.command_queue.put((self.start_configuration, ()))

    def request_write(self, record):
        """Asks the thread to write the given AvasTelecodingRecord to the module."""
        self.command_queue.put((self.write_telecoding_record, (record,)))

    def request_end(self):
        """Asks the thread to return an unlocked module to the default session and to end the configuration."""
        self.command_queue.put((self.end_configuration, ()))

    def request_shutdown(self):
        """Asks the thread to return the module to the default session and to end."""
        self.shutdown_event.set()
        self.command_queue.put(None)

    def run(self):
        while True:
            try:
                command = self.command_queue.get(timeout=COMMAND_POLL_TIME)
            except queue.Empty:
                self.send_tester_present_if_due()
                continue
            if command is None:
                break
            action, arguments = command
            try:
                action(*arguments)
            except Exception as error:
                # A programming error must not leave the buttons disabled forever.
                self.report_log(f"Internal error: {error!r}")
                self.set_state(ConfigSessionState.NOT_STARTED)
        self.close_session()

    def set_state(self, state):
        self.state = state
        self.report_state(state)

    def send_request(self, request):
        """Sends one UDS request, logs the exchange and returns the positive response.

        @param request complete UDS request as bytes
        @return complete positive response as bytes
        @raise UdsNegativeResponseError if the module refuses the request
        @raise AvasDiagnosticError if the transport fails or the response does not fit the request
        """
        self.channel.clear_receive_queue()
        self.report_log(f"0x5B0 -> {request.hex(' ').upper()}  ({describe_uds_request(request)})")
        self.last_request_time = time.monotonic()
        self.channel.send_message(request)
        timeout = UDS_RESPONSE_TIMEOUT
        while True:
            response = self.channel.receive_message(timeout)
            self.report_log(f"0x5AF <- {response.hex(' ').upper()}  ({describe_uds_response(response)})")
            if not (len(response) >= 3 and response[0] == UDS_NEGATIVE_RESPONSE
                    and response[2] == UdsNegativeResponseCode.RESPONSE_PENDING):
                break
            timeout = UDS_PENDING_RESPONSE_TIMEOUT
        if len(response) >= 3 and response[0] == UDS_NEGATIVE_RESPONSE:
            raise UdsNegativeResponseError(f"The AVAS module has refused service {response[1]:02X}: "
                                           f"{describe_negative_response_code(response[2])}.")
        if not response or response[0] != request[0] + UDS_POSITIVE_RESPONSE_OFFSET:
            raise AvasDiagnosticError(f"Response {response.hex(' ').upper()} does not fit the request.")
        return response

    def send_tester_present_if_due(self):
        """Keeps an unlocked session open and reports its loss, for example after the module lost its supply."""
        if self.state != ConfigSessionState.UNLOCKED:
            return
        if time.monotonic() - self.last_request_time < TESTER_PRESENT_PERIOD:
            return
        try:
            self.send_request(bytes([UdsServiceId.TESTER_PRESENT, 0x00]))
        except AvasDiagnosticError as error:
            self.report_log(f"The configuration session is lost: {error} Press Start Config again.")
            self.set_state(ConfigSessionState.NOT_STARTED)

    def start_configuration(self):
        """Waits for the standstill of the module, opens the extended session, unlocks it and reads the telecoding record."""
        self.set_state(ConfigSessionState.BUSY)
        if not self.wait_for_bus_silence():
            self.set_state(ConfigSessionState.NOT_STARTED)
            return
        try:
            self.send_request(bytes([UdsServiceId.DIAGNOSTIC_SESSION_CONTROL, UdsSessionType.EXTENDED]))
            self.unlock_module()
            record = self.read_telecoding_record()
        except AvasDiagnosticError as error:
            self.report_log(f"Start Config failed: {error}")
            self.set_state(ConfigSessionState.NOT_STARTED)
            return
        self.report_log("The AVAS module is unlocked, and the configuration can be changed.")
        self.report_record(record)
        self.set_state(ConfigSessionState.UNLOCKED)

    def wait_for_bus_silence(self):
        """Counts down BUS_SILENCE_TIME in the log and returns False if the program ends during the wait."""
        self.report_log(f"Input frames stopped, waiting {BUS_SILENCE_TIME} s for the standstill of the module.")
        for remaining_seconds in range(BUS_SILENCE_TIME, 0, -1):
            self.report_log(f"Extended session in {remaining_seconds} s ...")
            if self.shutdown_event.wait(1.0):
                return False
        return True

    def end_configuration(self):
        """Ends an unlocked session, which the user requests by a button or by starting a test with input frames."""
        if self.state != ConfigSessionState.UNLOCKED:
            return
        self.close_session()
        self.report_log("The configuration session has ended.")
        self.set_state(ConfigSessionState.NOT_STARTED)

    def unlock_module(self):
        """Performs SecurityAccess with the PSA seed/key algorithm, for which a seed of zeros means already unlocked."""
        seed_response = self.send_request(bytes([UdsServiceId.SECURITY_ACCESS, AVAS_SECURITY_LEVEL]))
        seed = seed_response[2:]
        if len(seed) != PSA_SEED_KEY_LENGTH:
            raise AvasDiagnosticError(f"The seed has {len(seed)} bytes instead of {PSA_SEED_KEY_LENGTH}.")
        if seed == bytes(PSA_SEED_KEY_LENGTH):
            return
        key = compute_psa_key(seed, AVAS_SECURITY_CONSTANT)
        self.send_request(bytes([UdsServiceId.SECURITY_ACCESS, AVAS_SECURITY_LEVEL + 1]) + key)

    def read_telecoding_record(self):
        """Reads the telecoding record from the module and returns it as AvasTelecodingRecord."""
        request = bytes([UdsServiceId.READ_DATA_BY_IDENTIFIER]) + TELECODING_DID.to_bytes(2, "big")
        response = self.send_request(request)
        if len(response) < len(request) + AvasTelecodingRecord.LENGTH:
            raise AvasDiagnosticError(f"The telecoding record is too short: {response.hex(' ').upper()}.")
        return AvasTelecodingRecord(response[len(request):len(request) + AvasTelecodingRecord.LENGTH])

    def write_telecoding_record(self, record):
        """Writes the record and reads it back, so that the window shows what the module really holds.

        The session stays open after a refused write, since the module refuses out-of-range values
        without any further effect. A lost session is detected by the next TesterPresent request.
        """
        self.set_state(ConfigSessionState.BUSY)
        try:
            self.send_request(bytes([UdsServiceId.WRITE_DATA_BY_IDENTIFIER]) + TELECODING_DID.to_bytes(2, "big")
                              + record.data)
            read_back_record = self.read_telecoding_record()
        except AvasDiagnosticError as error:
            self.report_log(f"Writing the telecoding record failed: {error}")
            self.set_state(ConfigSessionState.UNLOCKED)
            return
        if read_back_record.data == record.data:
            self.report_log(f"The telecoding record {record.format_hex()} was written and read back unchanged.")
        else:
            self.report_log(f"The write was accepted, but the module holds {read_back_record.format_hex()}.")
        self.report_record(read_back_record)
        self.set_state(ConfigSessionState.UNLOCKED)

    def close_session(self):
        """Returns an unlocked module to the default session."""
        if self.state != ConfigSessionState.UNLOCKED:
            return
        try:
            self.send_request(bytes([UdsServiceId.DIAGNOSTIC_SESSION_CONTROL, UdsSessionType.DEFAULT]))
        except AvasDiagnosticError as error:
            self.report_log(f"Closing the session failed: {error}")
