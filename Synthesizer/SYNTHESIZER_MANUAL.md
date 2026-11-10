# AVAS Synthesizer – Method and GUI Options

This document explains how the AVAS sound synthesizer works and what every option of its window does, for which the description follows the code in `synth_core.py` and `avas_synth_gui.py`.

## 1 Overview

This section states what the synthesizer produces and which processing steps lead to the result, so that the later sections can be read as details of this overview.

### 1.1 Purpose and result

The synthesizer turns an arbitrary sound file, which is called the source sample in this document and in the window just "sample", into an output loop for the AVAS module. The output loop repeats without a click at the loop point and contains no audible change over its duration, while it keeps the tonal character of the source sample. The result sounds as if the source sample continued forever at a constant speed.

This behaviour is achieved by averaging the source sample over time, which removes every slow change of level and pitch. The averaging also removes every rhythm and every transient event of the source sample, so that a source sample whose character depends on such events cannot be reproduced by this method.

Apart from the optional artificial noise, the frequency and level of every steady tone and the whole noise spectrum are taken from the source sample, although the source sample itself is not copied into the output loop.

### 1.2 Signal path

The processing runs once from top to bottom for every new sound. The source sample is read again from the file each time, so that no intermediate result is reused between two parameter sets.

```
 source sample (WAV, FLAC, OGG, any rate, mono or stereo)
        |
        v
 mono, resample to the wave sample rate, remove DC, high-pass
        |
        v
 averaged spectrum (mean of all short-time power spectra)
        |
        v
 spectral envelope (median filter on a logarithmic frequency axis)
        |
        +---------------------------+
        v                           v
 steady tones                 noise texture              artificial noise
 (peaks above the envelope)   (spectrum without peaks)   (power-law spectrum)
        |  x tonal gain             |  x texture gain          |
        +-------------+-------------+                          |
                      v                                        |
          normalise to a peak of 1                             |
                      |                                        | x noise gain
                      +--------------------+-------------------+
                                           v
                       scale to the maximum wave peak
                                           |
                                           v
          output loop (mono) -> 16-bit stereo -> preview and WAV file
```

**Figure 1.2.1**: Signal path from the source sample to the output loop

Every block of the figure corresponds to one function in `synth_core.py`, and the function `synthesize` calls these functions in the shown order. The following table lists this assignment.

**Table 1.2.1**: Processing steps and their functions in `synth_core.py`

| Processing step | Function | Described in |
|---|---|---|
| Reading, conversion to mono, resampling | `load_and_resample` | 2.1 |
| High-pass filter | `highpass` | 2.1 |
| Averaged spectrum | `averaged_psd` | 2.2 |
| Spectral envelope | `spectral_envelope` | 2.3 |
| Separation of steady tones and noise texture | `find_tonal_peaks` | 2.4 |
| Synthesis of the steady tones | `synthesize_tones` | 2.5 |
| Synthesis of the noise texture | `synthesize_texture` | 2.5 |
| Synthesis of the artificial noise | `synthesize_periodic_noise` | 2.5 |
| Mix and normalisation | `synthesize` | 2.6 |
| Conversion to stereo and WAV output | `to_stereo_int16`, `save_avas_wav` | 2.7 |

## 2 Method

This section describes the calculation of each processing step of figure 1.2.1. Names in code format are the attributes of the class `SynthParams`, and section 3 explains how they are set in the window.

### 2.1 Preparing the source sample

A stereo source sample is first reduced to mono by averaging its channels. If the sample rate of the file differs from `sample_rate`, a polyphase filter (`resample_poly`) converts it, which means that the whole analysis already runs at the sample rate of the output loop. The frequency range of the analysis therefore ends at the Nyquist frequency of the output loop, which is 5512.5 Hz at 11025 Hz.

The mean value of the signal is subtracted afterwards, and a Butterworth high-pass filter of order 4 with the cutoff frequency `min_freq` removes the low frequency content. The filter runs forward and backward over the signal (`filtfilt`), so that it adds no phase shift and attenuates by 6 dB at the cutoff frequency.

### 2.2 Averaged spectrum

The function `averaged_psd` cuts the signal into overlapping frames of `fft_size` sample values with a hop of a quarter frame, and it multiplies each frame with a Hann window. A source sample that is shorter than one frame is filled up with zeros. The power spectrum of every frame is calculated by an FFT, and the mean over all frames forms the averaged spectrum, which is scaled to a power spectral density (PSD).

This averaging is the central step of the method. A tone that glides from 950 Hz to 1050 Hz during the source sample appears in the averaged spectrum as one wide bump, and all information about the time at which each frequency was present is lost.

The parameter `frame_weighting` decides how much the loud moments count. With the value 0, the plain mean of the power spectra is used, so that loud moments dominate the result. With the value 1, each frame is first normalised to the same energy, so that every moment counts equally. Values in between blend both results linearly.

### 2.3 Spectral envelope

The spectral envelope is the smooth curve below the peaks of the averaged spectrum and represents the noise floor of the source sample. The function `spectral_envelope` interpolates the spectrum in dB onto a logarithmic frequency grid with 96 points per octave and applies a median filter whose width is `tonal_sep_octaves` octaves, whereby the width is rounded to an odd number of grid points.

A median filter ignores a peak that covers less than half of its width. With the default of 0.33 octaves, a bump that is narrower than about 0.17 octaves therefore does not lift the envelope and can later be detected as a steady tone, whereas a wider bump raises the envelope and is treated as a part of the noise floor.

### 2.4 Detection of steady tones

The function `find_tonal_peaks` marks all bins within the band from `min_freq` to the effective maximum frequency whose value exceeds the envelope by more than `tonal_threshold_db`. Each group of contiguous marked bins forms one candidate, for which the following values are calculated with the excess being the difference between spectrum and envelope in the bin.

```
power    = sum(excess) * bin width
centroid = sum(frequency * excess) / sum(excess)
width    = number of bins * bin width
```

A candidate is discarded if its width exceeds `max_tonal_width_pct` percent of its centroid frequency, because such a wide bump is more likely a resonance in the noise than a tone. The remaining candidates are sorted by power, and only the strongest `max_tonals` of them are kept.

Each kept candidate becomes one steady tone at its centroid frequency, and its bins are replaced by the envelope in a copy of the spectrum. This copy is the noise texture. A discarded candidate keeps its bump in the noise texture, so that its energy is not lost and merely rendered as noise. The energy of the averaged spectrum is thereby divided between the steady tones and the noise texture without being counted twice.

A perfectly steady tone already occupies three to four bins because of the main lobe of the Hann window. The width of a candidate therefore has a lower limit that depends on `fft_size`, which table 3.3.1.1 lists for the available sizes.

### 2.5 Periodic synthesis of the three parts

All three parts of the sound are generated by an inverse FFT of exactly `n_frames` points, whose result is periodic with `n_frames` by mathematical definition. No crossfade is needed at the loop point for this reason. An inverse FFT of this length can only contain multiples of the loop base frequency f0.

```
f0 = sample_rate / n_frames
```

The default values result in f0 = 11025 / 28665 = 0.3846 Hz, so that a frequency is moved by at most 0.19 Hz.

The **steady tones** are placed in the bin that lies nearest to their centroid frequency, with an amplitude that gives the sine wave the measured power and with a random phase. If two steady tones fall into the same bin, which is only likely with a very short output loop, they are added.

The **noise texture** is created by interpolating the texture spectrum in dB onto the grid of f0. Each bin receives a magnitude from a Rayleigh distribution and a uniformly distributed phase, which produces Gaussian noise whose expected spectrum equals the noise texture. Bins outside the band from `min_freq` to the effective maximum frequency are set to zero. The steady tones and the noise texture use the same power scale, which is why their level ratio corresponds to the ratio in the source sample.

The **artificial noise** does not depend on the source sample. Each bin receives a random phase and the following magnitude, whereby the bins outside the band are also set to zero and the result is normalised to a peak of 1.

```
magnitude = (frequency / 1000 Hz) ^ noise_slope
```

All random values come from one generator with the fixed start value 42, so that the same parameters always produce exactly the same output loop. The generator is used in the order steady tones, noise texture, artificial noise, which means that a different number of steady tones also changes the random pattern of the noise parts, although their spectra remain the same.

### 2.6 Mix and normalisation

The output loop is calculated in two normalisation stages. The two parts that originate from the source sample are mixed first and normalised to a peak of 1, the artificial noise is then added, and the sum is finally scaled so that its peak equals `max_peak`.

```
sample_part = tonal_gain * tones + texture_gain * texture
sample_part = sample_part / peak(sample_part)
mix         = sample_part + noise_gain * noise
output      = mix * max_peak / peak(mix)
```

Three consequences of these formulas are important for the operation of the window.

- The values of `tonal_gain` and `texture_gain` only act through their ratio, so that the values 1.0 and 1.0 produce the same sound as 0.5 and 0.5.
- The value of `noise_gain` states the peak of the artificial noise relative to the peak of the part from the source sample, so that 0.3 means 30 % of that peak before the final scaling.
- The gains never change the peak of the output loop, which is always determined by `max_peak`. The perceived loudness can nevertheless change, because a mix of different composition has a different ratio of peak to average power.

If only the steady tones are enabled and no steady tone was found, the output loop is silent, because the validation only rejects the case that all three gains are zero.

### 2.7 Stereo output and playback

The function `to_stereo_int16` converts the mono output loop into 16-bit stereo, whereby the right channel is attenuated by the fixed factor 0.97 of `right_channel_gain`, like in the stock sound of the module. The preview plays this stereo signal directly from the memory through the class `LoopingPlayer`, which crossfades to a new sound at the current loop position, whereas the button "Save WAV as..." writes the same signal into a file.

## 3 GUI options

This section explains every control of the window from top to bottom. All frequencies and durations given as examples apply to the default format with 11025 Hz and 28665 frames.

### 3.1 Text inputs

The three text inputs define the format and the peak of the output loop. A new value takes effect when it is confirmed with Enter or when the input field loses the focus. The grey line below the fields shows the resulting loop duration, the loop base frequency f0 and the Nyquist frequency.

**Table 3.1.1**: Text inputs of the window

| Label in the window | Attribute | Default | Permitted values |
|---|---|---|---|
| Wave sample rate [Hz] | `sample_rate` | 11025 | at least 2000 |
| Wave number of frames | `n_frames` | 28665 | at least 256 |
| Maximum wave peak (0..1) | `max_peak` | 0.56 | above 0, at most 1 |

#### 3.1.1 Wave sample rate

This value is the sample rate of the output loop and of the whole analysis, as explained in 2.1. It limits the highest possible frequency to half of its value and also changes the frequency resolution of the analysis, which is the sample rate divided by the FFT size. The tool `flashBinGenerate.py` only accepts 11025 Hz, so that other values are merely useful for experiments. A change restarts the preview with a short gap, because the audio stream has to be opened again.

#### 3.1.2 Wave number of frames

This value sets the length of the output loop, which is 2.600 s with the default. A longer output loop makes the repetition of the noise texture harder to recognise and provides a finer grid of f0. The tool `flashBinGenerate.py` only accepts exactly 28665 frames, so that this value must remain unchanged for a sound that is written into the module.

#### 3.1.3 Maximum wave peak

The output loop is scaled so that its highest absolute value reaches this fraction of the full scale. It is the only control that sets the level of the output loop, and it has no influence on the spectrum.

### 3.2 Main sliders

The main sliders control the frequency band and the composition of the sound. Table 3.2.1 gives an overview of them, and the following sections explain their effect in detail.

**Table 3.2.1**: Main sliders of the window

| Label in the window | Attribute | Default | Range | Step |
|---|---|---|---|---|
| Minimum frequency [Hz] | `min_freq` | 100 | 2 to 2000 | 1 |
| Maximum frequency [Hz] | `max_freq` | 7000 | 500 to 20000 | 10 |
| Tonal threshold [dB above envelope] | `tonal_threshold_db` | 8 | 0 to 30 | 0.5 |
| Tonal gain (steady tones from sample) | `tonal_gain` | 1.0 | 0 to 1 | 0.01 |
| Texture gain (noise part of sample) | `texture_gain` | 1.0 | 0 to 1 | 0.01 |
| Artificial noise gain | `noise_gain` | 0.3 | 0 to 1 | 0.01 |
| Noise slope (-3 dark .. +3 bright) | `noise_slope` | -1.5 | -3 to +3 | 0.1 |

#### 3.2.1 Minimum frequency

This slider is the cutoff frequency of the high-pass filter and the lower limit of the band in which steady tones are searched and noise is generated. It has to be below the Nyquist frequency and below the maximum frequency, because otherwise the sound is rejected as invalid.

#### 3.2.2 Maximum frequency

This slider is the upper limit of the band for the steady tones and for both noise parts. Its value is internally limited to 0.99 times the Nyquist frequency, which is 5457 Hz at 11025 Hz, so that the default value of 7000 Hz has no effect in the default format and the band ends at 5457 Hz. The source sample is not low-pass filtered by this slider, because the frequencies above the limit are merely left out of the synthesis.

#### 3.2.3 Tonal threshold

This slider sets how far a peak has to rise above the spectral envelope to become a steady tone. A lower value turns more of the source sample into steady tones and leaves less for the noise texture, which sounds cleaner and more artificial. A higher value keeps only the very prominent peaks as steady tones, so that the sound becomes more noisy. The threshold moves energy between the two parts without changing the total energy that the source sample provides.

#### 3.2.4 Tonal gain and texture gain

These two sliders set the balance between the steady tones and the noise texture, which is 1.0 to 1.0 by default and thereby corresponds to the balance in the source sample. As explained in 2.6, only their ratio matters. To emphasise the steady tones, the texture gain has to be lowered, because the tonal gain cannot be raised above 1.0. The value 0 for one of the sliders removes the corresponding part completely.

#### 3.2.5 Artificial noise gain

This slider adds noise that is unrelated to the source sample and is useful to fill frequency bands in which the source sample has too little energy, for example for the requirements of the R138 check. As explained in 2.6, its value is relative to the peak of the part from the source sample. The magnitudes of the artificial noise follow a smooth curve without random scatter, so that its spectrum is more even than that of the noise texture.

#### 3.2.6 Noise slope

This slider sets the spectral tilt of the artificial noise and has no effect if the artificial noise gain is 0. Each unit changes the amplitude by about 6 dB per octave, so that the default of -1.5 falls by about 9 dB per octave. The value 0 corresponds to white noise and -0.5 to pink noise, whereas positive values raise the high frequencies. The reference frequency of 1000 Hz has no audible meaning, because the artificial noise is normalised to its peak afterwards.

### 3.3 Advanced options

The advanced options control how the source sample is analysed. Their defaults are suitable for most source samples, which is why they are only worth changing if the report or the analysis diagram of section 4 shows a problem. The following table lists the attributes and ranges of the selection box and the sliders.

**Table 3.3.1**: Advanced options of the window

| Label in the window | Attribute | Default | Range | Step |
|---|---|---|---|---|
| Analysis FFT size | `fft_size` | 4096 | 1024, 2048, 4096, 8192, 16384 | selection |
| Tonal separation width [octaves] | `tonal_sep_octaves` | 0.33 | 0.05 to 1.0 | 0.01 |
| Max. tone spread [% of frequency] | `max_tonal_width_pct` | 20 | 1 to 50 | 1 |
| Max. number of steady tones | `max_tonals` | 60 | 0 to 200 | 1 |
| Frame weighting (0 loud moments .. 1 equal) | `frame_weighting` | 0 | 0 to 1 | 0.05 |

#### 3.3.1 Analysis FFT size

The selection box sets the length of the analysis frames and thereby the balance between frequency resolution and the number of averaged frames. A larger size separates partials that lie close together, whereas a smaller size averages over more frames. A source sample that is not much longer than the frame provides only a few frames, so that the averaged spectrum becomes rough. With the default output format and a source sample of 2.6 s, the size 16384 only yields about three frames. The grey line below the box shows the resolution and the frame duration for the current values.

The following table lists the values for the sample rate of 11025 Hz. The two right columns follow from the main lobe of the Hann window explained in 2.4 and show that a small FFT size can exclude low steady tones through the limit of the maximum tone spread.

**Table 3.3.1.1**: Properties of the FFT sizes at 11025 Hz

| FFT size | Bin width | Frame duration | Minimum width of a steady tone | Lowest steady tone at a tone spread of 20 % |
|---|---|---|---|---|
| 1024 | 10.77 Hz | 93 ms | 32 to 43 Hz | about 215 Hz |
| 2048 | 5.38 Hz | 186 ms | 16 to 22 Hz | about 108 Hz |
| 4096 (default) | 2.69 Hz | 372 ms | 8 to 11 Hz | about 54 Hz |
| 8192 | 1.35 Hz | 743 ms | 4 to 5 Hz | about 27 Hz |
| 16384 | 0.67 Hz | 1486 ms | 2 to 3 Hz | about 13 Hz |

#### 3.3.2 Tonal separation width

This slider is the width of the median filter for the spectral envelope, as explained in 2.3. A wider filter lets wider bumps and groups of close partials stand above the envelope, so that they can become steady tones. A narrower filter lets the envelope follow the spectrum more closely, so that only very narrow peaks stand out. If many partials lie close together and cover more than half of the filter width, they lift the envelope themselves and are no longer detected.

#### 3.3.3 Max. tone spread

This slider sets the maximum width of a candidate relative to its frequency, as explained in 2.4. A source sample with a strong pitch glide, such as a Doppler shift of a passing vehicle, smears its tones, so that a higher value is needed to still collapse them to steady tones. A lower value leaves such smeared peaks in the noise texture.

#### 3.3.4 Max. number of steady tones

This slider limits the number of steady tones to the strongest ones. All candidates beyond the limit remain in the noise texture, and the value 0 disables the steady tones completely.

#### 3.3.5 Frame weighting

This slider decides which moments of the source sample shape the sound, as explained in 2.2. In a recording of a passing vehicle, the value 0 makes the closest point dominate the result, whereas the value 1 also gives the approach and the departure their full share.

### 3.4 Buttons

The buttons start the actions on the current sound. Except for "Open sample..." and "Reset defaults", they need a synthesized sound or at least an opened source sample and otherwise only show a hint.

**Table 3.4.1**: Buttons of the window

| Button | Action |
|---|---|
| Open sample... | Selects a WAV, FLAC or OGG file as the source sample and synthesizes the sound at once. |
| Reset defaults | Sets all text inputs and sliders to their defaults and keeps the opened source sample. |
| Apply (synthesize) | Synthesizes and plays the sound with the current values, even if no value has changed, and reports errors in a dialog. |
| Preview ▶ / ■ | Starts or stops the gapless playback of the last sound. |
| Check R138 (synthesized) | Writes the last sound to `<temp>/avas_synthesizer/last_synth.wav` and opens the R138 analyzer with this file, which is updated with every change as long as the analyzer window is open. |
| Check R138 (sample file) | Opens the R138 analyzer with the source sample itself, for example to compare it with the stock sound. |
| Show analysis | Opens the diagram of section 4.2, which is updated with every change as long as the diagram is open. |
| Save WAV as... | Writes the last sound as a 16-bit stereo WAV file and stores `<name>.params.json` next to it, which contains all parameters, the steady tones and the report. The proposed file name is `<source sample>_avas.wav`. |

The buttons "Check R138" and "Show analysis" need the Python package matplotlib, whereas the synthesis and the preview also work without it.

### 3.5 Time at which a change takes effect

The window synthesizes and plays the sound automatically whenever a parameter has really changed, so that the button "Apply" is rarely needed. This happens when a slider is released or moved with the keyboard, when a text input is confirmed or left, when an FFT size is selected, when the defaults are reset and when a source sample is opened. No calculation takes place while a slider is being dragged.

A stopped preview starts again with the next change, so that a stopped preview does not stay silent during further editing. The new sound replaces the old one through a crossfade at the current loop position and is heard about 0.3 s later. Invalid values are only shown in the status line at the bottom of the window with the text "Not applied", and the last valid sound continues to play.

## 4 Reading the results

This section explains the two outputs that show what the synthesizer has made of a source sample, which are the report in the log area and the analysis diagram.

### 4.1 Report in the log area

The area "Result / log" shows a new report after every synthesis. The following table explains its lines.

**Table 4.1.1**: Lines of the synthesis report

| Line | Meaning |
|---|---|
| Source, original rate | The file, its original sample rate, its duration and the number of analysis frames that were averaged. |
| Loop | The number of frames, the sample rate, the duration and the loop base frequency f0 of the output loop. |
| Band | The band that was really used, which shows the limited maximum frequency, and the frequency resolution of the analysis. |
| Tonals | The number of steady tones, the threshold, the limit and the share of the energy of the source sample within the band that became steady tones. This share does not depend on the gains. |
| Gains | The values of the three gains and of the noise slope. |
| Peak | The peak of the output loop, which equals the maximum wave peak if the output loop is not silent. |
| Level fluctuation inside the loop | The difference between the highest and the lowest short-time level in dB, measured over 125 ms windows across the loop point. A lower value means a steadier sound. |
| Steady tones | One line per steady tone with the centroid frequency, the synthesized frequency, the spread and the level relative to the strongest steady tone. |

The spread of a steady tone is the width of its candidate in the averaged spectrum. A value in the range of the column "Minimum width of a steady tone" of table 3.3.1.1 means that the tone was already steady in the source sample, whereas a clearly larger value means that its pitch or level was moving.

Gaussian noise inherently causes a level fluctuation of about 1 to 2 dB, whereas steady tones cause none. A value of several dB indicates a narrowband component in the noise texture that causes beats, for which section 5 gives a remedy.

### 4.2 Analysis diagram

The button "Show analysis" shows the averaged spectrum of the source sample on a logarithmic frequency axis, whereby the level is given in dB relative to its maximum. The diagram directly shows which parts of the spectrum were recognised as steady tones.

**Table 4.2.1**: Curves of the analysis diagram

| Curve | Meaning |
|---|---|
| Blue line | The averaged spectrum of the source sample. |
| Orange dashed line | The spectral envelope. |
| Orange dotted line | The envelope plus the tonal threshold, which a peak has to exceed. |
| Black line | The noise texture, which follows the blue line except at the peaks that became steady tones. |
| Green lines with a dot | The steady tones at their synthesized frequency, drawn with their power within one analysis bin. |

A peak that rises above the dotted line but carries no green line was either rejected because of its width or was beyond the maximum number of steady tones.

## 5 Tuning guide

This section connects typical observations with the options that influence them, so that the effect of section 2 and section 3 can be used in practice. The following table is derived from the calculation in the code, whereas the final judgement of a sound remains a matter of listening and of the R138 check.

**Table 5.1**: Typical observations and suitable changes

| Observation | Suitable change |
|---|---|
| A clearly audible tone of the source sample is missing. | Lower the tonal threshold, raise the maximum tone spread or raise the FFT size if the tone is low. |
| The sound is too whistling or too artificial. | Raise the tonal threshold or lower the tonal gain. |
| The sound is too noisy. | Lower the texture gain or the artificial noise gain. |
| The level fluctuation is several dB. | Lower the tonal threshold or widen the tonal separation width, so that the narrowband component becomes a steady tone. |
| The repetition of the loop can be recognised in the noise. | Lower the texture gain, because the steady tones contain nothing that repeats audibly. |
| The tone is smeared in a recording of a passing vehicle. | Raise the maximum tone spread, and raise the frame weighting if the closest point is too dominant. |
| A one-third octave band of the R138 check above about 2 kHz lacks level. | Raise the artificial noise gain and move the noise slope towards positive values. |
| A deep rumble disturbs the sound. | Raise the minimum frequency. |
| The sound is rejected with "Not applied". | Read the reason in the status line, which names the invalid value. |
