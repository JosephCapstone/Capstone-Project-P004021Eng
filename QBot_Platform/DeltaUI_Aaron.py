import sys
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication, 
    QDialog, 
    QGroupBox, 
    QHBoxLayout, 
    QLabel, 
    QLineEdit,
    QMainWindow, 
    QPlainTextEdit, 
    QPushButton, 
    QVBoxLayout, 
    QWidget,
)
from delta_backend import DeltaBackend

# ---------- About Window ----------

def show_infomation(parent):
    info_dialog = QDialog(parent)
    info_dialog.setWindowTitle('About D.E.L.T.A')
    info_dialog.setFixedSize(500, 560)
    dialog_layout = QVBoxLayout(info_dialog)
    dialog_layout.setContentsMargins(18, 18, 18, 18)
    dialog_layout.setSpacing(10)
    title_label = QLabel('D.E.L.T.A')
    title_label.setStyleSheet("""
        font-size: 26px;
        font-weight: bold;
        color: black;
    """)
    dialog_layout.addWidget(title_label)
    description_label = QLabel(
        "D.E.L.T.A is a robotic damage assessment system designed to assist "
        "with environmental scanning, mapping and remote inspection."
    )
    description_label.setWordWrap(True)
    dialog_layout.addWidget(description_label)
    github_label = QLabel(
        '<a href="https://github.com/JosephCapstone/Capstone-Project-P004021Eng">'
        'GitHub Repository</a>'
    )
    github_label.setOpenExternalLinks(True)
    dialog_layout.addWidget(github_label)
    dialog_layout.addSpacing(8)
    team_heading = QLabel('The Team')
    team_heading.setStyleSheet("""
        font-size: 16px;
        font-weight: bold;
        color: black;
    """)
    dialog_layout.addWidget(team_heading)
    supervisor_label = QLabel('<b>Capstone Supervisor:</b><br>Dr Amirali Khodadadian Gostar')
    dialog_layout.addWidget(supervisor_label)
    dialog_layout.addSpacing(6)
    members_label = QLabel(
        "<b>Group Members:</b><br>"
        "Alicia Prinzi<br>Dylan Jarvis<br>Jayson Bilek<br>Mahmut Ercan<br>"
        "Aaron Tuohill<br>Joseph Hatton<br>Callum Day<br>Jordan Fabiyanic<br>"
        "Tom Soepono<br>Elias Bollas<br>Codi Barrett<br>Lucas Di Guglielmo"
    )
    dialog_layout.addWidget(members_label)
    dialog_layout.addStretch()
    close_button = QPushButton('Close')
    close_button.clicked.connect(info_dialog.accept)
    dialog_layout.addWidget(close_button)
    info_dialog.exec()

class DeltaUI(QMainWindow):

    def __init__(self, backend=None):
        super().__init__()
        self.backend = backend or DeltaBackend()
        self.last_event_id = 0
        self.last_map_version = -1
        self.last_camera_version = -1
        self.map_pixmap = None
        self.camera_pixmap = None
        self.camera_topic_fresh = False
        self.setWindowTitle('D.E.L.T.A')
        self.resize(1200, 800)
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(8)
        # ---------- Title ----------
        title_layout = QHBoxLayout()
        title_label = QLabel('D.E.L.T.A')
        title_label.setStyleSheet("""
            font-size: 32px;
            font-weight: bold;
            color: black;
        """)
        self.info_button = QPushButton('i')
        self.info_button.setFixedSize(32, 32)
        self.info_button.setToolTip('About D.E.L.T.A')
        self.info_button.clicked.connect(lambda: show_infomation(self))
        title_layout.addWidget(title_label)
        title_layout.addStretch()
        title_layout.addWidget(self.info_button)
        main_layout.addLayout(title_layout)
        # ---------- Camera ----------
        camera_group = QGroupBox('Camera Feed')
        camera_layout = QVBoxLayout(camera_group)
        self.camera_label = QLabel('Waiting for camera')
        self.camera_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.camera_label.setMinimumSize(360, 240)
        camera_layout.addWidget(self.camera_label)
        # ---------- Controls ----------
        controls_group = QGroupBox('Controls')
        controls_layout = QVBoxLayout(controls_group)
        self.start_button = QPushButton('Start')
        self.stop_button = QPushButton('Stop')
        self.start_button.setMinimumHeight(34)
        self.stop_button.setMinimumHeight(34)
        self.qbot_status_label = QLabel('QBot: checking')
        self.qbot_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.start_button.clicked.connect(
            lambda: self.run_action(self.backend.launch_script)
        )
        self.stop_button.clicked.connect(
            lambda: self.run_action(self.backend.stop_script)
        )
        controls_layout.addStretch(1)
        controls_layout.addWidget(self.qbot_status_label)
        controls_layout.addStretch(1)
        controls_layout.addWidget(self.start_button)
        controls_layout.addSpacing(6)
        controls_layout.addWidget(self.stop_button)
        controls_layout.addStretch(1)
        # ---------- Recording ----------
        recording_group = QGroupBox('Recording')
        recording_layout = QVBoxLayout(recording_group)
        self.bag_recording_number = QLineEdit()
        self.bag_recording_number.setPlaceholderText('Recording Number')
        self.bag_recording_number.setMinimumHeight(30)
        self.start_recording_button = QPushButton('Start Recording')
        self.stop_recording_button = QPushButton('Stop Recording')
        self.start_recording_button.setMinimumHeight(34)
        self.stop_recording_button.setMinimumHeight(34)
        self.recording_status_label = QLabel('Recording: checking')
        self.recording_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.start_recording_button.clicked.connect(
            lambda: self.run_action(
                self.backend.start_recording, self.bag_recording_number.text()
            )
        )
        self.stop_recording_button.clicked.connect(
            lambda: self.run_action(self.backend.stop_recording)
        )
        recording_layout.addStretch(1)
        recording_layout.addWidget(self.recording_status_label)
        recording_layout.addStretch(1)
        recording_layout.addWidget(self.bag_recording_number)
        recording_layout.addSpacing(6)
        recording_layout.addWidget(self.start_recording_button)
        recording_layout.addSpacing(6)
        recording_layout.addWidget(self.stop_recording_button)
        recording_layout.addStretch(1)
        # ---------- Mapping ----------
        mapping_group = QGroupBox('Mapping')
        mapping_layout = QVBoxLayout(mapping_group)
        self.mapping_status_label = QLabel('●')
        self.mapping_status_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.mapping_status_label.setStyleSheet("""
            QLabel {
                color: red;
                font-size: 36px;
                font-weight: bold;
                padding-right: 6px;
            }
        """)
        self.mapping_status_label.setToolTip('Mapping: connecting')
        self.map_label = QLabel('LIVE MAP')
        self.map_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.map_label.setMinimumSize(380, 380)
        self.map_name = QLineEdit(self.backend.default_map_name())
        self.map_name.setPlaceholderText('Map Name')
        self.start_mapping_button = QPushButton('Start Mapping')
        self.finish_mapping_button = QPushButton('Finish and Save')
        self.cancel_mapping_button = QPushButton('Cancel')
        self.new_map_button = QPushButton('New Map')
        self.start_mapping_button.clicked.connect(
            lambda: self.run_action(self.backend.start_mapping)
        )
        self.finish_mapping_button.clicked.connect(
            lambda: self.run_action(
                self.backend.finish_and_save, self.map_name.text()
            )
        )
        self.cancel_mapping_button.clicked.connect(
            lambda: self.run_action(self.backend.cancel_mapping)
        )
        self.new_map_button.clicked.connect(self.new_map)
        mapping_buttons = QHBoxLayout()
        mapping_buttons.setSpacing(6)
        mapping_buttons.addWidget(self.start_mapping_button)
        mapping_buttons.addWidget(self.finish_mapping_button)
        mapping_buttons.addWidget(self.cancel_mapping_button)
        mapping_buttons.addWidget(self.new_map_button)
        mapping_layout.addWidget(self.mapping_status_label)
        mapping_layout.addWidget(self.map_label, 1)
        mapping_layout.addWidget(self.map_name)
        mapping_layout.addLayout(mapping_buttons)
        # ---------- Log ----------
        log_group = QGroupBox('Log')
        log_layout = QVBoxLayout(log_group)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        log_layout.addWidget(self.log)
        self.log.appendPlainText('D.E.L.T.A GUI started')
        # ---------- Main Layout ----------
        top_layout = QHBoxLayout()
        top_layout.setSpacing(10)
        top_layout.addWidget(camera_group, 7)
        top_layout.addWidget(mapping_group, 4)
        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)
        button_layout.addWidget(controls_group, 1)
        button_layout.addWidget(recording_group, 1)
        main_layout.addLayout(top_layout, 7)
        main_layout.addLayout(button_layout, 2)
        main_layout.addWidget(log_group, 4)
        # ---------- Styling ----------
        self.setStyleSheet("""
            QGroupBox {
                border: 1px solid #888;
                border-radius: 5px;
                margin-top: 10px;
                padding: 10px;
            }

            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                top: 5px;
                left: 10px;
                padding: 0 5px;
            }

            QPushButton {
                min-height: 32px;
                padding: 3px 6px;
            }

            QLineEdit {
                min-height: 26px;
            }
        """)
        # ---------- Timers ----------
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_backend)
        self.refresh_timer.start(500)
        self.camera_timer = QTimer(self)
        self.camera_timer.timeout.connect(self.refresh_camera)
        self.camera_timer.start(100)
        self.refresh_backend()

    def run_action(self, action, *arguments):
        try:
            action(*arguments)
        except (RuntimeError, ValueError) as error:
            self.log.appendPlainText(f'Error: {error}')

    def new_map(self):
        self.run_action(self.backend.new_map)
        self.map_name.setText(self.backend.default_map_name())

    # ---------- Backend Refresh ----------
    def refresh_backend(self):
        state = self.backend.get_state()
        qbot_state = state.get('qbot', {}).get('state', 'unknown')
        recording_state = state.get('recording', {}).get('state', 'unknown')
        mapping = state.get('mapping', {})
        mapping_state = mapping.get('mapping', 'unavailable')
        visualization_state = state.get('visualization', {}).get('state', 'unknown')
        camera_age = mapping.get('topic_ages', {}).get('/camera/color_image')
        camera_topic_fresh = camera_age is not None and camera_age <= 2.0
        if not camera_topic_fresh and self.camera_topic_fresh:
            self.clear_camera('Camera feed unavailable')
        elif not camera_topic_fresh and self.camera_pixmap is None:
            message = (
                "Camera starts with QBot"
                if qbot_state in ("stopped", "offline", "error")
                else "Waiting for camera"
            )
            self.camera_label.setText(message)
        self.camera_topic_fresh = camera_topic_fresh
        self.qbot_status_label.setText(f'QBot: {qbot_state}')
        self.recording_status_label.setText(f'Recording: {recording_state}')
        mapping_connected_states = (
            "ready", "starting", "mapping", "playback_complete", "saved"
        )
        if mapping_state in mapping_connected_states:
            self.mapping_status_label.setStyleSheet("""
                QLabel {
                    color: green;
                    font-size: 36px;
                    font-weight: bold;
                    padding-right: 6px;
                }
            """)
        else:
            self.mapping_status_label.setStyleSheet("""
                QLabel {
                    color: red;
                    font-size: 36px;
                    font-weight: bold;
                    padding-right: 6px;
                }
            """)
        self.mapping_status_label.setToolTip(
            f"Mapping: {mapping_state}\n"
            f"View: {visualization_state}\n"
            f"{mapping.get('detail', '')}"
        )
        self.start_button.setEnabled(qbot_state in ('stopped', 'offline', 'error'))
        self.stop_button.setEnabled(
            qbot_state in ("starting", "running", "degraded", "stopping")
        )
        self.start_recording_button.setEnabled(
            recording_state in ("stopped", "error")
            and qbot_state in ("running", "degraded")
        )
        self.stop_recording_button.setEnabled(
            recording_state in ("starting", "recording", "degraded", "stopping")
        )
        self.start_mapping_button.setEnabled(mapping_state == 'ready')
        self.finish_mapping_button.setEnabled(
            mapping_state in ("mapping", "playback_complete", "save_failed")
        )
        self.cancel_mapping_button.setEnabled(
            mapping_state in (
                "starting", "mapping", "playback_complete", "save_failed"
            )
        )
        self.new_map_button.setEnabled(mapping_state in ('saved', 'cancelled', 'error'))
        for event in state.get('events', []):
            if event.get('id', 0) > self.last_event_id:
                self.last_event_id = event['id']
                self.log.appendPlainText(event.get('message', ''))
        map_version, map_data = self.backend.get_map()
        if map_version != self.last_map_version:
            self.last_map_version = map_version
            if map_data:
                pixmap = QPixmap()
                if pixmap.loadFromData(map_data, 'PPM'):
                    self.map_pixmap = pixmap
                    self.update_map_scale()
            else:
                self.map_pixmap = None
                self.map_label.setPixmap(QPixmap())
                self.map_label.setText('LIVE MAP')
        windows_paths = state.get('windows_saved_paths')
        if windows_paths and mapping_state == 'saved':
            self.mapping_status_label.setToolTip(
                f"Mapping: {mapping_state}\n"
                f"View: {visualization_state}\n"
                f"PGM: {windows_paths['pgm']}\n"
                f"YAML: {windows_paths['yaml']}"
            )

    # ---------- Camera ----------
    def refresh_camera(self):
        if not self.camera_topic_fresh:
            return
        camera_version, camera_data = self.backend.get_camera()
        if camera_version == self.last_camera_version:
            return
        self.last_camera_version = camera_version
        if not camera_data:
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(camera_data, 'PPM'):
            self.camera_pixmap = pixmap
            self.camera_label.setText('')
            self.update_camera_scale()
        else:
            self.clear_camera('Camera frame could not be decoded')

    def clear_camera(self, message):
        self.camera_pixmap = None
        self.camera_label.setPixmap(QPixmap())
        self.camera_label.setText(message)

    def update_camera_scale(self):
        if self.camera_pixmap is None:
            return
        self.camera_label.setPixmap(
            self.camera_pixmap.scaled(
                self.camera_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def update_map_scale(self):
        if self.map_pixmap is None:
            return
        self.map_label.setPixmap(
            self.map_pixmap.scaled(
                self.map_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_camera_scale()
        self.update_map_scale()

    def closeEvent(self, event):
        self.refresh_timer.stop()
        self.camera_timer.stop()
        self.backend.close()
        event.accept()
if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = DeltaUI()
    window.show()
    sys.exit(app.exec())
