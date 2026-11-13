#!/usr/bin/env python3
"""Per-user installation, without root or changing audio configuration."""
from pathlib import Path
import shutil

origin = Path(__file__).resolve().parent
destination = Path.home() / '.local/share/womic-wlan'
bin_dir = Path.home() / '.local/bin'
app_dir = Path.home() / '.local/share/applications'
for directory in (destination, bin_dir, app_dir):
    directory.mkdir(parents=True, exist_ok=True)
for name in ('womic.py', 'gui.py', 'firewall.py', 'README.md'):
    shutil.copy2(origin / name, destination / name)
for name, target in (('womic-wlan', 'gui.py'), ('womic-wlan-cli', 'womic.py'),
                     ('womic-wlan-firewall', 'firewall.py')):
    launcher = bin_dir / name
    launcher.write_text('#!/usr/bin/env python3\nimport os, sys\n'
                        f'os.execvp("python3", ["python3", {str(destination / target)!r}, *sys.argv[1:]])\n')
    launcher.chmod(0o755)

def desktop_quote(value):
    # Desktop Entry Exec quoting is not shell quoting.
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$') + '"'

(app_dir / 'womic-wlan.desktop').write_text(
    '[Desktop Entry]\nType=Application\nName=WO Mic WLAN\n'
    'Comment=iPhone-Mikrofon über lokales WLAN\n'
    f'Exec={desktop_quote(bin_dir / "womic-wlan")}\n'
    'Icon=audio-input-microphone\nTerminal=false\nCategories=AudioVideo;Audio;\n')
print('Installiert. Im KDE-Anwendungsmenü: WO Mic WLAN')
print(f'CLI: {bin_dir / "womic-wlan-cli"}')
