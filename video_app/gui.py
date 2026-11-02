import json
import os
import sys
import uuid
from datetime import datetime
from functools import partial
from pathlib import Path

from PySide6.QtCore import (
    QBuffer, QIODevice, QLockFile, QObject, QRunnable, Qt, QThreadPool,
    QTimer, QUrl, Signal, Slot,
)
from PySide6.QtGui import QDesktopServices, QFont, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSpinBox, QSplitter, QStackedWidget, QVBoxLayout, QWidget,
)

from .core import (
    ApiClient, History, MODEL, STANDARD_RESOLUTIONS, api_url, data_directory,
    generation_options, make_payload, public_url, read_inputs,
)
from .themes import HISTORY_STATUS_ROLE, HistoryItemDelegate, apply_theme


KEYRING_SERVICE = "openrouter-video-desktop"
STATUS_LABELS = {
    "submitting": "Отправка…",
    "pending": "В очереди",
    "in_progress": "Генерируется",
    "completed": "Готово",
    "failed": "Ошибка генерации",
    "cancelled": "Отменено",
    "expired": "Истёк срок хранения",
    "uncertain": "Отправка не подтверждена",
    "submission_error": "Ошибка отправки",
}
ERROR_STATUSES = {"failed", "cancelled", "expired", "uncertain", "submission_error"}


class WorkerSignals(QObject):
    done = Signal(str, object, object)


class Worker(QRunnable):
    def __init__(self, task_id, operation):
        super().__init__()
        self.task_id = task_id
        self.operation = operation
        self.signals = WorkerSignals()

    def run(self):
        try:
            result = self.operation()
        except Exception as error:
            self.signals.done.emit(self.task_id, None, error)
        else:
            self.signals.done.emit(self.task_id, result, None)


def button(text, callback):
    widget = QPushButton(text)
    widget.clicked.connect(callback)
    return widget


def selectable_label(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def heading_label(text, size, weight=QFont.Weight.DemiBold):
    label = QLabel(text)
    font = label.font()
    font.setPixelSize(size)
    font.setWeight(weight)
    label.setFont(font)
    return label


def image_dimensions(source):
    if not source:
        return None
    if isinstance(source, bytes):
        buffer = QBuffer()
        buffer.setData(source)
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
    else:
        reader = QImageReader(str(Path(source).expanduser()))
    size = reader.size()
    return (size.width(), size.height()) if size.isValid() and not size.isEmpty() else None


def resolution_description(inputs):
    if inputs.get("size"):
        kind = "исходное" if inputs.get("resolution_mode") == "original" else "своё"
        return "{} ({})".format(inputs["size"].replace("x", " × "), kind)
    return inputs.get("resolution") or "По выбору модели"


class TokenDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Доступ к OpenRouter")
        self.resize(560, 180)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("API-токен OpenRouter"))
        self.token = QLineEdit(parent.token)
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("sk-or-v1-…")
        layout.addWidget(self.token)
        show = QCheckBox("Показать токен")
        show.toggled.connect(lambda visible: self.token.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        ))
        layout.addWidget(show)
        self.remember = QCheckBox("Запомнить в системном хранилище паролей")
        self.remember.setChecked(parent.history.setting("remember_token") == "yes")
        layout.addWidget(self.remember)
        layout.addWidget(selectable_label("Без этой опции токен хранится только в памяти до закрытия окна."))
        actions = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        actions.accepted.connect(self.accept)
        actions.rejected.connect(self.reject)
        layout.addWidget(actions)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.history = History()
        self.token = os.environ.get("OPENROUTER_API_KEY", "").strip()
        self.keyring_error = ""
        if self.history.setting("remember_token") == "yes":
            try:
                import keyring
                self.token = keyring.get_password(KEYRING_SERVICE, "api_key") or self.token
            except Exception as error:
                self.keyring_error = str(error)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(4)
        self.workers = {}
        self.polling = set()
        self.selected_id = None
        self.setWindowTitle("OpenRouter Video")
        self.resize(1140, 800)
        self.setMinimumSize(850, 600)
        self.build_ui()
        self.change_theme()
        self.refresh_history()
        self.update_token_label()
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.poll_due)
        self.timer.start()
        if self.keyring_error:
            QTimer.singleShot(0, lambda: QMessageBox.warning(
                self, "Хранилище паролей недоступно",
                "Не удалось прочитать токен. Введите его в настройках.\n" + self.keyring_error,
            ))

    def build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        header = QHBoxLayout()
        title = heading_label("OpenRouter Video", 24, QFont.Weight.Bold)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(QLabel("Тема"))
        self.theme_selector = QComboBox()
        self.theme_selector.addItem("Светлая", "light")
        self.theme_selector.addItem("Тёмная", "dark")
        saved_theme = self.theme_selector.findData(self.history.setting("theme", "light"))
        self.theme_selector.setCurrentIndex(max(0, saved_theme))
        self.theme_selector.currentIndexChanged.connect(self.change_theme)
        header.addWidget(self.theme_selector)
        self.token_label = QLabel()
        header.addWidget(self.token_label)
        header.addWidget(button("Настроить токен", self.configure_token))
        layout.addLayout(header)
        splitter = QSplitter()
        layout.addWidget(splitter, 1)
        sidebar = QWidget()
        side = QVBoxLayout(sidebar)
        side.addWidget(button("+ Новый запрос", self.new_draft))
        side.addWidget(QLabel("История"))
        self.history_list = QListWidget()
        self.history_delegate = HistoryItemDelegate(self.history_list, self.theme_selector.currentData())
        self.history_list.setItemDelegate(self.history_delegate)
        self.history_list.itemSelectionChanged.connect(self.select_job)
        side.addWidget(self.history_list, 1)
        splitter.addWidget(sidebar)
        self.pages = QStackedWidget()
        splitter.addWidget(self.pages)
        splitter.setSizes([310, 830])
        self.build_draft()
        self.details = QScrollArea()
        self.details.setWidgetResizable(True)
        self.pages.addWidget(self.details)
        self.statusBar().showMessage("Модель: " + MODEL + " · Автопроверка каждые 30 секунд")

    def path_field(self, title, file_filter):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        field = QLineEdit()
        field.setPlaceholderText(title)
        layout.addWidget(field, 1)
        browse = button("Выбрать…", lambda: self.choose_file(field, title, file_filter))
        layout.addWidget(browse)
        return row, field

    def build_draft(self):
        draft = QWidget()
        layout = QVBoxLayout(draft)
        heading = heading_label("Новая генерация", 20)
        layout.addWidget(heading)
        form = QFormLayout()
        image_row, self.image_path = self.path_field(
            "Путь к изображению", "Изображения (*.png *.jpg *.jpeg *.webp *.gif)"
        )
        form.addRow("Референс", image_row)
        self.text_mode = QComboBox()
        self.text_mode.addItem("Текст в поле ввода", "inline")
        self.text_mode.addItem("Текст из файла (UTF-8)", "file")
        form.addRow("Источник текста", self.text_mode)
        self.text_file_row, self.text_path = self.path_field(
            "Путь к текстовому файлу", "Текстовые файлы (*.txt *.md);;Все файлы (*)"
        )
        self.text_file_label = QLabel("Файл текста")
        form.addRow(self.text_file_label, self.text_file_row)
        self.voice_id = QLineEdit()
        self.voice_id.setPlaceholderText("Например: 80441555167a467e967ab9487d844a30")
        form.addRow("voice_id", self.voice_id)
        self.duration = QSpinBox()
        self.duration.setRange(0, 2147483647)
        self.duration.setSpecialValueText("Не задавать")
        self.duration.setSuffix(" с")
        self.duration.setToolTip("0 — не передавать duration; другое значение — целое число секунд.")
        form.addRow("Длительность", self.duration)
        self.resolution_choice = QComboBox()
        self.resolution_choice.addItem("Не задавать (по выбору модели)", "auto")
        self.resolution_choice.addItem("Исходное — выберите изображение", "original")
        for resolution in STANDARD_RESOLUTIONS:
            self.resolution_choice.addItem(resolution, resolution)
        self.resolution_choice.addItem("Своё — ширина × высота", "custom")
        self.resolution_choice.setToolTip(
            "Стандарт передаётся как resolution, исходный или свой размер — как size=WIDTHxHEIGHT. "
            "Допустимые значения зависят от модели; это не гарантия максимальной стоимости."
        )
        form.addRow("Разрешение", self.resolution_choice)
        self.custom_resolution_row = QWidget()
        custom_layout = QHBoxLayout(self.custom_resolution_row)
        custom_layout.setContentsMargins(0, 0, 0, 0)
        self.output_width = QSpinBox()
        self.output_height = QSpinBox()
        for field, value, label in (
            (self.output_width, 1280, "Ширина"),
            (self.output_height, 720, "Высота"),
        ):
            field.setRange(1, 2147483647)
            field.setValue(value)
            field.setAccessibleName(label)
            field.setSuffix(" px")
        custom_layout.addWidget(self.output_width)
        custom_layout.addWidget(QLabel("×"))
        custom_layout.addWidget(self.output_height)
        self.custom_resolution_label = QLabel("Свой размер")
        form.addRow(self.custom_resolution_label, self.custom_resolution_row)
        self.resolution_choice.currentIndexChanged.connect(self.sync_resolution_mode)
        self.image_path.textChanged.connect(self.update_reference_dimensions)
        self.sync_resolution_mode()
        form.addRow("Модель", QLabel(MODEL))
        layout.addLayout(form)
        self.prompt_label = QLabel("Текст для озвучивания")
        layout.addWidget(self.prompt_label)
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("Введите текст, который должен произнести аватар…")
        layout.addWidget(self.prompt, 1)
        self.text_mode.currentIndexChanged.connect(self.sync_text_mode)
        self.sync_text_mode()
        self.submit_button = button("Создать видео", self.submit)
        self.submit_button.setMinimumHeight(42)
        layout.addWidget(self.submit_button)
        self.pages.addWidget(draft)

    def sync_text_mode(self):
        from_file = self.text_mode.currentData() == "file"
        self.text_file_label.setEnabled(from_file)
        self.text_file_row.setEnabled(from_file)
        self.prompt_label.setEnabled(not from_file)
        self.prompt.setEnabled(not from_file)

    def sync_resolution_mode(self):
        custom = self.resolution_choice.currentData() == "custom"
        self.custom_resolution_label.setEnabled(custom)
        self.custom_resolution_row.setEnabled(custom)

    def update_reference_dimensions(self):
        dimensions = image_dimensions(self.image_path.text())
        if dimensions:
            label = "Исходное — {} × {} px".format(*dimensions)
        elif self.image_path.text():
            label = "Исходное — размер не определён"
        else:
            label = "Исходное — выберите изображение"
        self.resolution_choice.setItemText(self.resolution_choice.findData("original"), label)

    def draft_generation_options(self, image):
        dimensions = image_dimensions(image)
        mode = self.resolution_choice.currentData()
        resolution = mode if mode in STANDARD_RESOLUTIONS else None
        size = None
        if mode == "original":
            if not dimensions:
                raise ValueError(
                    "Не удалось определить исходный размер изображения. "
                    "Выберите стандартное разрешение, свой размер или «Не задавать»."
                )
            size = "{}x{}".format(*dimensions)
        elif mode == "custom":
            size = "{}x{}".format(self.output_width.value(), self.output_height.value())
        options = generation_options(self.duration.value() or None, resolution, size)
        options["resolution_mode"] = mode
        if dimensions:
            options["image_size"] = "{}x{}".format(*dimensions)
        return options

    def change_theme(self):
        theme = self.theme_selector.currentData()
        apply_theme(theme)
        self.history_delegate.set_theme(theme)
        self.history.set_setting("theme", theme)

    def choose_file(self, field, title, file_filter):
        if not field.isEnabled():
            return
        filename, _ = QFileDialog.getOpenFileName(self, title, field.text(), file_filter)
        if filename:
            field.setText(filename)

    def update_token_label(self):
        self.token_label.setText("Токен задан" if self.token else "Токен не задан")

    def configure_token(self):
        dialog = TokenDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        token = dialog.token.text().strip()
        if dialog.remember.isChecked() and not token:
            QMessageBox.warning(self, "Токен", "Введите токен или отключите запоминание.")
            return
        try:
            import keyring
            if dialog.remember.isChecked():
                keyring.set_password(KEYRING_SERVICE, "api_key", token)
            elif self.history.setting("remember_token") == "yes":
                try:
                    keyring.delete_password(KEYRING_SERVICE, "api_key")
                except keyring.errors.PasswordDeleteError:
                    pass
        except Exception as error:
            QMessageBox.warning(self, "Хранилище паролей", str(error))
            return
        self.history.set_setting("remember_token", "yes" if dialog.remember.isChecked() else "no")
        self.token = token
        self.update_token_label()
        self.poll_due()

    def new_draft(self):
        self.selected_id = None
        self.history_list.setCurrentRow(-1)
        self.image_path.clear()
        self.text_path.clear()
        self.prompt.clear()
        self.voice_id.clear()
        self.text_mode.setCurrentIndex(0)
        self.duration.setValue(0)
        self.resolution_choice.setCurrentIndex(self.resolution_choice.findData("auto"))
        self.output_width.setValue(1280)
        self.output_height.setValue(720)
        self.pages.setCurrentIndex(0)

    def clone_job(self, local_id):
        inputs = self.history.get(local_id)["inputs"]
        self.new_draft()
        self.image_path.setText(inputs["image_path"])
        self.text_path.setText(inputs["text_path"])
        self.text_mode.setCurrentIndex(1 if inputs["text_mode"] == "file" else 0)
        self.prompt.setPlainText(inputs["inline_text"])
        self.voice_id.setText(inputs["voice_id"])
        self.duration.setValue(inputs.get("duration") or 0)
        mode = inputs.get("resolution_mode")
        if mode is None:
            mode = inputs.get("resolution") or ("custom" if inputs.get("size") else "auto")
        index = self.resolution_choice.findData(mode)
        self.resolution_choice.setCurrentIndex(index if index >= 0 else self.resolution_choice.findData("auto"))
        if inputs.get("size"):
            width, height = map(int, inputs["size"].split("x"))
            self.output_width.setValue(width)
            self.output_height.setValue(height)
        self.statusBar().showMessage(
            "Создан редактируемый черновик. При отправке файлы будут прочитаны заново.", 12000
        )

    def refresh_history(self):
        self.history_list.blockSignals(True)
        self.history_list.clear()
        for job in self.history.all():
            inputs = json.loads(job["inputs"])
            text = " ".join(inputs["prompt"].split())
            preview = text[:45] + ("…" if len(text) > 45 else "")
            created = datetime.fromtimestamp(job["created"]).strftime("%d.%m.%Y %H:%M")
            item = QListWidgetItem("{} · {}\n{}".format(
                created, STATUS_LABELS.get(job["status"], job["status"]), preview
            ))
            item.setData(Qt.ItemDataRole.UserRole, job["local_id"])
            if job["status"] == "completed":
                item.setData(HISTORY_STATUS_ROLE, "success")
            elif job["status"] in ERROR_STATUSES or job["error"]:
                item.setData(HISTORY_STATUS_ROLE, "error")
            self.history_list.addItem(item)
            if job["local_id"] == self.selected_id:
                self.history_list.setCurrentItem(item)
        self.history_list.blockSignals(False)
        if self.selected_id:
            self.show_job(self.selected_id)

    def select_job(self):
        selected_items = self.history_list.selectedItems()
        if selected_items:
            item = selected_items[0]
            self.selected_id = item.data(Qt.ItemDataRole.UserRole)
            self.show_job(self.selected_id)

    def show_job(self, local_id):
        job = self.history.get(local_id)
        inputs = job["inputs"]
        content = QWidget()
        layout = QVBoxLayout(content)
        heading = heading_label(STATUS_LABELS.get(job["status"], job["status"]), 22)
        layout.addWidget(heading)
        actions = QHBoxLayout()
        actions.addWidget(button("Скопировать в новый запрос", partial(self.clone_job, local_id)))
        refresh = button("Обновить статус / ссылки", partial(self.manual_poll, local_id))
        refresh.setEnabled(bool(job["polling_url"]) and local_id not in self.polling)
        actions.addWidget(refresh)
        layout.addLayout(actions)
        layout.addWidget(selectable_label("Отправленный запрос доступен только для просмотра."))
        form = QFormLayout()
        for title, value in (
            ("Дата", datetime.fromtimestamp(job["created"]).strftime("%d.%m.%Y %H:%M:%S")),
            ("ID OpenRouter", job["remote_id"] or "Ещё не получен"),
            ("Модель", inputs["model"]),
            ("Изображение", inputs["image_path"]),
            ("Источник текста", inputs["text_path"] if inputs["text_mode"] == "file" else "Текст в окне"),
            ("voice_id", inputs["voice_id"]),
            ("Длительность", "{} с".format(inputs["duration"]) if inputs.get("duration") else "Не задана"),
            ("Разрешение", resolution_description(inputs)),
        ):
            form.addRow(title, selectable_label(value))
        layout.addLayout(form)
        pixmap = QPixmap()
        pixmap.loadFromData(job["image"])
        if not pixmap.isNull():
            image = QLabel()
            image.setPixmap(pixmap.scaled(
                320, 180, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            ))
            layout.addWidget(image)
        layout.addWidget(QLabel("Отправленный текст (снимок)"))
        prompt = QPlainTextEdit(inputs["prompt"])
        prompt.setReadOnly(True)
        prompt.setMinimumHeight(120)
        prompt.setMaximumHeight(240)
        layout.addWidget(prompt)
        if job["error"]:
            error = selectable_label(job["error"])
            error.setStyleSheet("color: #d66b51")
            layout.addWidget(error)
        response = job["response"]
        for field, title in (
            ("unsigned_urls", "unsigned_urls · скачивание с авторизацией"),
            ("urls", "urls · подписанные ссылки, открываются в браузере"),
        ):
            layout.addWidget(QLabel(title))
            links = response.get(field, [])
            if not links:
                layout.addWidget(QLabel("Пока нет ссылок"))
            for index, url in enumerate(links):
                row = QHBoxLayout()
                link = button(
                    "Видео {} ↗".format(index + 1),
                    partial(self.open_result, url, field == "unsigned_urls", local_id, index),
                )
                link.setToolTip(url)
                row.addWidget(link)
                row.addWidget(button("Копировать ссылку", partial(self.copy_url, url)))
                row.addWidget(button(
                    "Скачать…", partial(self.download, url, field == "unsigned_urls", local_id, index)
                ))
                row.addStretch()
                layout.addLayout(row)
        if job["status"] == "completed" and not response.get("urls"):
            layout.addWidget(selectable_label(
                "Видео готово. Подписанные urls проверяются ещё до часа после завершения. "
                "Затем их можно запросить кнопкой обновления; urls могут отсутствовать."
            ))
        if response.get("expires_at"):
            layout.addWidget(selectable_label("Срок хранения (expires_at): " + str(response["expires_at"])))
        usage = response.get("usage")
        if isinstance(usage, dict) and "cost" in usage:
            layout.addWidget(selectable_label("Стоимость API, USD: " + str(usage["cost"])))
        layout.addStretch()
        old = self.details.takeWidget()
        self.details.setWidget(content)
        if old:
            old.deleteLater()
        self.pages.setCurrentIndex(1)

    def copy_url(self, url):
        QApplication.clipboard().setText(url)
        self.statusBar().showMessage("Полная ссылка скопирована", 4000)

    def open_result(self, url, authenticated, local_id, index):
        if authenticated:
            self.download(url, True, local_id, index)
            return
        try:
            target = public_url(url)
            QDesktopServices.openUrl(QUrl(target))
        except ValueError as error:
            QMessageBox.warning(self, "Ссылка", str(error))

    def download(self, url, authenticated, local_id, index):
        if authenticated and not self.token:
            QMessageBox.warning(self, "Нужен токен", "Задайте токен для скачивания unsigned_urls.")
            return
        filename, _ = QFileDialog.getSaveFileName(
            self, "Сохранить видео", "video-{}-{}.mp4".format(local_id[:8], index + 1), "Видео (*.mp4)"
        )
        if not filename:
            return
        client = ApiClient(self.token)
        self.start_task(
            partial(client.download, url, filename, authenticated),
            lambda path: self.statusBar().showMessage("Сохранено: " + path, 12000),
            lambda error: QMessageBox.warning(self, "Ошибка скачивания", self.clean_error(error)),
        )
        self.statusBar().showMessage("Скачивание видео в фоне…")

    def clean_error(self, error):
        message = str(error)
        return message.replace(self.token, "[TOKEN]") if self.token else message

    def submit(self):
        if not self.token:
            self.configure_token()
            if not self.token:
                return
        try:
            inputs, image = read_inputs(
                self.image_path.text(), self.text_mode.currentData(),
                self.text_path.text(), self.prompt.toPlainText(), self.voice_id.text(),
            )
            inputs.update(self.draft_generation_options(image))
        except (OSError, UnicodeError, ValueError) as error:
            QMessageBox.warning(self, "Проверьте параметры", str(error))
            return
        answer = QMessageBox.question(
            self, "Отправить платный запрос?",
            "Создать видео через {}?\n\nЗапрос будет отправлен один раз. "
            "Его параметры станут нередактируемыми.".format(MODEL),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        local_id = self.history.create(inputs, image)
        self.selected_id = local_id
        self.refresh_history()
        client = ApiClient(self.token)
        self.start_task(
            lambda: client.request("", make_payload(inputs, image)),
            partial(self.submitted, local_id),
            partial(self.submission_failed, local_id),
        )

    def submitted(self, local_id, result):
        try:
            if not result.get("id") and not result.get("polling_url"):
                raise ValueError("В ответе нет id / polling_url: результат отправки неизвестен.")
            self.history.apply_response(local_id, result)
        except (ValueError, TypeError) as error:
            self.submission_failed(local_id, error)
            return
        self.refresh_history()

    def submission_failed(self, local_id, error):
        self.history.update(
            local_id, status="uncertain",
            error=self.clean_error(error) + "\nPOST автоматически не повторяется. "
            "Заявка могла быть принята сервером; проверьте историю OpenRouter перед новым запросом.",
            next_poll=None,
        )
        self.refresh_history()

    def manual_poll(self, local_id):
        if not self.token:
            self.configure_token()
        if self.token and local_id not in self.polling:
            self.poll_one(local_id, self.history.get(local_id)["polling_url"])

    def poll_due(self):
        if not self.token:
            return
        for job in self.history.due():
            if job["local_id"] not in self.polling:
                self.poll_one(job["local_id"], job["polling_url"])

    def poll_one(self, local_id, url):
        try:
            api_url(url)
        except ValueError as error:
            self.history.update(local_id, error=str(error), next_poll=None)
            self.refresh_history()
            return
        self.polling.add(local_id)
        self.start_task(
            partial(ApiClient(self.token).request, url),
            partial(self.polled, local_id),
            partial(self.poll_failed, local_id),
        )

    def polled(self, local_id, result):
        self.polling.discard(local_id)
        try:
            self.history.apply_response(local_id, result)
        except (ValueError, TypeError) as error:
            self.history.poll_error(local_id, self.clean_error(error))
        self.refresh_history()

    def poll_failed(self, local_id, error):
        self.polling.discard(local_id)
        self.history.poll_error(local_id, self.clean_error(error))
        self.refresh_history()

    def start_task(self, operation, success, failure):
        task_id = uuid.uuid4().hex
        worker = Worker(task_id, operation)
        worker.signals.done.connect(self.task_done)
        self.workers[task_id] = (worker, success, failure)
        self.pool.start(worker)

    @Slot(str, object, object)
    def task_done(self, task_id, result, error):
        worker, success, failure = self.workers.pop(task_id)
        if error is None:
            success(result)
        else:
            failure(error)

    def closeEvent(self, event):
        if self.workers:
            QMessageBox.information(
                self, "Дождитесь завершения операции",
                "Сейчас выполняется сетевой запрос или скачивание. "
                "Дождитесь завершения, чтобы не потерять результат отправки.",
            )
            event.ignore()
            return
        self.timer.stop()
        self.history.close()
        event.accept()


def run():
    app = QApplication(sys.argv)
    app.setApplicationName("OpenRouter Video")
    app.setOrganizationName("OpenRouterVideoDesktop")
    directory = data_directory()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = QLockFile(str(directory / "app.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.warning(
            None, "Приложение уже запущено",
            "Этот каталог истории уже используется другим экземпляром приложения.",
        )
        return 1
    window = MainWindow()
    window.show()
    return app.exec()
