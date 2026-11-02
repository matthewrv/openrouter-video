from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QPalette
from PySide6.QtWidgets import QApplication, QStyledItemDelegate


HISTORY_STATUS_ROLE = int(Qt.ItemDataRole.UserRole) + 1


THEMES = {
    "light": {
        "window": "#f2f4f8",
        "text": "#1b2432",
        "base": "#ffffff",
        "alternate": "#edf1f7",
        "button": "#e4e9f1",
        "border": "#bec7d5",
        "highlight": "#2d62d4",
        "link": "#2458c6",
        "disabled_base": "#e4e8ee",
        "disabled_text": "#848c99",
        "history": {
            "success": {
                "background": "#e4f3e9",
                "text": "#1c5033",
                "selected_background": "#bbdfc9",
                "selected_text": "#153f29",
            },
            "error": {
                "background": "#fbe6e7",
                "text": "#792f35",
                "selected_background": "#f1bfc4",
                "selected_text": "#63212b",
            },
        },
    },
    "dark": {
        "window": "#20242c",
        "text": "#eef1f6",
        "base": "#151a22",
        "alternate": "#252c37",
        "button": "#303846",
        "border": "#475163",
        "highlight": "#3d70d6",
        "link": "#8bb4ff",
        "disabled_base": "#292d35",
        "disabled_text": "#767d89",
        "history": {
            "success": {
                "background": "#183a2d",
                "text": "#c6ecd6",
                "selected_background": "#285d46",
                "selected_text": "#eefbf3",
            },
            "error": {
                "background": "#43252d",
                "text": "#ffd1d7",
                "selected_background": "#6b3541",
                "selected_text": "#fff0f2",
            },
        },
    },
}


class HistoryItemDelegate(QStyledItemDelegate):
    def __init__(self, parent, theme):
        super().__init__(parent)
        self.theme = theme

    def set_theme(self, theme):
        self.theme = theme
        self.parent().viewport().update()

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        colors = THEMES[self.theme]["history"].get(index.data(HISTORY_STATUS_ROLE))
        if colors:
            option.backgroundBrush = QBrush(QColor(colors["background"]))
            option.palette.setColor(QPalette.ColorRole.Text, QColor(colors["text"]))
            option.palette.setColor(QPalette.ColorRole.Highlight, QColor(colors["selected_background"]))
            option.palette.setColor(QPalette.ColorRole.HighlightedText, QColor(colors["selected_text"]))


def apply_theme(theme):
    colors = THEMES[theme]
    app = QApplication.instance()
    if app.style().objectName().lower() != "fusion":
        app.setStyle("Fusion")
    palette = app.style().standardPalette()
    roles = {
        QPalette.ColorRole.Window: colors["window"],
        QPalette.ColorRole.WindowText: colors["text"],
        QPalette.ColorRole.Base: colors["base"],
        QPalette.ColorRole.AlternateBase: colors["alternate"],
        QPalette.ColorRole.Text: colors["text"],
        QPalette.ColorRole.Button: colors["button"],
        QPalette.ColorRole.ButtonText: colors["text"],
        QPalette.ColorRole.ToolTipBase: colors["base"],
        QPalette.ColorRole.ToolTipText: colors["text"],
        QPalette.ColorRole.Highlight: colors["highlight"],
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.Accent: colors["highlight"],
        QPalette.ColorRole.Link: colors["link"],
        QPalette.ColorRole.LinkVisited: colors["link"],
        QPalette.ColorRole.PlaceholderText: colors["disabled_text"],
        QPalette.ColorRole.Light: colors["button"],
        QPalette.ColorRole.Midlight: colors["alternate"],
        QPalette.ColorRole.Mid: colors["border"],
        QPalette.ColorRole.Dark: colors["border"],
        QPalette.ColorRole.Shadow: colors["base"],
        QPalette.ColorRole.BrightText: "#ff6666",
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.PlaceholderText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(colors["disabled_text"]))
    for role in (
        QPalette.ColorRole.Base,
        QPalette.ColorRole.AlternateBase,
        QPalette.ColorRole.Button,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(colors["disabled_base"]))
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.Highlight, QColor(colors["border"])
    )
    app.setPalette(palette)
