# WO Mic WLAN for Linux

Works with the WO Mic app, tested on Arch with iOS. No bullshit, you'll figure out how to use it.

1. Download the WO Mic app on your phone.
2. Set up the Python files on Linux:

## Setup
```bash
sudo pacman -S --needed python pyside6 opus pipewire-audio
python3 install.py
```

## What it does
* Grabs the Opus audio stream from the WO Mic iOS/Android app over local Wi-Fi (or USB tethering) and pipes it straight into a native PipeWire microphone (`WO Mic WLAN`).
* No closed-source AppImages, no `snd-aloop` kernel module bullshit, no root required.
* Comes with a basic Qt GUI (`womic-wlan`), a CLI (`womic-wlan-cli`), and a `firewalld` helper (`womic-wlan-firewall`) if your firewall blocks UDP.
