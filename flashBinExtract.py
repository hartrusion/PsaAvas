#!/usr/bin/env python3
"""
Extracts the six sounds of an AVAS flash image as WAV files.

The FIAMM A004BR AVAS module stores its sounds as raw PCM in six sound slots
of an external SPI NOR flash, whose layout is described in flash-layout.md.
This script reads a flash image, which is a full 2 MB dump of that flash, and
writes the audio data of each sound slot to one WAV file. The written WAV files
have exactly the format that flashBinGenerate.py requires, so that an extracted
sound can be put back into a new flash image.

Without arguments, the script reads flash.bin and writes sound1.wav ...
sound6.wav in the current working directory, whereas the option -i names another
flash image and the option -o names the six WAV files in the order of the slots:

    flashBinExtract.py -i backup.bin -o a.wav b.wav c.wav d.wav e.wav f.wav

The audio data in the flash view is signed 16-bit PCM in big-endian byte order
with two interleaved channels. It is converted with the same word swap that
flashBinGenerate.py applies, so that the channel order survives when extracted
sounds are built into a new flash image. The sample rate of 11025 Hz is an
assumption because the flash does not store it.

LLM code generated with Claude Opus 5.5
"""

import argparse
import struct
import wave
from pathlib import Path

# Flash image that is read unless the command line names another file.
DEFAULT_FLASH_IMAGE_FILE = Path("flash.bin")

# Size of a complete dump of the 16 Mbit flash in bytes.
FLASH_IMAGE_SIZE = 0x200000

# Number of sound slots at the start of the flash.
SOUND_SLOT_COUNT = 6

# WAV files, one per sound slot, that are written unless the command line names
# other files.
DEFAULT_SOUND_FILES = [
    Path(f"sound{sound_number}.wav")
    for sound_number in range(1, SOUND_SLOT_COUNT + 1)
]

# Size of one sound slot, which is also the grid of the slot start addresses.
SOUND_SLOT_SIZE = 0x20000

# Size of the slot length header in front of the audio data in bytes.
SLOT_LENGTH_HEADER_SIZE = 4

# Offset of the slot CRC from the start of a sound slot.
SLOT_CRC_OFFSET = 0x1FFFC

# Slot length header of the stock flash in the flash view, which holds the
# stock audio length.
STOCK_SLOT_LENGTH_HEADER = bytes.fromhex("0001BFE4")

# Slot CRC of the stock flash in the flash view, which only matches as long as
# the sound slot holds the stock sound.
STOCK_SLOT_CRC = bytes.fromhex("820535D6")

# Audio length of one sound slot in bytes, which is 0x1BFE4 in the stock flash.
AUDIO_BYTES = 114660

# Assumed playback sample rate of the module in Hz.
SAMPLE_RATE = 11025

# Number of interleaved channels of the audio data.
CHANNEL_COUNT = 2

# Size of one sample of one channel in bytes.
SAMPLE_WIDTH_BYTES = 2

# Size of the words that are byte-reversed between flash view and host view.
WORD_SIZE_BYTES = 4

# Number of stereo frames that are written to each WAV file.
FRAME_COUNT = AUDIO_BYTES // (CHANNEL_COUNT * SAMPLE_WIDTH_BYTES)


def swap_bytes_of_32_bit_words(data: bytes) -> bytes:
    """Reverses the bytes of every 4-byte group, which converts audio data from
    the flash view to the host view. Besides the byte order of the samples,
    this also exchanges the two samples of every stereo frame."""
    word_count = len(data) // WORD_SIZE_BYTES
    words = struct.unpack(f">{word_count}I", data)
    return struct.pack(f"<{word_count}I", *words)


def write_sound_file(path: Path, little_endian_pcm: bytes) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(CHANNEL_COUNT)
        wav_file.setsampwidth(SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(little_endian_pcm)


def extract_sound_slot(flash_image: bytes, slot_index: int) -> bytes:
    """Returns the little-endian PCM of one sound slot and prints a warning
    for every slot field that differs from the stock flash.

    @param flash_image complete flash image in the flash view
    @param slot_index index of the sound slot from 0 to 5
    @return audio data of the sound slot as little-endian PCM
    """
    slot_start = slot_index * SOUND_SLOT_SIZE
    audio_start = slot_start + SLOT_LENGTH_HEADER_SIZE
    crc_start = slot_start + SLOT_CRC_OFFSET

    slot_length_header = flash_image[slot_start:audio_start]
    audio_data = flash_image[audio_start:audio_start + AUDIO_BYTES]
    slot_crc = flash_image[crc_start:slot_start + SOUND_SLOT_SIZE]

    if slot_length_header != STOCK_SLOT_LENGTH_HEADER:
        print(
            f"Warning: The slot length header {slot_length_header.hex()} of "
            f"sound slot {slot_index} differs from the stock value "
            f"{STOCK_SLOT_LENGTH_HEADER.hex()}, although this script always "
            f"extracts {AUDIO_BYTES} bytes of audio data."
        )
    if slot_crc != STOCK_SLOT_CRC:
        print(
            f"Warning: The slot CRC {slot_crc.hex()} of sound slot "
            f"{slot_index} differs from the stock value "
            f"{STOCK_SLOT_CRC.hex()}, which is expected if the sound slot "
            f"holds a modified sound."
        )

    return swap_bytes_of_32_bit_words(audio_data)


def parse_command_line_arguments() -> argparse.Namespace:
    """Reads the file names from the command line, whereas each option that is
    not given keeps its default file names.

    @return namespace with the Path "input" and the list of Paths "output"
    """
    parser = argparse.ArgumentParser(
        description="Extracts the six sounds of an AVAS flash image as WAV files."
    )
    parser.add_argument(
        "-i", "--input", type=Path, default=DEFAULT_FLASH_IMAGE_FILE,
        metavar="FLASH_IMAGE",
        help=f"flash image to read (default: {DEFAULT_FLASH_IMAGE_FILE})",
    )
    parser.add_argument(
        "-o", "--output", type=Path, nargs=SOUND_SLOT_COUNT,
        default=DEFAULT_SOUND_FILES, metavar="WAV_FILE",
        help=f"the {SOUND_SLOT_COUNT} WAV files to write in the order of the "
             f"sound slots (default: sound1.wav ... "
             f"sound{SOUND_SLOT_COUNT}.wav)",
    )
    return parser.parse_args()


def main() -> None:
    arguments = parse_command_line_arguments()

    try:
        flash_image = arguments.input.read_bytes()
    except OSError as error:
        raise SystemExit(
            f"Error: The flash image {arguments.input} could not be read "
            f"({error})."
        )

    if len(flash_image) != FLASH_IMAGE_SIZE:
        print(
            f"Warning: The flash image has {len(flash_image)} bytes "
            f"instead of {FLASH_IMAGE_SIZE} bytes."
        )

    for slot_index, sound_file in enumerate(arguments.output):
        little_endian_pcm = extract_sound_slot(flash_image, slot_index)
        try:
            write_sound_file(sound_file, little_endian_pcm)
        except OSError as error:
            raise SystemExit(
                f"Error: The sound file {sound_file} could not be written "
                f"({error})."
            )
        print(
            f"Written: {sound_file} ({FRAME_COUNT} stereo frames, "
            f"{AUDIO_BYTES} bytes of PCM)"
        )


if __name__ == "__main__":
    main()
