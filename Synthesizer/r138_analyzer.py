#!/usr/bin/env python3
"""
r138_analyzer.py - UNECE R138 one-third octave band analyzer for AVAS sounds.

What it does
------------
1.  Loads a WAV file (mix to mono).
2.  Computes the power spectrum (periodogram over the whole file, so the
    total power is exact; for a perfectly periodic loop file this is the
    exact line spectrum).
3.  Applies A-weighting (IEC 61672) to every FFT bin.
4.  Scales the spectrum so that the overall A-weighted level equals the
    level typed in by the user (default 55 dB(A)).  This models "the vehicle
    is measured at 10 km/h with this overall level and no loudspeaker
    colouring" - the analyzer only looks at the spectral SHAPE of the file.
5.  Sums the A-weighted power into one-third octave bands (base-10 band
    edges, IEC 61260) and compares the bands with Table 2 of R138,
    paragraph 6.2.8, after rounding to the nearest integer as the regulation
    requires.
6.  R138 6.2.1.2 is checked: overall level >= minimum, at least two bands
    at or above their minimum, at least one of them at or below 1600 Hz.
    6.2.7 (max. 75 dB(A) overall) is checked as well.
7.  Draws a diagram: narrowband A-weighted spectrum, band levels, the limit
    curve, and a box around every band that passes.

Usage
-----
    python3 r138_analyzer.py                       # file dialog, GUI
    python3 r138_analyzer.py sound.wav             # GUI with file
    python3 r138_analyzer.py sound.wav --level 55 \
            --png out.png --report out.md --no-gui   # headless

Only the 10 km/h column of Table 2 is checked.  The 20 km/h limits are 5 dB
(overall 6 dB) higher with the same spectral shape, while the transfer
function of the vehicle and the level increase that the AVAS module applies
by itself at 20 km/h are unknown, so a 20 km/h check of the file alone would
fail almost every synthetic sound without telling anything new.

Library:
    from r138_analyzer import analyze, plot_analysis, R138Window
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import soundfile as sf
from scipy.signal import welch

# --------------------------------------------------------------------------
# R138 Table 2 (paragraph 6.2.8) - minimum levels in dB(A), column 3
# (constant speed test at 10 km/h).  The 20 km/h column is not used, see the
# module description.
# --------------------------------------------------------------------------

R138_TEST_SPEED = 10   # km/h, the only constant speed test that is checked
R138_OVERALL_MIN = 50
R138_OVERALL_MAX = 75  # paragraph 6.2.7, forward driving

# nominal centre frequency -> minimum band level at 10 km/h
R138_BAND_MIN = {
    160: 45,
    200: 44,
    250: 43,
    315: 44,
    400: 45,
    500: 45,
    630: 46,
    800: 46,
    1000: 46,
    1250: 46,
    1600: 44,
    2000: 42,
    2500: 39,
    3150: 36,
    4000: 34,
    5000: 31,
}
R138_LOW_BAND_LIMIT = 1600  # at least one passing band must be <= this

# nominal one-third octave centres shown in the diagram (context + table 2)
NOMINAL_CENTRES = [50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800,
                   1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000,
                   10000, 12500, 16000]


def exact_centre(nominal: float) -> float:
    """Exact base-10 centre frequency (IEC 61260) for a nominal value."""
    n = round(10 * math.log10(nominal / 1000.0))
    return 1000.0 * 10 ** (n / 10.0)


def band_edges(nominal: float):
    fm = exact_centre(nominal)
    return fm * 10 ** (-1 / 20.0), fm * 10 ** (1 / 20.0)


def a_weighting_db(f: np.ndarray) -> np.ndarray:
    """A-weighting in dB per IEC 61672-1 (0 dB at 1 kHz)."""
    f = np.asarray(f, dtype=np.float64)
    f2 = f ** 2
    num = (12194.0 ** 2) * f2 ** 2
    den = ((f2 + 20.6 ** 2) * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2))
           * (f2 + 12194.0 ** 2))
    with np.errstate(divide="ignore"):
        ra = np.where(den > 0, num / den, 0.0)
        a = 20 * np.log10(np.where(ra > 0, ra, 1e-30)) + 2.00
    return a


def round_half_up(x: float) -> int:
    """'Mathematically rounded to the nearest integer' (0.5 rounds up)."""
    return int(math.floor(x + 0.5))


# --------------------------------------------------------------------------
# Result containers
# --------------------------------------------------------------------------

@dataclass
class BandResult:
    nominal: int
    f_low: float
    f_high: float
    level: float               # scaled dB(A), unrounded
    limit: Optional[int]       # 10 km/h minimum, None = no requirement
    truncated: bool = False    # band upper edge above Nyquist of the file

    @property
    def level_rounded(self) -> int:
        return round_half_up(self.level)

    @property
    def margin(self) -> Optional[float]:
        return None if self.limit is None else self.level - self.limit

    @property
    def passes(self) -> bool:
        return self.limit is not None and self.level_rounded >= self.limit


@dataclass
class Analysis:
    file: str
    sample_rate: int
    n_frames: int
    overall_level: float            # the level typed in by the user
    bands: List[BandResult] = field(default_factory=list)
    # narrowband display spectrum (A-weighted, scaled)
    nb_freqs: np.ndarray = field(default_factory=lambda: np.zeros(0))
    nb_levels: np.ndarray = field(default_factory=lambda: np.zeros(0))
    nb_resolution: float = 0.0
    # raw peak info
    peak_abs: float = 0.0

    # ---- compliance -------------------------------------------------------
    @property
    def overall_min(self) -> int:
        return R138_OVERALL_MIN

    @property
    def overall_ok(self) -> bool:
        return round_half_up(self.overall_level) >= self.overall_min

    @property
    def overall_max_ok(self) -> bool:
        return round_half_up(self.overall_level) <= R138_OVERALL_MAX

    @property
    def passing_bands(self) -> List[BandResult]:
        return [b for b in self.bands if b.passes]

    @property
    def low_band_ok(self) -> bool:
        return any(b.nominal <= R138_LOW_BAND_LIMIT for b in self.passing_bands)

    @property
    def two_bands_ok(self) -> bool:
        return len(self.passing_bands) >= 2

    @property
    def compliant(self) -> bool:
        return self.overall_ok and self.overall_max_ok and self.two_bands_ok and self.low_band_ok

    def verdict_lines(self) -> List[str]:
        ok = lambda b: "PASS" if b else "FAIL"
        lines = [
            f"Overall level {self.overall_level:.1f} dB(A) >= {self.overall_min} dB(A) "
            f"({R138_TEST_SPEED} km/h): {ok(self.overall_ok)}",
            f"Overall level <= {R138_OVERALL_MAX} dB(A) (6.2.7): {ok(self.overall_max_ok)}",
            f"At least two bands at/above minimum: {len(self.passing_bands)} band(s): "
            f"{ok(self.two_bands_ok)}",
            f"At least one passing band <= {R138_LOW_BAND_LIMIT} Hz: {ok(self.low_band_ok)}",
            f"R138 6.2.1.2 result @ {R138_TEST_SPEED} km/h: {'COMPLIANT' if self.compliant else 'NOT COMPLIANT'}",
        ]
        return lines

    def table_lines(self) -> List[str]:
        lines = [f"{'Band':>6} {'Hz range':>14} {'Level':>7} {'Rnd':>4} {'Min':>5} {'Margin':>7}  Result",
                 "-" * 60]
        for b in self.bands:
            lim = "" if b.limit is None else str(b.limit)
            margin = "" if b.margin is None else f"{b.margin:+.1f}"
            res = "" if b.limit is None else ("PASS" if b.passes else "below")
            if b.truncated:
                res += "*"
            lines.append(f"{b.nominal:>6} {b.f_low:6.0f}-{b.f_high:<7.0f} {b.level:7.1f} "
                         f"{b.level_rounded:>4} {lim:>5} {margin:>7}  {res}")
        if any(b.truncated for b in self.bands):
            lines.append("* band cut by the Nyquist frequency of the file, evaluated on the existing part")
        return lines

    def report_markdown(self) -> str:
        lines = [
            f"# R138 one-third octave analysis",
            "",
            f"- File: `{os.path.basename(self.file)}`",
            f"- Sample rate: {self.sample_rate} Hz, frames: {self.n_frames} "
            f"({self.n_frames / self.sample_rate:.3f} s), peak {self.peak_abs:.3f}",
            f"- Assumed overall level: {self.overall_level:.1f} dB(A) (A-weighted, scaled)",
            f"- Checked against: constant speed test {R138_TEST_SPEED} km/h, Table 2 of R138 6.2.8",
            f"- Assumptions: no loudspeaker/enclosure colouring, free field, "
            f"levels mathematically rounded to integers before comparison",
            "",
            "## Verdict",
            "",
        ]
        lines += [f"- {l}" for l in self.verdict_lines()]
        lines += ["", "## Bands", "", "```"] + self.table_lines() + ["```", ""]
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

def load_mono(path: str):
    data, sr = sf.read(path, always_2d=True, dtype="float64")
    mono = data.mean(axis=1)
    return mono, int(sr)


def analyze(path: str, overall_level: float = 55.0,
            f_min_display: float = 40.0) -> Analysis:
    mono, sr = load_mono(path)
    n = len(mono)
    if n < 64:
        raise ValueError("File too short.")
    mono = mono - np.mean(mono)
    nyq = sr / 2.0

    # --- full-length periodogram: power per bin (Parseval exact) -----------
    spec = np.fft.rfft(mono)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    power = (np.abs(spec) ** 2) / (n ** 2)
    power[1:] *= 2.0                      # one-sided
    if n % 2 == 0:
        power[-1] /= 2.0
    a_lin = 10 ** (a_weighting_db(np.maximum(freqs, 1e-3)) / 10.0)
    power_a = power * a_lin
    power_a[freqs < 10.0] = 0.0           # below audio range / DC

    total_a = float(np.sum(power_a))
    if total_a <= 0:
        raise ValueError("File is silent.")
    offset_db = overall_level - 10 * np.log10(total_a)

    # --- bands --------------------------------------------------------------
    bands: List[BandResult] = []
    for nominal in NOMINAL_CENTRES:
        lo, hi = band_edges(nominal)
        # keep a band if its centre is below Nyquist; a band cut by Nyquist
        # (e.g. 5000 Hz at 11025 Hz sample rate) is evaluated on the part
        # that exists in the file and marked as truncated.
        if exact_centre(nominal) >= nyq or lo < f_min_display:
            continue
        sel = (freqs >= lo) & (freqs < min(hi, nyq))
        p = float(np.sum(power_a[sel]))
        level = (10 * np.log10(p) + offset_db) if p > 0 else -99.0
        bands.append(BandResult(
            nominal=nominal, f_low=lo, f_high=hi, level=level, truncated=hi > nyq,
            limit=R138_BAND_MIN.get(nominal)))

    # --- narrowband display spectrum ---------------------------------------
    nperseg = min(n, 8192)
    f_w, pxx = welch(mono, fs=sr, window="hann", nperseg=nperseg,
                     noverlap=nperseg // 2, scaling="density")
    df = f_w[1] - f_w[0]
    p_bin_a = pxx * df * 10 ** (a_weighting_db(np.maximum(f_w, 1e-3)) / 10.0)
    with np.errstate(divide="ignore"):
        nb_levels = 10 * np.log10(np.maximum(p_bin_a, 1e-30)) + offset_db
    sel = f_w >= f_min_display

    return Analysis(file=path, sample_rate=sr, n_frames=n,
                    overall_level=overall_level, bands=bands,
                    nb_freqs=f_w[sel], nb_levels=nb_levels[sel], nb_resolution=df,
                    peak_abs=float(np.max(np.abs(mono))))


# --------------------------------------------------------------------------
# Plot
# --------------------------------------------------------------------------

# colours of the diagram
C_SPECTRUM = "#2a78d6"
C_BAND = "#0b0b0b"
C_LIMIT = "#eb6834"
C_PASS = "#0ca30c"
C_GRID = "#d9d8d3"
C_TEXT2 = "#52514e"


def plot_analysis(an: Analysis, fig=None):
    """Draw the analysis into a matplotlib Figure (created if None)."""
    import matplotlib
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    if fig is None:
        fig = plt.figure(figsize=(11, 6.2), dpi=100)
    fig.clear()
    ax = fig.add_subplot(111)

    # narrowband spectrum
    ax.plot(an.nb_freqs, an.nb_levels, color=C_SPECTRUM, lw=0.9, alpha=0.8,
            label=f"A-weighted narrowband spectrum (Δf = {an.nb_resolution:.1f} Hz)")

    # band levels as horizontal segments
    first = True
    for b in an.bands:
        ax.hlines(b.level, b.f_low, b.f_high, color=C_BAND, lw=2.5,
                  label="1/3 octave band level" if first else None)
        first = False

    # limit curve (step) at 10 km/h
    lim_bands = [b for b in an.bands if b.limit is not None]
    if lim_bands:
        xs, ys = [], []
        for b in lim_bands:
            xs += [b.f_low, b.f_high]
            ys += [b.limit, b.limit]
        ax.plot(xs, ys, color=C_LIMIT, lw=2.0, ls="--",
                label=f"R138 minimum, {R138_TEST_SPEED} km/h (Table 2)")

    # boxes around passing bands
    first = True
    for b in an.passing_bands:
        top = b.level + 1.5
        rect = Rectangle((b.f_low, b.limit - 1.5), b.f_high - b.f_low,
                         top - (b.limit - 1.5), fill=True, fc=C_PASS, alpha=0.12,
                         ec=C_PASS, lw=2.0, label="band passes R138" if first else None)
        first = False
        ax.add_patch(rect)
        ax.text(math.sqrt(b.f_low * b.f_high), top + 0.6,
                f"{b.level_rounded}", ha="center", va="bottom", fontsize=9,
                color=C_PASS, fontweight="bold")

    # axis cosmetics
    ax.set_xscale("log")
    xmin = an.bands[0].f_low if an.bands else 40
    xmax = an.bands[-1].f_high if an.bands else an.sample_rate / 2
    ax.set_xlim(xmin, xmax)
    # labelled major ticks on the octave centres, unlabelled minor ticks on
    # every third-octave centre (labelling all bands is unreadable)
    octaves = [31.5, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
    ax.set_xticks([exact_centre(t) for t in octaves if xmin <= exact_centre(t) <= xmax])
    ax.set_xticklabels([f"{t / 1000:g}k" if t >= 1000 else f"{t:g}" for t in octaves
                        if xmin <= exact_centre(t) <= xmax], fontsize=9)
    ax.set_xticks([exact_centre(b.nominal) for b in an.bands], minor=True)
    ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax.tick_params(axis="x", which="minor", length=3, colors=C_TEXT2)
    ax.set_xlabel("One-third octave band nominal centre frequency / Hz")
    ax.set_ylabel("Sound pressure level / dB(A)")
    lv = [b.level for b in an.bands] + [b.limit for b in lim_bands]
    ymax = max(lv) + 8 if lv else 70
    ymin = max(min(an.nb_levels.min() if len(an.nb_levels) else 0, 20), -10)
    ax.set_ylim(ymin, ymax)
    ax.grid(True, which="both", axis="x", color=C_GRID, lw=0.6)
    ax.grid(True, which="major", axis="y", color=C_GRID, lw=0.6)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(C_TEXT2)
    ax.tick_params(colors=C_TEXT2, labelsize=9)

    verdict = "COMPLIANT" if an.compliant else "NOT COMPLIANT"
    ax.set_title(f"{os.path.basename(an.file)}  -  overall {an.overall_level:.1f} dB(A) "
                 f"assumed at {R138_TEST_SPEED} km/h  -  R138 6.2.1.2: {verdict}",
                 fontsize=11, color=C_PASS if an.compliant else "#d03b3b", loc="left")
    ax.legend(loc="upper left", bbox_to_anchor=(0.0, -0.09), ncol=2, fontsize=8,
              frameon=False)
    # fixed margins are used because tight_layout() would re-measure the legend
    # below the axes on every redraw and would shrink the plot each time
    fig.subplots_adjust(left=0.07, right=0.985, top=0.93, bottom=0.20)
    return fig


# --------------------------------------------------------------------------
# GUI window
# --------------------------------------------------------------------------

def _gui_imports():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
    from matplotlib.figure import Figure
    return tk, ttk, filedialog, messagebox, FigureCanvasTkAgg, NavigationToolbar2Tk, Figure


class R138Window:
    """Analyzer window.  `master` may be None (standalone root) or a Tk root
    (then a Toplevel is created)."""

    def __init__(self, master=None, file: Optional[str] = None,
                 level: float = 55.0):
        (tk, ttk, filedialog, messagebox, FigureCanvasTkAgg,
         NavigationToolbar2Tk, Figure) = _gui_imports()
        self.tk, self.ttk, self.filedialog, self.messagebox = tk, ttk, filedialog, messagebox

        self.win = tk.Tk() if master is None else tk.Toplevel(master)
        self.win.title("R138 one-third octave band analyzer")
        self.win.geometry("1300x840")
        self.file = file
        self.analysis: Optional[Analysis] = None

        # ---- controls ------------------------------------------------------
        top = ttk.Frame(self.win, padding=6)
        top.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(top, text="Open WAV...", command=self.open_file).pack(side=tk.LEFT)
        self.file_var = tk.StringVar(value=file or "(no file)")
        ttk.Label(top, textvariable=self.file_var, width=48, anchor="w").pack(side=tk.LEFT, padx=8)
        ttk.Label(top, text="Overall level dB(A):").pack(side=tk.LEFT)
        self.level_var = tk.StringVar(value=f"{level:g}")
        e = ttk.Entry(top, textvariable=self.level_var, width=6)
        e.pack(side=tk.LEFT, padx=4)
        e.bind("<Return>", lambda _e: self.run())
        ttk.Button(top, text="Analyze", command=self.run).pack(side=tk.LEFT, padx=10)
        ttk.Button(top, text="Save report...", command=self.save_report).pack(side=tk.LEFT)

        # ---- plot + table --------------------------------------------------
        body = ttk.Frame(self.win)
        body.pack(fill=tk.BOTH, expand=True)
        self.fig = Figure(figsize=(8.5, 6), dpi=100)
        self.canvas = FigureCanvasTkAgg(self.fig, master=body)
        self.canvas.get_tk_widget().pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        right = ttk.Frame(body, padding=4)
        right.pack(side=tk.RIGHT, fill=tk.Y)
        self.text = tk.Text(right, width=72, font=("DejaVu Sans Mono", 9), wrap="none")
        self.text.pack(fill=tk.BOTH, expand=True)
        self.text.tag_configure("pass", foreground=C_PASS)
        self.text.tag_configure("fail", foreground="#d03b3b")
        self.text.tag_configure("head", font=("DejaVu Sans Mono", 9, "bold"))

        self.toolbar = NavigationToolbar2Tk(self.canvas, self.win)
        self.toolbar.update()

        if file:
            self.run()

    # -------------------------------------------------------------------
    def open_file(self):
        f = self.filedialog.askopenfilename(
            title="Open WAV file for R138 check",
            filetypes=[("WAV files", "*.wav *.WAV"), ("All files", "*")])
        if f:
            self.file = f
            self.file_var.set(f)
            self.run()

    def set_file(self, path: str):
        self.file = path
        self.file_var.set(path)
        self.run()

    def run(self):
        if not self.file:
            return
        try:
            level = float(self.level_var.get().replace(",", "."))
        except ValueError:
            self.messagebox.showerror("Input", "Overall level must be a number.")
            return
        try:
            self.analysis = analyze(self.file, level)
        except Exception as e:
            self.messagebox.showerror("Analysis failed", str(e))
            return
        plot_analysis(self.analysis, self.fig)
        self.canvas.draw()
        self._fill_text()

    def _fill_text(self):
        an = self.analysis
        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("end", "R138 6.2.1.2 check\n", "head")
        for line in an.verdict_lines():
            tag = "pass" if ("PASS" in line or "COMPLIANT" in line and "NOT" not in line) else "fail"
            t.insert("end", line + "\n", tag)
        t.insert("end", "\nAssumptions: spectrum scaled to the typed overall level,\n"
                        "A-weighted, no loudspeaker colouring, levels rounded to\n"
                        "integers before comparison (R138 6.2.8).\n\n")
        t.insert("end", "One-third octave bands\n", "head")
        for line in an.table_lines():
            tag = "pass" if line.rstrip().endswith("PASS") else None
            t.insert("end", line + "\n", tag) if tag else t.insert("end", line + "\n")
        t.configure(state="disabled")

    def save_report(self):
        if not self.analysis:
            return
        base = os.path.splitext(os.path.basename(self.file))[0]
        f = self.filedialog.asksaveasfilename(
            title="Save report (markdown + PNG with same name)",
            defaultextension=".md", initialfile=f"{base}_r138_report.md",
            filetypes=[("Markdown", "*.md")])
        if not f:
            return
        png = os.path.splitext(f)[0] + ".png"
        self.fig.savefig(png, dpi=120)
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(self.analysis.report_markdown())
            fh.write(f"\n![diagram]({os.path.basename(png)})\n")
        self.messagebox.showinfo("Saved", f"Report written:\n{f}\n{png}")


# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="R138 one-third octave band analyzer")
    ap.add_argument("file", nargs="?", help="WAV file (dialog if omitted)")
    ap.add_argument("--level", type=float, default=55.0, help="overall dB(A), default 55")
    ap.add_argument("--png", help="write diagram to PNG")
    ap.add_argument("--report", help="write markdown report")
    ap.add_argument("--no-gui", action="store_true", help="headless: print table and exit")
    args = ap.parse_args(argv)

    if args.no_gui:
        if not args.file:
            ap.error("--no-gui needs a file")
        an = analyze(args.file, args.level)
        print("\n".join(an.verdict_lines()))
        print()
        print("\n".join(an.table_lines()))
        if args.png:
            import matplotlib
            matplotlib.use("Agg")
            fig = plot_analysis(an)
            fig.savefig(args.png, dpi=120)
            print(f"PNG written: {args.png}")
        if args.report:
            with open(args.report, "w", encoding="utf-8") as fh:
                fh.write(an.report_markdown())
                if args.png:
                    fh.write(f"\n![diagram]({os.path.basename(args.png)})\n")
            print(f"Report written: {args.report}")
        return 0 if an.compliant else 1

    win = R138Window(None, args.file, args.level)
    if not args.file:
        win.win.after(200, win.open_file)
    win.win.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
