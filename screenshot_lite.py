# -*- coding: utf-8 -*-
"""
ScreenshotLite 屏幕截图工具（基于原 debug_capture.py 增强）

保留原有功能：
    F2          立即截取鼠标所在屏幕（或命令行指定的显示器），保存到 input/ 目录
    F3          框选截图：触发时先冻结当前屏幕画面作为框选背景，再拖拽框选，
                避免视频等动态画面在框选期间继续播放导致截错帧
    ESC         取消框选
    自动编号    保存目录下按“年月日_编号”命名（如 20260811_1.png），同一天内自动顺延

新增 GUI：
    - 自定义“全屏截图 / 框选截图”热键（点击输入框后按下新按键组合即可录制）
    - 自定义保存目录（浏览选择或直接输入）
    - 开机自启动选项（写入当前用户的 Run 注册表键）
    - 实时日志窗口 + 系统托盘常驻（关闭窗口最小化到托盘）
    - 设置保存在 capture_config.json，重启后自动生效

用法：
    python screenshot_lite.py [monitor_index]
        monitor_index: 不传则自动截取鼠标所在屏幕；
                       传 1、2、3… 则按 mss 显示器索引截取对应屏幕
"""

import datetime
import json
import os
import sys
import time
import winreg

import cv2
import keyboard
import mss
import numpy as np
from PyQt5.QtCore import Qt, QObject, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QCursor, QGuiApplication, QImage, QPainter, QPen
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

# PyInstaller 冻结模式（--onefile）下 __file__ 位于临时解压目录，
# 配置与保存目录必须指向 exe 所在目录，否则重启即丢失。
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "capture_config.json")
DEFAULT_SAVE_DIR = os.path.join(BASE_DIR, "input")
DEFAULT_CONFIG = {
    "fullscreen_hotkey": "f2",
    "region_hotkey": "f3",
    "save_dir": DEFAULT_SAVE_DIR,
    "autostart": False,
}

AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "ScreenshotLite"

COOLDOWN = 0.6  # 秒，防止按住热键时连发重复截图
_last_shot_time = 0.0
CURRENT_SAVE_DIR = DEFAULT_SAVE_DIR
SUPPRESS = {"active": False}  # 录制热键期间屏蔽全局热键，避免误触发截图


class HotkeyBridge(QObject):
    """
    热键信号桥：keyboard 的回调运行在独立线程，不能直接操作 Qt 界面，
    因此由该对象的信号把事件排队投递到 Qt 主线程（跨线程信号为队列连接）。
    """
    full_screen = pyqtSignal()
    region = pyqtSignal()
    esc = pyqtSignal()


class SelectionOverlay(QWidget):
    """全屏冻结画面 + 半透明遮罩拖拽框选。"""
    selection_done = pyqtSignal(int, int, int, int)

    def __init__(self, screen, background_bgr=None):
        super().__init__(None)
        self._screen = screen
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.CrossCursor)
        if screen is not None:
            g = screen.geometry()
            self.setGeometry(g.x(), g.y(), g.width(), g.height())
        self.showFullScreen()

        self.start_x = None
        self.start_y = None
        self.end_x = None
        self.end_y = None
        self.drawing = False

        self.mask_color = QColor(0, 0, 0, 160)
        self.rect_color = QColor(0, 255, 0, 200)

        # 冻结的屏幕快照：作为框选背景，选中的区域将从该快照中裁切
        self.background_bgr = background_bgr
        self._bg_image = None
        if background_bgr is not None:
            h, w = background_bgr.shape[:2]
            rgb = cv2.cvtColor(background_bgr, cv2.COLOR_BGR2RGB)
            self._bg_image = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        if self._bg_image is not None and not self._bg_image.isNull():
            painter.drawImage(self.rect(), self._bg_image)
        else:
            painter.fillRect(self.rect(), self.mask_color)

        if self.start_x is not None and self.end_x is not None:
            x = min(self.start_x, self.end_x)
            y = min(self.start_y, self.end_y)
            w = abs(self.start_x - self.end_x)
            h = abs(self.start_y - self.end_y)

            if w > 5 and h > 5:
                painter.setCompositionMode(QPainter.CompositionMode_Clear)
                painter.fillRect(x, y, w, h, Qt.transparent)
                painter.setCompositionMode(QPainter.CompositionMode_SourceOver)

                pen = QPen(self.rect_color, 2, Qt.SolidLine)
                painter.setPen(pen)
                painter.drawRect(x, y, w, h)

                info = f"{w} x {h}"
                painter.setPen(QPen(Qt.white, 1))
                painter.drawText(x + 5, y - 10, info)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.start_x = event.x()
            self.start_y = event.y()
            self.end_x = self.start_x
            self.end_y = self.start_y
            self.drawing = True
            self.update()

    def mouseMoveEvent(self, event):
        if self.drawing:
            self.end_x = event.x()
            self.end_y = event.y()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.drawing:
            self.end_x = event.x()
            self.end_y = event.y()
            self.drawing = False

            x = min(self.start_x, self.end_x)
            y = min(self.start_y, self.end_y)
            w = abs(self.start_x - self.end_x)
            h = abs(self.start_y - self.end_y)

            if w > 20 and h > 20:
                g = self._screen.geometry()
                # 转成虚拟桌面绝对坐标，mss 按绝对坐标抓取，多屏才不会错位
                self.selection_done.emit(g.x() + x, g.y() + y, w, h)
            else:
                self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()

    def closeEvent(self, event):
        self.selection_done.emit(0, 0, 0, 0)


def next_save_path(save_dir):
    """按“年月日_编号”命名顺延（如 20260811_1.png），同一天内编号递增。"""
    os.makedirs(save_dir, exist_ok=True)
    prefix = datetime.date.today().strftime("%Y%m%d")
    numbers = []
    for name in os.listdir(save_dir):
        stem, ext = os.path.splitext(name)
        if ext.lower() == ".png" and stem.startswith(prefix + "_"):
            num = stem[len(prefix) + 1:]
            if num.isdigit():
                numbers.append(int(num))
    return os.path.join(save_dir, f"{prefix}_{max(numbers) + 1 if numbers else 1}.png")


def grab_bgr(monitor):
    """用 mss 截取指定显示器/区域，返回 BGR 图像。"""
    with mss.MSS() as sct:
        shot = sct.grab(monitor)
    return cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2BGR)


def screen_monitor(screen):
    """把 QScreen 转成 mss 的 monitor 字典（虚拟桌面绝对坐标）。"""
    if screen is not None:
        g = screen.geometry()
        return {"left": g.x(), "top": g.y(), "width": g.width(), "height": g.height()}
    # 无 QApplication 环境时回退到主显示器
    with mss.MSS() as sct:
        return dict(sct.monitors[1])


def cursor_monitor():
    """返回鼠标当前所在屏幕的 mss monitor 字典（虚拟桌面绝对坐标）。"""
    screen = QGuiApplication.screenAt(QCursor.pos())
    if screen is None:
        screen = QGuiApplication.primaryScreen()
    return screen_monitor(screen)


GUI_LOG = {"append": None}


def emit_log(msg):
    """同时输出到控制台和 GUI 日志窗口（若存在）。"""
    append = GUI_LOG["append"]
    if append is not None:
        append(msg)
    try:
        print(msg)
    except Exception:
        pass


def save_shot(img_bgr, source):
    global _last_shot_time
    now = time.time()
    if now - _last_shot_time < COOLDOWN:
        return
    _last_shot_time = now

    path = next_save_path(CURRENT_SAVE_DIR)
    # 用 imencode + tofile，兼容中文/特殊字符路径
    ok, buf = cv2.imencode(".png", img_bgr)
    if not ok:
        emit_log(f"[保存] {source} 编码失败")
        return
    buf.tofile(path)
    h, w = img_bgr.shape[:2]
    emit_log(f"[保存] {source} -> {path} ({w}x{h})")


def on_f2(monitor_index):
    try:
        if monitor_index >= 1:
            with mss.MSS() as sct:
                monitors = sct.monitors
                if monitor_index >= len(monitors):
                    emit_log(f"警告：显示器索引 {monitor_index} 不存在，改用鼠标所在屏幕")
                    monitor = cursor_monitor()
                else:
                    monitor = monitors[monitor_index]
        else:
            monitor = cursor_monitor()
        img = grab_bgr(monitor)
        save_shot(img, "全屏截图")
    except Exception as e:
        emit_log(f"全屏截图失败: {e}")


def on_f3(overlay_ref):
    if overlay_ref["overlay"] is not None:
        return
    screen = QGuiApplication.screenAt(QCursor.pos())
    if screen is None:
        screen = QGuiApplication.primaryScreen()
    try:
        # 先冻结当前屏幕画面作为框选背景，避免视频等动态画面在框选期间继续播放
        frozen = grab_bgr(screen_monitor(screen))
    except Exception as e:
        emit_log(f"框选截图准备失败（无法冻结画面）: {e}")
        return
    overlay = SelectionOverlay(screen, background_bgr=frozen)
    overlay_ref["overlay"] = overlay
    overlay.selection_done.connect(lambda x, y, w, h: on_region_done(overlay_ref, x, y, w, h))
    overlay.show()


def on_region_done(overlay_ref, x, y, w, h):
    overlay = overlay_ref["overlay"]
    frozen = None
    if overlay is not None:
        frozen = overlay.background_bgr
        # 断开信号，避免 close/deleteLater 触发 closeEvent 再次进入本回调
        try:
            overlay.selection_done.disconnect()
        except TypeError:
            pass
        overlay_ref["overlay"] = None
        overlay.close()
        overlay.deleteLater()
    if x == 0 and y == 0 and w == 0 and h == 0:
        emit_log("已取消框选")
        return
    if frozen is None:
        emit_log("框选截图失败：画面快照丢失")
        return
    try:
        # 直接从冻结的快照中裁切，保证截取的是框选触发瞬间的画面
        img = frozen[y:y + h, x:x + w]
        save_shot(img, "框选截图")
    except Exception as e:
        emit_log(f"框选截图失败: {e}")


def cancel_overlay(overlay_ref):
    overlay = overlay_ref["overlay"]
    if overlay is not None and overlay.isVisible():
        overlay.close()


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for key in DEFAULT_CONFIG:
                if data.get(key) is not None:
                    cfg[key] = data[key]
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        emit_log(f"[设置] 保存配置文件失败：{e}")


def autostart_command():
    """返回写入开机自启注册表的命令行。"""
    if getattr(sys, "frozen", False):
        return '"{}"'.format(os.path.abspath(sys.executable))
    script = os.path.abspath(__file__)
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if os.path.exists(pythonw):
        return '"{}" "{}"'.format(pythonw, script)
    return '"{}" "{}"'.format(sys.executable, script)


def set_autostart(enabled):
    """写入/删除当前用户的 Run 注册表键，返回是否成功。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError as e:
        emit_log(f"[设置] 开机自启设置失败：{e}")
        return False


def key_event_to_combo(event):
    """把 Qt 键盘事件转换成 keyboard 库的热键字符串，如 ctrl+shift+f2。"""
    mods = []
    if event.modifiers() & Qt.ControlModifier:
        mods.append("ctrl")
    if event.modifiers() & Qt.AltModifier:
        mods.append("alt")
    if event.modifiers() & Qt.ShiftModifier:
        mods.append("shift")
    if event.modifiers() & Qt.MetaModifier:
        mods.append("win")

    key = event.key()
    if Qt.Key_A <= key <= Qt.Key_Z:
        name = chr(key).lower()
    elif Qt.Key_0 <= key <= Qt.Key_9:
        name = chr(key)
    elif Qt.Key_F1 <= key <= Qt.Key_F24:
        name = f"f{key - Qt.Key_F1 + 1}"
    else:
        name = {
            Qt.Key_Escape: "esc",
            Qt.Key_Tab: "tab",
            Qt.Key_Backspace: "backspace",
            Qt.Key_Return: "enter",
            Qt.Key_Enter: "enter",
            Qt.Key_Insert: "insert",
            Qt.Key_Delete: "delete",
            Qt.Key_Home: "home",
            Qt.Key_End: "end",
            Qt.Key_PageUp: "page up",
            Qt.Key_PageDown: "page down",
            Qt.Key_Left: "left",
            Qt.Key_Right: "right",
            Qt.Key_Up: "up",
            Qt.Key_Down: "down",
            Qt.Key_Space: "space",
            Qt.Key_CapsLock: "caps lock",
            Qt.Key_NumLock: "num lock",
            Qt.Key_Print: "print screen",
            Qt.Key_Pause: "pause",
            Qt.Key_Comma: ",",
            Qt.Key_Period: ".",
            Qt.Key_Minus: "-",
            Qt.Key_Equal: "=",
            Qt.Key_Plus: "+",
            Qt.Key_Semicolon: ";",
            Qt.Key_Apostrophe: "'",
            Qt.Key_BracketLeft: "[",
            Qt.Key_BracketRight: "]",
            Qt.Key_Backslash: "\\",
            Qt.Key_Slash: "/",
            Qt.Key_QuoteLeft: "`",
        }.get(key)

    if not name or name in ("ctrl", "alt", "shift", "win"):
        return None
    return "+".join(mods + [name])


class HotkeyEdit(QLineEdit):
    """点击后进入录制模式，按下按键组合即填入文本。"""

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setReadOnly(True)
        self.setPlaceholderText("点击后按下新按键组合")
        self._listening = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._stop_listening)

    def start_listening(self):
        self._listening = True
        SUPPRESS["active"] = True
        self.setStyleSheet("QLineEdit { background-color: #fff2b0; }")
        self._timer.start(5000)
        self.setFocus()

    def _stop_listening(self):
        self._timer.stop()
        self._listening = False
        SUPPRESS["active"] = False
        self.setStyleSheet("")

    def mousePressEvent(self, event):
        self.start_listening()
        super().mousePressEvent(event)

    def focusOutEvent(self, event):
        self._stop_listening()
        super().focusOutEvent(event)

    def keyPressEvent(self, event):
        if not self._listening:
            super().keyPressEvent(event)
            return
        if event.key() == Qt.Key_Escape:
            self._stop_listening()
            event.accept()
            return
        combo = key_event_to_combo(event)
        if combo is None:
            event.accept()
            return
        self.setText(combo)
        self._stop_listening()
        event.accept()


class MainWindow(QMainWindow):
    def __init__(self, monitor_index=0):
        super().__init__()
        self.monitor_index = monitor_index
        self.bridge = HotkeyBridge()
        self.overlay_ref = {"overlay": None}
        self._hotkey_handlers = {}
        self._closed_to_tray = False

        self.setWindowTitle("ScreenshotLite 屏幕截图工具")
        self.setMinimumSize(540, 480)
        self._build_ui()

        config = load_config()
        global CURRENT_SAVE_DIR
        self.full_edit.setText(config["fullscreen_hotkey"])
        self.region_edit.setText(config["region_hotkey"])
        self.save_edit.setText(config["save_dir"])
        self.autostart_check.setChecked(bool(config["autostart"]))
        CURRENT_SAVE_DIR = config["save_dir"]

        self._connect_bridge()
        self.register_hotkeys()
        self.setup_tray()
        self.update_status()
        GUI_LOG["append"] = self.append_log
        QTimer.singleShot(100, self.list_monitors)

    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)

        hotkey_group = QGroupBox("快捷键设置（点击输入框后按下新按键组合）")
        grid = QGridLayout(hotkey_group)
        grid.addWidget(QLabel("全屏截图："), 0, 0)
        self.full_edit = HotkeyEdit(DEFAULT_CONFIG["fullscreen_hotkey"])
        grid.addWidget(self.full_edit, 0, 1)
        grid.addWidget(QLabel("框选截图："), 1, 0)
        self.region_edit = HotkeyEdit(DEFAULT_CONFIG["region_hotkey"])
        grid.addWidget(self.region_edit, 1, 1)
        grid.addWidget(QLabel("取消框选：ESC（固定）"), 2, 0, 1, 2)
        root.addWidget(hotkey_group)

        save_group = QGroupBox("保存设置")
        save_layout = QHBoxLayout(save_group)
        self.save_edit = QLineEdit(DEFAULT_SAVE_DIR)
        browse_btn = QPushButton("浏览…")
        browse_btn.clicked.connect(self.choose_save_dir)
        save_layout.addWidget(self.save_edit, 1)
        save_layout.addWidget(browse_btn)
        root.addWidget(save_group)

        general_group = QGroupBox("常规设置")
        general_layout = QVBoxLayout(general_group)
        self.autostart_check = QCheckBox("开机自动启动")
        general_layout.addWidget(self.autostart_check)
        root.addWidget(general_group)

        self.status_label = QLabel("")
        root.addWidget(self.status_label)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(1000)
        root.addWidget(self.log_view, 1)

        btn_row = QHBoxLayout()
        apply_btn = QPushButton("应用设置")
        apply_btn.clicked.connect(self.apply_settings)
        clear_btn = QPushButton("清空日志")
        clear_btn.clicked.connect(self.log_view.clear)
        quit_btn = QPushButton("退出")
        quit_btn.clicked.connect(self.quit_app)
        btn_row.addWidget(apply_btn)
        btn_row.addWidget(clear_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(quit_btn)
        root.addLayout(btn_row)

        self.setCentralWidget(central)

    def _connect_bridge(self):
        self.bridge.full_screen.connect(self.on_full_screen)
        self.bridge.region.connect(self.on_region)
        self.bridge.esc.connect(self.on_esc)

    def setup_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.style().standardIcon(QStyle.SP_ComputerIcon))
        self.tray.setToolTip("ScreenshotLite 屏幕截图工具")
        menu = QMenu()
        menu.addAction("全屏截图", self.on_full_screen)
        menu.addAction("框选截图", self.on_region)
        menu.addSeparator()
        menu.addAction("显示主窗口", self.show_window)
        menu.addAction("退出", self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.show_window()

    def show_window(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def register_hotkeys(self):
        """先注册新热键，成功后移除旧的，避免注册失败时留下空窗。"""
        full = self.full_edit.text().strip().lower() or DEFAULT_CONFIG["fullscreen_hotkey"]
        region = self.region_edit.text().strip().lower() or DEFAULT_CONFIG["region_hotkey"]
        new_handlers = {}
        try:
            new_handlers["fullscreen"] = keyboard.add_hotkey(full, self.bridge.full_screen.emit)
            new_handlers["region"] = keyboard.add_hotkey(region, self.bridge.region.emit)
            new_handlers["esc"] = keyboard.add_hotkey("esc", self.bridge.esc.emit)
        except Exception as e:
            for h in new_handlers.values():
                try:
                    keyboard.remove_hotkey(h)
                except Exception:
                    pass
            QMessageBox.warning(self, "热键注册失败", f"无法注册热键：{e}\n已保留原有热键。")
            return False

        for h in self._hotkey_handlers.values():
            try:
                keyboard.remove_hotkey(h)
            except Exception:
                pass
        self._hotkey_handlers = new_handlers
        return True

    def update_status(self):
        full = self.full_edit.text().strip().lower() or DEFAULT_CONFIG["fullscreen_hotkey"]
        region = self.region_edit.text().strip().lower() or DEFAULT_CONFIG["region_hotkey"]
        auto = "开" if self.autostart_check.isChecked() else "关"
        self.status_label.setText(
            f"热键已注册：{full} 全屏截图 / {region} 框选截图 / ESC 取消框选；"
            f"保存目录：{self.save_edit.text()}；开机自启：{auto}"
        )

    def choose_save_dir(self):
        path = QFileDialog.getExistingDirectory(self, "选择保存目录", self.save_edit.text())
        if path:
            self.save_edit.setText(path)

    def apply_settings(self):
        full = self.full_edit.text().strip().lower()
        region = self.region_edit.text().strip().lower()
        save_dir = self.save_edit.text().strip()

        if not full or not region:
            QMessageBox.warning(self, "提示", "全屏/框选热键不能为空")
            return
        if full == region:
            QMessageBox.warning(self, "提示", "全屏与框选热键不能相同")
            return
        if not save_dir:
            save_dir = DEFAULT_SAVE_DIR
        self.save_edit.setText(save_dir)

        if not self.register_hotkeys():
            return

        autostart = self.autostart_check.isChecked()
        if not set_autostart(autostart):
            QMessageBox.warning(self, "提示", "写入开机自启设置失败，请以管理员权限运行后重试")
            return

        global CURRENT_SAVE_DIR
        CURRENT_SAVE_DIR = save_dir
        save_config({
            "fullscreen_hotkey": full,
            "region_hotkey": region,
            "save_dir": save_dir,
            "autostart": autostart,
        })
        self.update_status()
        self.append_log(f"[设置] 已保存：全屏 {full} / 框选 {region} / 目录 {save_dir} / 开机自启 {'开' if autostart else '关'}")

    def append_log(self, msg):
        self.log_view.appendPlainText(msg)

    def list_monitors(self):
        try:
            with mss.MSS() as sct:
                for idx, m in enumerate(sct.monitors):
                    tag = "全部屏幕" if idx == 0 else f"屏幕{idx}"
                    self.append_log(f"mss[{idx}] {tag}: ({m['left']},{m['top']}) {m['width']}x{m['height']}")
            if self.monitor_index >= 1:
                self.append_log(f"当前模式：固定使用显示器索引 {self.monitor_index}")
            else:
                self.append_log("当前模式：自动截取鼠标所在屏幕（可在命令行传入显示器索引）")
        except Exception as e:
            self.append_log(f"枚举显示器失败：{e}")

    def on_full_screen(self):
        if SUPPRESS["active"]:
            return
        on_f2(self.monitor_index)

    def on_region(self):
        if SUPPRESS["active"]:
            return
        on_f3(self.overlay_ref)

    def on_esc(self):
        if SUPPRESS["active"]:
            return
        cancel_overlay(self.overlay_ref)

    def closeEvent(self, event):
        if self.tray and self.tray.isVisible():
            self.hide()
            if not self._closed_to_tray:
                self._closed_to_tray = True
                self.tray.showMessage(
                    "ScreenshotLite 屏幕截图工具", "已最小化到托盘，双击图标可恢复窗口",
                    QSystemTrayIcon.Information, 2000,
                )
            event.ignore()
        else:
            event.accept()

    def quit_app(self):
        for h in self._hotkey_handlers.values():
            try:
                keyboard.remove_hotkey(h)
            except Exception:
                pass
        self._hotkey_handlers.clear()
        GUI_LOG["append"] = None
        QApplication.quit()


def main():
    monitor_index = 0  # 0 = 自动：鼠标所在屏幕
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        monitor_index = int(sys.argv[1])

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    win = MainWindow(monitor_index)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
