#!/usr/bin/env python3
"""Explicit, narrowly scoped firewalld setup; never disable the firewall."""
import argparse
import shutil
import subprocess
import sys
from womic import parse_endpoint, ReceiverError


def rule_for(ip, port):
    ip, _ = parse_endpoint(ip)
    if not 1 <= port <= 65535:
        raise ReceiverError('Ungültiger UDP-Port.')
    return f'rule family="ipv4" source address="{ip}" port port="{port}" protocol="udp" accept'


def cmd(args, runner=subprocess.run):
    result = runner(['sudo', 'firewall-cmd', *args], capture_output=True, text=True)
    if result.returncode not in (0, 11, 12):
        raise ReceiverError((result.stderr or result.stdout).strip() or 'firewall-cmd fehlgeschlagen.')
    return result


def apply(ip, port=49152, interface='wlan0', remove=False, runner=subprocess.run):
    rule = rule_for(ip, port)
    zone = cmd([f'--get-zone-of-interface={interface}'], runner).stdout.strip()
    if zone in ('', 'no zone'):
        zone = cmd(['--get-default-zone'], runner).stdout.strip()
    if not zone or zone == 'no zone':
        raise ReceiverError('Keine Firewall-Zone gefunden.')
    if remove:
        cmd([f'--zone={zone}', '--permanent', f'--remove-rich-rule={rule}'], runner)
        cmd([f'--zone={zone}', f'--remove-rich-rule={rule}'], runner)
        print(f'WO-Mic-Freigabe für {ip}, UDP {port}, Zone {zone} entfernt.')
    else:
        # Write persistent rule first; replace a possibly timed runtime rule.
        cmd([f'--zone={zone}', '--permanent', f'--add-rich-rule={rule}'], runner)
        cmd([f'--zone={zone}', f'--remove-rich-rule={rule}'], runner)
        cmd([f'--zone={zone}', f'--add-rich-rule={rule}'], runner)
        cmd([f'--zone={zone}', '--permanent', f'--query-rich-rule={rule}'], runner)
        cmd([f'--zone={zone}', f'--query-rich-rule={rule}'], runner)
        print(f'Dauerhaft und sofort freigegeben: {ip} → UDP {port}, Zone {zone}.')


def main():
    parser = argparse.ArgumentParser(description='Gezielte WO-Mic-Freigabe in Firewalld')
    parser.add_argument('ip', help='Aktuelle IPv4-Adresse des iPhones')
    parser.add_argument('--port', type=int, default=49152)
    parser.add_argument('--interface', default='wlan0')
    parser.add_argument('--remove', action='store_true', help='Genau diese Freigabe entfernen')
    args = parser.parse_args()
    if not shutil.which('firewall-cmd') or not shutil.which('sudo'):
        print('firewall-cmd und sudo müssen installiert sein.', file=sys.stderr)
        return 1
    try:
        ip, _ = parse_endpoint(args.ip)
        apply(ip, args.port, args.interface, args.remove)
        return 0
    except (OSError, ReceiverError) as error:
        print(f'Firewall-Einrichtung fehlgeschlagen: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
