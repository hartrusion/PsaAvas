<#
.SYNOPSIS
  Finds the one connected CANable on Windows and starts AvasCanUtil on its COM port.
.DESCRIPTION
  Windows has no socketcan, so that nothing can be activated in advance and AvasCanUtil itself opens the CANable
  through its COM port with the slcan protocol. This script finds that COM port by the USB identifier of the
  CANable 2.0 slcan firmware and refuses to continue unless it finds exactly one adapter, because the program could
  otherwise transmit on the wrong bus. The candleLight firmware has no COM port and is therefore not supported here.
.PARAMETER ComPort
  Uses this COM port instead of searching for the adapter, for example for a CANable with a different USB identifier.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File can-up.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File can-up.ps1 -ComPort COM5
.NOTES
  Needs Python 3 with tkinter (part of the python.org installer), python-can and pyserial:
  python -m pip install python-can pyserial
  LLM code generated with Claude Opus 5.5.
#>
param(
    [string]$ComPort = ''
)

# USB vendor and product identifier of the CANable 2.0 slcan firmware in the notation of the Windows device identifier.
$CanableUsbId = 'VID_16D0&PID_117E'

# The py launcher is optional in the python.org installer, so that python.exe is used when it is missing. The
# python.exe of WindowsApps is only a stub that opens the Microsoft Store when no Python is installed.
$python = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $python = @('py', '-3')
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = @('python')
} else {
    Write-Error 'Python 3 was not found, because neither the py launcher nor python.exe is on the PATH.'
    exit 1
}
$exe = $python[0]
$pyArgs = @($python | Select-Object -Skip 1)

& $exe @pyArgs -c 'import sys; assert sys.version_info >= (3, 8)' 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error "'$exe' is not a working Python 3 (the WindowsApps stub without an installed Python?)."
    exit 1
}
& $exe @pyArgs -c 'import can, serial, tkinter' 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error "python-can, pyserial or tkinter is missing, install them with: $exe $($pyArgs -join ' ') -m pip install python-can pyserial"
    exit 1
}

if ($ComPort -eq '') {
    # Every serial port is listed by Windows with its COM number in parentheses at the end of the device name.
    $adapters = @(Get-CimInstance Win32_PnPEntity -Filter "Name LIKE '%(COM%'" |
        Where-Object { $_.DeviceID -like "*$CanableUsbId*" })
    if ($adapters.Count -ne 1) {
        Write-Error "Exactly one CANable with the slcan firmware has to be connected, but $($adapters.Count) were found. Use -ComPort if the adapter has a different USB identifier."
        $adapters | ForEach-Object { Write-Host "  $($_.Name)" }
        exit 1
    }
    if ($adapters[0].Name -notmatch '\((COM\d+)\)') {
        Write-Error "The COM port could not be read from the device name '$($adapters[0].Name)'."
        exit 1
    }
    $ComPort = $Matches[1]
}

Write-Host "Starting AvasCanUtil on $ComPort."
& $exe @pyArgs (Join-Path $PSScriptRoot 'avas_can_util.py') --bus slcan --channel $ComPort
exit $LASTEXITCODE
