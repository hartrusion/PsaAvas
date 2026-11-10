#!/usr/bin/env python3
"""
looping_player.py - gapless, non-interrupting loop playback for AVAS previews.

Design
------
The AVAS sound is a short loop (about 2.6 s).  A preview must repeat it
without any gap or click, otherwise the ear judges the loop point and not the
sound.  Simply calling a "play file" command repeatedly always leaves a gap.

This player therefore streams raw PCM continuously into an audio sink.  On
Linux, a background thread writes the loop buffer over and over into the stdin
pipe of one of these commands (first one found wins):

    paplay   --raw ...         (PulseAudio / PipeWire-pulse)   <- preferred
    pw-play  --raw ...         (PipeWire native)
    aplay    -t raw ...        (ALSA)

On Windows, where none of these commands exists, the background thread feeds
the waveOut interface of the system library winmm instead, which needs no
additional installation.

Optionally, if the Python module `sounddevice` is installed, it is used
instead on both systems (callback based, lowest latency).  It is NOT required.

Buffer swapping: `set_buffer()` replaces the loop while it is playing.  The
new buffer takes over at the current loop position within one short crossfade,
so that a change is heard at once and without a click.  The audio that is still
queued in front of the sink is kept small for the same reason.  If the sample
rate changes, the sink process has to be restarted (short gap, unavoidable).

The environment variable AVAS_PLAYER_CMD can override the command used, for
testing without a sound card, e.g.  AVAS_PLAYER_CMD="cat > /dev/null" on Linux
or AVAS_PLAYER_CMD="findstr x > NUL" on Windows.
"""

from __future__ import annotations

import ctypes
import math
import os
import shlex
import shutil
import subprocess
import sys
import threading
from typing import Optional

import numpy as np

# Frames per pipe write, which is also the length of the crossfade to a new
# buffer (46 ms at 11025 Hz).  A small value lets stop() and set_buffer() react
# quickly.
CHUNK_FRAMES = 512

# Requested size of the pipe to the sink.  The default of 64 KiB would queue
# about 1.5 s of stereo audio at 11025 Hz in front of the sink.
PIPE_BUFFER_BYTES = 4096

# Value of sys.platform on every Windows version.
WINDOWS_PLATFORM = "win32"

# Audio queued in front of the Windows audio engine, which is long enough to
# survive a blocked Python thread and short enough to hear a change quickly.
WAVE_OUT_QUEUE_SECONDS = 0.2

# Interval in which the sink checks whether the audio engine returned a buffer.
WAVE_OUT_POLL_SECONDS = 0.005

# Constants of the waveOut interface as they are defined in mmeapi.h and mmreg.h.
WAVE_MAPPER = 0xFFFFFFFF            # lets Windows choose the default output device
WAVE_FORMAT_PCM = 1                 # format tag of uncompressed integer samples
CALLBACK_NULL = 0                   # no notification, the sink polls WHDR_DONE
WHDR_DONE = 0x00000001              # the audio engine has finished the buffer
MMSYSERR_NOERROR = 0                # result code of a successful call
MAXERRORLENGTH = 256                # characters of a waveOut error text

PCM_SAMPLE_BYTES = 2                # size of one s16le sample


def _limit_pipe_buffer(pipe) -> None:
    """Shrinks the kernel buffer of a pipe so that little audio is queued.

    The default size is kept where this is not supported, which only delays
    the audible reaction to a new buffer.

    LLM code generated with Claude Opus 5.5
    """
    try:
        import fcntl
        fcntl.fcntl(pipe.fileno(), fcntl.F_SETPIPE_SZ, PIPE_BUFFER_BYTES)
    except (ImportError, AttributeError, OSError):
        pass


def _find_backend(sample_rate: int, channels: int):
    """Return (name, argv) of the first available CLI sink."""
    override = os.environ.get("AVAS_PLAYER_CMD")
    if override:
        if sys.platform == WINDOWS_PLATFORM:
            return "override", ["cmd", "/c", override]
        return "override", ["sh", "-c", override]
    if shutil.which("paplay"):
        return "paplay", ["paplay", "--raw", "--format=s16le",
                          f"--rate={sample_rate}", f"--channels={channels}",
                          "--latency-msec=200", "--stream-name=AVAS preview"]
    if shutil.which("pw-play"):
        return "pw-play", ["pw-play", "--raw", "--format", "s16",
                           "--rate", str(sample_rate), "--channels", str(channels), "-"]
    if shutil.which("aplay"):
        return "aplay", ["aplay", "-q", "-t", "raw", "-f", "S16_LE",
                         "-r", str(sample_rate), "-c", str(channels), "-"]
    return None, None


class WaveOutFormat(ctypes.Structure):
    """Describes the PCM format of a stream as the WAVEFORMATEX structure.

    WaveOutSink passes this structure to waveOutOpen, for which its binary
    layout has to match the definition of Windows exactly.

    LLM code generated with Claude Opus 5.5
    """
    _fields_ = [("wFormatTag", ctypes.c_uint16),
                ("nChannels", ctypes.c_uint16),
                ("nSamplesPerSec", ctypes.c_uint32),
                ("nAvgBytesPerSec", ctypes.c_uint32),
                ("nBlockAlign", ctypes.c_uint16),
                ("wBitsPerSample", ctypes.c_uint16),
                ("cbSize", ctypes.c_uint16)]


class WaveOutHeader(ctypes.Structure):
    """Describes one audio buffer as the WAVEHDR structure of Windows.

    WaveOutSink owns one header per queued buffer and hands it to the audio
    engine, which signals the end of the playback through the flag WHDR_DONE in
    dwFlags, so that a header must stay at the same address while it is queued.

    LLM code generated with Claude Opus 5.5
    """
    _fields_ = [("lpData", ctypes.c_void_p),
                ("dwBufferLength", ctypes.c_uint32),
                ("dwBytesRecorded", ctypes.c_uint32),
                ("dwUser", ctypes.c_size_t),
                ("dwFlags", ctypes.c_uint32),
                ("dwLoops", ctypes.c_uint32),
                ("lpNext", ctypes.c_void_p),
                ("reserved", ctypes.c_size_t)]


class WaveOutSink:
    """Plays a continuous PCM stream on Windows through the waveOut interface.

    Windows has no command that accepts raw PCM on its standard input, which is
    why LoopingPlayer uses this class there in place of the sink process that
    it starts on Linux. The system library winmm is used through ctypes because
    it is part of every Windows installation, so that the preview needs no
    additional package. A small ring of buffers is queued at the audio engine
    so that the stream has no gaps, while the queue stays short enough for a
    new sound to be heard quickly. The write method blocks like a pipe to a
    sink process, which paces the thread of LoopingPlayer at the audio rate.

    LLM code generated with Claude Opus 5.5
    """

    def __init__(self, sample_rate: int, channels: int, chunk_frames: int,
                 stop_event: threading.Event):
        """Opens the default output device and prepares the ring of buffers.

        @param sample_rate sample rate of the stream in Hz
        @param channels number of interleaved channels
        @param chunk_frames frames that every call of write delivers
        @param stop_event ends a write that is waiting for a free buffer
        @throws OSError if winmm is missing or the device cannot be opened
        """
        self._stop_event = stop_event
        self._winmm = ctypes.WinDLL("winmm")
        self._declare_winmm_functions()

        frame_bytes = channels * PCM_SAMPLE_BYTES
        chunk_bytes = chunk_frames * frame_bytes
        buffer_count = math.ceil(WAVE_OUT_QUEUE_SECONDS * sample_rate / chunk_frames)
        # The memory of the buffers and headers is read by the audio engine,
        # which is why both are kept for the whole lifetime of the sink.
        self._buffers = [ctypes.create_string_buffer(chunk_bytes)
                         for _ in range(buffer_count)]
        self._headers = (WaveOutHeader * buffer_count)()
        # Index of the header that receives the next chunk of the stream.
        self._next_index = 0

        stream_format = WaveOutFormat(wFormatTag=WAVE_FORMAT_PCM,
                                      nChannels=channels,
                                      nSamplesPerSec=sample_rate,
                                      nAvgBytesPerSec=sample_rate * frame_bytes,
                                      nBlockAlign=frame_bytes,
                                      wBitsPerSample=PCM_SAMPLE_BYTES * 8,
                                      cbSize=0)
        self._handle = ctypes.c_void_p()
        self._raise_on_error(
            self._winmm.waveOutOpen(ctypes.byref(self._handle), WAVE_MAPPER,
                                    ctypes.byref(stream_format), 0, 0, CALLBACK_NULL),
            "waveOutOpen")
        try:
            for header, buffer in zip(self._headers, self._buffers):
                header.lpData = ctypes.addressof(buffer)
                header.dwBufferLength = chunk_bytes
                self._raise_on_error(
                    self._winmm.waveOutPrepareHeader(self._handle, ctypes.byref(header),
                                                     ctypes.sizeof(WaveOutHeader)),
                    "waveOutPrepareHeader")
                # Marks the buffer as free, because it was never queued yet.
                header.dwFlags |= WHDR_DONE
        except OSError:
            self.close()
            raise

    def _declare_winmm_functions(self) -> None:
        """Declares the argument types, without which ctypes would truncate
        the 64-bit device handle to the size of an integer."""
        header_pointer = ctypes.POINTER(WaveOutHeader)
        self._winmm.waveOutOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint,
                                            ctypes.POINTER(WaveOutFormat), ctypes.c_size_t,
                                            ctypes.c_size_t, ctypes.c_uint32]
        for function in (self._winmm.waveOutPrepareHeader, self._winmm.waveOutWrite,
                         self._winmm.waveOutUnprepareHeader):
            function.argtypes = [ctypes.c_void_p, header_pointer, ctypes.c_uint]
        self._winmm.waveOutReset.argtypes = [ctypes.c_void_p]
        self._winmm.waveOutClose.argtypes = [ctypes.c_void_p]
        self._winmm.waveOutGetErrorTextW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p,
                                                     ctypes.c_uint]

    def _raise_on_error(self, result_code: int, function_name: str) -> None:
        """Converts a failed waveOut result into an OSError with its Windows text."""
        if result_code == MMSYSERR_NOERROR:
            return
        error_text = ctypes.create_unicode_buffer(MAXERRORLENGTH)
        self._winmm.waveOutGetErrorTextW(result_code, error_text, MAXERRORLENGTH)
        raise OSError(f"{function_name} returned {result_code}: {error_text.value}")

    def write(self, pcm_bytes: bytes) -> None:
        """Queues one chunk and blocks until a buffer is free for it.

        A chunk that is still waiting when the stop event is set is dropped.

        @param pcm_bytes exactly one chunk of interleaved s16le samples
        @throws OSError if the audio engine rejects the buffer
        """
        header = self._headers[self._next_index]
        while not header.dwFlags & WHDR_DONE:
            if self._stop_event.wait(WAVE_OUT_POLL_SECONDS):
                return
        header.dwFlags &= ~WHDR_DONE
        ctypes.memmove(header.lpData, pcm_bytes, header.dwBufferLength)
        self._raise_on_error(
            self._winmm.waveOutWrite(self._handle, ctypes.byref(header),
                                     ctypes.sizeof(WaveOutHeader)),
            "waveOutWrite")
        self._next_index = (self._next_index + 1) % len(self._headers)

    def close(self) -> None:
        """Stops the playback at once and releases the device and its buffers."""
        self._winmm.waveOutReset(self._handle)
        for header in self._headers:
            self._winmm.waveOutUnprepareHeader(self._handle, ctypes.byref(header),
                                               ctypes.sizeof(WaveOutHeader))
        self._winmm.waveOutClose(self._handle)


class LoopingPlayer:
    """Gapless loop player.  Thread safe for start/stop/set_buffer."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pcm: Optional[bytes] = None       # interleaved s16le
        self._sample_rate = 11025
        self._channels = 2
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self._stop = threading.Event()
        self._sd_stream = None
        self._active_pcm: Optional[bytes] = None   # buffer that is currently audible
        self._position = 0                         # next frame of the active buffer
        self.backend_name = "none"
        self.last_error = ""

    # ---------------------------------------------------------------- public

    @staticmethod
    def available_backends() -> str:
        names = []
        try:
            import sounddevice  # noqa: F401
            names.append("sounddevice")
        except Exception:
            pass
        if sys.platform == WINDOWS_PLATFORM:
            names.append("waveOut")
        for cmd in ("paplay", "pw-play", "aplay"):
            if shutil.which(cmd):
                names.append(cmd)
        if os.environ.get("AVAS_PLAYER_CMD"):
            names.insert(0, "override")
        return ", ".join(names) if names else "none"

    def set_buffer(self, pcm_int16: np.ndarray, sample_rate: int) -> None:
        """pcm_int16: (n, channels) or (n,) int16 array.

        Takes effect at once through a crossfade.  Restarts the stream if the
        sample rate or channel count changed while playing."""
        arr = np.asarray(pcm_int16, dtype=np.int16)
        if arr.ndim == 1:
            arr = arr[:, None]
        channels = arr.shape[1]
        restart = False
        with self._lock:
            if self.is_playing and (sample_rate != self._sample_rate
                                    or channels != self._channels):
                restart = True
            self._pcm = arr.tobytes()
            self._sample_rate = int(sample_rate)
            self._channels = channels
        if restart:
            self.stop()
            self.start()

    @property
    def is_playing(self) -> bool:
        return self._thread is not None and self._thread.is_alive() or self._sd_stream is not None

    @property
    def has_buffer(self) -> bool:
        return self._pcm is not None

    def start(self) -> bool:
        if self.is_playing:
            return True
        if self._pcm is None:
            self.last_error = "No sound buffer set."
            return False
        self._active_pcm = None
        self._position = 0
        if self._try_start_sounddevice():
            return True
        if sys.platform == WINDOWS_PLATFORM and not os.environ.get("AVAS_PLAYER_CMD"):
            return self._start_wave_out()
        return self._start_pipe()

    def stop(self) -> None:
        self._stop.set()
        if self._sd_stream is not None:
            try:
                self._sd_stream.stop()
                self._sd_stream.close()
            except Exception:
                pass
            self._sd_stream = None
        proc = self._proc
        if proc is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None
        self._proc = None
        self._stop.clear()

    def toggle(self) -> bool:
        """Start if stopped, stop if playing.  Returns new playing state."""
        if self.is_playing:
            self.stop()
            return False
        return self.start()

    # --------------------------------------------------------------- backends

    def _read_next_frames(self, frame_count: int) -> np.ndarray:
        """Returns the next frames of the loop as (frame_count, channels) array.

        A buffer that was set since the last call replaces the active buffer
        by a linear crossfade over the returned frames.  Called only from the
        audio thread of the active backend, which is why the position needs no
        lock.

        LLM code generated with Claude Opus 5.5
        """
        with self._lock:
            newest_pcm = self._pcm
            channels = self._channels
        if self._active_pcm is None:
            self._active_pcm = newest_pcm

        active = np.frombuffer(self._active_pcm, dtype=np.int16).reshape(-1, channels)
        indices = np.arange(self._position, self._position + frame_count)
        frames = np.take(active, indices, axis=0, mode="wrap")

        if newest_pcm is not self._active_pcm:
            newest = np.frombuffer(newest_pcm, dtype=np.int16).reshape(-1, channels)
            fade_in = np.linspace(0.0, 1.0, frame_count)[:, None]
            frames_new = np.take(newest, indices, axis=0, mode="wrap")
            frames = (frames * (1.0 - fade_in) + frames_new * fade_in).astype(np.int16)
            self._active_pcm = newest_pcm
            active = newest

        self._position = (self._position + frame_count) % len(active)
        return frames

    def _try_start_sounddevice(self) -> bool:
        if os.environ.get("AVAS_PLAYER_CMD"):
            return False
        try:
            import sounddevice as sd
        except Exception:
            return False
        try:
            def callback(outdata, frames, time_info, status):
                outdata[:] = self._read_next_frames(frames)

            self._sd_stream = sd.OutputStream(samplerate=self._sample_rate,
                                              channels=self._channels, dtype="int16",
                                              callback=callback)
            self._sd_stream.start()
            self.backend_name = "sounddevice"
            return True
        except Exception as e:  # pragma: no cover
            self.last_error = f"sounddevice failed: {e}"
            self._sd_stream = None
            return False

    def _start_wave_out(self) -> bool:
        """Starts the playback through the waveOut interface of Windows.

        LLM code generated with Claude Opus 5.5

        @return False if the output device could not be opened
        """
        self._stop.clear()
        try:
            sink = WaveOutSink(self._sample_rate, self._channels, CHUNK_FRAMES, self._stop)
        except OSError as error:
            self.last_error = f"Could not open the Windows audio output: {error}"
            return False
        self.backend_name = "waveOut"
        self._thread = threading.Thread(target=self._pump_wave_out, args=(sink,), daemon=True)
        self._thread.start()
        return True

    def _pump_wave_out(self, sink: WaveOutSink) -> None:
        """Feeds the sink until stop() is called, in the thread of the playback.

        The sink is closed here so that it is never released during a write.

        LLM code generated with Claude Opus 5.5
        """
        try:
            while not self._stop.is_set():
                sink.write(self._read_next_frames(CHUNK_FRAMES).tobytes())
        except OSError as error:
            self.last_error = f"waveOut stopped: {error}"
        finally:
            sink.close()

    def _start_pipe(self) -> bool:
        name, argv = _find_backend(self._sample_rate, self._channels)
        if argv is None:
            self.last_error = "No audio backend found (need paplay, pw-play or aplay)."
            return False
        try:
            self._proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                          stdout=subprocess.DEVNULL,
                                          stderr=subprocess.PIPE)
        except Exception as e:
            self.last_error = f"Could not start {name}: {e}"
            return False
        _limit_pipe_buffer(self._proc.stdin)
        self.backend_name = name
        self._stop.clear()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        return True

    def _pump(self) -> None:
        proc = self._proc
        try:
            while not self._stop.is_set():
                proc.stdin.write(self._read_next_frames(CHUNK_FRAMES).tobytes())
                proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as e:
            if not self._stop.is_set():
                err = b""
                try:
                    err = proc.stderr.read() if proc.stderr else b""
                except Exception:
                    pass
                self.last_error = f"{self.backend_name} stopped: {e} {err.decode(errors='ignore').strip()}"


# ------------------------------------------------------------------------------

if __name__ == "__main__":
    # Smoke test: play a WAV file in a loop for a few seconds.
    import sys
    import time
    import soundfile as sf
    if len(sys.argv) < 2:
        print("usage: looping_player.py file.wav [seconds]")
        sys.exit(1)
    data, sr = sf.read(sys.argv[1], dtype="int16", always_2d=True)
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
    pl = LoopingPlayer()
    print("backends:", pl.available_backends())
    pl.set_buffer(data, sr)
    if not pl.start():
        print("start failed:", pl.last_error)
        sys.exit(2)
    print("playing with", pl.backend_name, "for", seconds, "s")
    time.sleep(seconds)
    pl.stop()
    print("stopped.", pl.last_error or "")
