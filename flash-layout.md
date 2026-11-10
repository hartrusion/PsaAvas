# SPI Flash Layout of the AVAS Sound Memory

This document describes how the sound data of the FIAMM A004BR AVAS module is stored in its external SPI NOR flash, which is an ISSI IS25LP016 with 16 Mbit (2 MB) at designator U10. The description is based on an unmodified flash dump of such a module, which was read with a CH341A programmer and is called the stock dump in this document. Statements marked with **[hypothesis]** are plausible, but they cannot be proven from the flash content alone.

## 1 Overview

The flash dump contains six sound slots of equal size at the beginning of the flash, while the rest of the flash is erased. The sound data is stored as raw PCM without any WAV or RIFF file structure, so that the slot layout described in section 3 is the only structure on the flash.

**Table 1.1**: General properties of the stock flash dump

| Property | Value |
|---|---|
| File size | 2 097 152 bytes (`0x200000`), which is the full 16 Mbit chip |
| SHA-256 | `3869ae3a788eda33c9677a995b9a4427e66970787e981e466d1803b7cf186cb7` |
| Used area | `0x000000`–`0x0BFFFF` (768 KB) |
| Erased area | `0x0C0000`–`0x1FFFFF`, all bytes `0xFF` |
| File system or WAV header | none (the strings `RIFF`, `WAVE`, `fmt `, `data` and `LIST` do not occur) |
| Audio format | signed 16-bit PCM, stereo interleaved, big-endian in the flash view (see 3.1) |
| Sample rate | not stored in the flash (see 4.1) |
| Integrity check | one CRC-32 at the end of each sound slot (see 3.3) |

## 2 Memory Map

The used area of the flash is divided into a fixed grid of six sound slots with `0x20000` bytes (128 KB) each, which are followed by the erased remainder of the chip. Slot n starts at the address `S = n × 0x20000` with n = 0…5. Since the telecoding record with the UDS data identifier 2101 of the module selects one of six sounds, each sound slot is assumed to hold one of these sounds **[hypothesis]**.

```
0x000000 ┌──────────────────────────────┐
         │ slot 0  (128 KB)  sound 1    │
0x020000 ├──────────────────────────────┤
         │ slot 1            sound 2    │
0x040000 ├──────────────────────────────┤
         │ slot 2            sound 3    │
0x060000 ├──────────────────────────────┤
         │ slot 3            sound 4    │
0x080000 ├──────────────────────────────┤
         │ slot 4            sound 5    │
0x0A0000 ├──────────────────────────────┤
         │ slot 5            sound 6    │
0x0C0000 ├──────────────────────────────┤
         │ erased (0xFF), 1.25 MB       │
0x200000 └──────────────────────────────┘
```

**Figure 2.1**: Memory map of the SPI flash with the six sound slots

The following table lists the absolute flash addresses of the four slot parts, which are explained in section 3.1, for the stock audio length of 114 660 bytes. In the stock dump, all six sound slots are byte-identical including their slot length header and slot CRC, so that all six sound selections refer to the same sound data. The tested module nevertheless played only the sounds 1 and 2, whereas it accepted the selections 3 to 6 but stayed silent.

**Table 2.1**: Absolute flash addresses of the slot parts in the stock dump

| Slot / sound | Slot length header | Audio data | Padding | Slot CRC |
|---|---|---|---|---|
| 0 / 1 | `0x000000`–`0x000003` | `0x000004`–`0x01BFE7` | `0x01BFE8`–`0x01FFFB` | `0x01FFFC`–`0x01FFFF` |
| 1 / 2 | `0x020000`–`0x020003` | `0x020004`–`0x03BFE7` | `0x03BFE8`–`0x03FFFB` | `0x03FFFC`–`0x03FFFF` |
| 2 / 3 | `0x040000`–`0x040003` | `0x040004`–`0x05BFE7` | `0x05BFE8`–`0x05FFFB` | `0x05FFFC`–`0x05FFFF` |
| 3 / 4 | `0x060000`–`0x060003` | `0x060004`–`0x07BFE7` | `0x07BFE8`–`0x07FFFB` | `0x07FFFC`–`0x07FFFF` |
| 4 / 5 | `0x080000`–`0x080003` | `0x080004`–`0x09BFE7` | `0x09BFE8`–`0x09FFFB` | `0x09FFFC`–`0x09FFFF` |
| 5 / 6 | `0x0A0000`–`0x0A0003` | `0x0A0004`–`0x0BBFE7` | `0x0BBFE8`–`0x0BFFFB` | `0x0BFFFC`–`0x0BFFFF` |

## 3 Sound Slot Layout

Each sound slot consists of a slot length header, the audio data, a padding area and a slot CRC. The slot can be described in two equivalent ways, which are the flash view of the bytes as they are stored on the chip (3.1) and the host view, in which every 4-byte group is byte-reversed (3.2). The host view is the simpler description, because it turns the slot into a plain little-endian structure.

### 3.1 Flash View

The flash view describes the bytes in the order in which they are read from the chip with a programmer. In this view, the slot length header, the audio samples and the slot CRC all appear in big-endian byte order. The offsets in the following table are relative to the slot start S.

**Table 3.1.1**: Sound slot layout in the flash view

| Offset | Length | Content |
|---|---|---|
| `0x00000` | 4 | slot length header as u32 big-endian, which holds the number of audio bytes that follow (stock value `00 01 BF E4` = 114 660) |
| `0x00004` | 114 660 (`0x1BFE4`) in the stock dump | audio data as stereo frames, each of which consists of two s16 big-endian values (stock dump: 28 665 stereo frames) |
| `0x1BFE8` | 16 404 (`0x4014`) in the stock dump | padding, all bytes `0xFF` |
| `0x1FFFC` | 4 | slot CRC as u32 big-endian over the slot bytes `0x00000`–`0x1FFFB` (stock value `82 05 35 D6`, see 3.3) |

### 3.2 Host View

The host view is obtained when every 4-byte group of the whole slot is byte-reversed, which turns the slot into an ordinary little-endian structure. Because the length, the samples and the CRC are all affected by the same 32-bit word swap, this view explains why all three appear big-endian in the flash view. The slot can therefore be understood as a little-endian sound image that was written to the flash with every 32-bit word byte-swapped.

**Table 3.2.1**: Sound slot layout in the host view

| Offset | Content |
|---|---|
| `0x00000` | slot length header as u32 little-endian (stock value 114 660) |
| `0x00004` | audio data as s16 little-endian stereo samples |
| `0x1BFE8` (stock) | padding, all bytes `0xFF` |
| `0x1FFFC` | slot CRC as u32 little-endian, which is the zlib CRC-32 of the host view bytes `0x00000`–`0x1FFFB` |

The reason for the word swap cannot be read from the flash. The microcontroller is presumably an NXP S32K142, whose Cortex-M4F core is little-endian **[hypothesis]**. A plausible cause is therefore that the sound image is transferred through the SPI peripheral in 32-bit frames that are shifted out with the most significant byte first, so that each little-endian word from the memory ends up byte-reversed on the flash **[hypothesis]**.

### 3.3 Slot CRC

The slot CRC protects the complete sound slot except for its last four bytes, which hold the slot CRC itself. It is the standard CRC-32 as used by zlib, PNG and Ethernet (CRC-32/ISO-HDLC with the reflected polynomial `0x04C11DB7`, the initial value `0xFFFFFFFF` and the final XOR value `0xFFFFFFFF`), which is computed over the host view of the first `0x1FFFC` bytes of the slot. The result is stored as u32 little-endian in the host view, which corresponds to u32 big-endian in the flash view. The following Python code checks the slot CRC of a sound slot that was read in the flash view.

```python
import zlib
import numpy as np

def swap32(data):
    return np.frombuffer(data, dtype=">u4").astype("<u4").tobytes()

slot = flash[S:S + 0x20000]
assert zlib.crc32(swap32(slot[:0x1FFFC])) == int.from_bytes(slot[0x1FFFC:], "big")
```

**Figure 3.3.1**: Python check of the slot CRC on a slot in the flash view

The slot CRC always covers exactly `0x1FFFC` bytes, so that it includes the `0xFF` padding and is independent of the audio length. For comparison, the same calculation over the slot length header and the audio data alone (`0x1BFE8` bytes) results in `0xFC3B960F`, whereas the calculation over `0x1FFF8` bytes results in `0x62F1755C`. Neither the CRC over the unswapped flash view (`0x5206EBD8`) nor a CRC over 16-bit swapped words matches the stored value, which shows that the 32-bit word swap is an essential part of the rule.

The firmware verifies the slot CRC, because the module stays silent when the slot CRC of the selected sound does not match.

### 3.4 Constraints for Modified Sound Slots

A modified sound slot has to keep the structure of the host view, which means the slot length header, the audio data, the `0xFF` padding up to offset `0x1FFFC` and a valid slot CRC, before the whole slot is word-swapped into the flash view. The audio data of one slot can be at most `0x1FFF8` bytes (131 064 bytes) long, which corresponds to 32 766 stereo frames. Since the length of the audio data has to be a multiple of four bytes for whole stereo frames, the 4-byte grid of the word swap is kept automatically. It is not known whether the firmware accepts audio lengths that differ from the stock value of 114 660 bytes.

## 4 Audio Data

The audio data in each sound slot is uncompressed linear PCM, and it is played by the module in a loop. This section describes the sample format, the measured properties of the stock sound, the channel order and the non-audio values that occur when the used flash area is read as one contiguous audio stream.

### 4.1 Sample Format

The sample format is fixed by the flash content except for the sample rate, which is not stored in the flash and is set by the firmware through the I2S clock. The value of 11 025 Hz results in a plausible sound when the data is played in Audacity, but it has not been confirmed in any other way.

**Table 4.1.1**: Sample format of the audio data

| Property | Value | Confidence |
|---|---|---|
| Encoding | signed 16-bit linear PCM | confirmed by listening and by the byte statistics, which show a structured high byte (134 distinct values) and a uniformly distributed low byte (all 256 values) |
| Byte order | big-endian in the flash view and little-endian in the host view | confirmed |
| Channels | two channels, which are interleaved as one stereo frame of two values | confirmed by listening |
| Sample rate | unknown, 11 025 Hz is assumed from listening | open |
| Frames per sound (stock) | 28 665, which is 114 660 bytes divided by four | confirmed by the slot length header |
| Duration (stock) | 2.600 s at 11 025 Hz, 1.791 s at 16 000 Hz or 1.300 s at 22 050 Hz | depends on the unknown sample rate |

The data is presumably stored as two channels because the audio DAC, which is presumably a TI PCM5100A with a stereo I2S input, always receives two channels per I2S frame **[hypothesis]**. Since the class D amplifier, which is presumably a TI TPA3111, drives only one speaker, it is not known whether the speaker receives one of the two channels or a mix of both.

### 4.2 Properties of the Stock Sound

The following values were measured on the audio data of the stock dump, in which all six sound slots contain the same sound. The channel names "first" and "second" refer to the order of the two s16 values inside a stereo frame in the flash view.

**Table 4.2.1**: Measured properties of the stock sound

| Property | Value |
|---|---|
| Value range | first channel −15 916 … +18 241, second channel −16 838 … +17 576 |
| Highest amplitude | 18 241 in the first channel at frame 228 (slot offset `0x394`), which is 55.7 % of full scale or −5.09 dBFS |
| Most negative value | −16 838 in the second channel at frame 21 962 |
| RMS | 4 538 per channel (−17.2 dBFS) |
| Amplitude distribution | 34.8 % of all 57 330 samples are below 2 048 and 63.1 % are below 4 096, while only 15 samples reach 16 384 or more |
| Channel relation | the correlation of both channels is 0.951 at equal RMS, although the channels are not identical (maximum difference 5 840, only 14 of 28 665 frames are equal) |
| Loop seam | the last frame (−4 443, −3 817) and the first frame (−4 080, −4 482) differ by 514, which is a quarter of the mean step between consecutive samples (2 172), so that the sound is presumably designed as a seamless loop |
| Fundamental | the autocorrelation of the first channel peaks at a lag of 31 frames, which results in a fundamental of the sample rate divided by 31 (355.6 Hz at 11 025 Hz or 516 Hz at 16 000 Hz) |

The stock sound uses a little more than half of the 16-bit range, which leaves a headroom of about 5 dB.

### 4.3 Channel Order

The order of the two values inside a stereo frame depends on whether the flash view or the host view is used, because the 32-bit word swap exchanges the two 16-bit values of each frame. The first value of a frame in the flash view is therefore the second value of the same frame in the host view. If the original sound file followed the common PC convention of s16 little-endian samples with the left channel first, the first channel of the flash view is the right channel of the original file **[hypothesis]**. Throughout this document, the channel names "first" and "second" always refer to the flash view.

### 4.4 Non-Audio Values in a Contiguous Readout

When the used flash area is imported into an audio editor as one contiguous stream, the slot length header and the slot CRC appear as stereo frames that do not belong to the audio. Each of these 4-byte fields has exactly the size of one stereo frame, and because the slot CRC of one slot is directly followed by the slot length header of the next slot, sounds 2 to 6 begin with two non-audio frames, whereas sound 1 begins with only one. The slot CRC at `0x0BFFFC` is the last non-audio frame after sound 6.

**Table 4.4.1**: Values of the non-audio fields in the stock dump

| Bytes in flash view | Position | Value as u32 big-endian | Value as s16 big-endian pair | Meaning |
|---|---|---|---|---|
| `00 01 BF E4` | `S + 0x00000` | 114 660 (`0x0001BFE4`) | 1, −16 412 | slot length header |
| `82 05 35 D6` | `S + 0x1FFFC` | 2 181 379 542 (`0x820535D6`) | −32 251, 13 782 | slot CRC |

Both fields are audible as a short click, because their values are large in comparison with the quiet start of the audio data, whose first frame is (−4 080, −4 482). The audio of a single sound slot can be imported without these fields in Audacity with File → Import → Raw Data, using the settings "Signed 16-bit PCM", "Big-endian", two channels, the start offset `S + 4` and a length of 114 660 bytes.

## 5 Content Not Stored in the Flash

The external flash holds only the sound slots, while all other information that is needed for playback is defined by the firmware of the microcontroller. In particular, the flash contains no file system, directory or header table, because the slot positions follow the fixed 128 KB grid. It also contains no sample rate or channel information, no firmware code and no checksum over the whole flash image apart from the six slot CRCs.

## 6 Open Questions

The following questions about the sound data cannot be answered from the flash content and remain open.

- It is not known whether the speaker receives the first channel, the second channel or a mix of both, and whether the first channel is the left or the right channel of the original sound (see 4.3).
- The real sample rate of the playback is not known (see 4.1).
- It is not known whether the firmware accepts audio lengths per sound slot that differ from the stock value of 114 660 bytes.
