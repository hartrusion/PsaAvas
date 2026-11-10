#!/usr/bin/env python3
"""
Builds a flash image for the AVAS module from six WAV files.

The FIAMM A004BR AVAS module plays its sounds from six sound slots of an
external SPI NOR flash, and it stays silent if the slot CRC of the selected
sound does not match, as described in flash-layout.md. This script reads six
WAV files and writes a complete flash image, which has a valid slot length
header and slot CRC in each sound slot and can be written to the flash with a
programmer.

Without arguments, the script reads sound1.wav ... sound6.wav and writes
flash-new.bin in the current working directory, whereas the option -i names the
six WAV files in the order of the slots and the option -o names the flash image:

    flashBinGenerate.py -i a.wav b.wav c.wav d.wav e.wav f.wav -o image.bin

Every WAV file has to be 16-bit PCM stereo at 11025 Hz with exactly 28665
stereo frames, which is the format that the "Save WAV as..." button of the
Synthesizer writes with its default settings and that flashBinExtract.py
writes. The script checks all six files before it writes anything and lists
every mismatch at once, so that a file with a wrong format never ends up in a
flash image.

LLM code generated with Claude Opus 5.5
"""

import argparse
import struct
import wave
import zlib
from pathlib import Path

import numpy as np

# Flash image that is written unless the command line names another file.
DEFAULT_FLASH_IMAGE_FILE = Path("flash-new.bin")

# Size of a complete image of the 16 Mbit flash in bytes.
FLASH_IMAGE_SIZE = 0x200000

# Number of sound slots at the start of the flash.
SOUND_SLOT_COUNT = 6

# WAV files, one per sound slot, that are read unless the command line names
# other files.
DEFAULT_SOUND_FILES = [
    Path(f"sound{sound_number}.wav")
    for sound_number in range(1, SOUND_SLOT_COUNT + 1)
]

# Size of one sound slot, which is also the grid of the slot start addresses.
SOUND_SLOT_SIZE = 0x20000

# Offset of the slot CRC from the start of a sound slot.
SLOT_CRC_OFFSET = 0x1FFFC

# Size of the slot length header in front of the audio data in bytes.
SLOT_LENGTH_HEADER_SIZE = 4

# Byte value of the padding behind the audio data and of the erased flash area.
ERASED_FLASH_BYTE = b"\xFF"

# Audio length of one sound slot in bytes, which is 0x1BFE4 in the stock flash.
# It is fixed because it is not known whether the firmware accepts other lengths.
AUDIO_BYTES = 114660

# Assumed playback sample rate of the module in Hz.
SAMPLE_RATE = 11025

# Number of interleaved channels of the audio data.
CHANNEL_COUNT = 2

# Size of one sample of one channel in bytes.
SAMPLE_WIDTH_BYTES = 2

# Number of stereo frames that each WAV file has to hold.
FRAME_COUNT = AUDIO_BYTES // (CHANNEL_COUNT * SAMPLE_WIDTH_BYTES)


def find_wav_format_problems(wav_file: wave.Wave_read) -> list[str]:
    """Compares the WAV header with the audio format of a sound slot.

    @param wav_file opened WAV file
    @return one description per mismatch, which is empty for a matching file
    """
    problems = []
    if wav_file.getnchannels() != CHANNEL_COUNT:
        problems.append(
            f"The file has {wav_file.getnchannels()} channel(s), whereas a "
            f"sound slot requires {CHANNEL_COUNT} channels (stereo)."
        )
    if wav_file.getsampwidth() != SAMPLE_WIDTH_BYTES:
        problems.append(
            f"The file has {wav_file.getsampwidth() * 8}-bit samples, whereas "
            f"a sound slot requires {SAMPLE_WIDTH_BYTES * 8}-bit samples."
        )
    if wav_file.getframerate() != SAMPLE_RATE:
        problems.append(
            f"The file has a sample rate of {wav_file.getframerate()} Hz, "
            f"whereas a sound slot requires {SAMPLE_RATE} Hz."
        )
    if wav_file.getnframes() != FRAME_COUNT:
        problems.append(
            f"The file has {wav_file.getnframes()} frames, whereas a sound "
            f"slot requires exactly {FRAME_COUNT} frames."
        )
    return problems


def read_sound_file(path: Path) -> tuple[bytes, list[str]]:
    """Reads the little-endian PCM of a WAV file and checks its format.

    Unreadable files are reported as problems instead of exceptions, so that
    the caller can list the problems of all sound files at once.

    @param path WAV file to read
    @return PCM data, which is only usable if the list of problems is empty,
            and the list of problems
    """
    if not path.exists():
        return b"", ["The file does not exist."]
    try:
        with wave.open(str(path), "rb") as wav_file:
            problems = find_wav_format_problems(wav_file)
            if problems:
                return b"", problems
            little_endian_pcm = wav_file.readframes(FRAME_COUNT)
    except EOFError:
        return b"", ["The file is empty or ends inside its WAV header."]
    except (wave.Error, OSError) as error:
        return b"", [f"The file is not a readable PCM WAV file ({error})."]

    if len(little_endian_pcm) != AUDIO_BYTES:
        return b"", [
            f"The file holds only {len(little_endian_pcm)} bytes of PCM "
            f"although its header announces {AUDIO_BYTES} bytes."
        ]
    return little_endian_pcm, []


def format_sound_file_problems(problems_by_file: dict[Path, list[str]]) -> str:
    """Builds the error message that tells the user what has to be corrected.

    @param problems_by_file problems of every rejected sound file
    @return complete message for the console
    """
    lines = [
        f"Error: The flash image was not written because "
        f"{len(problems_by_file)} of {SOUND_SLOT_COUNT} sound files cannot be "
        f"used.",
        "",
    ]
    for path, problems in problems_by_file.items():
        lines.append(f"{path}:")
        lines.extend(f"  - {problem}" for problem in problems)
    lines.extend([
        "",
        f"Every sound file has to be a {SAMPLE_WIDTH_BYTES * 8}-bit PCM WAV "
        f"file in stereo with {SAMPLE_RATE} Hz and exactly {FRAME_COUNT} "
        f"frames, which is {AUDIO_BYTES} bytes of PCM.",
        "The Synthesizer writes this format with its default sample rate and "
        "number of frames, and flashBinExtract.py writes it from a flash image.",
    ])
    return "\n".join(lines)


def swap_bytes_of_32_bit_words(data: bytes) -> bytes:
    """Reverses the bytes of every 4-byte group, which converts a sound slot
    between the host view and the flash view in both directions."""
    return np.frombuffer(data, dtype=">u4").astype("<u4").tobytes()


def build_sound_slot(little_endian_pcm: bytes) -> bytes:
    """Builds one sound slot in the flash view.

    The sound slot is first assembled in the host view from the u32
    little-endian slot length header, the PCM and the padding. The slot CRC
    is the zlib CRC-32 of these bytes and is appended as u32 little-endian,
    before the complete sound slot is word-swapped into the flash view.

    @param little_endian_pcm audio data with exactly AUDIO_BYTES bytes
    @return sound slot with SOUND_SLOT_SIZE bytes in the flash view
    """
    slot_length_header = struct.pack("<I", AUDIO_BYTES)
    padding_size = SLOT_CRC_OFFSET - SLOT_LENGTH_HEADER_SIZE - AUDIO_BYTES
    host_view = slot_length_header + little_endian_pcm + ERASED_FLASH_BYTE * padding_size
    slot_crc = struct.pack("<I", zlib.crc32(host_view))
    return swap_bytes_of_32_bit_words(host_view + slot_crc)


def parse_command_line_arguments() -> argparse.Namespace:
    """Reads the file names from the command line, whereas each option that is
    not given keeps its default file names.

    @return namespace with the list of Paths "input" and the Path "output"
    """
    parser = argparse.ArgumentParser(
        description="Builds a flash image for the AVAS module from six WAV files."
    )
    parser.add_argument(
        "-i", "--input", type=Path, nargs=SOUND_SLOT_COUNT,
        default=DEFAULT_SOUND_FILES, metavar="WAV_FILE",
        help=f"the {SOUND_SLOT_COUNT} WAV files to read in the order of the "
             f"sound slots (default: sound1.wav ... "
             f"sound{SOUND_SLOT_COUNT}.wav)",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=DEFAULT_FLASH_IMAGE_FILE,
        metavar="FLASH_IMAGE",
        help=f"flash image to write (default: {DEFAULT_FLASH_IMAGE_FILE})",
    )
    return parser.parse_args()


def main() -> None:
    arguments = parse_command_line_arguments()

    sound_slots = []
    problems_by_file = {}
    for sound_file in arguments.input:
        print(f"Reading {sound_file} ...")
        little_endian_pcm, problems = read_sound_file(sound_file)
        if problems:
            problems_by_file[sound_file] = problems
        else:
            sound_slots.append(build_sound_slot(little_endian_pcm))

    if problems_by_file:
        raise SystemExit(format_sound_file_problems(problems_by_file))

    erased_area_size = FLASH_IMAGE_SIZE - SOUND_SLOT_COUNT * SOUND_SLOT_SIZE
    flash_image = b"".join(sound_slots) + ERASED_FLASH_BYTE * erased_area_size
    try:
        arguments.output.write_bytes(flash_image)
    except OSError as error:
        raise SystemExit(
            f"Error: The flash image {arguments.output} could not be written "
            f"({error})."
        )
    print(f"Written: {arguments.output} ({len(flash_image)} bytes)")
    print("The flash image is ready to be written to the IS25LP016 SPI NOR flash.")


if __name__ == "__main__":
    main()
