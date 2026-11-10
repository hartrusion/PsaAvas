#!/bin/bash
# can-up.sh - activates the one connected CANable as a socketcan interface for AvasCanUtil on Linux.
#
# The setup of AvasCanUtil consists only of one CANable that is wired to the AVAS module, so that the
# adapter is not selected by a serial number. The script refuses to continue if it does not find
# exactly one CANable, because it could otherwise transmit on the wrong bus.
#
# Both CANable firmwares are supported. With the slcan firmware, the adapter is a serial device for
# which slcand creates the socketcan interface. With the candleLight firmware, the gs_usb driver has
# already created the interface, so that only the bitrate has to be set.
#
# LLM code generated with Claude Opus 5.5
#
# Usage: sudo ./can-up.sh
#        sudo ./can-up.sh down

# Socketcan interface that is created for the slcan firmware, which is the default of the program.
SLCAN_INTERFACE=can0
# Bitrate of the CAN bus of the AVAS module in bit/s.
BITRATE=500000
# Number that selects 500 kbit/s in the slcan firmware.
SLCAN_BITRATE_CODE=6

set -e

if [ "$(id -u)" != 0 ]; then
    echo "Run with sudo." >&2
    exit 1
fi

# Stops only the slcand process that serves the interface of this script.
stop_slcand() {
    pkill -f "slcand .* $SLCAN_INTERFACE$" 2>/dev/null && sleep 0.5 || true
}

if [ "${1:-}" = down ]; then
    ip link set "$SLCAN_INTERFACE" down 2>/dev/null || true
    stop_slcand
    echo "$SLCAN_INTERFACE down."
    exit 0
fi

modprobe can
modprobe can_raw

# Serial devices of CANables with the slcan firmware, of which the USB product name holds "CANable".
mapfile -t SERIAL_DEVICES < <(ls /dev/serial/by-id/ 2>/dev/null | grep -i canable || true)

# Network interfaces of CANables with the candleLight firmware, which are served by the gs_usb driver.
NATIVE_INTERFACES=()
for INTERFACE_DIRECTORY in /sys/class/net/can*; do
    if [ "$(basename "$(readlink -f "$INTERFACE_DIRECTORY/device/driver" 2>/dev/null)")" = gs_usb ]; then
        NATIVE_INTERFACES+=("$(basename "$INTERFACE_DIRECTORY")")
    fi
done

ADAPTER_COUNT=$((${#SERIAL_DEVICES[@]} + ${#NATIVE_INTERFACES[@]}))
if [ "$ADAPTER_COUNT" != 1 ]; then
    echo "Exactly one CANable has to be connected, but $ADAPTER_COUNT were found." >&2
    echo "Serial devices: ${SERIAL_DEVICES[*]:-none}" >&2
    echo "Native interfaces: ${NATIVE_INTERFACES[*]:-none}" >&2
    exit 1
fi

if [ "${#SERIAL_DEVICES[@]}" = 1 ]; then
    if ! command -v slcand >/dev/null; then
        echo "slcand not found, install it with: sudo apt install can-utils" >&2
        exit 1
    fi
    INTERFACE=$SLCAN_INTERFACE
    SERIAL_DEVICE=/dev/serial/by-id/${SERIAL_DEVICES[0]}
    echo "Using slcan firmware on $SERIAL_DEVICE -> $INTERFACE."
    modprobe slcan
    ip link set "$INTERFACE" down 2>/dev/null || true
    stop_slcand
    slcand -o -c -f -s$SLCAN_BITRATE_CODE "$SERIAL_DEVICE" "$INTERFACE"
    sleep 0.5
    ip link set "$INTERFACE" txqueuelen 1000 up
else
    INTERFACE=${NATIVE_INTERFACES[0]}
    echo "Using candleLight firmware on $INTERFACE."
    ip link set "$INTERFACE" down
    # The option restart-ms lets the interface recover from bus-off by itself.
    ip link set "$INTERFACE" type can bitrate "$BITRATE" listen-only off restart-ms 100
    ip link set "$INTERFACE" txqueuelen 1000 up
fi

ip -d -s link show "$INTERFACE"
echo
echo "The CANable is active. Start the program with: ./avas_can_util.py --channel $INTERFACE"
