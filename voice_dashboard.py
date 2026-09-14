#!/usr/bin/env python3
"""
voice_dashboard.py

PyQt6 GUI wrapper around windows_voice_client.py. See NOTES.txt for
architecture notes (why the voice pipeline runs on a QThread, etc).

Install: pip install vosk websockets sounddevice PyQt6
Run:     python voice_dashboard.py
"""

import asyncio
import json
import os
import sys
from datetime import datetime

import vosk
import websockets
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import windows_voice_client as vc


class VoiceWorker(QThread):
    """Runs the voice pipeline on a background thread; reports via Qt signals."""

    status_changed = pyqtSignal(str, str)
    partial_ready = pyqtSignal(str)
    final_ready = pyqtSignal(str, str, str)

    def __init__(self, ws_url: str, mic_device_index, parent=None):
        super().__init__(parent)
        self.ws_url = ws_url
        self.mic_device_index = mic_device_index
        self._loop = None
        self._stop_event = None

    def stop(self):
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)

    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.close()

    async def _main(self):
        if not os.path.isdir(vc.MODEL_PATH):
            self.status_changed.emit(f"Vosk model folder not found: '{vc.MODEL_PATH}'", "error")
            return

        vosk.SetLogLevel(-1)
        model = vosk.Model(vc.MODEL_PATH)
        transcriber = vc.LiveTranscriber(model, device=self.mic_device_index)

        while not self._stop_event.is_set():
            try:
                self.status_changed.emit(f"Connecting to {self.ws_url} ...", "warn")
                async with websockets.connect(self.ws_url) as websocket:
                    self.status_changed.emit("Connected to Node-RED", "ok")
                    await self._send_loop(websocket, transcriber)
            except (ConnectionRefusedError, websockets.ConnectionClosed, OSError) as e:
                if self._stop_event.is_set():
                    break
                self.status_changed.emit(
                    f"Connection lost/failed ({e}). Retrying in {vc.RECONNECT_DELAY_SEC}s...",
                    "error",
                )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=vc.RECONNECT_DELAY_SEC
                    )
                except asyncio.TimeoutError:
                    pass

        self.status_changed.emit("Stopped", "warn")

    async def _send_loop(self, websocket, transcriber: vc.LiveTranscriber):
        loop = asyncio.get_event_loop()
        last_partial = ""

        with transcriber.stream():
            while not self._stop_event.is_set():
                result = await loop.run_in_executor(None, transcriber.next_result, 0.2)
                if result is None:
                    continue
                kind, text = result

                if kind == "partial":
                    if text != last_partial:
                        last_partial = text
                        self.partial_ready.emit(text)
                    continue

                last_partial = ""
                self.partial_ready.emit("")
                if not text:
                    continue

                command, reason = vc.match_command(text)
                vc.log_speech(text, command, reason)
                self.final_ready.emit(text, command or "", reason)

                if command is None and not vc.SEND_UNMATCHED:
                    continue

                payload = json.dumps({
                    "command": command,
                    "raw_text": text,
                    "match_reason": reason,
                    "source": vc.SOURCE_NAME,
                })
                await websocket.send(payload)


STATUS_COLORS = {
    "ok": "#2e7d32",
    "warn": "#ef6c00",
    "error": "#c62828",
}


class DashboardWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LIMO Voice Command Dashboard")
        self.resize(820, 640)

        self.worker: "VoiceWorker | None" = None

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        layout.addWidget(self._build_config_box())
        layout.addWidget(self._build_status_box())
        layout.addWidget(self._build_caption_box())
        layout.addWidget(self._build_history_box(), stretch=1)

        self._populate_mic_devices()

    def _build_config_box(self) -> QGroupBox:
        box = QGroupBox("Connection")
        form = QFormLayout(box)

        self.ip_edit = QLineEdit(vc.NODE_RED_IP)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(vc.PORT)
        self.path_edit = QLineEdit(vc.PATH)
        self.mic_combo = QComboBox()

        form.addRow("Node-RED IP", self.ip_edit)
        form.addRow("Port", self.port_spin)
        form.addRow("Path", self.path_edit)
        form.addRow("Microphone", self.mic_combo)

        button_row = QHBoxLayout()
        self.start_button = QPushButton("Start Listening")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.start_button.clicked.connect(self._on_start)
        self.stop_button.clicked.connect(self._on_stop)
        button_row.addWidget(self.start_button)
        button_row.addWidget(self.stop_button)
        form.addRow(button_row)

        return box

    def _build_status_box(self) -> QGroupBox:
        box = QGroupBox("Status")
        layout = QVBoxLayout(box)
        self.status_label = QLabel("Not started")
        self.status_label.setStyleSheet("font-weight: bold; color: #616161;")
        layout.addWidget(self.status_label)
        return box

    def _build_caption_box(self) -> QGroupBox:
        box = QGroupBox("Live")
        layout = QVBoxLayout(box)

        self.caption_label = QLabel(" ")
        self.caption_label.setStyleSheet("font-size: 20px; color: #555;")
        self.caption_label.setWordWrap(True)
        layout.addWidget(self.caption_label)

        self.last_command_label = QLabel("No command heard yet")
        self.last_command_label.setStyleSheet(
            "font-size: 22px; font-weight: bold; padding: 6px;"
        )
        layout.addWidget(self.last_command_label)

        return box

    def _build_history_box(self) -> QGroupBox:
        box = QGroupBox("History")
        layout = QVBoxLayout(box)

        self.history_table = QTableWidget(0, 4)
        self.history_table.setHorizontalHeaderLabels(
            ["Time", "Raw Text", "Command", "Match Reason"]
        )
        self.history_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.history_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        self.history_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.history_table)

        return box

    def _populate_mic_devices(self):
        self.mic_combo.clear()
        self.mic_combo.addItem("System default", None)
        for index, info in vc.get_input_devices():
            self.mic_combo.addItem(f"[{index}] {info['name']}", index)
            if index == vc.MIC_DEVICE_INDEX:
                self.mic_combo.setCurrentIndex(self.mic_combo.count() - 1)

    def _on_start(self):
        ws_url = f"ws://{self.ip_edit.text().strip()}:{self.port_spin.value()}{self.path_edit.text().strip()}"
        mic_index = self.mic_combo.currentData()

        self.worker = VoiceWorker(ws_url, mic_index)
        self.worker.status_changed.connect(self._on_status_changed)
        self.worker.partial_ready.connect(self._on_partial)
        self.worker.final_ready.connect(self._on_final)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start()

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.ip_edit.setEnabled(False)
        self.port_spin.setEnabled(False)
        self.path_edit.setEnabled(False)
        self.mic_combo.setEnabled(False)

    def _on_stop(self):
        if self.worker is not None:
            self.stop_button.setEnabled(False)
            self.worker.stop()

    def _on_worker_finished(self):
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.ip_edit.setEnabled(True)
        self.port_spin.setEnabled(True)
        self.path_edit.setEnabled(True)
        self.mic_combo.setEnabled(True)
        self.worker = None

    def _on_status_changed(self, message: str, level: str):
        color = STATUS_COLORS.get(level, "#616161")
        self.status_label.setStyleSheet(f"font-weight: bold; color: {color};")
        self.status_label.setText(message)

    def _on_partial(self, text: str):
        self.caption_label.setText(f"\U0001f3a4 {text}" if text else " ")

    def _on_final(self, raw_text: str, command: str, reason: str):
        display_command = command if command else "(no match)"
        color = "#2e7d32" if command else "#c62828"
        self.last_command_label.setStyleSheet(
            f"font-size: 22px; font-weight: bold; padding: 6px; color: {color};"
        )
        self.last_command_label.setText(f"'{raw_text}'  ->  {display_command}")

        row = 0
        self.history_table.insertRow(row)
        self.history_table.setItem(row, 0, QTableWidgetItem(datetime.now().strftime("%H:%M:%S")))
        self.history_table.setItem(row, 1, QTableWidgetItem(raw_text))
        command_item = QTableWidgetItem(display_command)
        command_item.setForeground(QColor("#2e7d32" if command else "#c62828"))
        self.history_table.setItem(row, 2, command_item)
        self.history_table.setItem(row, 3, QTableWidgetItem(reason))

    def closeEvent(self, event):
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(3000)
        event.accept()


def main():
    app = QApplication(sys.argv)
    window = DashboardWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
