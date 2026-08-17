"""Dark theme stylesheet for MichaelTVPlayer."""


DARK_QSS = """
* { font-family: 'Segoe UI', 'Arial', sans-serif; font-size: 10pt; }
QMainWindow, QDialog, QWidget { background-color: #1e1e1e; color: #e6e6e6; }
QMenuBar { background-color: #2b2b2b; color: #e6e6e6; padding: 2px; border-bottom: 1px solid #333; }
QMenuBar::item { padding: 5px 10px; background: transparent; }
QMenuBar::item:selected { background-color: #3a3a3a; }
QMenu { background-color: #2b2b2b; color: #e6e6e6; border: 1px solid #3a3a3a; }
QMenu::item { padding: 6px 26px; }
QMenu::item:selected { background-color: #0a84ff; }
QMenu::separator { height: 1px; background: #3a3a3a; margin: 4px 8px; }
QPushButton { background-color: #3a3a3a; color: #e6e6e6; border: 1px solid #4a4a4a;
              padding: 6px 12px; border-radius: 4px; }
QPushButton:hover { background-color: #46464a; border-color: #5a5a5a; }
QPushButton:pressed { background-color: #0a84ff; }
QPushButton:disabled { color: #777; background: #2b2b2b; }
QLineEdit, QSpinBox, QComboBox { background-color: #2b2b2b; color: #e6e6e6;
                                 border: 1px solid #4a4a4a; padding: 5px; border-radius: 3px;
                                 selection-background-color: #0a84ff; }
QComboBox QAbstractItemView { background-color: #2b2b2b; color: #e6e6e6;
                              selection-background-color: #0a84ff; border: 1px solid #4a4a4a; }
QListWidget, QTreeWidget, QTableWidget { background-color: #252526; color: #e6e6e6;
                                         alternate-background-color: #2b2b2b; border: 1px solid #333; }
QListWidget::item, QTreeWidget::item { padding: 3px; }
QListWidget::item:selected, QTreeWidget::item:selected { background-color: #0a84ff; color: white; }
QListWidget::item:hover, QTreeWidget::item:hover { background-color: #333; }
QHeaderView::section { background-color: #3a3a3a; color: #e6e6e6; padding: 5px; border: none; }
QLabel { color: #e6e6e6; background: transparent; }
QCheckBox { color: #e6e6e6; spacing: 8px; }
QCheckBox::indicator { width: 16px; height: 16px; }
QTabWidget::pane { border: 1px solid #333; top: -1px; }
QTabBar::tab { background: #2b2b2b; color: #b0b0b0; padding: 8px 16px;
               border: 1px solid #333; border-bottom: none; border-top-left-radius: 4px;
               border-top-right-radius: 4px; margin-right: 2px; }
QTabBar::tab:selected { background: #1e1e1e; color: #ffffff; }
QTabBar::tab:hover:!selected { background: #333; }
QSlider::groove:horizontal { background: #4a4a4a; height: 5px; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #0a84ff; border-radius: 2px; }
QSlider::handle:horizontal { background: #e6e6e6; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }
QSlider::handle:horizontal:hover { background: #ffffff; }
QSlider::groove:vertical { background: #4a4a4a; width: 5px; border-radius: 2px; }
QSplitter::handle { background-color: #2a2a2a; }
QSplitter::handle:horizontal { width: 6px; }
QSplitter::handle:hover { background-color: #0a84ff; }
/* standard, Windows-style scrollbars */
QScrollBar:vertical { background: transparent; width: 12px; margin: 0; }
QScrollBar::handle:vertical { background: #4c4c4c; min-height: 28px;
                              border-radius: 3px; margin: 3px 2px 3px 2px; }
QScrollBar::handle:vertical:hover { background: #5f5f5f; }
QScrollBar::handle:vertical:pressed { background: #0a84ff; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 12px; margin: 0; }
QScrollBar::handle:horizontal { background: #4c4c4c; min-width: 28px;
                                border-radius: 3px; margin: 2px 3px 2px 3px; }
QScrollBar::handle:horizontal:hover { background: #5f5f5f; }
QScrollBar::handle:horizontal:pressed { background: #0a84ff; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }
QToolTip { background-color: #2b2b2b; color: #e6e6e6; border: 1px solid #4a4a4a; padding: 4px; }
QScrollArea { border: none; }
QGroupBox { border: 1px solid #4a4a4a; border-radius: 4px; margin-top: 10px; padding-top: 8px; }
"""


def apply_theme(app) -> None:
    """Apply the dark theme to a QApplication."""
    app.setStyleSheet(DARK_QSS)
