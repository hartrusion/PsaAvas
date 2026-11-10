"""Window of AvasCanUtil, which operates the speed simulation and the configuration of the AVAS module.

LLM code generated with Claude Opus 5.5
"""

import math
import queue
import time
import tkinter as tk
import tkinter.messagebox
import tkinter.scrolledtext
from tkinter import ttk

from avas_config_session import (AvasConfigSession, ConfigSessionState, DEFAULT_FORWARD_VOLUME_GAIN,
                                 DEFAULT_REAR_VOLUME_GAIN, DEFAULT_SOUND, IsoTpChannel, VOLUME_FULL_LEVEL)
from avas_vehicle_simulation import SpeedRampProfile, VehicleDirection, VehicleSpeedSimulation
from can_bus_worker import CanBusWorker

# Time in milliseconds between two updates of the window from the worker threads.
WINDOW_UPDATE_INTERVAL = 50

# Highest speed in km/h of the constant tone slider.
CONSTANT_SPEED_MAXIMUM = 50

# Time in seconds that closing the window waits for the configuration session to end.
SESSION_SHUTDOWN_TIMEOUT = 3.0

# Time in seconds that closing the window waits for the CAN bus worker to end.
WORKER_SHUTDOWN_TIMEOUT = 1.0

# Sounds that can be selected. The module refuses the value 0 and all values above 6.
SELECTABLE_SOUNDS = ("1", "2", "3", "4", "5", "6")

# Note below the sound selection, which states the observed behaviour of a tested module.
SOUND_SELECTION_NOTE = ("Sounds 3 to 6 will probably not work. The tested module accepts them,\n"
                        "but stayed silent, whereas sounds 1 and 2 play.")

# Number of decimals of a volume field, with which every Q14 gain survives the conversion to the
# range from 0 to 1 and back without a change of its raw value.
VOLUME_DECIMALS = 5

# Input fields of the speed ramp as (key, label, default value, minimum, maximum, increment).
RAMP_FIELD_DEFINITIONS = (
    ("maximum_speed", "Maximum speed in km/h", 50.0, 1.0, 100.0, 1.0),
    ("hold_before_time", "Hold before the ramp in s", 0.2, 0.0, 60.0, 0.1),
    ("ramp_up_time", "Ramp-up time in s", 12.0, 0.1, 600.0, 1.0),
    ("ramp_down_time", "Ramp-down time in s", 6.0, 0.1, 600.0, 1.0),
    ("hold_after_time", "Hold after the ramp in s", 0.2, 0.0, 60.0, 0.1),
)


def format_volume_description(gain):
    """Returns the raw value and the level in dB of a Q14 gain for the label beside a volume field."""
    if gain == 0:
        return "0x0000, silent"
    return f"0x{gain:04X}, {20.0 * math.log10(gain / VOLUME_FULL_LEVEL):.1f} dB"


class AvasCanUtilWindow:
    """Main window of the program, which also creates and connects the worker threads.

    The window has a panel for the speed simulation, a panel for the configuration session and a log
    of the UDS requests and of the status changes of the module. All work on the CAN bus is done by
    the CanBusWorker and the AvasConfigSession, which run in their own threads because tkinter may
    only be used from the GUI thread and must not be blocked. Results of these threads are therefore
    put into a queue of function calls that the window executes at a fixed interval.
    """

    def __init__(self, root, bus):
        """Creates the window and starts the worker threads.

        @param root tk.Tk object of the program
        @param bus opened python-can bus, whose shutdown is left to the caller
        """
        self.root = root
        # Function calls from the worker threads that wait for their execution in the GUI thread.
        self.pending_gui_calls = queue.Queue()
        # Last record read from the module, on which a write is based, or None before the first read.
        self.telecoding_record = None
        self.simulation = VehicleSpeedSimulation()
        self.can_worker = CanBusWorker(bus, self.simulation, self.post_log_line)
        channel = IsoTpChannel(self.can_worker.diagnostic_transmit_queue, self.can_worker.diagnostic_receive_queue)
        self.config_session = AvasConfigSession(channel, self.post_log_line, self.post_config_state,
                                                self.post_telecoding_record)

        root.title("AvasCanUtil")
        root.protocol("WM_DELETE_WINDOW", self.close_window)
        self.status_text = tk.StringVar()
        ttk.Label(root, textvariable=self.status_text).grid(row=0, column=0, columnspan=2, sticky="w", padx=8, pady=4)
        self.build_simulation_panel().grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        self.build_configuration_panel().grid(row=1, column=1, sticky="nsew", padx=8, pady=4)
        self.log_widget = tkinter.scrolledtext.ScrolledText(root, height=14, width=100, state="disabled")
        self.log_widget.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=8, pady=4)
        root.columnconfigure(0, weight=1)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(2, weight=1)

        self.apply_config_state(ConfigSessionState.NOT_STARTED)
        self.can_worker.start()
        self.config_session.start()
        self.update_window()

    def build_simulation_panel(self):
        """Creates the panel with the driving direction, the ramp, the constant tone and the stop button."""
        panel = ttk.LabelFrame(self.root, text="Speed simulation")
        self.direction_value = tk.IntVar(value=VehicleDirection.FORWARD)
        direction_panel = ttk.Frame(panel)
        direction_panel.grid(row=0, column=0, columnspan=2, sticky="w", padx=6, pady=4)
        ttk.Radiobutton(direction_panel, text="Forward", variable=self.direction_value,
                        value=VehicleDirection.FORWARD).grid(row=0, column=0, padx=4)
        ttk.Radiobutton(direction_panel, text="Reverse", variable=self.direction_value,
                        value=VehicleDirection.REARWARD).grid(row=0, column=1, padx=4)

        # Text variables and limits of the ramp input fields by the key of RAMP_FIELD_DEFINITIONS.
        self.ramp_fields = {}
        row = 1
        for key, label, default, minimum, maximum, increment in RAMP_FIELD_DEFINITIONS:
            text_variable = tk.StringVar(value=str(default))
            ttk.Label(panel, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=1)
            ttk.Spinbox(panel, textvariable=text_variable, from_=minimum, to=maximum, increment=increment,
                        width=8).grid(row=row, column=1, sticky="w", padx=6, pady=1)
            self.ramp_fields[key] = (text_variable, label, minimum, maximum)
            row += 1
        self.start_ramp_button = ttk.Button(panel, text="Start ramp", command=self.start_ramp)
        self.start_ramp_button.grid(row=row, column=0, columnspan=2, sticky="ew", padx=6, pady=6)
        row += 1

        ttk.Separator(panel).grid(row=row, column=0, columnspan=2, sticky="ew", pady=4)
        row += 1
        # Speed in km/h that the slider currently selects.
        self.constant_speed = 0
        self.constant_speed_text = tk.StringVar(value="Constant tone speed: 0 km/h")
        ttk.Label(panel, textvariable=self.constant_speed_text).grid(row=row, column=0, columnspan=2, sticky="w",
                                                                     padx=6)
        row += 1
        self.constant_speed_slider = ttk.Scale(panel, from_=0, to=CONSTANT_SPEED_MAXIMUM, orient="horizontal",
                                               command=self.change_constant_speed)
        self.constant_speed_slider.grid(row=row, column=0, columnspan=2, sticky="ew", padx=6)
        row += 1
        self.start_constant_tone_button = ttk.Button(panel, text="Start constant tone",
                                                     command=self.start_constant_tone)
        self.start_constant_tone_button.grid(row=row, column=0, columnspan=2, sticky="ew", padx=6, pady=6)
        row += 1

        ttk.Separator(panel).grid(row=row, column=0, columnspan=2, sticky="ew", pady=4)
        row += 1
        self.stop_button = ttk.Button(panel, text="Stop (0 km/h)", command=self.stop_simulation)
        self.stop_button.grid(row=row, column=0, columnspan=2, sticky="ew", padx=6, pady=6)
        panel.columnconfigure(1, weight=1)
        return panel

    def build_configuration_panel(self):
        """Creates the panel with the session button, the volumes, the sound selection and the write button."""
        panel = ttk.LabelFrame(self.root, text="Configuration")
        self.start_config_button = ttk.Button(panel, text="Start Config", command=self.start_configuration)
        self.start_config_button.grid(row=0, column=0, columnspan=3, sticky="ew", padx=6, pady=6)

        # Volume input widgets, which are enabled only while the module is unlocked.
        self.configuration_inputs = []
        self.forward_volume_text = tk.StringVar()
        self.forward_volume_description = tk.StringVar()
        self.build_volume_row(panel, 1, "Volume forward (0 to 1)", self.forward_volume_text,
                              self.forward_volume_description)
        self.rear_volume_text = tk.StringVar()
        self.rear_volume_description = tk.StringVar()
        self.build_volume_row(panel, 2, "Volume reverse (0 to 1)", self.rear_volume_text,
                              self.rear_volume_description)

        ttk.Label(panel, text="Sound").grid(row=3, column=0, sticky="w", padx=6, pady=4)
        self.sound_text = tk.StringVar()
        self.sound_box = ttk.Combobox(panel, textvariable=self.sound_text, values=SELECTABLE_SOUNDS, width=9)
        self.sound_box.grid(row=3, column=1, sticky="w", padx=6, pady=4)
        ttk.Label(panel, text=SOUND_SELECTION_NOTE).grid(row=4, column=0, columnspan=3, sticky="w", padx=6, pady=2)

        self.write_button = ttk.Button(panel, text="Write DID 2101", command=self.confirm_write_record)
        self.write_button.grid(row=6, column=0, columnspan=3, sticky="ew", padx=6, pady=6)
        self.restore_defaults_button = ttk.Button(panel, text="Restore Defaults", command=self.restore_default_settings)
        self.restore_defaults_button.grid(row=5, column=0, columnspan=3, sticky="ew", padx=6, pady=6)
        self.configuration_state_text = tk.StringVar()
        ttk.Label(panel, textvariable=self.configuration_state_text).grid(row=7, column=0, columnspan=3, sticky="w",
                                                                          padx=6, pady=2)
        self.end_config_button = ttk.Button(panel, text="End Config Session", command=self.config_session.request_end)
        self.end_config_button.grid(row=8, column=0, columnspan=3, sticky="ew", padx=6, pady=6)
        panel.columnconfigure(2, weight=1)
        return panel

    def build_volume_row(self, panel, row, label, text_variable, description_variable):
        """Creates one row with a volume field and the label that shows its raw value and level."""
        ttk.Label(panel, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=4)
        volume_box = ttk.Spinbox(panel, textvariable=text_variable, from_=0.0, to=1.0, increment=0.01,
                                 format=f"%.{VOLUME_DECIMALS}f", width=9)
        volume_box.grid(row=row, column=1, sticky="w", padx=6, pady=4)
        self.configuration_inputs.append(volume_box)
        ttk.Label(panel, textvariable=description_variable).grid(row=row, column=2, sticky="w", padx=6, pady=4)
        text_variable.trace_add("write", lambda *unused: self.update_volume_description(text_variable,
                                                                                        description_variable))

    def post_log_line(self, text):
        """Queues a log line, callable from any thread."""
        self.pending_gui_calls.put((self.append_log_line, (time.strftime("%H:%M:%S"), text)))

    def post_config_state(self, state):
        """Queues a new state of the configuration session, callable from any thread."""
        self.pending_gui_calls.put((self.apply_config_state, (state,)))

    def post_telecoding_record(self, record):
        """Queues a record that was read from the module, callable from any thread."""
        self.pending_gui_calls.put((self.apply_telecoding_record, (record,)))

    def update_window(self):
        """Executes the queued calls of the worker threads, updates the status line and schedules itself again."""
        while True:
            try:
                function, arguments = self.pending_gui_calls.get_nowait()
            except queue.Empty:
                break
            function(*arguments)
        state = self.can_worker.current_state
        if state.frames_enabled:
            frames_text = "sending"
        else:
            frames_text = "off"
        self.status_text.set(f"Speed {state.speed:5.1f} km/h   Phase: {state.phase.value}   "
                             f"Direction: {state.direction.name.lower()}   Input frames: {frames_text}   "
                             f"0x571={self.can_worker.status_monitor.get_last_avas_status_text()}")
        self.root.after(WINDOW_UPDATE_INTERVAL, self.update_window)

    def append_log_line(self, time_text, text):
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", f"{time_text}  {text}\n")
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def apply_config_state(self, state):
        """Enables the buttons that fit the state of the configuration session."""
        if state == ConfigSessionState.BUSY:
            start_button_state = "disabled"
        else:
            start_button_state = "normal"
        self.start_config_button.configure(state=start_button_state)
        # A test must not start while the module waits for its standstill or is being unlocked.
        self.start_ramp_button.configure(state=start_button_state)
        self.start_constant_tone_button.configure(state=start_button_state)
        if state == ConfigSessionState.UNLOCKED:
            write_state = "normal"
            sound_box_state = "readonly"
            self.configuration_state_text.set("Module unlocked. Starting a test ends the session.")
        else:
            write_state = "disabled"
            sound_box_state = "disabled"
            if state == ConfigSessionState.BUSY:
                self.configuration_state_text.set("Communicating with the module ...")
            else:
                self.configuration_state_text.set("Press Start Config to unlock the module.")
        self.write_button.configure(state=write_state)
        self.restore_defaults_button.configure(state=write_state)
        self.end_config_button.configure(state=write_state)
        self.sound_box.configure(state=sound_box_state)
        for input_widget in self.configuration_inputs:
            input_widget.configure(state=write_state)

    def apply_telecoding_record(self, record):
        """Shows the values of a record that was read from the module."""
        self.telecoding_record = record
        self.display_settings(record.get_forward_volume_gain(), record.get_rear_volume_gain(), record.get_sound())

    def display_settings(self, forward_volume_gain, rear_volume_gain, sound):
        """Fills the input fields with the given Q14 gains and sound, of which an unselectable sound leaves the box empty."""
        self.forward_volume_text.set(f"{forward_volume_gain / VOLUME_FULL_LEVEL:.{VOLUME_DECIMALS}f}")
        self.rear_volume_text.set(f"{rear_volume_gain / VOLUME_FULL_LEVEL:.{VOLUME_DECIMALS}f}")
        sound_text = str(sound)
        if sound_text in SELECTABLE_SOUNDS:
            self.sound_text.set(sound_text)
        else:
            self.sound_text.set("")

    def restore_default_settings(self):
        """Fills the input fields with the factory settings, which reach the module only with the write button."""
        self.display_settings(DEFAULT_FORWARD_VOLUME_GAIN, DEFAULT_REAR_VOLUME_GAIN, DEFAULT_SOUND)
        self.post_log_line("Default values entered. Press Write DID 2101 to store them in the module.")

    def update_volume_description(self, text_variable, description_variable):
        """Updates the label beside a volume field while the user edits the value."""
        try:
            description_variable.set(format_volume_description(self.convert_volume_text_to_gain(text_variable.get())))
        except ValueError:
            description_variable.set("invalid")

    @staticmethod
    def convert_volume_text_to_gain(volume_text):
        """Returns the Q14 gain of a volume text from 0 to 1 and raises ValueError for any other text."""
        volume = float(volume_text.replace(",", "."))
        if not 0.0 <= volume <= 1.0:
            raise ValueError(f"{volume_text} is not between 0 and 1")
        return round(volume * VOLUME_FULL_LEVEL)

    def read_ramp_profile(self):
        """Creates the SpeedRampProfile from the input fields and raises ValueError with a text for the user."""
        values = {}
        for key, (text_variable, label, minimum, maximum) in self.ramp_fields.items():
            try:
                value = float(text_variable.get().replace(",", "."))
            except ValueError:
                raise ValueError(f"{label}: '{text_variable.get()}' is not a number.") from None
            if not minimum <= value <= maximum:
                raise ValueError(f"{label} must be between {minimum} and {maximum}.")
            values[key] = value
        return SpeedRampProfile(**values)

    def get_selected_direction(self):
        return VehicleDirection(self.direction_value.get())

    def start_ramp(self):
        try:
            profile = self.read_ramp_profile()
        except ValueError as error:
            tkinter.messagebox.showerror("Speed ramp", str(error))
            return
        self.config_session.request_end()
        self.simulation.start_ramp(profile, self.get_selected_direction(), time.monotonic())

    def start_constant_tone(self):
        self.config_session.request_end()
        self.simulation.start_constant_speed(float(self.constant_speed), self.get_selected_direction())

    def start_configuration(self):
        """Makes the bus silent and blocks the test buttons at once, before the session thread starts its wait."""
        self.simulation.stop_input_frames()
        self.apply_config_state(ConfigSessionState.BUSY)
        self.config_session.request_start()

    def change_constant_speed(self, slider_value):
        """Takes over a new slider position in whole km/h, which a running constant tone follows at once."""
        self.constant_speed = round(float(slider_value))
        self.constant_speed_text.set(f"Constant tone speed: {self.constant_speed} km/h")
        self.simulation.set_constant_speed(float(self.constant_speed))

    def stop_simulation(self):
        """Returns the speed to 0 km/h, whereas the slider keeps its position, so that the same constant tone can be restarted."""
        self.simulation.stop()

    def confirm_write_record(self):
        """Builds the new record from the input fields and writes it after a confirmation of the user."""
        try:
            forward_volume_gain = self.convert_volume_text_to_gain(self.forward_volume_text.get())
            rear_volume_gain = self.convert_volume_text_to_gain(self.rear_volume_text.get())
        except ValueError:
            tkinter.messagebox.showerror("Configuration", "Both volumes must be numbers between 0 and 1.")
            return
        if self.sound_text.get() not in SELECTABLE_SOUNDS:
            tkinter.messagebox.showerror("Configuration", "Please select a sound from 1 to 6.")
            return
        record = self.telecoding_record.with_settings(forward_volume_gain, rear_volume_gain, int(self.sound_text.get()))
        if tkinter.messagebox.askyesno("Write DID 2101",
                                       f"Write the telecoding record to the AVAS module?\n\n{record.format_hex()}"):
            self.config_session.request_write(record)

    def close_window(self):
        """Ends the simulation and the session in this order, because the session still needs the worker to say goodbye."""
        self.simulation.stop()
        self.config_session.request_shutdown()
        self.config_session.join(SESSION_SHUTDOWN_TIMEOUT)
        self.can_worker.stop()
        self.can_worker.join(WORKER_SHUTDOWN_TIMEOUT)
        self.root.destroy()
