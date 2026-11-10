#!/usr/bin/env python3
"""
avas_synth_gui.py - GUI for the AVAS sound synthesizer and the R138 check.

Start:   python3 avas_synth_gui.py [sample.wav]

Workflow
--------
1.  "Open sample..."   choose any WAV (the sound to be imitated).
2.  Adjust parameters   text inputs and sliders (see synth_core.SynthParams).
                        Every change is synthesized and played at once when
                        a slider is released or a text input is confirmed
                        with Enter or left.
3.  "Apply"             synthesizes and plays with the current parameters
                        without waiting for a change.
4.  "Preview"           toggles gapless loop playback.  A stopped preview
                        starts again with the next change.
5.  "Check R138"        writes the last synthesized sound to the temp folder
                        and opens the one-third octave analyzer with this
                        file.  "Check sample" analyzes the
                        sample file itself instead.  "Show analysis" shows
                        the averaged sample spectrum and which parts became
                        steady tones and which became texture.
6.  "Save WAV as..."    stores the loop (16 bit stereo) plus a JSON side car
                        file with all parameters used, for documentation.

Temporary file: <system temp>/avas_synthesizer/last_synth.wav
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from dataclasses import asdict

from synth_core import SynthParams, synthesize, save_avas_wav, to_stereo_int16, plot_analysis
from looping_player import LoopingPlayer
from r138_analyzer import R138Window

TEMP_DIR = os.path.join(tempfile.gettempdir(), "avas_synthesizer")
TEMP_WAV = os.path.join(TEMP_DIR, "last_synth.wav")

# (attribute, label, min, max, resolution)
SLIDERS = [
    ("min_freq", "Minimum frequency [Hz]", 2, 2000, 1),
    ("max_freq", "Maximum frequency [Hz]", 500, 20000, 10),
    ("tonal_threshold_db", "Tonal threshold [dB above envelope]", 0, 30, 0.5),
    ("tonal_gain", "Tonal gain (steady tones from sample)", 0.0, 1.0, 0.01),
    ("texture_gain", "Texture gain (noise part of sample)", 0.0, 1.0, 0.01),
    ("noise_gain", "Artificial noise gain", 0.0, 1.0, 0.01),
    ("noise_slope", "Noise slope (-3 dark .. +3 bright)", -3.0, 3.0, 0.1),
]
ADVANCED = [
    ("tonal_sep_octaves", "Tonal separation width [octaves]", 0.05, 1.0, 0.01),
    ("max_tonal_width_pct", "Max. tone spread [% of frequency]", 1, 50, 1),
    ("max_tonals", "Max. number of steady tones", 0, 200, 1),
    ("frame_weighting", "Frame weighting (0 loud moments .. 1 equal)", 0.0, 1.0, 0.05),
]
FFT_SIZES = [1024, 2048, 4096, 8192, 16384]
TEXTS = [
    ("sample_rate", "Wave sample rate [Hz]"),
    ("n_frames", "Wave number of frames"),
    ("max_peak", "Maximum wave peak (0..1)"),
]
INT_ATTRS = ("max_tonals",)


class SynthApp:
    def __init__(self, root: tk.Tk, sample: str | None = None):
        self.root = root
        root.title("AVAS Sound Synthesizer - R138 preparation")
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.player = LoopingPlayer()
        self.result = None
        self.r138_win: R138Window | None = None
        self.ana_win = None
        self.sample_file = sample or ""
        defaults = SynthParams()

        main = ttk.Frame(root, padding=8)
        main.pack(fill=tk.BOTH, expand=True)

        # ---- sample file ---------------------------------------------------
        frow = ttk.Frame(main)
        frow.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Button(frow, text="Open sample...", command=self.open_sample).pack(side=tk.LEFT)
        self.file_var = tk.StringVar(value=self.sample_file or "(no sample file)")
        ttk.Label(frow, textvariable=self.file_var, anchor="w").pack(side=tk.LEFT, padx=8, fill=tk.X, expand=True)

        # ---- parameters ----------------------------------------------------
        pf = ttk.LabelFrame(main, text="Synthesizer parameters", padding=6)
        pf.grid(row=1, column=0, sticky="nsew")
        self.text_vars: dict[str, tk.StringVar] = {}
        r = 0
        for attr, label in TEXTS:
            ttk.Label(pf, text=label).grid(row=r, column=0, sticky="w", pady=2)
            v = tk.StringVar(value=str(getattr(defaults, attr)))
            entry = ttk.Entry(pf, textvariable=v, width=10)
            entry.grid(row=r, column=1, sticky="w", padx=6)
            entry.bind("<Return>", self.schedule_live_apply)
            entry.bind("<FocusOut>", self.schedule_live_apply)
            self.text_vars[attr] = v
            r += 1
        self.loop_info = tk.StringVar()
        ttk.Label(pf, textvariable=self.loop_info, foreground="#52514e").grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(0, 6))
        r += 1
        self.slider_vars: dict[str, tk.DoubleVar] = {}

        def make_slider(parent, row, attr, label, lo, hi, res):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w")
            v = tk.DoubleVar(value=float(getattr(defaults, attr)))
            sc = tk.Scale(parent, variable=v, from_=lo, to=hi, resolution=res,
                          orient=tk.HORIZONTAL, length=340, showvalue=True)
            sc.grid(row=row, column=1, sticky="ew", padx=6)
            sc.bind("<ButtonRelease-1>", self.schedule_live_apply)
            sc.bind("<KeyRelease>", self.schedule_live_apply)
            self.slider_vars[attr] = v

        for attr, label, lo, hi, res in SLIDERS:
            make_slider(pf, r, attr, label, lo, hi, res)
            r += 1

        af = ttk.LabelFrame(pf, text="Advanced (defaults are fine for most samples)", padding=4)
        af.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(af, text="Analysis FFT size").grid(row=0, column=0, sticky="w")
        self.fft_var = tk.StringVar(value=str(defaults.fft_size))
        fft_box = ttk.Combobox(af, textvariable=self.fft_var, values=[str(x) for x in FFT_SIZES],
                               width=8, state="readonly")
        fft_box.grid(row=0, column=1, sticky="w", padx=6)
        fft_box.bind("<<ComboboxSelected>>", self.schedule_live_apply)
        self.fft_info = tk.StringVar()
        ttk.Label(af, textvariable=self.fft_info, foreground="#52514e").grid(row=1, column=0, columnspan=2, sticky="w")
        ar = 2
        for attr, label, lo, hi, res in ADVANCED:
            make_slider(af, ar, attr, label, lo, hi, res)
            ar += 1
        self.fft_var.trace_add("write", lambda *_: self.update_loop_info())
        r += 1
        ttk.Button(pf, text="Reset defaults", command=self.reset_defaults).grid(
            row=r, column=0, sticky="w", pady=(6, 0))
        for v in self.text_vars.values():
            v.trace_add("write", lambda *_: self.update_loop_info())
        self.update_loop_info()

        # ---- log -----------------------------------------------------------
        lf = ttk.LabelFrame(main, text="Result / log", padding=4)
        lf.grid(row=1, column=1, sticky="nsew", padx=(8, 0))
        self.log = tk.Text(lf, width=66, height=40, font=("DejaVu Sans Mono", 9), wrap="word")
        self.log.pack(fill=tk.BOTH, expand=True)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(1, weight=1)

        # ---- buttons -------------------------------------------------------
        bf = ttk.Frame(main)
        bf.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.btn_apply = ttk.Button(bf, text="Apply (synthesize)", command=self.apply)
        self.btn_apply.pack(side=tk.LEFT)
        self.preview_text = tk.StringVar(value="Preview ▶")
        self.btn_preview = ttk.Button(bf, textvariable=self.preview_text, command=self.toggle_preview)
        self.btn_preview.pack(side=tk.LEFT, padx=6)
        ttk.Button(bf, text="Check R138 (synthesized)", command=self.check_r138).pack(side=tk.LEFT, padx=6)
        ttk.Button(bf, text="Check R138 (sample file)", command=self.check_r138_sample).pack(side=tk.LEFT)
        ttk.Button(bf, text="Show analysis", command=self.show_analysis).pack(side=tk.LEFT, padx=6)
        ttk.Button(bf, text="Save WAV as...", command=self.save_wav).pack(side=tk.RIGHT)

        self.status = tk.StringVar(value=f"Audio backends: {self.player.available_backends()}")
        ttk.Label(main, textvariable=self.status, foreground="#52514e").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self.log_write(__doc__.split("Workflow")[0].strip() + "\n\n"
                       "Open a sample file. Every change is played at once.\n")

    # ------------------------------------------------------------- helpers
    def log_write(self, text: str, clear: bool = False):
        if clear:
            self.log.delete("1.0", "end")
        self.log.insert("end", text + "\n")
        self.log.see("end")

    def update_loop_info(self):
        try:
            sr = int(self.text_vars["sample_rate"].get())
            n = int(self.text_vars["n_frames"].get())
            self.loop_info.set(f"loop = {n / sr:.3f} s, f0 = {sr / n:.4f} Hz, Nyquist = {sr / 2:.0f} Hz")
            nfft = int(self.fft_var.get())
            self.fft_info.set(f"resolution {sr / nfft:.2f} Hz, window {1000 * nfft / sr:.0f} ms")
        except (ValueError, ZeroDivisionError, AttributeError):
            self.loop_info.set("invalid sample rate / frames")

    def read_params(self) -> SynthParams:
        p = SynthParams(source_file=self.sample_file)
        try:
            p.sample_rate = int(float(self.text_vars["sample_rate"].get()))
            p.n_frames = int(float(self.text_vars["n_frames"].get()))
            p.max_peak = float(self.text_vars["max_peak"].get().replace(",", "."))
        except ValueError as e:
            raise ValueError(f"Text input is not a number: {e}")
        for attr, *_ in SLIDERS + ADVANCED:
            val = self.slider_vars[attr].get()
            setattr(p, attr, int(round(val)) if attr in INT_ATTRS else float(val))
        p.fft_size = int(self.fft_var.get())
        return p

    def reset_defaults(self):
        d = SynthParams()
        for attr, _ in TEXTS:
            self.text_vars[attr].set(str(getattr(d, attr)))
        for attr, *_ in SLIDERS + ADVANCED:
            self.slider_vars[attr].set(float(getattr(d, attr)))
        self.fft_var.set(str(d.fft_size))
        self.apply_live()

    # ------------------------------------------------------------- actions
    def open_sample(self):
        f = filedialog.askopenfilename(
            title="Open sample WAV", initialdir=os.path.dirname(os.path.abspath(__file__)),
            filetypes=[("Sound files", "*.wav *.WAV *.flac *.ogg"), ("All files", "*")])
        if f:
            self.sample_file = f
            self.file_var.set(f)
            self.log_write(f"Sample: {f}")
            self.apply_live()

    def apply(self):
        if not self.sample_file:
            messagebox.showwarning("No sample", "Open a sample file first.")
            return
        try:
            self.synthesize_and_play(self.read_params())
        except Exception as e:
            messagebox.showerror("Apply failed", str(e))

    def schedule_live_apply(self, event=None):
        """Defers apply_live() until the event has been processed completely,
        because the class binding of a widget can still change its value.

        LLM code generated with Claude Opus 5.5
        """
        self.root.after_idle(self.apply_live)

    def apply_live(self):
        """Synthesizes and plays the sound if a parameter has changed.

        Problems only go to the status line, because a dialog would interrupt
        the work with the sliders and would open again whenever an invalid text
        input loses the focus.

        LLM code generated with Claude Opus 5.5
        """
        if not self.sample_file:
            return
        try:
            params = self.read_params()
            if self.result is None or params != self.result.params:
                self.synthesize_and_play(params)
        except Exception as e:
            self.status.set(f"Not applied: {e}")

    def synthesize_and_play(self, params: SynthParams):
        """Replaces the sound of the preview, which is started if it is stopped.

        The sound goes directly from the memory to the player, so that no file
        is written here unless an open R138 window has to show the new sound.
        The synthesis takes only some 10 ms, so that it can run in the GUI thread.

        LLM code generated with Claude Opus 5.5

        @param params parameters of the new sound
        @throws ValueError if the parameters are invalid
        @throws RuntimeError if the playback cannot be started
        """
        self.root.configure(cursor="watch")
        self.root.update_idletasks()
        try:
            self.result = synthesize(params)
        finally:
            self.root.configure(cursor="")
        self.player.set_buffer(to_stereo_int16(self.result.mono, params.right_channel_gain),
                               params.sample_rate)
        if not self.start_preview():
            raise RuntimeError(f"Playback failed: {self.player.last_error}")
        self.log_write(self.result.report(), clear=True)
        if self.r138_win is not None and self._r138_alive() and self.r138_win.file == TEMP_WAV:
            self.write_temp_wav()
            self.r138_win.run()
        if self.ana_win is not None and self._win_alive(self.ana_win):
            plot_analysis(self.result, self.ana_fig)
            self.ana_canvas.draw()

    def write_temp_wav(self):
        """Writes the last sound for the R138 analyzer, which only reads files.

        LLM code generated with Claude Opus 5.5
        """
        params = self.result.params
        os.makedirs(TEMP_DIR, exist_ok=True)
        save_avas_wav(self.result.mono, TEMP_WAV, params.sample_rate, params.right_channel_gain)

    def start_preview(self) -> bool:
        """Starts the playback and shows its state.

        LLM code generated with Claude Opus 5.5

        @return False if the playback could not be started
        """
        if not self.player.start():
            return False
        self.preview_text.set("Preview ■ (stop)")
        self.status.set(f"Preview playing (backend: {self.player.backend_name}), gapless loop.")
        return True

    def toggle_preview(self):
        if self.player.is_playing:
            self.player.stop()
            self.preview_text.set("Preview ▶")
            self.status.set("Preview stopped.")
            return
        if not self.player.has_buffer:
            messagebox.showinfo("Preview", "Press Apply first to synthesize a sound.")
            return
        if not self.start_preview():
            messagebox.showerror("Preview", self.player.last_error)

    def _r138_alive(self) -> bool:
        try:
            return bool(self.r138_win and self.r138_win.win.winfo_exists())
        except tk.TclError:
            return False

    def _open_r138(self, path: str):
        if self._r138_alive():
            self.r138_win.set_file(path)
            self.r138_win.win.lift()
        else:
            self.r138_win = R138Window(self.root, path)

    def check_r138(self):
        if self.result is None:
            messagebox.showinfo("Check R138", "Press Apply first to synthesize a sound.")
            return
        self.write_temp_wav()
        self._open_r138(TEMP_WAV)

    def check_r138_sample(self):
        if not self.sample_file:
            messagebox.showinfo("Check R138", "Open a sample file first.")
            return
        self._open_r138(self.sample_file)

    def save_wav(self):
        if self.result is None:
            messagebox.showinfo("Save", "Press Apply first to synthesize a sound.")
            return
        base = os.path.splitext(os.path.basename(self.sample_file))[0] + "_avas.wav"
        f = filedialog.asksaveasfilename(title="Save AVAS loop", defaultextension=".wav",
                                         initialfile=base, filetypes=[("WAV", "*.wav")])
        if not f:
            return
        p = self.result.params
        save_avas_wav(self.result.mono, f, p.sample_rate, p.right_channel_gain)
        side = os.path.splitext(f)[0] + ".params.json"
        with open(side, "w", encoding="utf-8") as fh:
            json.dump({"params": asdict(p),
                       "components": [asdict(c) for c in self.result.components],
                       "report": self.result.report()}, fh, indent=2)
        self.log_write(f"\nSaved: {f}\nParameters: {side}")

    def show_analysis(self):
        """Window with the averaged spectrum, envelope, texture and steady tones."""
        if self.result is None:
            messagebox.showinfo("Analysis", "Press Apply first to synthesize a sound.")
            return
        if self.ana_win is not None and self._win_alive(self.ana_win):
            plot_analysis(self.result, self.ana_fig)
            self.ana_canvas.draw()
            self.ana_win.lift()
            return
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        from matplotlib.figure import Figure
        self.ana_win = tk.Toplevel(self.root)
        self.ana_win.title("Synthesizer analysis: averaged spectrum, texture, steady tones")
        self.ana_fig = Figure(figsize=(10, 5.5), dpi=100)
        self.ana_canvas = FigureCanvasTkAgg(self.ana_fig, master=self.ana_win)
        self.ana_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        NavigationToolbar2Tk(self.ana_canvas, self.ana_win).update()
        plot_analysis(self.result, self.ana_fig)
        self.ana_canvas.draw()

    @staticmethod
    def _win_alive(win) -> bool:
        try:
            return bool(win.winfo_exists())
        except tk.TclError:
            return False

    def on_close(self):
        self.player.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    SynthApp(root, sys.argv[1] if len(sys.argv) > 1 else None)
    root.mainloop()


if __name__ == "__main__":
    main()
