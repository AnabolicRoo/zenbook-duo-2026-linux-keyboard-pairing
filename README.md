# ASUS Zenbook Duo 2026 Keyboard Bluetooth pairing

Just a script to automatically pair the keyboard without the need to manually use another keyboard for BT commands.

## Pairing

1. Start the program:

   ```sh
   ./pair-zenbook-keyboard.py
   ```

   It picks the USB dongle if there is one, powers the adapter on, and starts
   scanning (45 s by default).

2. Detach the keyboard from the laptop, then press **F11** to put it into
   Bluetooth pairing mode. The LED blinks while it advertises.

3. The script finds the keyboard and starts pairing. It then prints something
   like:

   ```
   >>> type this on the ASUS keyboard, then press Enter:  482913  <<<
   ```

4. Type those 6 digits **on the ASUS keyboard** and press **Enter**. Nothing
   appears on screen while typing — that is normal.

5. The script trusts and connects the keyboard, and prints `done:` when it is
   ready to use.

## Requirements

Python 3.14+, BlueZ (`bluetoothctl`, `btmgmt`).

## Development

Lint and format with ruff:

```sh
uv run ruff check .        # lint
uv run ruff check --fix .  # autofix
uv run ruff format .       # format
```
