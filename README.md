# PsaAvas

Various tools to change and test the AVAS sound of the Corsa-e F and similar PSA vehicles.

This is a non-maintained side project. It is not actively maintained but the provided code and script should do the job.

**Legal note:** The vehicles sound is part of the R138 homologation. Any change of the vehicles sound will void the homologation and needs testing for R138 compliance. There are test tracks designed for this purpose. The provided synthesizer does only check for proper frequency requirement but not the overall result!

Use for testing, closed road or non-car use only **OR** with proper authorized and certified measurements.

All code in this repository is LLM generated with Claude Opus 5.5.

## AVAS Hardware

The Corsa-e F uses an FIAMM A004BR Acoustic Vehicle Alert System.

* 9839315880
* HW REV - 1
* SW REV - 1.2
* FIAMM AVAS - GEN 1 - CEM01
* PSW3

There seems to be no possibility to upload different sound effects to the A004BR via CAN, however, the sound data is stored as raw PCM in an external SPI NOR flash (ISSI IS25LP016, 16 Mbit), which can be accessed with a **SOIC8 test clip** and a **CH341A** programmer modified for 3.3 V.

**6** audio samples are stored in the SPI flash. The memory layout is described in `flash-layout.md`.

Flash can be read with `flashrom -p ch341a_spi -r backup.bin` and written with `flashrom -p ch341a_spi -w flash-new.bin`.

Note that for some reasons not known, only Sound 1 and Sound 2 are working, even if the module is not modified.

## Audio format

Signed 16-bit PCM, Big-Endian, Stereo (L/R interleaved), 11025 Hz

The AVAS module plays the WAV with a sample rate depending on vehicle speed, the 11025 Hz value was found by trying different rates and listening to the output. It matches the sound of the Corsa-e on slowest speeds.

## Provided Tools

* flashBinExtract.py: Extracts all 6 WAV files from a provided .bin file. Without arguments, it reads `flash.bin` and writes `sound1.wav` to `sound6.wav` in the current directory.
* flashBinGenerate.py: Generates a new flash image from 6 provided audio samples, each of which has to be a 16-bit PCM stereo WAV file with 11025 Hz and exactly 28665 frames. The script lists every mismatch and writes no file unless all 6 samples match. Without arguments, it reads `sound1.wav` to `sound6.wav` and writes `flash-new.bin` in the current directory.
* AvasCanUtil: Standalone program with a GUI that operates an AVAS module on the table through one CANable, as described below
* Synthesizer: Analyzes a given audio sample and turns it into a continuous loop, checks the spectrum for R138 compliance as far as possible.

Both flash scripts accept other file names with `-i`/`--input` and `-o`/`--output`, of which the option for the WAV files always takes exactly 6 names in the order of the sound slots:

    python3 flashBinExtract.py -i backup.bin -o a.wav b.wav c.wav d.wav e.wav f.wav
    python3 flashBinGenerate.py -i a.wav b.wav c.wav d.wav e.wav f.wav -o flash-new.bin

### AvasCanUtil

AvasCanUtil needs only one CANable that is wired to the CAN pins of the AVAS module with 120 Ohm termination and a 12 V supply for the module, so that no part of the vehicle is required. The program plays the sound of the module along an adjustable speed ramp or as a constant tone from 0 to 50 km/h, and its stop button returns the simulated speed to 0 km/h at once. "Start Config" ends the simulated CAN frames, because the module refuses SecurityAccess unless the vehicle is at standstill, and it opens the UDS extended session with SecurityAccess after 5 s of silence, so that the forward volume, the reverse volume and the sound can be changed and written to the DID 2101 of the module. The button "Restore Defaults" enters the factory values into the fields, which reach the module only with the write button. The button "End Config Session" returns the module to the default session, which also happens when a ramp or a constant tone is started.

The program requires Python 3 with tkinter, python-can and pyserial. On Linux, the CANable is activated first, whereas on Windows the script finds the COM port and starts the program by itself:

    # Linux
    cd AvasCanUtil
    sudo ./can-up.sh
    ./avas_can_util.py
    sudo ./can-up.sh down

    # Windows
    powershell -ExecutionPolicy Bypass -File AvasCanUtil\can-up.ps1

Both scripts refuse to continue unless exactly one CANable is connected. The Windows script requires the slcan firmware of the CANable 2.0 and accepts `-ComPort COM5` for any other slcan adapter.

The file `psa_seed_key.py` is a translation of a GPL-licensed algorithm and is therefore licensed under the GPL version 3, as its header explains.

### Synthesizer

Tools to design a replacement sound for the Corsa F AVAS module and to
pre-check it against UNECE Regulation No 138 before going to the test track.

Everything is Python 3 (Debian and Windows 11), GUI with Tk, analysis with
numpy/scipy, plotting with matplotlib, playback through PulseAudio/PipeWire on
Linux and through the waveOut interface of winmm on Windows.

| File | Purpose |
|---|---|
| `avas_synth_gui.py` | Main GUI: open sample, set parameters, Apply, gapless Preview, Check R138, Save |
| `synth_core.py` | Synthesizer library: time-averaged STFT, steady tones + noise texture, periodic resynthesis |
| `looping_player.py` | Gapless, non-interrupting loop playback (paplay / pw-play / aplay pipe on Linux, waveOut on Windows, optional sounddevice) |
| `r138_analyzer.py` | R138 one-third octave analyzer: library, CLI and GUI window |
| `SYNTHESIZER_MANUAL.md` | Method of the synthesizer and detailed explanation of all GUI options |

**Run**

```
python3 avas_synth_gui.py                 # Start GUI
python3 avas_synth_gui.py sample.wav      # Start GUI and open file as Sample
python3 r138_analyzer.py                  # analyzer only, file dialog
python3 r138_analyzer.py sound1.wav --level 55
python3 r138_analyzer.py sound1.wav --no-gui --png out.png --report out.md   # headless
python3 synth_core.py sample.wav out.wav  # synthesize with defaults, no GUI
```

The headless analyzer exits with code 0 when compliant and 1 otherwise, so it
can be used in scripts.

### Required Python Packages

All tools need Python 3.9 or newer. The following table lists the packages that have to be installed for each tool in addition to the standard library, together with the names of the corresponding Debian packages for an installation without pip.

| Tool | pip packages | Debian packages | Remarks |
|---|---|---|---|
| `flashBinExtract.py` | none | none | uses only the standard library |
| `flashBinGenerate.py` | `numpy` | `python3-numpy` | |
| AvasCanUtil | `python-can`, `pyserial` | `python3-can`, `python3-serial`, `python3-tk`, `can-utils` | `pyserial` is only needed for the slcan backend, which is always used on Windows. `can-utils` provides `slcand` for the CANable with slcan firmware on Linux. |
| Synthesizer | `numpy`, `scipy`, `soundfile`, `matplotlib` | `python3-numpy`, `python3-scipy`, `python3-soundfile`, `python3-matplotlib`, `python3-tk` | `matplotlib` is only needed for "Check R138", "Show analysis" and `r138_analyzer.py`. The optional package `sounddevice` is used for the preview if it is installed. On Linux, the preview otherwise needs one of the commands `paplay`, `pw-play` or `aplay`. |

The module tkinter is part of the python.org installer on Windows, whereas it has to be installed as the package `python3-tk` on Debian. Note that the PyPI package `can` is an unrelated package and must not be installed in place of `python-can`, because both provide a module with the same name. All packages for every tool can be installed at once with:

    python3 -m pip install numpy scipy soundfile matplotlib python-can pyserial

## Audio Examples

A few WAV files are provided in `ExampleSounds`. Those were generated using the provided synthesizer. R143, R179 and R211 are sounds from the New York subway, ICEBr403 is a german high speed train departing and there's also some audio that mimics the famous Mad Max interceptor.

## Acknowledgments

This work would not be possible without the [PSA-RE](https://github.com/prototux/PSA-RE) and the [PSA Seed/Key Algorithm](https://github.com/ludwig-v/psa-seedkey-algorithm) projects.