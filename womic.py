#!/usr/bin/env python3
"""Independent WLAN receiver for the existing WO Mic phone app."""
import argparse
import array
import collections
import ctypes as C
import ctypes.util
import errno
import ipaddress
import json
import math
import os
import re
import selectors
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import unicodedata
from urllib.parse import urlsplit
import wave

RATE = 48000
HELLO, MEDIA, START, HEARTBEAT = 0x65, 0x66, 0x67, 0x69
MAX_CONTROL = 4096
VERSION = '1.0.0'


class ReceiverError(Exception):
    pass


class ConnectionLost(ReceiverError):
    """Recoverable transport failure; audio/protocol errors remain fatal."""


def parse_endpoint(value):
    """Return canonical IPv4 and optional port; tolerate copied display text."""
    text = unicodedata.normalize('NFKC', value)
    text = ''.join(c for c in text if unicodedata.category(c) != 'Cf').strip()
    text = text.translate(str.maketrans({'。': '.', '․': '.', '﹒': '.'}))
    text = ''.join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in text)
    if not text:
        raise ReceiverError('Das IP-Feld ist leer. Die graue Beispieladresse ist nur ein Platzhalter.')
    port = None
    if '://' in text:
        try:
            url = urlsplit(text)
            if (url.scheme not in ('http', 'https') or url.username is not None
                    or url.password is not None or url.query or url.fragment
                    or url.path not in ('', '/')):
                raise ValueError()
            host, port = url.hostname or '', url.port
        except ValueError:
            raise ReceiverError('Bitte nur die iPhone-Adresse oder Adresse:Port eingeben.') from None
    else:
        if text.count(':') > 1:
            raise ReceiverError('Diese Version benötigt die IPv4-Adresse; IPv6 wird noch nicht unterstützt.')
        host = text
        if ':' in text:
            host, port_text = text.rsplit(':', 1)
            if not re.fullmatch(r'\s*[0-9]+\s*', port_text):
                raise ReceiverError('Der Port hinter dem Doppelpunkt muss eine Zahl sein.')
            port = int(port_text)
    parts = host.split('.')
    if len(parts) != 4 or any(not re.fullmatch(r'\s*[0-9]{1,3}\s*', p) for p in parts):
        raise ReceiverError('Erwartet werden vier Zahlen von 0 bis 255, getrennt durch Punkte.')
    octets = [int(p) for p in parts]
    if any(n > 255 for n in octets):
        raise ReceiverError('Ein Zahlenblock der IPv4-Adresse ist größer als 255.')
    if port is not None and not 1 <= port <= 65535:
        raise ReceiverError('Der Steuerport muss zwischen 1 und 65535 liegen.')
    ip = '.'.join(map(str, octets))
    address = ipaddress.IPv4Address(ip)
    if address.is_multicast or address.is_unspecified or ip == '255.255.255.255':
        raise ReceiverError('Bitte die Geräteadresse des iPhones eingeben, keine Broadcast-/Multicast-Adresse.')
    return ip, port


def emit(kind, **fields):
    print(json.dumps(dict(event=kind, **fields), ensure_ascii=False), flush=True)


def frame(cmd, payload=b''):
    return struct.pack('!BI', cmd, len(payload)) + payload


def recv_exact(sock, length):
    data = bytearray()
    while len(data) < length:
        part = sock.recv(length - len(data))
        if not part:
            raise ConnectionLost('iPhone hat die Steuerverbindung geschlossen.')
        data.extend(part)
    return bytes(data)


def response(sock, expected):
    cmd, length = struct.unpack('!BI', recv_exact(sock, 5))
    if length > MAX_CONTROL:
        raise ReceiverError(f'Unerwartete Steuerantwort: {length} Bytes.')
    payload = recv_exact(sock, length)
    if cmd != expected:
        raise ReceiverError(f'Unerwartete Antwort 0x{cmd:02x}, erwartet 0x{expected:02x}.')
    if expected in (MEDIA, START) and (not payload or payload[0] != 0):
        raise ReceiverError(f'iPhone lehnt Befehl 0x{cmd:02x} ab: {payload.hex()}')
    return payload


class OpusDecoder:
    def __init__(self):
        name = ctypes.util.find_library('opus')
        if not name:
            raise ReceiverError('libopus fehlt. Unter EndeavourOS: sudo pacman -S opus')
        self.lib = C.CDLL(name)
        self.lib.opus_decoder_create.argtypes = [C.c_int, C.c_int, C.POINTER(C.c_int)]
        self.lib.opus_decoder_create.restype = C.c_void_p
        self.lib.opus_decode.argtypes = [C.c_void_p, C.c_void_p, C.c_int,
                                        C.POINTER(C.c_int16), C.c_int, C.c_int]
        self.lib.opus_decode.restype = C.c_int
        self.lib.opus_decoder_destroy.argtypes = [C.c_void_p]
        error = C.c_int()
        self.ptr = self.lib.opus_decoder_create(RATE, 1, C.byref(error))
        if not self.ptr or error.value:
            raise ReceiverError(f'Opus-Initialisierung fehlgeschlagen: {error.value}')
        self.buffer = (C.c_int16 * 5760)()

    def decode(self, payload):
        n = self.lib.opus_decode(self.ptr, payload, len(payload), self.buffer, 5760, 0)
        if n < 0:
            raise ReceiverError(f'Opus-Dekodierung fehlgeschlagen: {n}')
        pcm = array.array('h', self.buffer[:n])
        if sys.byteorder != 'little':
            pcm.byteswap()
        return pcm.tobytes()

    def close(self):
        if self.ptr:
            self.lib.opus_decoder_destroy(self.ptr)
            self.ptr = None


class WaveOutput:
    def __init__(self, path):
        self.file = wave.open(path, 'wb')
        self.file.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
        self.dropped = 0

    def write(self, pcm):
        self.file.writeframesraw(pcm)

    def check(self):
        pass

    def reset(self):
        pass

    def close(self):
        self.file.close()


class PipeWireOutput:
    """Bounded PCM queue feeding a native PipeWire Audio/Source node."""
    def __init__(self, buffer_ms=40):
        executable = shutil.which('pw-cat')
        if not executable:
            raise ReceiverError('pw-cat fehlt. Unter EndeavourOS: sudo pacman -S pipewire-audio')
        try:
            help_result = subprocess.run([executable, '--help'], capture_output=True, timeout=3)
        except subprocess.TimeoutExpired:
            raise ReceiverError('pw-cat antwortet nicht auf --help.') from None
        # Old versions treat stdin as raw; newer versions require --raw.
        raw_args = ['--raw'] if b'--raw' in help_result.stdout + help_result.stderr else []
        properties = {
            'media.class': 'Audio/Source', 'node.name': 'womic_wlan',
            'node.description': 'WO Mic WLAN', 'node.virtual': True,
            'node.always-process': True, 'node.want-driver': True,
            'node.pause-on-idle': False, 'stream.is-live': True,
            'audio.position': ['MONO'],
        }
        self.proc = subprocess.Popen([
            executable, '--playback', *raw_args, '--rate=48000', '--channels=1',
            '--channel-map=mono', '--format=s16', '--target=0', '--latency=10ms',
            '--properties', json.dumps(properties), '-'
        ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            bufsize=0)
        os.set_blocking(self.proc.stdin.fileno(), False)
        self.queue = bytearray()
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.target = RATE * 2 * buffer_ms // 1000
        self.limit = max(self.target * 3, RATE * 2 // 10)
        self.dropped = 0
        self.error = None
        self.stderr = collections.deque(maxlen=20)
        self.started = False
        self.reader = threading.Thread(target=self._errors, daemon=True)
        self.reader.start()
        self.worker = threading.Thread(target=self._pump, daemon=True)
        self.worker.start()

    def _errors(self):
        for line in self.proc.stderr:
            self.stderr.append(line.decode(errors='replace').strip())

    def write(self, pcm):
        with self.lock:
            self.queue.extend(pcm)
            if len(self.queue) > self.limit:
                excess = (len(self.queue) - self.target) // 2 * 2
                del self.queue[:excess]
                self.dropped += excess // 2

    def _pump(self):
        # 10 ms playout blocks, silence on underruns; backlog capped above.
        block = RATE * 2 // 100
        pending = b''
        deadline = time.monotonic()
        try:
            with selectors.DefaultSelector() as sel:
                sel.register(self.proc.stdin, selectors.EVENT_WRITE)
                while not self.stopping.is_set():
                    if self.proc.poll() is not None:
                        raise ReceiverError('PipeWire-Ausgabe wurde beendet.')
                    if not pending:
                        with self.lock:
                            if not self.started and len(self.queue) >= self.target:
                                self.started = True
                            if self.started:
                                pending = bytes(self.queue[:block])
                                del self.queue[:block]
                            else:
                                pending = b''
                            pending += bytes(block - len(pending))
                        now = time.monotonic()
                        if now - deadline > .1:
                            deadline = now
                        if self.stopping.wait(max(0, deadline - now)):
                            break
                        deadline += .01
                    if sel.select(.1):
                        try:
                            n = os.write(self.proc.stdin.fileno(), pending)
                            pending = pending[n:]
                        except BlockingIOError:
                            continue
        except (OSError, ReceiverError) as e:
            self.error = str(e)

    def check(self):
        if self.error or self.proc.poll() is not None:
            detail = '\n'.join(self.stderr)
            raise ReceiverError((self.error or 'PipeWire-Ausgabe beendet.') + '\n' + detail)

    def reset(self):
        with self.lock:
            self.queue.clear()
            self.started = False

    def close(self):
        self.stopping.set()
        self.worker.join(timeout=1)
        self.proc.stdin.close()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.reader.join(timeout=1)
        self.proc.stderr.close()


def level(pcm):
    samples = array.array('h', pcm)
    if sys.byteorder != 'little':
        samples.byteswap()
    rms = math.sqrt(sum(x*x for x in samples) / max(1, len(samples))) / 32768
    return round(20 * math.log10(max(rms, 1e-5)), 1)


def receive(ip, control_port, media_port, output, stop, duration=0,
            header_bytes=11, report=emit):
    ip, copied_port = parse_endpoint(ip)
    if copied_port is not None:
        control_port = copied_port
    decoder = OpusDecoder()
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tcp = None
    try:
        udp.bind(('0.0.0.0', media_port))
        actual_port = udp.getsockname()[1]
        report('status', message=f'Verbinde mit {ip}:{control_port} …')
        tcp = socket.create_connection((ip, control_port), timeout=5)
        tcp.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        tcp.sendall(frame(HELLO, bytes([4, 4, 6, 2, 0, 0])))
        hello = response(tcp, HELLO)
        report('protocol', hello=hello.hex(), media_port=actual_port)
        tcp.sendall(frame(MEDIA, bytes([2, 2, 0, 0]) + struct.pack('!H', actual_port)))
        response(tcp, MEDIA)
        tcp.sendall(frame(START))
        response(tcp, START)
        tcp.setblocking(False)
        udp.setblocking(False)
        report('status', message='Steuerverbindung steht; warte auf Audio …')
        started = last_audio = last_valid = time.monotonic()
        heartbeat = started
        next_report = started + 1
        packets = errors = foreign = 0
        db = -100
        control = bytearray()
        with selectors.DefaultSelector() as sel:
            sel.register(tcp, selectors.EVENT_READ, 'tcp')
            sel.register(udp, selectors.EVENT_READ, 'udp')
            while not stop.is_set():
                now = time.monotonic()
                if duration and now - started >= duration:
                    break
                output.check()
                if now >= heartbeat:
                    # Tiny bounded writes; restore nonblocking mode for receive.
                    tcp.settimeout(1)
                    tcp.sendall(frame(HEARTBEAT))
                    tcp.setblocking(False)
                    heartbeat = now + 1
                for key, _ in sel.select(.1):
                    if key.data == 'tcp':
                        chunk = tcp.recv(8192)
                        if not chunk:
                            raise ConnectionLost('iPhone hat die Verbindung beendet.')
                        control.extend(chunk)
                        while len(control) >= 5:
                            cmd, size = struct.unpack('!BI', control[:5])
                            if size > MAX_CONTROL:
                                raise ReceiverError('Ungültige Steuerantwort nach Streamstart.')
                            if len(control) < 5 + size:
                                break
                            payload = control[5:5+size]
                            del control[:5+size]
                            if cmd != HEARTBEAT:
                                report('protocol', command=cmd, payload=payload.hex())
                    else:
                        packet, sender = udp.recvfrom(65535)
                        if sender[0] != ip:
                            foreign += 1
                            continue
                        last_audio = time.monotonic()
                        if len(packet) <= header_bytes:
                            errors += 1
                            continue
                        try:
                            pcm = decoder.decode(packet[header_bytes:])
                        except ReceiverError:
                            errors += 1
                            continue
                        if not pcm:
                            errors += 1
                            continue
                        output.write(pcm)
                        last_valid = time.monotonic()
                        packets += 1
                        db = level(pcm)
                        if packets == 1:
                            report('status', message='Audio empfangen — Mikrofon bereit.')
                if now - last_audio > 8:
                    raise ConnectionLost(
                        f'Keine Audiopakete auf UDP-Port {actual_port}. '
                        'WO Mic starten, lokalen Netzwerkzugriff auf iOS erlauben; '
                        'Firewall und Gast-WLAN/Client-Isolation prüfen.')
                if now - last_valid > 8:
                    raise ReceiverError(
                        'UDP-Pakete kommen an, sind aber nicht als Opus dekodierbar. '
                        'Die Protokollvariante deiner App muss geprüft werden.')
                if now >= next_report:
                    report('stats', packets=packets, decode_errors=errors,
                           foreign_packets=foreign, level_db=db,
                           dropped_samples=output.dropped)
                    next_report = now + .5
        return packets
    finally:
        if tcp:
            tcp.close()
        udp.close()
        decoder.close()


def run_sessions(ip, control_port, media_port, output, stop, duration=0,
                 header_bytes=11, reconnect=False, report=emit):
    """Keep the same microphone node while retrying transient WLAN failures."""
    deadline = time.monotonic() + duration if duration else None
    delay = 2
    while not stop.is_set():
        remaining = max(0, deadline - time.monotonic()) if deadline else 0
        if deadline and remaining <= 0:
            return
        output.reset()
        try:
            receive(ip, control_port, media_port, output, stop, remaining,
                    header_bytes, report)
            return
        except (ConnectionLost, OSError) as error:
            if stop.is_set():
                return
            if isinstance(error, OSError) and error.errno in (errno.EADDRINUSE, errno.EACCES, errno.EPERM):
                raise ReceiverError(f'Lokaler Netzwerkfehler: {error}') from error
            if not reconnect:
                raise
            output.check()
            report('reconnecting', message=f'{error} Neuer Versuch in {delay} Sekunden …')
            wait = min(delay, max(0, deadline-time.monotonic())) if deadline else delay
            if stop.wait(wait):
                return
            delay = min(delay * 2, 10)


def diagnose():
    print('WO Mic WLAN — Diagnose (ändert keine Einstellungen)')
    print('libopus:', ctypes.util.find_library('opus') or 'FEHLT')
    for args in (['pw-cat', '--version'], ['wpctl', 'status'], ['pactl', 'info']):
        print('\n$', ' '.join(args))
        if not shutil.which(args[0]):
            print('Nicht installiert')
            continue
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=5)
            print((result.stdout + result.stderr).strip())
        except subprocess.TimeoutExpired:
            print('Zeitüberschreitung')


def tone(output, stop, seconds):
    end = time.monotonic() + seconds
    phase = 0
    while time.monotonic() < end and not stop.is_set():
        pcm = struct.pack('<480h', *(int(3000 * math.sin(2*math.pi*440*(phase+i)/RATE))
                                   for i in range(480)))
        output.write(pcm)
        output.check()
        phase += 480
        stop.wait(.01)


def main():
    parser = argparse.ArgumentParser(description='WO-Mic-iPhone → natives PipeWire-Mikrofon')
    parser.add_argument('--version', action='version', version=VERSION)
    parser.add_argument('ip', nargs='?', help='IPv4-Adresse aus der iPhone-App')
    parser.add_argument('--control-port', type=int, default=8125)
    parser.add_argument('--media-port', type=int, default=49152)
    parser.add_argument('--buffer-ms', type=int, default=40)
    parser.add_argument('--header-bytes', type=int, default=11,
                        help='Protokoll-Diagnose; normalerweise 11')
    parser.add_argument('--duration', type=float, default=0, help='Sekunden; 0 = bis Strg+C')
    parser.add_argument('--wav', help='WAV aufnehmen, ohne PipeWire (Audio-Diagnose)')
    parser.add_argument('--test-tone', action='store_true', help='Testton statt iPhone')
    parser.add_argument('--diagnose', action='store_true')
    parser.add_argument('--reconnect', action='store_true', help='Bei WLAN-Abbruch erneut verbinden')
    args = parser.parse_args()
    if args.diagnose:
        diagnose()
        return 0
    if not args.ip and not args.test_tone:
        parser.error('IP-Adresse angeben oder --test-tone verwenden.')
    if not 1 <= args.control_port <= 65535 or not 0 <= args.media_port <= 65535:
        parser.error('Ungültiger Port.')
    if not 10 <= args.buffer_ms <= 200 or not 0 <= args.header_bytes <= 64 or args.duration < 0:
        parser.error('Ungültiger Puffer, Header oder Zeitraum.')
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    output = None
    try:
        if not args.test_tone:
            args.ip, copied_port = parse_endpoint(args.ip)
            if copied_port is not None:
                args.control_port = copied_port
        output = WaveOutput(args.wav) if args.wav else PipeWireOutput(args.buffer_ms)
        if args.test_tone:
            emit('status', message='Testton als WO Mic WLAN verfügbar.')
            tone(output, stop, args.duration or 10)
        else:
            run_sessions(args.ip, args.control_port, args.media_port, output, stop,
                         args.duration, args.header_bytes, args.reconnect)
        emit('status', message='Beendet.')
        return 0
    except (OSError, ValueError, ReceiverError) as e:
        emit('error', message=str(e))
        return 1
    finally:
        if output:
            output.close()


if __name__ == '__main__':
    sys.exit(main())
