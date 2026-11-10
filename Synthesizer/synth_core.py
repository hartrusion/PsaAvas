#!/usr/bin/env python3
"""
synth_core.py - AVAS loop synthesizer ("stationary texture").

Goal: turn ANY sample (any length, any content) into a perfectly periodic
loop that sounds like the sample "going on forever" at constant speed, i.e.
with all slow changes (pitch glide, level rise and fall, pass-by) removed.

A change of pitch or level over time is not a low frequency component of the
signal but a spread of every partial (sidebands) in the spectrum of the whole
file, so that it cannot be removed by a filter and has to be averaged out.

Method
------
1.  Short-time Fourier transform (STFT) of the whole sample.
2.  Average the power spectra of all frames -> the long-term spectrum.
    Averaging over time IS a low-pass of the modulation down to 0 Hz:
    every slow change of level or pitch disappears, what remains is the
    "mix of the sound over its whole duration".
3.  Split the averaged spectrum into
      * tonal peaks: narrow bumps that stand `tonal_threshold_db` above a
        smooth spectral envelope (median filter over `tonal_sep_octaves`),
      * texture: the rest (noise-like part, the envelope under the peaks).
4.  Synthesize over exactly `n_frames` samples:
      * every tonal peak becomes ONE stable sine at the power centroid of
        the peak, snapped to a multiple of f0 = sample_rate / n_frames,
        with the total power of the peak.  A peak that was smeared by a
        pitch glide collapses to a single steady tone.
      * the texture becomes random-phase noise with exactly the averaged
        texture spectrum (periodic by construction, Gaussian).
      * optional artificial noise with a power-law slope.
5.  Mix with three gains, normalise to the maximum peak, write stereo WAV.

Apart from the optional artificial noise, every tone and every bit of the
texture spectrum comes from the sample.  Changing the thresholds only moves
energy between "steady tone" and "noise texture".

Library:
    from synth_core import SynthParams, synthesize, save_avas_wav
CLI:
    python3 synth_core.py sample.wav out.wav
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from math import gcd
from typing import List, Optional

import numpy as np
import soundfile as sf
import wave
from scipy.ndimage import median_filter, label
from scipy.signal import resample_poly, butter, filtfilt


# --------------------------------------------------------------------------
# Parameters
# --------------------------------------------------------------------------

@dataclass
class SynthParams:
    source_file: str = ""

    # text inputs
    sample_rate: int = 11025
    n_frames: int = 28665
    max_peak: float = 0.56

    # main sliders
    min_freq: float = 100.0        # 2 .. 2000     high-pass / lower band edge
    max_freq: float = 7000.0       # 500 .. 20000  upper band edge (clamped to Nyquist)
    tonal_threshold_db: float = 8.0  # 0 .. 30     peak must stand this far above the envelope
    tonal_gain: float = 1.0        # 0 .. 1        steady tones from the sample
    texture_gain: float = 1.0      # 0 .. 1        noise texture from the sample
    noise_gain: float = 0.3        # 0 .. 1        artificial noise
    noise_slope: float = -1.5      # -3 .. 3       spectral tilt of the artificial noise

    # advanced
    fft_size: int = 4096           # analysis window (resolution = sample_rate / fft_size)
    tonal_sep_octaves: float = 0.33  # width of the envelope median filter
    max_tonal_width_pct: float = 20.0  # wider bumps are texture, not tones
    max_tonals: int = 60           # 0 = no tonal part
    frame_weighting: float = 0.0   # 0 = loud moments dominate, 1 = every moment equal

    # fixed
    noise_seed: int = 42
    right_channel_gain: float = 0.97

    def validate(self) -> List[str]:
        problems = []
        if self.sample_rate < 2000:
            problems.append("Sample rate must be >= 2000 Hz.")
        if self.n_frames < 256:
            problems.append("Number of frames must be >= 256.")
        if not (0.0 < self.max_peak <= 1.0):
            problems.append("Maximum peak must be in (0, 1].")
        nyq = self.sample_rate / 2.0
        if self.min_freq >= nyq:
            problems.append(f"Minimum frequency must be below Nyquist ({nyq:.0f} Hz).")
        if self.max_freq <= self.min_freq:
            problems.append("Maximum frequency must be above minimum frequency.")
        if self.fft_size < 256:
            problems.append("FFT size must be >= 256.")
        if self.tonal_gain <= 0 and self.texture_gain <= 0 and self.noise_gain <= 0:
            problems.append("All gains are zero - nothing to synthesize.")
        return problems

    @property
    def effective_max_freq(self) -> float:
        return min(self.max_freq, self.sample_rate / 2.0 * 0.99)

    @property
    def loop_base_freq(self) -> float:
        return self.sample_rate / self.n_frames

    @property
    def loop_seconds(self) -> float:
        return self.n_frames / self.sample_rate

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TonalComponent:
    freq: float            # power centroid in the averaged spectrum (Hz)
    snapped_freq: float    # multiple of f0 actually synthesized
    width_hz: float        # width of the peak region (pitch spread in the sample)
    power: float           # linear power of the tone (relative units)
    level_db: float        # 10 log10(power / strongest tone power)


@dataclass
class SynthResult:
    mono: np.ndarray
    params: SynthParams
    components: List[TonalComponent] = field(default_factory=list)
    source_sample_rate: int = 0
    source_seconds: float = 0.0
    n_stft_frames: int = 0
    tonal_energy_share: float = 0.0     # share of sample energy that became tones
    level_fluctuation_db: float = 0.0   # max-min of short-time level inside the loop
    # analysis data for plotting
    ana_freqs: np.ndarray = field(default_factory=lambda: np.zeros(0))
    ana_psd: np.ndarray = field(default_factory=lambda: np.zeros(0))
    ana_env: np.ndarray = field(default_factory=lambda: np.zeros(0))
    ana_texture: np.ndarray = field(default_factory=lambda: np.zeros(0))

    def report(self) -> str:
        p = self.params
        lines = [
            f"Source          : {p.source_file}",
            f"  original rate : {self.source_sample_rate} Hz, {self.source_seconds:.2f} s, "
            f"{self.n_stft_frames} analysis frames",
            f"Loop            : {p.n_frames} frames @ {p.sample_rate} Hz = {p.loop_seconds:.3f} s, "
            f"f0 = {p.loop_base_freq:.4f} Hz",
            f"Band            : {p.min_freq:.0f} .. {p.effective_max_freq:.0f} Hz, "
            f"analysis resolution {p.sample_rate / p.fft_size:.2f} Hz",
            f"Tonals          : {len(self.components)} steady tones (threshold {p.tonal_threshold_db:.0f} dB, "
            f"max {p.max_tonals}), {100 * self.tonal_energy_share:.0f} % of the sample energy",
            f"Gains           : tonal {p.tonal_gain:.2f}, texture {p.texture_gain:.2f}, "
            f"noise {p.noise_gain:.2f} (slope {p.noise_slope:+.1f})",
            f"Peak            : {np.max(np.abs(self.mono)):.3f}",
            f"Level fluctuation inside the loop: {self.level_fluctuation_db:.1f} dB "
            f"(short-time level, max - min; lower = more steady)",
            "Steady tones (centroid -> synthesized, spread in sample, level):",
        ]
        for i, c in enumerate(self.components, 1):
            lines.append(f"  {i:2d}. {c.freq:8.1f} Hz -> {c.snapped_freq:8.1f} Hz  "
                         f"spread {c.width_hz:5.1f} Hz  {c.level_db:6.1f} dB")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

def load_and_resample(path: str, sample_rate: int):
    data, sr = sf.read(path, always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    data = data.astype(np.float64)
    if sr != sample_rate:
        g = gcd(int(sr), int(sample_rate))
        data = resample_poly(data, sample_rate // g, sr // g)
    return data, int(sr)


def highpass(data: np.ndarray, cutoff: float, sr: int) -> np.ndarray:
    nyq = sr / 2.0
    wn = min(max(cutoff / nyq, 1e-4), 0.99)
    b, a = butter(4, wn, btype="high")
    if len(data) < 3 * max(len(a), len(b)):
        return data
    return filtfilt(b, a, data)


def averaged_psd(x: np.ndarray, fs: int, nfft: int, frame_weighting: float):
    """Time-averaged one-sided power spectral density of x (V^2/Hz).

    frame_weighting 0: plain mean of the frame power spectra (loud moments
    dominate).  1: every frame is normalised to equal energy first (every
    moment of the sample counts the same).  Values in between blend."""
    if len(x) < nfft:
        x = np.concatenate([x, np.zeros(nfft - len(x))])
    hop = nfft // 4
    w = np.hanning(nfft + 1)[:-1]
    frames = np.lib.stride_tricks.sliding_window_view(x, nfft)[::hop]
    spec = np.fft.rfft(frames * w, axis=1)
    pf = np.abs(spec) ** 2                               # (frames, bins)
    mean_plain = pf.mean(axis=0)
    if frame_weighting > 0:
        e = pf.sum(axis=1, keepdims=True)
        e[e <= 0] = 1.0
        pf_norm = pf / e * e.mean()
        mean_eq = pf_norm.mean(axis=0)
        p = (1 - frame_weighting) * mean_plain + frame_weighting * mean_eq
    else:
        p = mean_plain
    psd = p * 2.0 / (fs * np.sum(w ** 2))
    psd[0] /= 2.0
    if nfft % 2 == 0:
        psd[-1] /= 2.0
    freqs = np.fft.rfftfreq(nfft, 1.0 / fs)
    return freqs, psd, len(frames)


def spectral_envelope(freqs: np.ndarray, psd: np.ndarray, sep_octaves: float,
                      points_per_octave: int = 96) -> np.ndarray:
    """Smooth envelope of the PSD: median filter on a log-frequency grid."""
    fmin = max(freqs[1], 10.0)
    fmax = freqs[-1]
    n_oct = np.log2(fmax / fmin)
    n = max(int(n_oct * points_per_octave), 8)
    lf = np.linspace(np.log2(fmin), np.log2(fmax), n)
    logp = 10 * np.log10(np.maximum(psd, 1e-30))
    lp = np.interp(lf, np.log2(np.maximum(freqs, fmin)), logp)
    width = max(3, int(round(sep_octaves * points_per_octave)) | 1)
    env_l = median_filter(lp, size=width, mode="nearest")
    env = np.interp(np.log2(np.maximum(freqs, fmin)), lf, env_l)
    return 10 ** (env / 10.0)


def find_tonal_peaks(freqs, psd, env, p: SynthParams):
    """Split the averaged PSD into tonal peaks and texture.

    Returns (components, texture_psd)."""
    df = freqs[1] - freqs[0]
    thr = 10 ** (p.tonal_threshold_db / 10.0)
    band = (freqs >= p.min_freq) & (freqs <= p.effective_max_freq)
    mask = band & (psd > env * thr)
    texture = psd.copy()
    comps: List[TonalComponent] = []
    if p.max_tonals <= 0 or not mask.any():
        return comps, texture

    regions, n = label(mask)
    f0 = p.loop_base_freq
    candidates = []
    for r in range(1, n + 1):
        idx = np.where(regions == r)[0]
        excess = psd[idx] - env[idx]
        power = float(np.sum(excess) * df)
        if power <= 0:
            continue
        fc = float(np.sum(freqs[idx] * excess) / np.sum(excess))
        width = len(idx) * df
        if width > fc * p.max_tonal_width_pct / 100.0:
            continue                      # too broad for a tone: stays texture
        candidates.append((power, fc, width, idx))
    candidates.sort(key=lambda c: c[0], reverse=True)
    candidates = candidates[: p.max_tonals]
    if not candidates:
        return comps, texture
    pmax = candidates[0][0]
    for power, fc, width, idx in candidates:
        texture[idx] = env[idx]           # the tone leaves only the envelope behind
        k = max(1, int(round(fc / f0)))
        comps.append(TonalComponent(freq=fc, snapped_freq=k * f0, width_hz=width,
                                    power=power, level_db=10 * np.log10(power / pmax)))
    return comps, texture


# --------------------------------------------------------------------------
# Synthesis (all in one rFFT of exactly n_frames -> periodic by construction)
# --------------------------------------------------------------------------

def _bin_amplitude(power: float, n: int) -> float:
    """|X_k| so that irfft gives a sinusoid of power `power` (mean square)."""
    return n * np.sqrt(power / 2.0)


def synthesize_tones(comps: List[TonalComponent], p: SynthParams, rng) -> np.ndarray:
    n = p.n_frames
    spec = np.zeros(n // 2 + 1, dtype=complex)
    f0 = p.loop_base_freq
    for c in comps:
        k = int(round(c.snapped_freq / f0))
        if 0 < k < len(spec):
            spec[k] += _bin_amplitude(c.power, n) * np.exp(1j * rng.uniform(0, 2 * np.pi))
    return np.fft.irfft(spec, n=n)


def synthesize_texture(freqs, texture_psd, p: SynthParams, rng) -> np.ndarray:
    """Random-phase noise with the averaged texture spectrum, period n_frames."""
    n = p.n_frames
    fk = np.fft.rfftfreq(n, 1.0 / p.sample_rate)
    logp = np.interp(np.log2(np.maximum(fk, 1e-3)),
                     np.log2(np.maximum(freqs, 1e-3)),
                     10 * np.log10(np.maximum(texture_psd, 1e-30)))
    psd_k = 10 ** (logp / 10.0)
    psd_k[(fk < p.min_freq) | (fk > p.effective_max_freq)] = 0.0
    power_k = psd_k * p.loop_base_freq        # power per output bin
    # Gaussian texture: Rayleigh distributed magnitudes, uniform phases
    mag = _bin_amplitude(1.0, n) * np.sqrt(power_k) * np.sqrt(-np.log(rng.uniform(1e-12, 1, len(fk))))
    spec = mag * np.exp(1j * rng.uniform(0, 2 * np.pi, len(fk)))
    spec[0] = 0.0
    if n % 2 == 0:
        spec[-1] = spec[-1].real
    return np.fft.irfft(spec, n=n)


def synthesize_periodic_noise(p: SynthParams, rng) -> np.ndarray:
    """Artificial noise with power-law slope, peak normalised to 1."""
    n = p.n_frames
    freqs = np.fft.rfftfreq(n, 1.0 / p.sample_rate)
    phases = rng.uniform(0, 2 * np.pi, len(freqs))
    mag = (np.maximum(freqs, 1.0) / 1000.0) ** p.noise_slope
    mag[freqs < p.min_freq] = 0
    mag[freqs > p.effective_max_freq] = 0
    spectrum = mag * np.exp(1j * phases)
    spectrum[0] = 0.0
    if n % 2 == 0:
        spectrum[-1] = spectrum[-1].real
    noise = np.fft.irfft(spectrum, n=n)
    peak = np.max(np.abs(noise))
    return noise / peak if peak > 0 else noise


def level_fluctuation_db(x: np.ndarray, fs: int, win_s: float = 0.125) -> float:
    """Max - min of the short-time RMS level inside the loop (wrapped)."""
    n = max(8, int(win_s * fs))
    xx = np.concatenate([x, x[:n]])
    e = np.convolve(xx ** 2, np.ones(n) / n, mode="valid")
    e = e[e > 0]
    if len(e) == 0:
        return 0.0
    return float(10 * np.log10(e.max() / e.min()))


def synthesize(p: SynthParams) -> SynthResult:
    problems = p.validate()
    if problems:
        raise ValueError("\n".join(problems))

    audio, src_sr = load_and_resample(p.source_file, p.sample_rate)
    src_seconds = len(audio) / p.sample_rate
    audio = audio - np.mean(audio)
    audio = highpass(audio, p.min_freq, p.sample_rate)

    freqs, psd, n_stft = averaged_psd(audio, p.sample_rate, p.fft_size, p.frame_weighting)
    env = spectral_envelope(freqs, psd, p.tonal_sep_octaves)
    comps, texture = find_tonal_peaks(freqs, psd, env, p)

    rng = np.random.default_rng(p.noise_seed)
    tones = synthesize_tones(comps, p, rng) if p.tonal_gain > 0 and comps else np.zeros(p.n_frames)
    tex = synthesize_texture(freqs, texture, p, rng) if p.texture_gain > 0 else np.zeros(p.n_frames)
    noise = synthesize_periodic_noise(p, rng) if p.noise_gain > 0 else np.zeros(p.n_frames)

    # the two sample-derived parts share one scale (their ratio is physical),
    # the artificial noise is peak normalised on its own
    sample_part = p.tonal_gain * tones + p.texture_gain * tex
    sp_peak = np.max(np.abs(sample_part))
    if sp_peak > 0:
        sample_part /= sp_peak
    mix = sample_part + p.noise_gain * noise
    peak = np.max(np.abs(mix))
    if peak > 0:
        mix *= p.max_peak / peak

    band = (freqs >= p.min_freq) & (freqs <= p.effective_max_freq)
    total = float(np.sum(psd[band])) or 1.0
    share = float(np.sum(psd[band] - texture[band])) / total

    return SynthResult(mono=mix, params=p, components=comps,
                       source_sample_rate=src_sr, source_seconds=src_seconds,
                       n_stft_frames=n_stft, tonal_energy_share=share,
                       level_fluctuation_db=level_fluctuation_db(mix, p.sample_rate),
                       ana_freqs=freqs, ana_psd=psd, ana_env=env, ana_texture=texture)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def to_stereo_int16(mono: np.ndarray, right_gain: float = 0.97) -> np.ndarray:
    stereo = np.stack([mono, mono * right_gain], axis=1)
    return np.clip(stereo * 32767, -32768, 32767).astype(np.int16)


def save_avas_wav(mono: np.ndarray, path: str, sample_rate: int,
                  right_gain: float = 0.97) -> None:
    pcm = to_stereo_int16(mono, right_gain)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm.tobytes())


def plot_analysis(res: SynthResult, fig=None):
    """Averaged spectrum, envelope, texture and the steady tones (for the GUI)."""
    import matplotlib.pyplot as plt
    if fig is None:
        fig = plt.figure(figsize=(10, 5.5), dpi=100)
    fig.clear()
    ax = fig.add_subplot(111)
    p = res.params
    f, m = res.ana_freqs, res.ana_freqs > 0
    db = lambda v: 10 * np.log10(np.maximum(v, 1e-30))
    ref = db(res.ana_psd[m]).max()
    ax.plot(f[m], db(res.ana_psd[m]) - ref, color="#2a78d6", lw=0.8,
            label="averaged spectrum of the sample")
    ax.plot(f[m], db(res.ana_texture[m]) - ref, color="#0b0b0b", lw=1.2, alpha=0.7,
            label="texture (noise part)")
    ax.plot(f[m], db(res.ana_env[m]) - ref, color="#eb6834", lw=1.0, ls="--",
            label=f"envelope + {p.tonal_threshold_db:.0f} dB threshold")
    ax.plot(f[m], db(res.ana_env[m]) - ref + p.tonal_threshold_db, color="#eb6834", lw=0.8, ls=":")
    if res.components:
        pmax = max(c.power for c in res.components)
        df = f[1] - f[0]
        for i, c in enumerate(res.components):
            # a tone's "equivalent bin level" for display: its power in one analysis bin
            lvl = db(c.power / df) - ref
            ax.plot([c.snapped_freq, c.snapped_freq], [-100, lvl], color="#0ca30c", lw=1.5,
                    label="steady tones" if i == 0 else None)
            ax.plot(c.snapped_freq, lvl, "o", color="#0ca30c", ms=4)
    ax.set_xscale("log")
    ax.set_xlim(max(20, p.min_freq * 0.7), p.sample_rate / 2)
    ax.set_ylim(-80, 5)
    ticks = [t for t in (31.5, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000) if t < p.sample_rate / 2]
    ax.set_xticks(ticks)
    ax.set_xticklabels([f"{t / 1000:g}k" if t >= 1000 else f"{t:g}" for t in ticks])
    ax.xaxis.set_minor_formatter(plt.NullFormatter())
    ax.set_xlabel("Frequency / Hz")
    ax.set_ylabel("Level / dB re max")
    ax.grid(True, color="#d9d8d3", lw=0.6)
    ax.set_title(f"{p.source_file.split('/')[-1]}: {len(res.components)} steady tones, "
                 f"{100 * res.tonal_energy_share:.0f} % tonal energy", loc="left", fontsize=10)
    ax.legend(loc="lower left", fontsize=8, frameon=False)
    fig.subplots_adjust(left=0.08, right=0.98, top=0.92, bottom=0.12)
    return fig


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    params = SynthParams(source_file=sys.argv[1])
    res = synthesize(params)
    save_avas_wav(res.mono, sys.argv[2], params.sample_rate)
    print(res.report())
    print(f"Saved: {sys.argv[2]}")
