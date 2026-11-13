#!/usr/bin/env python3
"""Qt frontend; audio/network work runs in a separate cancellable process."""
import json
import os
from pathlib import Path
import sys
from womic import parse_endpoint, ReceiverError, VERSION

try:
    from PySide6.QtCore import QProcess, QSettings, QTimer
    from PySide6.QtWidgets import (QApplication, QCheckBox, QFormLayout, QHBoxLayout, QLabel,
        QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox,
        QVBoxLayout, QWidget)
except ImportError:
    print('Qt fehlt: sudo pacman -S pyside6', file=sys.stderr)
    sys.exit(1)


class Window(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f'WO Mic WLAN {VERSION}')
        self.resize(500, 460)
        self.settings = QSettings('WomicWlan', 'Client')
        self.process = None
        self.pending = b''
        layout = QVBoxLayout(self)
        title = QLabel('<h2>iPhone als Mikrofon</h2>')
        layout.addWidget(title)
        info = QLabel('WO Mic auf dem iPhone starten und die angezeigte IP eingeben.\n'
                      'Beide Geräte müssen im selben lokalen Netzwerk sein.')
        info.setWordWrap(True)
        layout.addWidget(info)
        form = QFormLayout()
        self.ip = QLineEdit(str(self.settings.value('ip', '')))
        self.ip.setPlaceholderText('192.168.1.100')
        form.addRow('iPhone-IP', self.ip)
        self.control = QSpinBox()
        self.control.setRange(1, 65535)
        self.control.setValue(int(self.settings.value('control', 8125)))
        form.addRow('Steuerport', self.control)
        self.media = QSpinBox()
        self.media.setRange(1, 65535)
        self.media.setValue(int(self.settings.value('media', 49152)))
        form.addRow('Lokaler Audioport (UDP)', self.media)
        self.buffer = QSpinBox()
        self.buffer.setRange(10, 200)
        self.buffer.setSuffix(' ms')
        self.buffer.setValue(int(self.settings.value('buffer', 40)))
        form.addRow('Audiopuffer', self.buffer)
        layout.addLayout(form)
        self.reconnect = QCheckBox('Bei WLAN-Abbruch automatisch erneut verbinden')
        self.reconnect.setChecked(str(self.settings.value('reconnect', 'true')).lower() == 'true')
        layout.addWidget(self.reconnect)
        self.status = QLabel('Bereit')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.meter = QProgressBar()
        self.meter.setRange(0, 60)
        self.meter.setValue(0)
        self.meter.setFormat('Kein Audiosignal')
        layout.addWidget(self.meter)
        row = QHBoxLayout()
        self.connect = QPushButton('Verbinden')
        self.connect.clicked.connect(lambda: self.start(False))
        self.test = QPushButton('Testton (10 Sekunden)')
        self.test.clicked.connect(lambda: self.start(True))
        self.disconnect = QPushButton('Trennen')
        self.disconnect.clicked.connect(self.stop)
        self.disconnect.setEnabled(False)
        for button in (self.connect, self.test, self.disconnect):
            row.addWidget(button)
        layout.addLayout(row)
        layout.addWidget(QLabel('Mikrofon in Discord/OBS/KDE auswählen: WO Mic WLAN'))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(300)
        layout.addWidget(self.log)
        utilities = QHBoxLayout()
        copy = QPushButton('Log kopieren')
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.log.toPlainText()))
        clear = QPushButton('Log leeren')
        clear.clicked.connect(self.log.clear)
        firewall = QPushButton('WLAN-Freigabe einrichten')
        firewall.clicked.connect(self.firewall_help)
        for button in (copy, clear, firewall):
            utilities.addWidget(button)
        layout.addLayout(utilities)

    def firewall_help(self):
        try:
            ip, _ = parse_endpoint(self.ip.text())
        except ReceiverError as error:
            self.status.setText(str(error))
            return
        command = (f'python3 "{Path(__file__).with_name("firewall.py")}" '
                   f'{ip} --port {self.media.value()}')
        dialog = QMessageBox(self)
        dialog.setWindowTitle('Dauerhafte WLAN-Freigabe')
        dialog.setText('Dieser Terminalbefehl erlaubt Audio von deinem iPhone dauerhaft. '
                       'Der Helfer fragt über sudo nach deinem Passwort.')
        dialog.setInformativeText(command)
        dialog.setDetailedText('Die Regel betrifft nur die angegebene iPhone-IP und den '
                               'UDP-Audioport. Zum Entfernen denselben Befehl mit --remove verwenden. '
                               'Bei anderer iPhone-IP die alte Freigabe entfernen und neu einrichten.')
        button = dialog.addButton('Befehl kopieren', QMessageBox.ActionRole)
        button.clicked.connect(lambda: QApplication.clipboard().setText(command))
        dialog.addButton(QMessageBox.Close)
        dialog.exec()

    def start(self, test):
        if self.process:
            return
        if not test:
            original = self.ip.text()
            try:
                address, port = parse_endpoint(original)
            except ReceiverError as error:
                self.status.setText(str(error))
                self.log.appendPlainText(str(error))
                self.log.appendPlainText('Eingabe (mit sichtbaren Sonderzeichen): '
                                         + json.dumps(original, ensure_ascii=True))
                return
            self.ip.setText(address)
            if port is not None:
                self.control.setValue(port)
            self.log.appendPlainText(f'Adresse akzeptiert: {address}:{self.control.value()}')
        for key, value in (('ip', self.ip.text().strip()),
                           ('control', self.control.value()), ('media', self.media.value()),
                           ('buffer', self.buffer.value()), ('reconnect', self.reconnect.isChecked())):
            self.settings.setValue(key, value)
        child = QProcess(self)
        self.process = child
        self.pending = b''
        child.setProcessChannelMode(QProcess.MergedChannels)
        child.readyReadStandardOutput.connect(self.read)
        child.finished.connect(self.finished)
        child.errorOccurred.connect(lambda error: self.process_error(child, error))
        args = [str(Path(__file__).with_name('womic.py'))]
        if test:
            args += ['--test-tone']
        else:
            args += [self.ip.text().strip(), '--control-port', str(self.control.value()),
                     '--media-port', str(self.media.value()),
                     '--buffer-ms', str(self.buffer.value())]
            if self.reconnect.isChecked():
                args += ['--reconnect']
        child.start(sys.executable, args)
        self.busy(True)
        self.status.setText('Starte …')

    def busy(self, yes):
        for widget in (self.ip, self.control, self.media, self.buffer, self.reconnect,
                       self.connect, self.test):
            widget.setEnabled(not yes)
        self.disconnect.setEnabled(yes)

    def process_error(self, child, error):
        self.status.setText(child.errorString())
        if error == QProcess.FailedToStart:
            self.process = None
            self.busy(False)

    def read(self):
        if not self.process:
            return
        self.pending += bytes(self.process.readAllStandardOutput())
        while b'\n' in self.pending:
            line, self.pending = self.pending.split(b'\n', 1)
            text = line.decode(errors='replace')
            try:
                event = json.loads(text)
            except ValueError:
                self.log.appendPlainText(text)
                continue
            if event.get('event') == 'stats':
                db = event['level_db']
                self.meter.setValue(max(0, min(60, round(db + 60))))
                self.meter.setFormat(f'{db:.1f} dBFS | {event["packets"]} Pakete')
                if event['decode_errors']:
                    self.status.setText(f'Audio läuft; {event["decode_errors"]} ungültige Pakete.')
            else:
                message = event.get('message', text)
                self.log.appendPlainText(message)
                if event.get('event') in ('status', 'error', 'reconnecting'):
                    self.status.setText(message)
                if event.get('event') == 'reconnecting':
                    self.meter.setValue(0)
                    self.meter.setFormat('Verbindung unterbrochen')

    def stop(self):
        child = self.process
        if child:
            child.terminate()
            QTimer.singleShot(3000, lambda: child.kill() if child.state() != QProcess.NotRunning else None)

    def finished(self, code, _):
        self.read()
        self.process = None
        self.busy(False)
        self.meter.setValue(0)
        if code == 0:
            self.status.setText('Getrennt')

    def closeEvent(self, event):
        if self.process:
            self.process.terminate()
            if not self.process.waitForFinished(3000):
                self.process.kill()
                self.process.waitForFinished(1000)
        event.accept()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setApplicationName('WO Mic WLAN')
    window = Window()
    window.show()
    sys.exit(app.exec())
