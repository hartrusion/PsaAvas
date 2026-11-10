# Copyright (C) 2026 Viktor Alexander Hartung
#
# This program is free software: you can redistribute it and/or modify it under the terms of the
# GNU General Public License as published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without
# even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If
# not, see <http://www.gnu.org/licenses/>.
#
# ORIGIN AND CREDITS
#
# The seed/key algorithm in this file is not the work of the author of this file, who merely
# translated it into Python. It is the work of the following authors, who published it before this
# project existed:
#
#   Original algorithm by Wouter Bokslag & Jason F.
#   <https://github.com/prototux>
#
#   Implementations: Copyright 2020, Ludwig V. <https://github.com/ludwig-v>
#   https://github.com/ludwig-v/psa-seedkey-algorithm
#
#   This program is free software: you can redistribute it and/or modify it under the terms of the
#   GNU General Public License as published by the Free Software Foundation, either version 3 of
#   the License, or (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
#   without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See
#   the GNU General Public License at <http://www.gnu.org/licenses/> for more details.
#
#   The above copyright notice and this permission notice shall be included in all copies or
#   substantial portions of the Software.
#
#   Pseudocode PSA_SEED_KEY in diag/protocols/UDS.yml and the ECU constants in the ecu.yml files of
#   PSA-RE <https://github.com/prototux/PSA-RE>, licensed under the Apache License 2.0.
#
# The implementations of Ludwig V. name Wouter Bokslag and Jason F. (prototux) as the authors of
# the original algorithm, and PSA-RE thanks Wouter Bokslag for his reverse engineering of the PSA
# immobilizer. Ludwig V. published the algorithm as implementations in C, C#, PHP, JavaScript and
# for Arduino, together with a Python port by contributors, in the repository
# ludwig-v/psa-seedkey-algorithm under the GNU General Public License version 3 or later. That
# repository also lists the 16-bit constants of many ECUs in its file ECU_KEYS.md. The project
# PSA-RE, which is licensed under the Apache License 2.0, describes the same algorithm as
# pseudocode PSA_SEED_KEY in diag/protocols/UDS.yml and gives the constant of each ECU model in the
# security_access entries of its ecu.yml files, e.g. C504 for the pedestrian warning sound
# generator (AVAS) of the Corsa F.
#
# The functions in this file are a translation of that pseudocode into Python, whose structure,
# constants and steps are the same as in the function compute_response of algorithm.c by
# Ludwig V., so that it is a derived work of the implementations above and is distributed under
# the GNU General Public License version 3 as they require. The only own contribution of this file
# is the translation and the check of the keys against a real ECU.
"""Seed/key computation of the PSA control units for the UDS service SecurityAccess.

A PSA control unit accepts writing requests only after the tester has requested a seed with an odd
SecurityAccess sub-function and has answered with a key that is computed from the seed and a 16-bit
constant of the control unit family. The configuration session of AvasCanUtil uses this module to
unlock the AVAS module before it writes the telecoding record.

LLM code generated with Claude Opus 5.5
"""

# Number of bytes of the seed and of the key.
PSA_SEED_KEY_LENGTH = 4

# Coefficients (divisor, quotient factor, remainder factor) of the first transformation.
FIRST_TRANSFORM_COEFFICIENTS = (0xB2, 0x3F, 0xAA)

# Coefficients (divisor, quotient factor, remainder factor) of the second transformation.
SECOND_TRANSFORM_COEFFICIENTS = (0xB1, 0x02, 0xAB)


def transform_psa_word(word, coefficients):
    """Applies one transformation of the algorithm to a 16-bit word that is read as a signed number.

    The algorithm was defined in C, whose integer division truncates toward zero, whereas the
    operator // of Python rounds down, so that the quotient is calculated on the absolute value.

    @param word unsigned 16-bit input value
    @param coefficients one of the coefficient tuples of this module
    @return unsigned 16-bit result
    """
    divisor, quotient_factor, remainder_factor = coefficients
    signed_word = word
    if signed_word >= 0x8000:
        signed_word -= 0x10000
    quotient = abs(signed_word) // divisor
    if signed_word < 0:
        quotient = -quotient
    remainder = signed_word - quotient * divisor
    result = remainder * remainder_factor - quotient * quotient_factor
    if result < 0:
        result += divisor * remainder_factor + quotient_factor
    return result & 0xFFFF


def compute_psa_key(seed, constant):
    """Computes the key that answers a seed of a PSA control unit.

    @param seed the four seed bytes in the order of the response
    @param constant 16-bit constant of the control unit family
    @return the four key bytes
    """
    if len(seed) != PSA_SEED_KEY_LENGTH:
        raise ValueError(f"The seed has {len(seed)} bytes instead of {PSA_SEED_KEY_LENGTH}.")
    outer_seed_word = seed[0] << 8 | seed[3]
    inner_seed_word = seed[1] << 8 | seed[2]
    key_high_word = (transform_psa_word(constant, FIRST_TRANSFORM_COEFFICIENTS)
                     | transform_psa_word(outer_seed_word, SECOND_TRANSFORM_COEFFICIENTS))
    key_low_word = (transform_psa_word(inner_seed_word, FIRST_TRANSFORM_COEFFICIENTS)
                    | transform_psa_word(key_high_word, SECOND_TRANSFORM_COEFFICIENTS))
    return bytes([key_high_word >> 8, key_high_word & 0xFF, key_low_word >> 8, key_low_word & 0xFF])
