#!/usr/bin/env python3
"""
Pair the "ASUS Zenbook Duo Keyboard" over Bluetooth using a chosen adapter.

The keyboard uses the "Passkey Entry" pairing method: BlueZ shows a 6-digit
code that must be typed on the keyboard itself, followed by Enter. This script
drives bluetoothctl through a pty so that its interactive agent works, scans
for the keyboard, starts pairing, and prints the passkey in a way that is hard
to miss.

Usage:
    ./pair-zenbook-keyboard.py                  # scan and pair
    ./pair-zenbook-keyboard.py --adapter hci0   # force an adapter
    ./pair-zenbook-keyboard.py --mac XX:XX:...  # skip the scan
    ./pair-zenbook-keyboard.py --remove         # forget the keyboard first
    ./pair-zenbook-keyboard.py --name "Other"   # match another device name
"""

import argparse
import os
import pty
import re
import select
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_NAME = "ASUS Zenbook Duo Keyboard"
SCAN_TIMEOUT = 45
PAIR_TIMEOUT = 120

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[=>]|\r")
MAC = r"([0-9A-F]{2}(?::[0-9A-F]{2}){5})"

BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
OFF = "\033[0m"


def log(msg, color=CYAN):
    print(f"{color}::{OFF} {msg}", flush=True)


def die(msg):
    print(f"{RED}error:{OFF} {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def run(*args):
    return subprocess.run(args, capture_output=True, text=True).stdout


class Bluetoothctl:
    """bluetoothctl running on a pty, so its interactive agent stays alive."""

    def __init__(self, debug=False):
        self.debug = debug
        self.buf = ""
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # child
            os.environ["TERM"] = "dumb"
            os.execvp("bluetoothctl", ["bluetoothctl"])
        time.sleep(0.5)

    def send(self, cmd):
        if self.debug:
            print(f"{YELLOW}>{OFF} {cmd}", flush=True)
        os.write(self.fd, (cmd + "\n").encode())
        time.sleep(0.3)

    def read(self, timeout):
        """Read whatever arrived within `timeout`; returns new text only."""
        out = ""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r, _, _ = select.select([self.fd], [], [], 0.2)
            if not r:
                continue
            try:
                chunk = os.read(self.fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            text = ANSI.sub("", chunk.decode(errors="replace"))
            out += text
            if self.debug:
                sys.stdout.write(text)
                sys.stdout.flush()
        self.buf += out
        return out

    def expect(self, patterns, timeout):
        """Wait for the first matching pattern. Returns (key, match) or None."""
        deadline = time.monotonic() + timeout
        pending = ""
        while time.monotonic() < deadline:
            pending += self.read(min(1.0, max(0.1, deadline - time.monotonic())))
            for key, rx in patterns.items():
                m = re.search(rx, pending, re.IGNORECASE)
                if m:
                    return key, m
        return None

    def close(self):
        try:
            self.send("quit")
            time.sleep(0.3)
            os.close(self.fd)
        except OSError:
            pass


def list_adapters():
    """Return [(hciN, MAC, is_usb)] for every local controller."""
    out = []
    # `btmgmt info` is the only reliable hciN -> address mapping: recent
    # kernels no longer expose /sys/class/bluetooth/hciN/address.
    info = run("btmgmt", "info")
    for hci, addr in re.findall(
        rf"^(hci\d+):.*?\n\s+addr\s+{MAC}", info, re.MULTILINE | re.DOTALL | re.IGNORECASE
    ):
        link = os.path.realpath(f"/sys/class/bluetooth/{hci}")
        out.append((hci, addr.upper(), "/usb" in link))

    if not out:
        # Fallback: addresses only, no adapter names.
        for addr in re.findall(rf"^Controller\s+{MAC}", run("bluetoothctl", "list"), re.MULTILINE):
            out.append(("?", addr.upper(), False))
    return out


def pick_adapter(requested):
    """Return (hciN, MAC). Prefers the USB dongle when nothing is requested."""
    adapters = list_adapters()
    if not adapters:
        die("no Bluetooth adapter found")

    if requested:
        req = requested.lower()
        for hci, addr, _ in adapters:
            if req in (hci.lower(), addr.lower()):
                return hci, addr
        die(
            f"adapter {requested!r} not found (have: "
            + ", ".join(f"{h}={a}" for h, a, _ in adapters)
            + ")"
        )

    usb = [a for a in adapters if a[2]]
    chosen = (usb or adapters)[0]
    if len(adapters) > 1:
        log("adapters: " + ", ".join(f"{h} {a}{' (USB)' if u else ''}" for h, a, u in adapters))
    return chosen[0], chosen[1]


def find_device(bt, name, timeout):
    """Scan until a device whose name matches `name` shows up."""
    # Ask inside the session so the list belongs to the selected adapter.
    bt.send("devices")
    known = re.search(rf"Device\s+{MAC}\s+{re.escape(name)}", bt.read(2.0), re.IGNORECASE)
    if known:
        log(f"already known to this adapter: {known.group(1)}")
        return known.group(1)

    log(f"scanning up to {timeout}s for {name!r} (put the keyboard in pairing mode now)")
    bt.send("scan on")
    hit = bt.expect({"dev": rf"Device\s+{MAC}\s+.*{re.escape(name)}"}, timeout)
    bt.send("scan off")
    if not hit:
        return None
    return hit[1].group(1)


def pair(bt, mac, timeout):
    """Run the pairing, surfacing the passkey the user must type."""
    log(f"pairing with {mac}")
    bt.send(f"pair {mac}")

    patterns = {
        # Passkey Entry: the code goes on the keyboard, then Enter.
        "passkey": r"Passkey:?\s*(\d{1,6})",
        # Legacy keyboards ask for a PIN instead.
        "pin": r"Enter PIN code:?\s*(\d+)?",
        # Numeric comparison, rare for keyboards.
        "confirm": r"Confirm passkey\s*(\d+)\s*\(yes/no\)",
        "authorize": r"Authorize service.*\(yes/no\)",
        "success": r"Pairing successful",
        "failed": r"Failed to pair:?\s*(.*)",
    }

    deadline = time.monotonic() + timeout
    shown = False
    while time.monotonic() < deadline:
        hit = bt.expect(patterns, max(1.0, deadline - time.monotonic()))
        if not hit:
            break
        key, m = hit

        if key == "passkey" and not shown:
            shown = True
            code = m.group(1).zfill(6)
            print()
            print(
                f"{BOLD}{GREEN}  >>> type this on the ASUS keyboard, "
                f"then press Enter:  {code}  <<<{OFF}"
            )
            print()
        elif key == "pin":
            code = (m.group(1) or "0000").zfill(4)
            print()
            print(
                f"{BOLD}{GREEN}  >>> type this PIN on the ASUS keyboard, "
                f"then press Enter:  {code}  <<<{OFF}"
            )
            print()
            bt.send(code)
        elif key in ("confirm", "authorize"):
            bt.send("yes")
        elif key == "success":
            return True
        elif key == "failed":
            die(f"pairing failed: {m.group(1).strip() or 'unknown reason'}")

    return False


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--name", default=DEFAULT_NAME, help="device name to match")
    ap.add_argument("--mac", help="pair this address directly, skip scanning")
    ap.add_argument("--adapter", help="adapter to use (hciN or its MAC)")
    ap.add_argument("--remove", action="store_true", help="forget an existing pairing first")
    ap.add_argument(
        "--timeout",
        type=int,
        default=SCAN_TIMEOUT,
        help=f"scan timeout in seconds (default {SCAN_TIMEOUT})",
    )
    ap.add_argument("--debug", action="store_true", help="echo the raw bluetoothctl session")
    args = ap.parse_args()

    if not run("which", "bluetoothctl").strip() and not Path("/usr/bin/bluetoothctl").exists():
        die("bluetoothctl not found (install bluez-utils)")

    hci, addr = pick_adapter(args.adapter)
    log(f"using adapter {hci} ({addr})")

    bt = Bluetoothctl(debug=args.debug)
    try:
        bt.send(f"select {addr}")
        bt.send("power on")
        bt.send("agent KeyboardDisplay")
        bt.send("default-agent")
        bt.send("pairable on")
        bt.read(1.0)

        mac = args.mac.upper() if args.mac else None

        if args.remove and mac:
            bt.send(f"remove {mac}")
            bt.read(2.0)

        if not mac:
            mac = find_device(bt, args.name, args.timeout)
            if not mac:
                die(
                    f"{args.name!r} not seen. Hold the keyboard's Bluetooth "
                    "pairing key (Fn + F1/F2/F3 on the Zenbook Duo) until the "
                    "LED blinks, then run this again."
                )
            if args.remove:
                bt.send(f"remove {mac}")
                bt.read(2.0)

        if not pair(bt, mac, PAIR_TIMEOUT):
            die("timed out waiting for pairing to complete")

        log("paired, trusting and connecting", GREEN)
        bt.send(f"trust {mac}")
        bt.read(2.0)
        bt.send(f"connect {mac}")
        hit = bt.expect({"ok": r"Connection successful", "err": r"Failed to connect:?\s*(.*)"}, 25)
        if hit and hit[0] == "ok":
            print(f"{GREEN}done:{OFF} {args.name} paired and connected on {hci}")
        else:
            reason = hit[1].group(1).strip() if hit else "no response"
            print(
                f"{YELLOW}warn:{OFF} paired and trusted, but connect failed "
                f"({reason}). It should reconnect on its own — press a key."
            )
    finally:
        bt.close()


if __name__ == "__main__":
    main()
