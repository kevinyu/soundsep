import sys
sys.path.append("code/soundsep")

import glob
import os
import re
import uuid
from functools import partial

import hdbscan
import numpy as np
import pandas as pd
import pyqtgraph as pg
import scipy
import scipy.ndimage
import sounddevice as sd
import umap
from fbs_runtime.application_context.PyQt5 import ApplicationContext
from PyQt5.QtCore import (Qt, QObject, QProcess, QSettings, QThread, QTimer,
        pyqtSignal, pyqtSlot)
from PyQt5.QtMultimedia import QAudioFormat, QAudioOutput, QMediaPlayer
from PyQt5.QtWidgets import QMainWindow
from PyQt5 import QtGui as gui
from PyQt5 import QtCore
from PyQt5 import QtWidgets as widgets
from sklearn.decomposition import PCA

from soundsig.sound import spectrogram
from soundsig.signal import bandpass_filter

from audio_utils import get_amplitude_envelope
from detection.thresholding import threshold_all_events
from interfaces.audio import LazyMultiWavInterface, LazyWavInterface


MAX_RECENT_FILES = 5


def _spec2icon(spec, dBNoise=40):
    """Convert a spectrogram into a grayscale Qt icon

    Chooses the channel with the higher amplitude and converts to dB
    """
    spec = np.abs(spec)
    if spec.ndim == 3:
        best_ch = np.where(spec == np.max(spec))[0][0]
        spec = spec[best_ch]

    # spec must be flipped upside down for icon
    spec = 20 * np.log10(spec)
    min_val = np.max(spec) - dBNoise
    spec[spec < min_val] = min_val
    spec = spec - np.min(spec)
    spec = spec[::-1]

    # Convert to grayscale pixel values
    spec = 255 * spec / np.max(spec)
    spec = np.require(spec, np.uint8, "C")

    qtimage = gui.QImage(
        spec.data,
        spec.shape[1],
        spec.shape[0],
        gui.QImage.Format_Indexed8
    )
    icon = gui.QPixmap.fromImage(qtimage)
    return gui.QIcon(icon)


class Singleton():
    """Alex Martelli implementation of Singleton (Borg)
    http://python-3-patterns-idioms-test.readthedocs.io/en/latest/Singleton.html"""
    _shared_state = {}

    def __init__(self):
        self.__dict__ = self._shared_state


class AppData(Singleton):
    def __init__(self):
        Singleton.__init__(self)
        self.reset()

    def get(self, key):
        return self._data.get(key)

    def set(self, key, value):
        self._data[key] = value

    def update(self, update_dict):
        for key, val in update_dict.items():
            self._data[key] = val

    def has(self, key):
        return key in self._data

    def clear(self, key):
        del self._data[key]

    def reset(self):
        self._data = {}


class BackgroundEmbedding(QObject):
    """Async worker for computing umap embedding"""
    finished = pyqtSignal(object)

    def __init__(self, data):
        super().__init__()
        self.data = data

    @pyqtSlot()
    def compute(self):
        points = self.data.reshape(len(self.data), -1)
        points = PCA(n_components=20, whiten=True).fit_transform(points)
        self.finished.emit(points[:, :2])
        return
        embedding = umap.UMAP(
            n_neighbors=10, repulsion_strength=10.0, min_dist=0.9
        ).fit_transform(points)
        self.finished.emit(embedding)


class SpectrogramWorker(QObject):
    """Async worker for computing spectrogram"""
    finished = pyqtSignal(object, object)

    def __init__(self, key, data, *args, **kwargs):
        super().__init__()
        self.key = key
        self.data = data
        self.args = args
        self.kwargs = kwargs

    @pyqtSlot()
    def compute(self):
        t_spec, f_spec, spec, _ = spectrogram(self.data, *self.args, **self.kwargs)
        self.finished.emit(self.key, spec)


def read_settings_dict(settings, heading, key_descriptions):
    """Read nested settings under one heading

    Tries to cast any bools to bool and numbers to float
    """
    casting_fns = dict(
        (v[0], v[3]) for v in key_descriptions
    )

    result = {}
    for key in settings.allKeys():
        if key.startswith("{}/".format(heading)):
            key_rest = key[len(heading) + 1:]
            casting_fn = casting_fns[key_rest]
            try:
                if isinstance(casting_fn, type):
                    val = settings.value(key, type=casting_fn)
                else:
                    val = casting_fn(settings.value(key))
            except:
                raise
            else:
                result[key_rest] = val

    return result


class BasePreferencesWindow(gui.QDialog):
    # This is a list of 4-tuples
    # Key, key description, default value, casting fn
    key_descriptions = []
    heading = None
    submitted = pyqtSignal()

    def __init__(self, settings, parent=None):
        super().__init__(parent=parent)
        self.settings = settings
        self.init_ui()

    def init_ui(self):
        # self.mainWidget = widgets.QWidget()
        self.mainLayout = widgets.QGridLayout()
        self.fields = {}

        current_settings = read_settings_dict(
            self.settings,
            self.heading,
            self.key_descriptions
        )
        for row, (key, description, default, _, choices) in enumerate(self.key_descriptions):
            label = widgets.QLabel(self, text=description)
            if choices is None:
                field = widgets.QLineEdit(self, text=str(current_settings.get(key, default)))
            else:
                field = widgets.QComboBox(self)
                for choice in choices:
                    field.addItem(choice)
                idx_of_current = choices.index(current_settings.get(key, choices[default]))
                field.setCurrentIndex(idx_of_current)
            self.mainLayout.addWidget(label, row, 0)
            self.mainLayout.addWidget(field, row, 1)
            self.fields[key] = (field, choices)

        restore_defaults_button = widgets.QPushButton("Restore Defaults")
        self.mainLayout.addWidget(restore_defaults_button, row + 1, 0)
        restore_defaults_button.clicked.connect(self.restore_defaults)

        submit_button = widgets.QPushButton("Submit")
        self.mainLayout.addWidget(submit_button, row + 1, 1)
        submit_button.clicked.connect(self.submit)

        self.setLayout(self.mainLayout)
        self.setMaximumWidth(self.width())
        self.setMaximumHeight(self.height())

    def validate(self, kwargs):
        return True

    def submit(self):
        to_submit = {}
        for key, (field, choices) in self.fields.items():
            if isinstance(field, widgets.QLineEdit):
                to_submit[key] = field.text()
            elif isinstance(field, widgets.QComboBox):
                to_submit[key] = choices[field.currentIndex()]

        if self.validate(to_submit):
            for (key, _, _, casting_fn, _) in self.key_descriptions:
                full_key = "/".join([self.heading, key])
                val = to_submit[key]
                val = casting_fn(val)
                self.settings.setValue(full_key, val)
            self.submitted.emit()
            self.close()
        else:
            widgets.QMessageBox.warning(
                self,
                "Error",
                "Validation failed.",
            )

    def restore_defaults(self):
        for (key, _, default, _, choices) in self.key_descriptions:
            if isinstance(self.fields[key][0], widgets.QLineEdit):
                self.fields[key][0].setText(str(default))
            elif isinstance(self.fields[key][0], widgets.QComboBox):
                self.fields[key][0].setCurrentIndex(default)


class AmpEnvPreferencesWindow(BasePreferencesWindow):
    key_descriptions = [
        ("lowpass", "Lowpass Cutoff (Hz)", 8000.0, float, None),
        ("highpass", "Highpass Cutoff (Hz)", 1000.0, float, None),
        ("rectify_lowpass", "Rectified Cutoff Lowpass (Hz)", 600.0, float, None),
        ("mode", "Mode", 0, str,
            ("broadband", "max_zscore")
        ),
    ]
    heading = "AMP_ENV_ARGS"

    def validate(self, kwargs):
        for key, val in kwargs.items():
            if key == "mode":
                if val not in ("broadband", "max_zscore"):
                    return False
                else:
                    continue
            try:
                float(val)
            except:
                return False
            else:
                pass

        if float(kwargs["highpass"]) > float(kwargs["lowpass"]):
            return False

        return True


class CommandLineOutput(widgets.QDialog):
    """Dialog window that shows the output of a python command
    """
    completed = pyqtSignal()

    def __init__(self, command, *args, parent=None):
        super().__init__(parent=parent)
        self._cmd = command
        self._args = args
        self._process = QProcess(self)
        self._process.finished.connect(self.completed.emit)
        self._process.finished.connect(self.closeProcess)
        self._process.readyRead.connect(self.on_data_received)
        self.init_ui()

    def init_ui(self):
        self.layout = widgets.QVBoxLayout()
        self.cmdLabel = widgets.QLabel(str(self._cmd))
        self.cmdLabel.setDisabled(True)

        self.consoleOut = widgets.QTextEdit()
        self.consoleOut.setReadOnly(True)

        self.buttonsLayout = widgets.QHBoxLayout()
        self.okayButton = widgets.QPushButton("OK [y]")
        self.okayButton.clicked.connect(partial(self._process.write, "y\n".encode()))
        self.closeButton = widgets.QPushButton("Close")
        self.closeButton.clicked.connect(self.closeProcess)
        self.buttonsLayout.addWidget(self.okayButton)
        self.buttonsLayout.addWidget(self.closeButton)

        self.layout.addWidget(self.cmdLabel)
        self.layout.addWidget(self.consoleOut)
        self.layout.addLayout(self.buttonsLayout)
        self.setLayout(self.layout)

    def closeProcess(self):
        self._process.close()
        self.consoleOut.setText("")
        self.close()

    def set_args(self, *args):
        self._args = args

    def run(self):
        self._process.start(self._cmd, self._args)

    def on_data_received(self):
        cursor = self.consoleOut.textCursor()
        cursor.movePosition(cursor.End)
        cursor.insertText(str(self._process.readAll(), "utf-8"))
        self.consoleOut.ensureCursorVisible()


class App(widgets.QMainWindow):
    """Main App instance with logic for file io
    """
    # Dictionary of data with keys wav, intervals, spectrograms, labels
    signalLoadedData = pyqtSignal(object)

    redrawSignal = pyqtSignal()

    # Cluster label, array of spectrograms, array of indices into original data
    clusterSelected = pyqtSignal(int, object, object)

    # Overall index of snippet selected
    snippetSelected = pyqtSignal(int)

    # When a time range is clicked and dragged
    rangeSelected = pyqtSignal(object, object)

    def __init__(self):
        super().__init__()
        self.title = "SoundSep"
        self.settings = QSettings("Theuniseen Lab", "Sound Separation")

        # There seems to be some issues with using threads
        # (at least the way I figured out to use them) on Mac.
        # So don't.
        if sys.platform == "darwin":
            self.settings.setValue("ASYNC_FLAG", False)
        else:
            self.settings.setValue("ASYNC_FLAG", True)

        self.loaded_data = AppData()

        self.amp_env_pref_window = AmpEnvPreferencesWindow(self.settings, parent=self)
        self.amp_env_pref_window.submitted.connect(self.redrawSignal.emit)

        self.run_call_detection_window = CommandLineOutput(
            "python",
            parent=None
        )
        self.run_call_detection_window.completed.connect(self._reload_dir)

        self.init_ui()
        self.init_actions()
        self.init_menus()
        self.update_open_recent_actions()

        self.thread = None

        if self.settings.value("OPEN_RECENT", []):
            self.load_dir(self.settings.value("OPEN_RECENT")[-1])

    def init_actions(self):
        self.open_directory_action = widgets.QAction("Open Directory", self)
        self.open_directory_action.triggered.connect(self.run_directory_loader)

        self.open_recent_actions = []
        for i in range(MAX_RECENT_FILES):
            action = widgets.QAction("", self)
            action.setVisible(False)
            action.triggered.connect(partial(self.open_recent, i))
            self.open_recent_actions.append(action)

        self.run_embedding_action = widgets.QAction("Run UMAP", self)
        self.run_embedding_action.triggered.connect(self.run_embedding)
        self.run_labeler_action = widgets.QAction("Run HDBSCAN Labeling", self)
        self.run_labeler_action.triggered.connect(self.run_labeler)
        self.save_embedding_action = widgets.QAction("Save Embedding", self)
        self.save_embedding_action.triggered.connect(self.save_embedding)
        self.save_labels_action = widgets.QAction("Save Labels", self)
        self.save_labels_action.triggered.connect(self.save_labels)
        self.save_intervals_action = widgets.QAction("Save Intervals", self)
        self.save_intervals_action.triggered.connect(self.save_intervals)

        self.toggle_amp_norm_action = widgets.QAction("Normalize Amplitude Envelope", self)
        self.toggle_amp_norm_action.setCheckable(True)
        self.toggle_amp_norm_action.setData(self.settings.value("AMP_NORM", False, type=bool))
        self.toggle_amp_norm_action.triggered.connect(self._toggle_amp_norm)

        self.show_pref_action = widgets.QAction("Amplitude Envelope Parameters", self)
        self.show_pref_action.triggered.connect(self.amp_env_pref_window.show)

        self.detect_all_calls_action = widgets.QAction("Detect All Calls", self)
        self.detect_calls_in_window_action = widgets.QAction("Detect Calls in Window", self)
        self.detect_calls_in_window_action.triggered.connect(self.run_detection_in_window)
        self.detect_all_calls_action.triggered.connect(self.run_detection_in_full)

    def _toggle_amp_norm(self, val):
        self.settings.setValue("AMP_NORM", val)
        self.redrawSignal.emit()

    def update_open_recent_actions(self):
        recently_opened = self.settings.value("OPEN_RECENT", [])
        for i in range(MAX_RECENT_FILES):
            if i < len(recently_opened):
                self.open_recent_actions[i].setText(recently_opened[-i])
                self.open_recent_actions[i].setData(recently_opened[-i])
                self.open_recent_actions[i].setVisible(True)
            else:
                self.open_recent_actions[i].setText(None)
                self.open_recent_actions[i].setData(None)
                self.open_recent_actions[i].setVisible(False)
        if not len(recently_opened):
            self.openRecentMenu.setDisabled(True)
        else:
            self.openRecentMenu.setDisabled(False)

    def init_ui(self):
        self.setWindowTitle(self.title)

    def init_menus(self):
        mainMenu = self.menuBar()

        fileMenu = mainMenu.addMenu("&File")
        fileMenu.addAction(self.open_directory_action)
        self.openRecentMenu = fileMenu.addMenu("&Open Recent")
        for i in range(MAX_RECENT_FILES):
            self.openRecentMenu.addAction(self.open_recent_actions[i])
        fileMenu.addSeparator()
        fileMenu.addAction(self.save_embedding_action)
        fileMenu.addAction(self.save_labels_action)
        fileMenu.addAction(self.save_intervals_action)

        viewMenu = mainMenu.addMenu("&View")
        soundMenu = viewMenu.addMenu("&Sound Display")
        soundMenu.addAction(self.toggle_amp_norm_action)

        settingsMenu = mainMenu.addMenu("&Settings")
        settingsMenu.addAction(self.show_pref_action)

        analysisMenu = mainMenu.addMenu("&Analysis")
        analysisMenu.addAction(self.run_embedding_action)
        analysisMenu.addAction(self.run_labeler_action)
        analysisMenu.addSeparator()
        analysisMenu.addAction(self.detect_calls_in_window_action)
        analysisMenu.addAction(self.detect_all_calls_action)
        self.display_viewer()

    def setup_shortcuts(self):
        self.save_shortcut = widgets.QShortcut(gui.QKeySequence.Save, self)
        self.save_shortcut.activated.connect(self.save_intervals)

    def display_viewer(self):
        self.main_view = MainView(self)
        self.setCentralWidget(self.main_view)
        self.show()

    def closeEvent(self, evt):
        if self.thread:
            self.thread.terminate()

    def run_embedding(self):
        """Compute a pca->umap embedding for the detected audio data async"""
        specs = self.loaded_data.get("spectrograms")
        self.worker = BackgroundEmbedding(specs)
        self.run_embedding_action.setDisabled(True)
        self.run_labeler_action.setDisabled(True)
        self.worker.finished.connect(self._on_embedding_completed)

        if self.settings.value("ASYNC_FLAG", False, type=bool):
            self._reset_thread()
            self.worker.moveToThread(self.thread)
            self.thread.started.connect(self.worker.compute)
            self.thread.start()
        else:
            self.worker.compute()

    def _on_embedding_completed(self, embedding):
        self.run_embedding_action.setDisabled(False)
        self.run_labeler_action.setDisabled(False)

        self.loaded_data.set("embedding", embedding)
        self.signalLoadedData.emit(self.loaded_data)

    def _reset_thread(self):
        self.thread = QThread(self)
        return self.thread

    def run_labeler(self):
        """Label the loaded data using a pca->umap embedding and hdbscan
        """
        if self.loaded_data.get("labels") is not None:
            msg = "Are you sure you want to overwrite current labels?"
            reply = widgets.QMessageBox.question(
                    self,
                    'Confirmation',
                    msg,
                    widgets.QMessageBox.Yes,
                    widgets.QMessageBox.No)
            if reply == widgets.QMessageBox.No:
                return

        if not self.loaded_data.has("embedding"):
            self.run_embedding()
            if self.settings.value("ASYNC_FLAG", False, type=bool):
                self.worker.finished.connect(self._run_labeler)
            else:
                self._run_labeler()
        else:
            self._run_labeler()

    def _run_labeler(self):
        embedding = self.loaded_data.get("embedding")
        labels = hdbscan.HDBSCAN().fit_predict(embedding)
        self.loaded_data.set("labels", labels)
        self.signalLoadedData.emit(self.loaded_data)

    def run_directory_loader(self):
        """Dialog to read in a directory of wav files and intervals

        At some point we may have the gui generate the intervals file if it doesn't
        exist yet, but for now it must be precomputed.

        The directory should contain the following files (one wav per channel):
            - ch0.wav
            - ch1.wav
            - ch2.wav
            ...
            - intervals.npy
            - spectrograms.npy
        """
        options = widgets.QFileDialog.Options()
        selected_file = widgets.QFileDialog.getExistingDirectory(
            self,
            "Load directory",
            self.settings.value("OPEN_RECENT", ["."])[-1],
            options=options
        )

        if selected_file:
            self.load_dir(selected_file)

    def save_embedding(self):
        if self.loaded_data.has("embedding"):
            np.save(self.embedding_file, self.loaded_data.get("embedding"))
            widgets.QMessageBox.about(
                self,
                "Saved",
                "Embedding saved successfully.",
            )
        else:
            widgets.QMessageBox.warning(
                self,
                "Error",
                "No embedding loaded.",
            )

    def save_labels(self):
        if self.loaded_data.has("labels"):
            np.save(self.labels_file, self.loaded_data.get("labels"))
            widgets.QMessageBox.about(
                self,
                "Saved",
                "Labels saved successfully.",
            )
        else:
            widgets.QMessageBox.warning(
                self,
                "Error",
                "No labels found.",
            )

    def save_intervals(self):
        if self.loaded_data.has("intervals"):
            self.loaded_data.get("intervals").to_pickle(self.intervals_file)
            widgets.QMessageBox.about(
                self,
                "Saved",
                "Intervals saved successfully.",
            )
        else:
            widgets.QMessageBox.warning(
                self,
                "Error",
                "No intervals found.",
            )

    def _reload_dir(self):
        """Reload last loaded file without the frills"""
        self._load_dir(self.loaded_data.get("loaded_dir"))

    def open_recent(self, i):
        self.load_dir(self.settings.value("OPEN_RECENT")[-i])

    def load_dir(self, selected_file):
        if not os.path.isdir(selected_file):
            raise IOError("{} is not a directory".format(selected_file))

        # Update the open recent menu item
        open_recent = self.settings.value("OPEN_RECENT", [])
        try:
            idx = open_recent.index(selected_file)
        except ValueError:
            open_recent.append(selected_file)
        else:
            open_recent.pop(idx)
            open_recent.append(selected_file)
        open_recent = open_recent[-MAX_RECENT_FILES:]
        self.settings.setValue("OPEN_RECENT", open_recent)
        self.update_open_recent_actions()

        self.loaded_data.reset()
        self.loaded_data.set("view_ch", 0)
        self.loaded_data.set("current_step", 0)

        self._load_dir(selected_file)

    def _load_dir(self, selected_file):
        # Reset the loaded data and view position
        self.data_directory = os.path.join(selected_file, "outputs")
        wav_files = glob.glob(os.path.join(selected_file, "ch[0-9]*.wav"))
        self.intervals_file = os.path.join(self.data_directory, "intervals.pkl")
        self.spectrograms_file = os.path.join(self.data_directory, "spectrograms.npy")
        self.labels_file = os.path.join(self.data_directory, "labels.npy")
        self.embedding_file = os.path.join(self.data_directory, "embedding.npy")

        if not len(wav_files):
            raise IOError("No files matching {} found".format(
                    os.path.join(selected_file, regexp)))
        # if not os.path.exists(self.intervals_file):
        #     raise IOError("No file named {} exists".format(self.intervals_file))
        # if not os.path.exists(self.spectrograms_file):
        #     raise IOError("No file named {} exists".format(self.spectrograms_file))

        # TODO (kevin): Make it optional for intervals, spectrograms, and labels
        # to exist... we should be able to generate these
        if len(wav_files) > 1:
            wav_object = LazyMultiWavInterface.create_from_directory(selected_file)
        else:
            wav_object = LazyWavInterface(wav_files[0])

        self.loaded_data.set("wav", wav_object)
        if os.path.exists(self.intervals_file):
            self.loaded_data.set(
                "intervals",
                pd.read_pickle(self.intervals_file)
            )
        if os.path.exists(self.spectrograms_file):
            self.loaded_data.set("spectrograms", np.load(self.spectrograms_file)[()])
        if os.path.exists(self.embedding_file):
            self.loaded_data.set("embedding", np.load(self.embedding_file)[()])
        if os.path.exists(self.labels_file):
            self.loaded_data.set("labels", np.load(self.labels_file)[()])
        self.loaded_data.set("loaded_dir", selected_file)
        self.signalLoadedData.emit(self.loaded_data)

    def run_detection_in_window(self):
        current_step = self.loaded_data.get("current_step")
        current_range = self.loaded_data.get("selected_range")
        if current_range is None:
            t0 = current_step * (self.settings.value("AUDIO_VIEW/window_size", 5.0) / 10.0)
            t1 = t0 + self.settings.value("AUDIO_VIEW/window_size", 5.0)
        else:
            t0, t1 = current_range

        events = threshold_all_events(
            self.loaded_data.get("wav"),
            window_size=None,
            channel=self.loaded_data.get("view_ch"),
            t_start=t0,
            t_stop=t1,
            ignore_width=0.005,
            min_size=0.005,
            fuse_duration=0.01,
            threshold_z=2.0,
            amp_env_mode=self.settings.value("AMP_ENV_ARGS/mode", "broadband")

        )
        self.loaded_data.set("temporary_intervals", np.array(events))
        self.redrawSignal.emit()

    def run_detection_in_full(self):
        """Wrapper around the script for detecting intervals
        """
        # Detection channels
        loaded_wav = self.loaded_data.get("wav")
        ch_dfs = []
        for ch in range(loaded_wav.n_channels):
            events = threshold_all_events(
                loaded_wav,
                window_size=10.0,
                channel=ch,
                ignore_width=0.005,
                min_size=0.005,
                fuse_duration=0.01,
                threshold_z=2.0,
                amp_env_mode=self.settings.value("AMP_ENV_ARGS/mode", "broadband")
            )
            ch_df = pd.DataFrame(events, columns=["t_start", "t_stop"])
            ch_df["channel"] = ch * np.ones(len(ch_df))
            ch_dfs.append(ch_df)
        intervals = pd.concat(ch_dfs)
        intervals = intervals.sort_values(by="t_start")
        self.loaded_data.set("intervals", intervals)
        self.redrawSignal.emit()


class MainView(widgets.QWidget):
    """Container for the overall layout of the app

    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.init_ui()
        self.init_plots()

        self.loaded_data = AppData()

        self.parent().signalLoadedData.connect(self.on_data_load)

    def init_ui(self):
        self.topBar = widgets.QHBoxLayout()
        self.topBarLabel = widgets.QLabel("")
        self.topBarLabel.setDisabled(True)
        self.topBar.addWidget(self.topBarLabel)
        self.topBar.addStretch(1)
        self.topLeftBox = widgets.QGroupBox("Scatter 2D")
        self.topRightBox = widgets.QGroupBox("Sound Viewer")
        self.bottomLeftBox = widgets.QGroupBox("Clusters")
        self.bottomRightBox = widgets.QGroupBox("Detected Snippets")
        self.footerBox = widgets.QGroupBox("")

        self.bottomSplitter = widgets.QSplitter(Qt.Horizontal)
        self.bottomSplitter.addWidget(self.bottomLeftBox)
        self.bottomSplitter.addWidget(self.bottomRightBox)

        self.topSplitter = widgets.QSplitter(Qt.Horizontal)
        self.topSplitter.addWidget(self.topLeftBox)
        self.topSplitter.addWidget(self.topRightBox)

        self.mainLayout = widgets.QGridLayout()
        self.mainLayout.addLayout(self.topBar, 0, 0)
        self.mainLayout.addWidget(self.topSplitter, 1, 0, 1, 6)
        self.mainLayout.addWidget(self.bottomSplitter, 2, 0, 1, 6)

        self.mainLayout.setRowStretch(1, 1)
        self.mainLayout.setRowStretch(2, 1)

        for col in range(6):
            self.mainLayout.setColumnStretch(col, 1)

        self.setLayout(self.mainLayout)

        self.topLeftBox.setDisabled(True)
        self.topRightBox.setDisabled(True)

    def init_plots(self):
        self.scatter_widget = Scatter2DView(
            None,
            data_loaded_signal=self.parent().signalLoadedData
        )

        self.spectrogram_widget = AudioView(
            None,
            data_loaded_signal=self.parent().signalLoadedData,
            snippet_selected_signal=self.parent().snippetSelected,
            range_selected_signal=self.parent().rangeSelected,
            redraw_signal=self.parent().redrawSignal,
        )

        self.cluster_select_widget = ClusterSelectView(
            None,
            data_loaded_signal=self.parent().signalLoadedData,
            cluster_selected_signal=self.parent().clusterSelected
        )

        self.snippet_select_widget = SnippetSelectView(
            None,
            cluster_selected_signal=self.parent().clusterSelected,
            snippet_selected_signal=self.parent().snippetSelected
        )

        layout = widgets.QVBoxLayout()
        layout.addWidget(self.scatter_widget)
        layout.addStretch(1)
        self.topLeftBox.setLayout(layout)

        layout = widgets.QVBoxLayout()
        layout.addWidget(self.spectrogram_widget)
        layout.addStretch(1)
        self.topRightBox.setLayout(layout)

        layout = widgets.QVBoxLayout()
        layout.addWidget(self.cluster_select_widget)
        self.bottomLeftBox.setLayout(layout)

        layout = widgets.QVBoxLayout()
        layout.addWidget(self.snippet_select_widget)
        self.bottomRightBox.setLayout(layout)

    def on_data_load(self, data):
        self.topLeftBox.setDisabled(False)
        self.topRightBox.setDisabled(False)
        self.topBarLabel.setText(self.loaded_data.get("loaded_dir"))


class Scatter2DView(widgets.QWidget):
    """Panel for 2d scatter plot of data
    """
    valid_projections = ["pca", "umap"] #, "umap", "tsne"]

    def __init__(self, parent=None, data_loaded_signal=None):
        super().__init__(parent)
        self.init_ui()
        self.loaded_data = AppData()
        data_loaded_signal.connect(self.on_data_load)

    def init_ui(self):
        self.plot = pg.PlotWidget()
        pen = pg.mkPen((255, 255, 255, 255))
        self.scatter = pg.ScatterPlotItem(pen=pen, symbol="o", size=1)
        self.plot.setLimits(xMin=None, xMax=None, yMin=None, yMax=None)
        self.plot.addItem(self.scatter)
        self.plot.plotItem.setMouseEnabled(x=False, y=False)
        self.plot.hideAxis("left")
        self.plot.hideAxis("bottom")

        layout = widgets.QVBoxLayout()
        layout.addWidget(self.plot)
        layout.addStretch(1)
        self.setLayout(layout)

    def on_data_load(self, data):
        if not self.loaded_data.has("embedding"):
            self.setDisabled(True)
            self.set_data(None)
        else:
            self.setDisabled(False)
            self.set_data(self.loaded_data.get("embedding"))

    def set_data(self, embedding):
        if embedding is None:
            self.scatter.setData([])
            return

        self.scatter.setData([
            {"pos": [x, y]} for x, y in embedding
        ])

        dyn_range_x = np.max(embedding[:, 0]) - np.min(embedding[:, 0])
        dyn_range_y = np.max(embedding[:, 1]) - np.min(embedding[:, 1])
        self.plot.setLimits(
            xMin=np.min(embedding[:, 0]) - 0.1 * dyn_range_x,
            xMax=np.max(embedding[:, 0]) + 0.1 * dyn_range_x,
            yMin=np.min(embedding[:, 1]) - 0.1 * dyn_range_y,
            yMax=np.max(embedding[:, 1]) + 0.1 * dyn_range_y
        )

        return embedding


class SpectrogramViewBox(pg.ViewBox):
    """docstring for SpectrogramViewBox."""
    dragComplete = pyqtSignal(QtCore.QPointF, QtCore.QPointF)
    dragInProgress = pyqtSignal(QtCore.QPointF, QtCore.QPointF)
    clicked = pyqtSignal(QtCore.QPointF)

    def mouseDragEvent(self, event):
        event.accept()
        start_pos = self.mapSceneToView(event.buttonDownScenePos())
        end_pos = self.mapSceneToView(event.scenePos())
        if event.isFinish():
            self.dragComplete.emit(start_pos, end_pos)
        else:
            self.dragInProgress.emit(start_pos, end_pos)

    def mouseClickEvent(self, event):
        event.accept()
        self.clicked.emit(self.mapSceneToView(event.scenePos()))


class AudioView(widgets.QWidget):
    """Panel for viewing spectrogram of a time period

    1. Channel selection
    2. Play sample audio of selected window
    3. Show spectrogram
    4. Select time range with scrollbar

    Also include a second tab to switch between spectrogram and amplitude
    """
    win_size = 5.0  # seconds
    spec_sample_rate = 1000
    spec_freq_spacing = 50
    min_freq = 250
    max_freq = 8000
    line_playback_step = 10

    updateViewSignal = pyqtSignal()

    def __init__(
            self,
            parent=None,
            data_loaded_signal=None,
            snippet_selected_signal=None,
            range_selected_signal=None,
            redraw_signal=None
        ):
        super().__init__(parent)
        self.loaded_data = AppData()

        self.init_actions()
        self.init_ui()
        self.setup_shortcuts()

        self.settings = QSettings("Theuniseen Lab", "Sound Separation")

        self.t_step = self.win_size / 40
        # Set up playback line
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.advance_playback_line)
        self.reset_playback_line()

        self.thread = None

        self.range_selected_signal = range_selected_signal
        data_loaded_signal.connect(self.on_data_load)
        snippet_selected_signal.connect(self.on_snippet_selected)
        redraw_signal.connect(self.update_image)
        self.range_selected_signal.connect(self.on_range_selected)
        self.snippet_selected_signal = snippet_selected_signal

        self._lowres_preview_only = False

    def _set_channel(self, ch):
        self.loaded_data.set("view_ch", ch)
        self.update_image()

    def _set_n_channels(self, n):
        """Update radio buttons to match number of channels in current audio file
        """
        for i in reversed(range(self.channelSelectLayout.count())):
            item = self.channelSelectLayout.itemAt(i)
            if item.widget():
                item.widget().deleteLater()
            else:
                # Remove the spacer
                self.channelSelectLayout.removeItem(item)

        for i in range(n):
            button = widgets.QRadioButton("Ch{}".format(i))
            button.released.connect(partial(self._set_channel, i))
            self.channelSelectLayout.addWidget(button)
            if i == 0:
                # Have channel 0 checked by default
                button.setChecked(True)

        # Adds a spacer to keep all items left aligned
        self.channelSelectLayout.addStretch()

        play_button = widgets.QPushButton("Play")
        play_button.released.connect(self.play_audio)
        self.channelSelectLayout.addWidget(play_button)

    def play_audio(self):
        """Play the sound thats in the currently selected window"""
        selected_range = self.loaded_data.get("selected_range")
        if selected_range is None:
            self.reset_playback_line()
            sd.play(
                self._sig[:, self.loaded_data.get("view_ch")],
                self.loaded_data.get("wav").sampling_rate,
                blocking=False
            )
        else:
            base_t = self.t_step * self.loaded_data.get("current_step")
            start_idx = int(round((selected_range[0] - base_t)* self.loaded_data.get("wav").sampling_rate))
            stop_idx = int(round((selected_range[1] - base_t) * self.loaded_data.get("wav").sampling_rate))
            playback_line_idx = int(round((selected_range[0] - base_t) * self.spec_sample_rate))
            self.reset_playback_line(playback_line_idx)
            sd.play(
                self._sig[start_idx:stop_idx, self.loaded_data.get("view_ch")],
                self.loaded_data.get("wav").sampling_rate,
                blocking=False
            )

        self.timer.start(self.line_playback_step * 1000 / self.spec_sample_rate)

    def reset_playback_line(self, start_at=None):
        self.timer.stop()
        self._playback_line_pos = start_at or -1
        self.spectrogram_plot.playback_line.setValue(self._playback_line_pos)
        self.amplitude_plot.playback_line.setValue(self._playback_line_pos)

    def advance_playback_line(self):
        """Step the playback line forward in time

        Reset the line when it reaches the end
        """
        base_t = self.t_step * self.loaded_data.get("current_step")
        selected_range = self.loaded_data.get("selected_range")
        if selected_range is None:
            max_playback_line_pos = int(self.win_size * self.spec_sample_rate)
        else:
            max_playback_line_pos = int((selected_range[1] - base_t) * self.spec_sample_rate)
        if self._playback_line_pos < max_playback_line_pos:
            self._playback_line_pos += self.line_playback_step
            self.spectrogram_plot.playback_line.setValue(self._playback_line_pos)
            self.amplitude_plot.playback_line.setValue(self._playback_line_pos)
        else:
            self.reset_playback_line()

    def _get_drag_mode(self, click_location):
        """Use the click location to determine how the dragging will affect
        the selected range

        "move": moves the entire range
        "left": moves only the left boundary
        "right": moves only the right boundary
        "new": creates a new range
        """
        if not self.loaded_data.has("selected_range"):
            return "new", None

        elif self.loaded_data.has("selected_range"):
            current_range = self.loaded_data.get("selected_range")
            base_t = self.t_step * self.loaded_data.get("current_step")
            start_t, end_t = current_range
            start_idx = int(round((start_t - base_t) * self.spec_sample_rate))
            end_idx = int(round((end_t - base_t) * self.spec_sample_rate))
            buffer = (end_idx - start_idx) / 10

            if start_idx + buffer <= click_location.x() <= end_idx - buffer:
                return "move", (start_idx, end_idx)
            elif np.abs(start_idx - click_location.x()) < buffer:
                return "left", start_idx
            elif np.abs(end_idx - click_location.x()) < buffer:
                return "right", end_idx
            else:
                return "new", None

    def on_drag_in_progress(self, start, end):
        # Drag can move a range or select a range or clear a range
        drag_mode, extra = self._get_drag_mode(start)

        dx = end.x() - start.x()
        self._clear_drag_lines()
        if drag_mode == "move":
            start_idx, end_idx = extra
            self.spectrogram_plot.selected_range_line_start.setValue(start_idx + dx)
            self.spectrogram_plot.selected_range_line_stop.setValue(end_idx + dx)
            self.amplitude_plot.selected_range_line_start.setValue(start_idx + dx)
            self.amplitude_plot.selected_range_line_stop.setValue(end_idx + dx)
        elif drag_mode == "left":
            start_idx = extra
            self.spectrogram_plot.selected_range_line_start.setValue(start_idx + dx)
            self.amplitude_plot.selected_range_line_start.setValue(start_idx + dx)
        elif drag_mode == "right":
            end_idx = extra
            self.spectrogram_plot.selected_range_line_stop.setValue(end_idx + dx)
            self.amplitude_plot.selected_range_line_stop.setValue(end_idx + dx)
        elif drag_mode == "new":
            curve_1 = self._draw_drag_curves(self.spectrogram_plot, start, end)
            curve_2 = self._draw_drag_curves(self.amplitude_plot, start, end)
            self.drag_curves = {
                self.spectrogram_plot: curve_1,
                self.amplitude_plot: curve_2
            }

    def on_drag_complete(self, start, end):
        dx = 0

        drag_mode, extra = self._get_drag_mode(start)

        if drag_mode == "new":
            start_dt = start.x() / self.spec_sample_rate
            end_dt = end.x() / self.spec_sample_rate
            base_t = self.t_step * self.loaded_data.get("current_step")
            start_t = base_t + start_dt
            end_t = base_t + end_dt
        else:
            start_t, end_t = self.loaded_data.get("selected_range")
            base_t = self.t_step * self.loaded_data.get("current_step")
            dx = end.x() - start.x()
            if drag_mode == "left":
                start_idx = extra
                start_dt = (start_idx + dx) / self.spec_sample_rate
                new_start_t = base_t + start_dt
                start_t, end_t = min(new_start_t, end_t), max(new_start_t, end_t)
            elif drag_mode == "right":
                end_idx = extra
                end_dt = (end_idx + dx) / self.spec_sample_rate
                new_end_t = base_t + end_dt
                start_t, end_t = min(start_t, new_end_t), max(start_t, new_end_t)
            elif drag_mode == "move":
                start_idx, end_idx = extra
                start_dt = (start_idx + dx) / self.spec_sample_rate
                end_dt = (end_idx + dx) / self.spec_sample_rate
                start_t = base_t + start_dt
                end_t = base_t + end_dt

        self.range_selected_signal.emit(start_t, end_t)

    def _get_clicked_interval(self, click_loc):
        """Returns the df index of the clicked interval and its bounds

        returns None if the click is outside any known interval"""
        click_dt = click_loc.x() / self.spec_sample_rate
        base_t = self.t_step * self.loaded_data.get("current_step")
        click_t = base_t + click_dt

        intervals = self.loaded_data.get("intervals")

        found = np.where(
            (intervals["t_start"] <= click_t) &
            (intervals["t_stop"] >= click_t) &
            (intervals["channel"] == self.loaded_data.get("view_ch"))
        )[0]
        if len(found):
            return found[0], tuple(intervals.iloc[found[0]][["t_start", "t_stop"]])

    def on_click(self, loc):
        """Process a click event
        """
        clicked = self._get_clicked_interval(loc)
        self._clear_drag_lines()
        if clicked is None:
            self.range_selected_signal.emit(None, None)
        else:
            # TODO: might be nicer to have this eventually use
            #       self.snippet_selected_signal.emit(clicked[0])
            self.range_selected_signal.emit(clicked[1][0], clicked[1][1])

    def _draw_drag_curves(self, plot, start, end):
        pen = pg.mkPen((59, 124, 32, 255))
        self._clear_drag_lines()
        curve = pg.PlotCurveItem(
            [start.x(), end.x()],
            [start.y(), end.y()],
        )
        plot.addItem(curve)
        return curve

    def _clear_drag_lines(self):
        if len(self.drag_curves):
            self.spectrogram_plot.removeItem(self.drag_curves[self.spectrogram_plot])
            self.amplitude_plot.removeItem(self.drag_curves[self.amplitude_plot])

    def on_range_selected(self, start_t, end_t):
        if start_t is None or end_t is None:
            self._clear_drag_lines()
            if self.loaded_data.has("selected_range"):
                self.loaded_data.clear("selected_range")
            self.spectrogram_plot.selected_range_line_start.setValue(-1)
            self.spectrogram_plot.selected_range_line_stop.setValue(-1)
            self.amplitude_plot.selected_range_line_start.setValue(-1)
            self.amplitude_plot.selected_range_line_stop.setValue(-1)
        else:
            base_t = self.t_step * self.loaded_data.get("current_step")
            start_t, end_t = min(start_t, end_t), max(start_t, end_t)
            self.loaded_data.set("selected_range", (start_t, end_t))
            start_idx = int(round((start_t - base_t) * self.spec_sample_rate))
            end_idx = int(round((end_t - base_t) * self.spec_sample_rate))
            self.spectrogram_plot.selected_range_line_start.setValue(start_idx)
            self.spectrogram_plot.selected_range_line_stop.setValue(end_idx)
            self.amplitude_plot.selected_range_line_start.setValue(start_idx)
            self.amplitude_plot.selected_range_line_stop.setValue(end_idx)

    def init_actions(self):
        self.play_action = gui.QAction("Play Selected Audio")
        self.play_action.triggered.connect(self.play_audio)

    def init_ui(self):
        ### Spectrogram Plot
        self.spectrogram_viewbox = SpectrogramViewBox()
        self.spectrogram_plot = pg.PlotWidget(viewBox=self.spectrogram_viewbox)
        self.spectrogram_plot.plotItem.setMouseEnabled(x=False, y=False)
        self.image = pg.ImageItem()
        self.spectrogram_plot.playback_line = pg.InfiniteLine()
        self.spectrogram_plot.snippet_line_start = pg.InfiniteLine()
        self.spectrogram_plot.snippet_line_stop = pg.InfiniteLine()
        self.spectrogram_plot.selected_range_line_start = pg.InfiniteLine()
        self.spectrogram_plot.selected_range_line_stop = pg.InfiniteLine()
        self.spectrogram_plot.addItem(self.image)
        self.spectrogram_plot.addItem(self.spectrogram_plot.playback_line)
        self.spectrogram_plot.addItem(self.spectrogram_plot.snippet_line_start)
        self.spectrogram_plot.addItem(self.spectrogram_plot.snippet_line_stop)
        self.spectrogram_plot.addItem(self.spectrogram_plot.selected_range_line_start)
        self.spectrogram_plot.addItem(self.spectrogram_plot.selected_range_line_stop)
        self.spectrogram_plot.hideAxis("left")
        self.spectrogram_plot.hideButtons()  # Gets rid of "A" autorange button
        self.spectrogram_plot.getViewBox().setRange(
            xRange=(0, int(self.win_size * self.spec_sample_rate)),
            yRange=(0, int((self.max_freq - self.min_freq) / self.spec_freq_spacing)),
            padding=0,
            disableAutoRange=True
        )
        self.spectrogram_viewbox.dragComplete.connect(self.on_drag_complete)
        self.spectrogram_viewbox.dragInProgress.connect(self.on_drag_in_progress)
        self.spectrogram_viewbox.clicked.connect(self.on_click)

        self.drag_curves = {}

        self._drawn_intervals = []

        ### Amplitude Plot
        # The amplitude plot will use the same sampling rate as the spectrograms
        # ideally to make the playback line simpler...
        self.amplitude_viewbox = SpectrogramViewBox()
        self.amplitude_plot = pg.PlotWidget(viewBox=self.amplitude_viewbox)
        self.amplitude_plot.plotItem.setMouseEnabled(x=False, y=False)
        self.amplitude_plot.playback_line = pg.InfiniteLine()
        self.amplitude_plot.snippet_line_start = pg.InfiniteLine()
        self.amplitude_plot.snippet_line_stop = pg.InfiniteLine()
        self.amplitude_plot.selected_range_line_start = pg.InfiniteLine()
        self.amplitude_plot.selected_range_line_stop = pg.InfiniteLine()
        self.amplitude_plot.addItem(self.amplitude_plot.playback_line)
        self.amplitude_plot.addItem(self.amplitude_plot.snippet_line_start)
        self.amplitude_plot.addItem(self.amplitude_plot.snippet_line_stop)
        self.amplitude_plot.addItem(self.amplitude_plot.selected_range_line_start)
        self.amplitude_plot.addItem(self.amplitude_plot.selected_range_line_stop)
        self.amplitude_plot.hideAxis("left")
        self.amplitude_plot.hideButtons()  # Gets rid of "A" autorange button
        self.amplitude_plot.getViewBox().setRange(
            xRange=(0, int(self.win_size * self.spec_sample_rate)),
            yRange=(0, 1),
            padding=0,
            disableAutoRange=True
        )
        self.amplitude_viewbox.dragComplete.connect(self.on_drag_complete)
        self.amplitude_viewbox.dragInProgress.connect(self.on_drag_in_progress)
        self.amplitude_viewbox.clicked.connect(self.on_click)

        # Put the plots in a tab widget
        self.tab_panel = widgets.QTabWidget(self)
        self.tab_panel.addTab(self.spectrogram_plot, "Spectrogram")
        self.tab_panel.addTab(self.amplitude_plot, "Amplitude Envelope")

        # Other widgets in this panel
        self.channelSelectLayout = widgets.QHBoxLayout()
        self._set_n_channels(1)

        self.scrollbar = widgets.QScrollBar(Qt.Horizontal, self)
        self.scrollbar.setValue(0)
        self.scrollbar.setMinimum(0)
        self.scrollbar.setMaximum(100)
        self.scrollbar.sliderPressed.connect(self.on_slider_press)
        self.scrollbar.valueChanged.connect(self.change_range)
        self.scrollbar.sliderReleased.connect(self.on_slider_release)

        self.windowInfoLayout = widgets.QHBoxLayout()
        self.time_label = widgets.QLabel()
        self.windowInfoLayout.addWidget(self.time_label)
        self.windowInfoLayout.addStretch()

        layout = widgets.QVBoxLayout()
        layout.addLayout(self.channelSelectLayout)
        layout.addWidget(self.tab_panel)
        layout.addWidget(self.scrollbar)
        layout.addLayout(self.windowInfoLayout)
        layout.addStretch(1)
        self.setLayout(layout)

    def setup_shortcuts(self):
        self.play_shortcut = widgets.QShortcut(gui.QKeySequence("Space"), self)
        self.play_shortcut.activated.connect(self.play_audio)

        self.delete_shortcut = widgets.QShortcut(gui.QKeySequence.Delete, self)
        self.delete_shortcut.activated.connect(self.on_delete_selection)

        self.merge_shortcut = widgets.QShortcut(gui.QKeySequence("M"), self)
        self.merge_shortcut.activated.connect(self.on_merge_selection)

    def on_delete_selection(self):
        """Delete from [intervals] any intervals that are entirely contained within

        the currently selected range.
        """
        if not self.loaded_data.has("selected_range"):
            return

        if not self.loaded_data.has("intervals"):
            return

        selection_start, selection_stop = self.loaded_data.get("selected_range")
        df = self.loaded_data.get("intervals")
        selector = (
            ((df["t_start"] >= selection_start) & (df["t_stop"] <= selection_stop)) &
            (df["channel"] == self.loaded_data.get("view_ch"))
        )
        self.loaded_data.set("intervals", df[~selector].copy())
        self._draw_intervals()

    def on_merge_selection(self):
        if not self.loaded_data.has("selected_range"):
            return

        if not self.loaded_data.has("intervals"):
            return

        selection_start, selection_stop = self.loaded_data.get("selected_range")
        df = self.loaded_data.get("intervals")
        selector = (
            ((df["t_start"] >= selection_start) & (df["t_stop"] <= selection_stop)) &
            (df["channel"] == self.loaded_data.get("view_ch"))
        )

        if not len(df[selector]):
            return

        # Merge those selected into a single row
        # First: make a new df with the non-selected rows
        new_df = df[~selector].copy()

        # Second: Create a single row encapsulating the selected intervals
        new_row = {
            "t_start": np.min(df[selector]["t_start"]),
            "t_stop": np.max(df[selector]["t_stop"]),
            "channel": self.loaded_data.get("view_ch")
        }

        # Third: Add the row to the dataframe and resort by t_start
        new_df = new_df.append([new_row], ignore_index=False, sort=True)
        new_df = new_df.sort_values(by="t_start")

        self.loaded_data.set("intervals", new_df)
        self._draw_intervals()

    def on_slider_press(self):
        self._lowres_preview_only = True

    def on_slider_release(self):
        self._lowres_preview_only = False
        self.change_range(self.scrollbar.value())

    def on_data_load(self, data):
        self._update_scroll_bar()
        self.change_range(0)
        self._set_n_channels(self.loaded_data.get("wav").n_channels)

    def on_snippet_selected(self, idx):
        raise Exception("Function must be updated to work with DataFrames")
        t0, t1 = self.loaded_data.get("intervals")[idx]
        midpoint = np.mean([t0, t1])
        approx_start_time = midpoint - (self.win_size / 2)
        approx_step = approx_start_time // self.t_step
        self.change_range(approx_step)

        line_pos_start = (t0 - approx_step * self.t_step) * self.spec_sample_rate
        line_pos_stop = (t1 - approx_step * self.t_step) * self.spec_sample_rate

        self.spectrogram_plot.snippet_line_start.setValue(line_pos_start)
        self.spectrogram_plot.snippet_line_stop.setValue(line_pos_stop)
        self.amplitude_plot.snippet_line_start.setValue(line_pos_start)
        self.amplitude_plot.snippet_line_stop.setValue(line_pos_stop)

    def _update_scroll_bar(self):
        """Set scrollbar params to match current audio file length and window size"""
        # Set the scrollbar values
        t_last = self.loaded_data.get("wav").t_max - self.win_size
        page_step = 10
        single_step = 1
        steps = int(np.ceil(t_last / self.win_size)) * page_step

        self.scrollbar.setValue(0)
        self.scrollbar.setMinimum(0)
        self.scrollbar.setMaximum(steps)
        self.scrollbar.setSingleStep(single_step)
        self.scrollbar.setPageStep(page_step)

    def change_range(self, new_value):
        self.range_selected_signal.emit(None, None)
        # if self._lowres_preview_only:
        #     return

        self.spectrogram_plot.snippet_line_start.setValue(-1)
        self.spectrogram_plot.snippet_line_stop.setValue(-1)
        self.amplitude_plot.snippet_line_start.setValue(-1)
        self.amplitude_plot.snippet_line_stop.setValue(-1)
        self.loaded_data.set("current_step", new_value)
        self.scrollbar.setValue(new_value)
        self.update_image(lowres=self._lowres_preview_only)
        self._update_time_label()
        self._draw_intervals()
        self._draw_xaxis()

    def _nice_tick_spacing(self, win_size):
        """Compute a nice tick spacing for the given window size
        """
        if win_size < 60:
            first_guess = win_size / 10

        choices = [0.1, 0.2, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0]
        best_choice = np.searchsorted(choices, first_guess)
        return choices[best_choice]

    def _draw_xaxis(self):
        ax = self.spectrogram_plot.getAxis("bottom")
        base_t = self.t_step * self.loaded_data.get("current_step")

        ticks = []
        spacing = self._nice_tick_spacing(self.win_size)
        for t in np.arange(0.0, self.win_size, spacing):
            samples = int(np.round(t * self.spec_sample_rate))
            ticks.append([samples, np.around(base_t + t, 2)])

        ax.setTicks([ticks])

    def update_image(self, lowres=False):
        t1 = self.t_step * self.loaded_data.get("current_step")

        t2 = t1 + self.win_size
        t2 = min(t2, self.loaded_data.get("wav").t_max)

        t_arr, sig = self.loaded_data.get("wav").time_slice(t1, t2)
        sig -= np.mean(sig, axis=0)
        self._sig = sig

        sd.stop()
        self.reset_playback_line()

        self._update_image_spectrogram(sig, lowres=lowres)
        self._update_image_amplitude(sig, lowres=lowres)

        if not lowres:
            self._draw_intervals()

    def _draw_intervals(self):
        """Draws rectangles illustrating the intervals in the visible window
        """
        for rect in self._drawn_intervals:
            self.spectrogram_plot.removeItem(rect)
            self.amplitude_plot.removeItem(rect)

        self._drawn_intervals = []

        t1 = self.t_step * self.loaded_data.get("current_step")
        t2 = t1 + self.win_size

        # first find all intervals that either
        #  > end after t1 and before t2 OR
        #  > start after t1 and before t2
        if self.loaded_data.has("intervals"):
            intervals = self.loaded_data.get("intervals")
            if isinstance(intervals, pd.DataFrame):
                start_index = np.searchsorted(intervals["t_start"], t1)
                for idx in range(start_index, len(intervals)):
                    interval_t1, interval_t2, ch = intervals.iloc[idx][["t_start", "t_stop", "channel"]]
                    if ch != self.loaded_data.get("view_ch"):
                        continue
                    if interval_t1 > t2:
                        break
                    if (t1 <= interval_t2 <= t2) or (t1 <= interval_t1 <= t2):
                        for plot in [self.spectrogram_plot, self.amplitude_plot]:
                            viewbox = plot.getPlotItem().getViewBox()
                            _, pixel_size = viewbox.viewPixelSize()
                            ((_, _), (_, ymax)) = viewbox.viewRange()
                            rect = gui.QGraphicsRectItem(
                                (interval_t1 - t1) * self.spec_sample_rate,
                                ymax - 5 * pixel_size,
                                (interval_t2 - interval_t1) * self.spec_sample_rate,
                                10 * pixel_size)
                            rect.setPen(pg.mkPen(None))
                            rect.setBrush(pg.mkBrush("r"))
                            plot.addItem(rect)
                            self._drawn_intervals.append(rect)
            else:
                start_idx = np.searchsorted(intervals[:, 0], t1)
                for interval_t1, interval_t2 in intervals[start_idx:]:
                    if (t1 <= interval_t2 <= t2) or (t1 <= interval_t1 <= t2):
                        for plot in [self.spectrogram_plot, self.amplitude_plot]:
                            viewbox = plot.getPlotItem().getViewBox()
                            _, pixel_size = viewbox.viewPixelSize()
                            ((_, _), (_, ymax)) = viewbox.viewRange()
                            rect = gui.QGraphicsRectItem(
                                (interval_t1 - t1) * self.spec_sample_rate,
                                ymax - 2.5 * pixel_size,
                                (interval_t2 - interval_t1) * self.spec_sample_rate,
                                5 * pixel_size)
                            rect.setPen(pg.mkPen(None))
                            rect.setBrush(pg.mkBrush("r"))
                            plot.addItem(rect)
                            self._drawn_intervals.append(rect)

        temporary_intervals = self.loaded_data.get("temporary_intervals")
        if temporary_intervals is not None and len(temporary_intervals):
            start_idx = np.searchsorted(temporary_intervals[:, 0], t1)
            for interval_t1, interval_t2 in temporary_intervals[start_idx:]:
                if (t1 <= interval_t2 <= t2) or (t1 <= interval_t1 <= t2):
                    for plot in [self.spectrogram_plot, self.amplitude_plot]:
                        viewbox = plot.getPlotItem().getViewBox()
                        _, pixel_size = viewbox.viewPixelSize()
                        ((_, _), (_, ymax)) = viewbox.viewRange()
                        rect = gui.QGraphicsRectItem(
                            (interval_t1 - t1) * self.spec_sample_rate,
                            ymax - 18.5 * pixel_size,
                            (interval_t2 - interval_t1) * self.spec_sample_rate,
                            10 * pixel_size)
                        rect.setPen(pg.mkPen(None))
                        rect.setBrush(pg.mkBrush("g"))
                        plot.addItem(rect)
                        self._drawn_intervals.append(rect)

    def _on_spectrogram_completed(self, key, spec):
        if key != self._last_spec_key:
            return
        else:
            self._draw_spectrogram(spec)

    def _draw_spectrogram(self, spec):
        logspec = 20 * np.log10(np.abs(spec))
        max_b = logspec.max()
        min_b = logspec.max() - 40
        logspec[logspec < min_b] = min_b

        self.image.setImage(logspec.T)
        viewbox = self.spectrogram_plot.getPlotItem().getViewBox()

    def _update_image_spectrogram(self, sig, lowres=False):
        # Always makes a low resolution spectrogram and draw that first
        resolution_factors = (4, 5)
        t_spec, f_spec, spec, _ = spectrogram(
            sig[:, self.loaded_data.get("view_ch")],
            self.loaded_data.get("wav").sampling_rate,
            spec_sample_rate=self.spec_sample_rate / resolution_factors[0],
            freq_spacing=self.spec_freq_spacing * resolution_factors[1],
            min_freq=self.min_freq,
            max_freq=self.max_freq, cmplx=False
        )
        spec = np.repeat(spec, resolution_factors[1], axis=0)
        spec = np.repeat(spec, resolution_factors[0], axis=1)
        spec = scipy.ndimage.gaussian_filter(spec, (1, 1))
        self._draw_spectrogram(spec)

        # If we want to make a highres spectrogram as well, attempt to do that
        # in a second thread.
        if not lowres:
            # Since multiple spectrograms can be requested (scrolling is happening)
            # we don't want to accidentally draw old spectrograms. So, assign
            # the worker a unique key. When it returns its spectrogram, will
            # make sure the key matches the latest generated key
            # (in _on_spectrogram_completed)
            self._last_spec_key = uuid.uuid4().hex
            self.worker = SpectrogramWorker(
                self._last_spec_key,
                sig[:, self.loaded_data.get("view_ch")],
                self.loaded_data.get("wav").sampling_rate,
                spec_sample_rate=self.spec_sample_rate,
                freq_spacing=self.spec_freq_spacing,
                min_freq=self.min_freq,
                max_freq=self.max_freq, cmplx=False
            )
            self.worker.finished.connect(self._on_spectrogram_completed)
            if self.settings.value("ASYNC_FLAG", False, type=bool):
                self._reset_thread()
                self.worker.moveToThread(self.thread)
                self.thread.started.connect(self.worker.compute)
                self.thread.start()
            else:
                self.worker.compute()

    def _reset_thread(self):
        if self.thread is not None:
            self.thread.exit()
        self.thread = QThread(self)
        return self.thread

    def closeEvent(self):
        if self.thread is not None:
            self.thread.stop()

    def _update_image_amplitude(self, sig, lowres=False):
        """Updates the amp env plot

        Ignores the lowres flag
        """
        highlighter_pen = pg.mkPen((29, 224, 32, 255))
        bg_pen = pg.mkPen((204, 204, 204, 63))

        amp_env_max = 0
        for ch in range(sig.shape[1]):
            amp_env_settings = read_settings_dict(
                self.settings,
                AmpEnvPreferencesWindow.heading,
                AmpEnvPreferencesWindow.key_descriptions
            )
            amp_env = get_amplitude_envelope(
                sig[:, ch],
                fs=self.loaded_data.get("wav").sampling_rate,
                lowpass=amp_env_settings.get("lowpass", 8000.0),
                highpass=amp_env_settings.get("highpass", 1000.0),
                rectify_lowpass=amp_env_settings.get("rectify_lowpass", 600.0),
                mode=amp_env_settings.get("mode", "broadband")
            )
            # Downsample amp_env to spectrogram sampling rate
            downsample_to_n_samples = int(self.win_size * self.spec_sample_rate)
            amp_env = scipy.signal.resample(amp_env, downsample_to_n_samples)

            if self.settings.value("AMP_NORM", False, type=bool):
                amp_env = amp_env / np.max(amp_env)

            self.amplitude_plot.plot(
                list(np.arange(len(amp_env))),
                list(amp_env),
                clear=True if ch == 0 else False,
                pen=highlighter_pen if ch == self.loaded_data.get("view_ch") else bg_pen
            )
            amp_env_max = max(np.max(amp_env), amp_env_max)

        # Need to add the overlaied lines again since they were cleared by
        # the plot function above.
        self.amplitude_plot.addItem(self.amplitude_plot.playback_line)
        self.amplitude_plot.addItem(self.amplitude_plot.snippet_line_start)
        self.amplitude_plot.addItem(self.amplitude_plot.snippet_line_stop)
        self.amplitude_plot.addItem(self.amplitude_plot.selected_range_line_start)
        self.amplitude_plot.addItem(self.amplitude_plot.selected_range_line_stop)

        self.amplitude_plot.getViewBox().setRange(
            xRange=(0, len(amp_env)),
            yRange=(0, amp_env_max),
            padding=0,
            disableAutoRange=True
        )

    def _update_time_label(self):
        t1 = self.t_step * self.loaded_data.get("current_step")
        t2 = t1 + self.win_size
        self.time_label.setText("{:.2f}s - {:.2f}s".format(t1, t2))


class ClusterSelectView(widgets.QScrollArea):
    """A grid of pressable buttons for every cluster currently seen"""

    n_rows = 2

    def __init__(self, parent=None, data_loaded_signal=None, cluster_selected_signal=None):
        super().__init__(parent)
        self.loaded_data = AppData()
        self.init_ui()
        self.cluster_selected_signal = cluster_selected_signal
        data_loaded_signal.connect(self.on_data_load)

    def on_data_load(self, data):
        if not data.has("labels"):
            self.setDisabled(True)
        else:
            self.setDisabled(False)
        self.reset_buttons()

    def init_ui(self):
        self.frame = widgets.QGroupBox()
        self.layout = widgets.QGridLayout()
        self.frame.setLayout(self.layout)
        self.setWidget(self.frame)
        self.setWidgetResizable(True)

    def reset_buttons(self):
        self._button_positions = {}
        for i in reversed(range(self.layout.count())):
            item = self.layout.itemAt(i)
            if item.widget():
                item.widget().deleteLater()
            else:
                raise Exception("Not supposed to be anything else")

        if self.loaded_data.has("labels"):
            for idx, l in enumerate(np.unique(self.loaded_data.get("labels"))):
                row = idx % self.n_rows
                col = idx // self.n_rows
                cluster_select = ImgButton(
                    self._get_cluster_icon(l),
                    l,
                    button_callback=self._button_callback,
                    radio=True,
                    parent=self
                )
                self._button_positions[l] = (row, col)
                self.layout.addWidget(cluster_select, row, col, 1, 1)

    def _button_callback(self, label):
        self._button_positions = {}
        self.cluster_selected_signal.emit(
            label,
            self.loaded_data.get("spectrograms")[
                self.loaded_data.get("labels") == label
            ],
            np.where(self.loaded_data.get("labels") == label)[0]
        )

        # Manual implementation of mutually exclusive radio buttons
        # - only leave selected the currently chosen button
        for button_label, (row, col) in self._button_positions.items():
            button = self.layout.itemAtPosition(row, col)
            if button_label == label:
                button.widget().button.setChecked(True)
            else:
                button.widget().button.setChecked(False)

    def _get_cluster_icon(self, label):
        mean_spectrogram = np.mean(
            self.loaded_data.get("spectrograms")[
                self.loaded_data.get("labels") == label
            ],
            axis=0
        )
        return _spec2icon(mean_spectrogram)


class SnippetSelectView(widgets.QScrollArea):
    """A grid of pressable buttons for every cluster currently seen"""

    n_rows = 2

    def __init__(self, parent=None, cluster_selected_signal=None,
                snippet_selected_signal=None):
        super().__init__(parent)
        self._spectrograms = []

        self.init_ui()

        self.snippet_selected_signal = snippet_selected_signal
        cluster_selected_signal.connect(self.on_cluster_select)

    def init_ui(self):
        self.frame = widgets.QGroupBox()
        self.layout = widgets.QGridLayout()
        self.frame.setLayout(self.layout)
        self.setWidget(self.frame)
        self.setWidgetResizable(True)

    def reset_buttons(self):
        self._button_positions = {}
        for i in reversed(range(self.layout.count())):
            item = self.layout.itemAt(i)
            if item.widget():
                item.widget().deleteLater()
            else:
                raise Exception("Not supposed to be anything else")

        for i, idx in enumerate(self._indices):
            row = i % self.n_rows
            col = i // self.n_rows
            cluster_select = ImgButton(
                self._get_spectrogram_icon(i),
                idx,
                self._button_callback
            )
            self._button_positions[idx] = (row, col)
            self.layout.addWidget(cluster_select, row, col, 1, 1)

    def _get_spectrogram_icon(self, idx):
        # Need to flip the freq axis
        spec = np.abs(self._spectrograms[idx])
        return _spec2icon(spec)

    def _button_callback(self, idx):
        """Report the index of the selected snippet up the chain
        """
        modifiers = widgets.QApplication.keyboardModifiers()
        if modifiers == Qt.ShiftModifier:
            # select multiple mode - make the buttons pressed
            if idx in self._selected:
                self._selected.remove(idx)
            else:
                self._selected.add(idx)
        else:
            self._selected = set([idx])

        for button_idx, (row, col) in self._button_positions.items():
            button = self.layout.itemAtPosition(row, col)
            if button_idx in self._selected:
                button.widget().button.setChecked(True)
            else:
                button.widget().button.setChecked(False)

        self.snippet_selected_signal.emit(idx)

    def on_cluster_select(self, label, spectrograms, indices):
        self._spectrograms = spectrograms
        self._indices = indices
        self.reset_buttons()


class ImgButton(widgets.QFrame):
    """A pressable button that has a picture of a spectrogram on it
    """

    def __init__(self, icon, label, button_callback=None, radio=False, parent=None):
        super().__init__(parent)
        self._icon = icon
        self._label = label
        self._button_callback = button_callback
        self._radio = radio
        self.init_ui()

        if self._button_callback is not None:
            self.button.released.connect(partial(self._button_callback, label))

    def update_icon(self):
        self.button.setIcon(self._icon)

        # TODO (kevin): fix these vlaues or configure them
        self.button.setIconSize(QtCore.QSize(60, 70))
        self.button.setFixedSize(QtCore.QSize(65, 76))
        self.setFixedSize(QtCore.QSize(100, 115))

        self.label.setText("{}".format(self._label))

    def init_ui(self):
        self.label = widgets.QLabel("n")
        if self._radio:
            self.button = widgets.QRadioButton(parent=self.parent().frame)
        else:
            self.button = widgets.QPushButton()
            self.button.setCheckable(True)

        layout = widgets.QVBoxLayout()
        layout.addWidget(self.label)
        layout.addWidget(self.button)
        layout.addStretch(1)
        self.setLayout(layout)
        self.update_icon()


if __name__ == '__main__':
    appctxt = ApplicationContext()       # 1. Instantiate ApplicationContext
    window = App()
    window.show()
    exit_code = appctxt.app.exec_()      # 2. Invoke appctxt.app.exec_()
    sys.exit(exit_code)
