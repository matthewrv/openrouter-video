import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtGui import QImage, QPalette
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton, QStyleOptionViewItem
    from video_app.core import make_payload, read_inputs
    from video_app.gui import MainWindow, TokenDialog, image_dimensions
    from video_app.themes import HISTORY_STATUS_ROLE, THEMES
    HAS_QT = True
except ImportError:
    HAS_QT = False


@unittest.skipUnless(HAS_QT, "Для GUI-тестов установите зависимости через uv sync")
class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {
            "OPENROUTER_VIDEO_DATA_DIR": str(self.directory / "history"),
            "OPENROUTER_API_KEY": "",
        })
        self.environment.start()
        self.window = MainWindow()
        self.window.timer.stop()
        self.image = self.directory / "reference.png"
        image = QImage(40, 40, QImage.Format.Format_RGB32)
        image.fill(0x4488AA)
        image.save(str(self.image))

    def tearDown(self):
        self.wait_for_tasks()
        self.window.close()
        self.environment.stop()
        self.temporary.cleanup()

    def wait_for_tasks(self):
        deadline = time.monotonic() + 5
        while self.window.workers and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertFalse(self.window.workers, "Фоновый запрос не завершился")

    def test_submit_poll_and_clone(self):
        self.window.image_path.setText(str(self.image))
        self.window.prompt.setPlainText("Привет!")
        self.window.voice_id.setText("voice-123")
        self.window.duration.setValue(5)
        self.window.token = "test-token"
        with patch("video_app.gui.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), patch(
            "video_app.gui.ApiClient.request",
            side_effect=[
                {"id": "gen-test", "status": "pending"},
                {"id": "gen-test", "status": "completed",
                 "unsigned_urls": ["/api/v1/videos/gen-test/content"],
                 "urls": ["https://cdn.example/video.mp4"]},
            ],
        ) as request:
            self.window.submit_button.click()
            self.wait_for_tasks()
            local_id = self.window.selected_id
            self.assertIsNotNone(local_id)
            self.assertEqual(self.window.pages.currentIndex(), 1)
            refresh = next(
                widget for widget in self.window.details.findChildren(QPushButton)
                if widget.text() == "Обновить статус / ссылки"
            )
            refresh.click()
            self.wait_for_tasks()
            self.assertEqual(request.call_count, 2)
            payload = request.call_args_list[0].args[1]
            self.assertEqual(payload["duration"], 5)
            self.assertNotIn("size", payload)
            self.assertNotIn("resolution", payload)
            job = self.window.history.get(local_id)
            self.assertEqual(job["status"], "completed")
            self.assertEqual(len(job["response"]["urls"]), 1)
            self.assertEqual(job["inputs"]["duration"], 5)
            self.assertNotIn("size", job["inputs"])
            self.assertNotIn("test-token", str(job))
        clone = next(
            widget for widget in self.window.details.findChildren(QPushButton)
            if widget.text() == "Скопировать в новый запрос"
        )
        clone.click()
        self.assertEqual(self.window.pages.currentIndex(), 0)
        self.assertEqual(self.window.image_path.text(), str(self.image.resolve()))
        self.assertEqual(self.window.prompt.toPlainText(), "Привет!")
        self.assertEqual(self.window.voice_id.text(), "voice-123")
        self.assertEqual(self.window.duration.value(), 5)
        self.assertEqual(self.window.resolution_choice.currentData(), "auto")
        self.window.prompt.setPlainText("Новый текст")
        self.assertEqual(self.window.history.get(local_id)["inputs"]["prompt"], "Привет!")
        self.window.history_list.setCurrentRow(0)
        self.assertEqual(self.window.pages.currentIndex(), 1)

    def test_copy_full_url_and_file_mode(self):
        url = "https://cdn.example/video.mp4?signature=" + "x" * 500
        self.window.copy_url(url)
        self.assertEqual(self.app.clipboard().text(), url)
        self.window.text_mode.setCurrentIndex(1)
        self.assertFalse(self.window.prompt.isEnabled())
        self.assertTrue(self.window.text_file_row.isEnabled())

    def test_text_sources_block_alternative_controls_and_keep_values(self):
        self.window.prompt.setPlainText("Ручной текст")
        self.window.text_path.setText("script.txt")
        browse = self.window.text_file_row.findChild(QPushButton)
        self.assertTrue(self.window.prompt.isEnabled())
        self.assertFalse(self.window.text_path.isEnabled())
        self.assertFalse(browse.isEnabled())
        with patch("video_app.gui.QFileDialog.getOpenFileName") as choose_file:
            browse.click()
            self.window.choose_file(self.window.text_path, "Файл текста", "*.txt")
            choose_file.assert_not_called()
        self.window.text_mode.setCurrentIndex(1)
        self.assertFalse(self.window.prompt.isEnabled())
        self.assertFalse(self.window.prompt_label.isEnabled())
        self.assertTrue(self.window.text_path.isEnabled())
        self.assertTrue(browse.isEnabled())
        QTest.keyClicks(self.window.prompt, "ignored")
        self.assertEqual(self.window.prompt.toPlainText(), "Ручной текст")
        self.window.text_mode.setCurrentIndex(0)
        self.assertTrue(self.window.prompt.isEnabled())
        self.assertFalse(self.window.text_path.isEnabled())
        self.assertFalse(self.window.text_file_label.isEnabled())
        self.assertFalse(browse.isEnabled())
        QTest.keyClicks(self.window.text_path, "ignored")
        self.assertEqual(self.window.text_path.text(), "script.txt")
        self.assertEqual(self.window.prompt.toPlainText(), "Ручной текст")

    def test_theme_changes_palette_and_preserves_disabled_controls(self):
        for theme in ("dark", "light"):
            with self.subTest(theme=theme):
                self.window.theme_selector.setCurrentIndex(self.window.theme_selector.findData(theme))
                self.app.processEvents()
                palette = self.app.palette()
                brightness = palette.color(QPalette.ColorRole.Window).lightness()
                self.assertEqual(brightness < 128, theme == "dark")
                self.assertNotEqual(
                    palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Text),
                    palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text),
                )
                self.assertNotEqual(
                    palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Base),
                    palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base),
                )
                self.assertEqual(self.window.history.setting("theme"), theme)
                self.assertFalse(self.window.text_path.isEnabled())
                for label in self.window.findChildren(QLabel):
                    if label.text() in ("OpenRouter Video", "Новая генерация"):
                        self.assertEqual(
                            label.palette().color(QPalette.ColorRole.WindowText),
                            palette.color(QPalette.ColorRole.WindowText),
                        )
                dialog = TokenDialog(self.window)
                self.assertEqual(
                    dialog.palette().color(QPalette.ColorRole.Window),
                    palette.color(QPalette.ColorRole.Window),
                )
                dialog.deleteLater()

    def test_theme_is_restored_after_restart(self):
        self.window.theme_selector.setCurrentIndex(self.window.theme_selector.findData("dark"))
        self.window.close()
        self.window = MainWindow()
        self.window.timer.stop()
        self.assertEqual(self.window.theme_selector.currentData(), "dark")
        self.assertLess(self.app.palette().color(QPalette.ColorRole.Window).lightness(), 128)

    def test_removed_notices_are_absent(self):
        labels = [label.text() for label in self.window.findChildren(QLabel)]
        self.assertIn("История", labels)
        self.assertFalse(any("исходные параметры неизменяемы" in text for text in labels))
        self.assertFalse(any("Отправка может быть платной" in text for text in labels))

    def test_resolution_defaults_to_model_and_tracks_reference(self):
        self.assertEqual(self.window.resolution_choice.currentData(), "auto")
        self.assertEqual(self.window.duration.value(), 0)
        self.assertFalse(self.window.output_width.isEnabled())
        self.window.image_path.setText(str(self.image))
        self.assertEqual(self.window.resolution_choice.currentData(), "auto")
        inputs, image_bytes = read_inputs(str(self.image), "inline", "", "Текст", "voice")
        inputs.update(self.window.draft_generation_options(image_bytes))
        payload = make_payload(inputs, image_bytes)
        self.assertNotIn("size", payload)
        self.assertNotIn("resolution", payload)
        self.window.resolution_choice.setCurrentIndex(self.window.resolution_choice.findData("original"))
        self.assertIn("40 × 40", self.window.resolution_choice.currentText())
        image = QImage(160, 90, QImage.Format.Format_RGB32)
        image.fill(0x4488AA)
        other = self.directory / "other.png"
        image.save(str(other))
        self.window.image_path.setText(str(other))
        self.assertIn("160 × 90", self.window.resolution_choice.currentText())
        self.window.image_path.clear()
        self.assertIn("выберите изображение", self.window.resolution_choice.currentText())

    def test_image_dimensions_use_snapshot_bytes(self):
        original_bytes = self.image.read_bytes()
        self.assertEqual(image_dimensions(original_bytes), (40, 40))
        self.assertIsNone(image_dimensions(b"not an image"))
        self.window.image_path.setText(str(self.image))
        self.window.resolution_choice.setCurrentIndex(self.window.resolution_choice.findData("original"))
        image = QImage(200, 100, QImage.Format.Format_RGB32)
        image.fill(0x4488AA)
        image.save(str(self.image))
        self.assertIn("40 × 40", self.window.resolution_choice.currentText())
        options = self.window.draft_generation_options(self.image.read_bytes())
        self.assertEqual(options["size"], "200x100")
        self.assertEqual(options["image_size"], "200x100")

    def test_resolution_modes_control_payload_and_custom_fields(self):
        image_bytes = self.image.read_bytes()
        self.window.duration.setValue(6)
        for mode in ("original", "auto", "720p", "1080p", "custom"):
            with self.subTest(mode=mode):
                self.window.resolution_choice.setCurrentIndex(self.window.resolution_choice.findData(mode))
                self.assertEqual(self.window.output_width.isEnabled(), mode == "custom")
                self.assertEqual(self.window.output_height.isEnabled(), mode == "custom")
                self.window.output_width.setValue(800)
                self.window.output_height.setValue(600)
                options = self.window.draft_generation_options(image_bytes)
                payload = make_payload({
                    "model": "heygen/avatar-iv", "prompt": "Текст",
                    "voice_id": "voice", "image_mime": "image/png", **options,
                }, image_bytes)
                self.assertEqual(payload["duration"], 6)
                if mode in ("720p", "1080p"):
                    self.assertEqual(payload["resolution"], mode)
                    self.assertNotIn("size", payload)
                elif mode in ("original", "custom"):
                    self.assertEqual(payload["size"], "40x40" if mode == "original" else "800x600")
                    self.assertNotIn("resolution", payload)
                else:
                    self.assertNotIn("resolution", payload)
                    self.assertNotIn("size", payload)
        self.window.duration.setValue(0)
        self.assertNotIn("duration", self.window.draft_generation_options(image_bytes))

    def test_unknown_original_dimensions_require_explicit_alternative(self):
        self.window.resolution_choice.setCurrentIndex(self.window.resolution_choice.findData("original"))
        with self.assertRaises(ValueError):
            self.window.draft_generation_options(b"invalid")
        self.window.resolution_choice.setCurrentIndex(self.window.resolution_choice.findData("auto"))
        options = self.window.draft_generation_options(b"invalid")
        self.assertNotIn("size", options)

    def test_custom_generation_settings_survive_history_and_clone(self):
        self.window.image_path.setText(str(self.image))
        self.window.duration.setValue(12)
        self.window.resolution_choice.setCurrentIndex(self.window.resolution_choice.findData("custom"))
        self.window.output_width.setValue(800)
        self.window.output_height.setValue(600)
        inputs, image_bytes = read_inputs(str(self.image), "inline", "", "Текст", "voice")
        inputs.update(self.window.draft_generation_options(image_bytes))
        local_id = self.window.history.create(inputs, image_bytes)
        self.window.show_job(local_id)
        labels = [label.text() for label in self.window.details.findChildren(QLabel)]
        self.assertIn("12 с", labels)
        self.assertIn("800 × 600 (своё)", labels)
        self.window.clone_job(local_id)
        self.assertEqual(self.window.duration.value(), 12)
        self.assertEqual(self.window.resolution_choice.currentData(), "custom")
        self.assertEqual(self.window.output_width.value(), 800)
        self.assertEqual(self.window.output_height.value(), 600)
        self.assertTrue(self.window.output_width.isEnabled())
        self.window.duration.setValue(4)
        self.assertEqual(self.window.history.get(local_id)["inputs"]["duration"], 12)
        self.window.new_draft()
        self.assertEqual(self.window.duration.value(), 0)
        self.assertEqual(self.window.resolution_choice.currentData(), "auto")

    def test_explicit_original_resolution_is_preserved_when_cloning(self):
        self.window.resolution_choice.setCurrentIndex(self.window.resolution_choice.findData("original"))
        inputs, image_bytes = read_inputs(str(self.image), "inline", "", "Текст", "voice")
        inputs.update(self.window.draft_generation_options(image_bytes))
        local_id = self.window.history.create(inputs, image_bytes)
        self.window.clone_job(local_id)
        self.assertEqual(self.window.resolution_choice.currentData(), "original")
        self.assertEqual(self.window.draft_generation_options(image_bytes)["size"], "40x40")

    def test_old_history_without_generation_settings_remains_compatible(self):
        inputs, image_bytes = read_inputs(str(self.image), "inline", "", "Старый текст", "voice")
        local_id = self.window.history.create(inputs, image_bytes)
        self.window.show_job(local_id)
        labels = [label.text() for label in self.window.details.findChildren(QLabel)]
        self.assertIn("Не задана", labels)
        self.assertIn("По выбору модели", labels)
        self.window.clone_job(local_id)
        self.assertEqual(self.window.duration.value(), 0)
        self.assertEqual(self.window.resolution_choice.currentData(), "auto")
        options = self.window.draft_generation_options(image_bytes)
        self.assertNotIn("duration", options)
        self.assertNotIn("resolution", options)
        self.assertNotIn("size", options)

    def create_history_job(self, status, error=""):
        inputs, image_bytes = read_inputs(str(self.image), "inline", "", "Текст", "voice")
        local_id = self.window.history.create(inputs, image_bytes)
        self.window.history.update(local_id, status=status, error=error)
        return local_id

    def history_style_option(self, row):
        option = QStyleOptionViewItem()
        option.initFrom(self.window.history_list)
        index = self.window.history_list.model().index(row, 0)
        self.window.history_delegate.initStyleOption(option, index)
        return option

    def test_history_status_colors_for_both_themes(self):
        statuses = {
            self.create_history_job("completed"): "success",
            self.create_history_job("completed", "Не удалось обновить ссылки"): "success",
            self.create_history_job("pending"): None,
            self.create_history_job("in_progress"): None,
            self.create_history_job("submitting"): None,
            self.create_history_job("pending", "Ошибка polling"): "error",
        }
        for status in ("failed", "cancelled", "expired", "uncertain", "submission_error"):
            statuses[self.create_history_job(status)] = "error"
        self.window.refresh_history()
        for theme in ("light", "dark"):
            self.window.theme_selector.setCurrentIndex(self.window.theme_selector.findData(theme))
            self.app.processEvents()
            for row in range(self.window.history_list.count()):
                item = self.window.history_list.item(row)
                category = statuses[item.data(Qt.ItemDataRole.UserRole)]
                with self.subTest(theme=theme, row=row, category=category):
                    self.assertEqual(item.data(HISTORY_STATUS_ROLE), category)
                    option = self.history_style_option(row)
                    if category is None:
                        self.assertEqual(option.backgroundBrush.style(), Qt.BrushStyle.NoBrush)
                        self.assertEqual(
                            option.palette.color(QPalette.ColorRole.Highlight),
                            self.app.palette().color(QPalette.ColorRole.Highlight),
                        )
                    else:
                        colors = THEMES[theme]["history"][category]
                        self.assertEqual(option.backgroundBrush.color().name(), colors["background"])
                        self.assertEqual(option.palette.color(QPalette.ColorRole.Text).name(), colors["text"])
                        self.assertEqual(
                            option.palette.color(QPalette.ColorRole.Highlight).name(),
                            colors["selected_background"],
                        )
                        self.assertEqual(
                            option.palette.color(QPalette.ColorRole.HighlightedText).name(),
                            colors["selected_text"],
                        )

    def test_history_colors_update_with_status(self):
        local_id = self.create_history_job("pending")
        self.window.refresh_history()
        self.assertIsNone(self.window.history_list.item(0).data(HISTORY_STATUS_ROLE))
        self.window.history.update(local_id, status="failed", error="Ошибка генерации")
        self.window.refresh_history()
        self.assertEqual(self.window.history_list.item(0).data(HISTORY_STATUS_ROLE), "error")
        self.window.history.update(local_id, status="completed", error="")
        self.window.refresh_history()
        self.assertEqual(self.window.history_list.item(0).data(HISTORY_STATUS_ROLE), "success")

    def test_history_theme_change_preserves_selection(self):
        local_id = self.create_history_job("completed")
        self.window.refresh_history()
        self.window.history_list.setCurrentRow(0)
        selected = self.window.history_list.currentItem()
        before_option = self.history_style_option(0)
        before = before_option.backgroundBrush.color()
        self.window.theme_selector.setCurrentIndex(self.window.theme_selector.findData("dark"))
        after_option = self.history_style_option(0)
        self.assertNotEqual(after_option.backgroundBrush.color(), before)
        self.assertIs(self.window.history_list.currentItem(), selected)
        self.assertEqual(self.window.selected_id, local_id)

    def test_history_status_text_contrast(self):
        def luminance(hex_color):
            channels = [int(hex_color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [
                channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
                for channel in channels
            ]
            return sum(channel * weight for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

        for theme, palette in THEMES.items():
            for category, colors in palette["history"].items():
                for prefix in ("", "selected_"):
                    with self.subTest(theme=theme, category=category, prefix=prefix):
                        foreground = luminance(colors[prefix + "text"])
                        background = luminance(colors[prefix + "background"])
                        contrast = (max(foreground, background) + 0.05) / (min(foreground, background) + 0.05)
                        self.assertGreaterEqual(contrast, 4.5)


if __name__ == "__main__":
    unittest.main()
