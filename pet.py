import sys
import json
import random
import re
import time
import logging
from pathlib import Path

from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QMenu, QAction, 
    QSystemTrayIcon, QGraphicsDropShadowEffect, QInputDialog
)
from PyQt5.QtGui import QPixmap, QPainter, QIcon, QColor, QFont, QFontMetrics
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QRectF

from openai import OpenAI, BadRequestError, APITimeoutError, APIConnectionError

import music_plugin

# ---------------- 路径与常量 ----------------
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

CONFIG_PATH = BASE_DIR / "config.json"
ASSET_PATH = BASE_DIR / "assets" / "pet.png"
LOG_PATH = BASE_DIR / "pet_error.log"

# ---------------- 配置日志系统 ----------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_PATH, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

DEFAULT_CONFIG = {
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-flash",
    "system_prompt": "你是一只住在用户电脑桌面上的小猫桌宠。回复简短、俏皮，一般不超过两句话。用户戳你、拖你的时候，用调皮或撒娇的语气回应。不要长篇大论，不要像客服。"
}

def load_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2))
        logging.info(f"已生成 {CONFIG_PATH}，请填写 api_key 后重新运行。")
        sys.exit(0)
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

# ---------------- AI 工作线程 ----------------
MAX_TOKENS = 200        # 桌宠只说一两句，200 足够；低于 100 容易被"思考"吃光
NO_THINKING = {"thinking": {"type": "disabled"}}   # 关掉推理：不吃 token、也不拖慢回复
ATTEMPT_TIMEOUT = 45    # 单次最多等多久（秒）。实测最慢约 43s，放宽到 45s 让它自己想完
MAX_ATTEMPTS = 2        # 只在预算内重试；全失败则出本地台词
TOTAL_BUDGET = 50       # 从开口到出话的总预算，超了就用本地台词，不再无限等

LOCAL_FALLBACKS = [
    "喵……脑子突然打结了，再戳我一下嘛。",
    "唔，网络好像卡住了，等我缓一缓~",
    "哼，我刚刚走神了，你再说一遍嘛。",
]


class ChatWorker(QThread):
    finished = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, user_text, config):
        super().__init__()
        self.user_text = user_text
        self.config = config

    def _client(self, timeout):
        # 关掉 SDK 自带重试：重试由本类控制，总等待时间才可预期
        return OpenAI(
            api_key=self.config["api_key"],
            base_url=self.config["base_url"],
            max_retries=0,
            timeout=timeout,
        )

    def _ask(self, client, messages, max_tokens):
        """优先关闭思考；网关不支持该参数时自动退回普通请求。"""
        kwargs = dict(
            model=self.config["model"],
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.85,
        )
        try:
            return client.chat.completions.create(extra_body=NO_THINKING, **kwargs)
        except BadRequestError as e:
            logging.warning(f"该接口不接受关闭思考的参数，退回普通请求：{e}")
            return client.chat.completions.create(**kwargs)

    def run(self):
        messages = [
            {"role": "system", "content": self.config["system_prompt"]},
            {"role": "user", "content": self.user_text},
        ]
        logging.info(f"发送请求至 AI，触发词: {self.user_text[:20]}...")

        last_reason = "未知原因"
        deadline = time.time() + TOTAL_BUDGET
        for attempt in range(1, MAX_ATTEMPTS + 1):
            remaining = deadline - time.time()
            if remaining < 5:
                last_reason = "总等待预算用尽"
                logging.warning(f"总预算 {TOTAL_BUDGET}s 已用尽（{last_reason}），不再重试。")
                break

            started = time.time()
            try:
                resp = self._ask(
                    self._client(min(ATTEMPT_TIMEOUT, remaining)),
                    messages,
                    MAX_TOKENS * attempt,
                )
            except (APITimeoutError, APIConnectionError) as e:
                last_reason = f"超时/连接失败: {e}"
                logging.warning(f"第 {attempt} 次请求{last_reason}（{time.time() - started:.1f}s）")
                continue
            except Exception as e:
                # 鉴权、模型名、参数之类的硬错误，重试没有意义
                logging.error(f"API调用失败: {e}")
                self.error.emit(f"API调用失败: {e}")
                return

            choice = resp.choices[0]
            reply = (choice.message.content or "").strip()
            if reply and choice.finish_reason != "length":
                logging.info(f"AI 回复（{time.time() - started:.2f}s，第 {attempt} 次）: {reply}")
                self.finished.emit(reply)
                return

            last_reason = f"空回复或被截断(finish={choice.finish_reason})"
            logging.warning(f"第 {attempt} 次：{last_reason}，重试。")

        # 都没拿到可用回复：说句本地台词，别让气泡一直挂着"……"
        logging.warning(f"未能拿到可用回复（{last_reason}），改用本地台词。")
        self.finished.emit(random.choice(LOCAL_FALLBACKS))

# ---------------- QQ 音乐控制（插件：music_plugin.py） ----------------
MUSIC_INTENTS = [
    (r"(下一首|下首|切歌|换一首|换歌|next)", "next"),
    (r"(上一首|上首|前一首|prev)", "prev"),
    (r"(暂停|停一下|先别放|别放了)", "pause"),
    (r"(继续|播放|放吧|接着放|开始放)", "play"),
    (r"(什么歌|放的是|在听什么|当前歌曲|现在放|这首歌)", "status"),
    (r"(静音|关声音|没声音)", "mute"),
    (r"(大声|音量大|音量加|调大|响一点|大一点)", "vol_up"),
    (r"(小声|音量小|音量减|调小|轻一点|小一点)", "vol_down"),
]


def parse_music_command(text):
    """识别“下一首 / 暂停 / 大声点”这类指令；不是音乐指令就返回 None。"""
    t = (text or "").strip().lower()
    if not t or len(t) > 20:      # 长句子不当指令，交给 AI
        return None
    for pattern, action in MUSIC_INTENTS:
        if re.search(pattern, t):
            return action
    return None


def _music_error_text(result):
    if result.get("error") == "no-session":
        return "QQ 音乐好像没打开呢，要我帮你打开吗？"
    return "音乐这边好像卡住了…"


def run_music_action(action):
    """执行音乐动作，返回要显示在气泡里的话。"""
    if action == "launch":
        if music_plugin.launch_qqmusic():
            return "好，我帮你把 QQ 音乐叫起来～"
        return "没找到 QQ 音乐装在哪呢…"

    if action == "status":
        s = music_plugin.status()
        if s.get("ok"):
            state = "正在放" if s.get("playing") else "暂停在"
            return (f"{state}《{s.get('title') or '未知歌曲'}》"
                    f"—{s.get('artist') or '未知歌手'}  "
                    f"{s.get('position_text', '')}/{s.get('duration_text', '')}")
        return _music_error_text(s)

    if action in ("toggle", "play", "pause"):
        if action == "toggle":
            r = music_plugin.toggle()
        elif action == "play":
            r = music_plugin.play()
        else:
            r = music_plugin.pause()
        if not r.get("ok"):
            return _music_error_text(r)
        if bool(r.get("playing")):
            return "好嘞，接着听～"
        return "先歇一会儿～"

    if action == "next":
        r = music_plugin.next_track()
    elif action == "prev":
        r = music_plugin.prev_track()
    elif action == "vol_up":
        music_plugin.volume_up()
        return "再大声一点点～"
    elif action == "vol_down":
        music_plugin.volume_down()
        return "小声一点点～"
    elif action == "mute":
        music_plugin.toggle_mute()
        return "嘘——静音啦。"
    else:
        return "这个我还不会呢。"

    if not r.get("ok"):
        return _music_error_text(r)
    return "换一首～" if action == "next" else "回到上一首～"


class MusicWorker(QThread):
    """音乐操作放后台线程，避免调 PowerShell 时卡住气泡。"""

    done = pyqtSignal(str)

    def __init__(self, action):
        super().__init__()
        self.action = action

    def run(self):
        try:
            text = run_music_action(self.action)
        except Exception as e:
            logging.error(f"音乐控制失败: {e}")
            text = "音乐控制出错了…"
        self.done.emit(text)

# ---------------- 磨砂气泡窗口 ----------------
class Bubble(QWidget):
    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignCenter)
        
        # 字体调大(18px)，内边距和圆角调大
        self.label.setStyleSheet("""
            QLabel {
                background-color: rgba(40, 40, 40, 180);
                color: rgba(255, 255, 255, 230);
                border-radius: 16px;
                border: 1px solid rgba(255, 255, 255, 40);
                padding: 14px 20px;
                font-family: "Microsoft YaHei", "PingFang SC";
                font-size: 18px; 
            }
        """)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(15)
        shadow.setColor(QColor(0, 0, 0, 120))
        shadow.setOffset(0, 4)
        self.label.setGraphicsEffect(shadow)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)

    def show_text(self, text, pet_geom):
        # 先解除上一次的固定宽度，否则 adjustSize 不会重新按内容计算宽度
        self.label.setMinimumWidth(0)
        self.label.setMaximumWidth(16777215)
        self.label.setText(text)
        self.label.adjustSize()

        # 最大宽度增加到 450，让文字横向排布
        max_w = 450
        if self.label.width() > max_w:
            self.label.setFixedWidth(max_w)
            self.label.adjustSize()
        else:
            self.label.setFixedWidth(self.label.width())

        self.setFixedSize(self.label.width() + 8, self.label.height() + 8)
        self.label.move(4, 4)

        screen = QApplication.primaryScreen().availableGeometry()
        
        x = pet_geom.x() + pet_geom.width() // 2 - self.width() // 2
        x = max(screen.left(), min(x, screen.right() - self.width()))
        
        y = pet_geom.y() - self.height() - 5
        
        if y < screen.top():
            y = pet_geom.y() + pet_geom.height() + 5
            if y + self.height() > screen.bottom():
                y = screen.bottom() - self.height() - 10

        self.move(x, y)
        self.show()
        self.timer.start(5000)

# ---------------- 桌宠主窗口 ----------------
class PetWindow(QWidget):
    def __init__(self, config):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.config = config
        self.drag_offset = None
        self.is_busy = False
        self.music_busy = False
        
        # 全局 AI 触发冷却记录
        self.last_ai_trigger_time = 0
        self.ai_cooldown = 2.0  # 冷却时间 2 秒
        
        # 空闲计时
        self.last_interact = time.time() 
        self.idle_threshold = 60 
        self.idle_timer = QTimer(self)
        self.idle_timer.timeout.connect(self.check_idle)
        self.idle_timer.start(15000) 

        # 喝水提醒相关
        self.water_reminder_enabled = True
        self.pending_water_reminder = False # 挂起标记
        self.water_timer = QTimer(self)
        self.water_timer.timeout.connect(self.trigger_water_reminder)
        self.water_interval = 45 * 60 * 1000 # 默认 45 分钟
        self.water_timer.start(self.water_interval)

        # 等待提示：等 AI 时在气泡里轮换文字，避免长时间只剩一个"……"
        self.wait_hints = [
            "唔……让我再想想~",
            "还在想，马上就好…",
            "今天脑子有点慢，再等我一下哦。",
        ]
        self.wait_hint_index = 0
        self.wait_timer = QTimer(self)
        self.wait_timer.setInterval(6000)
        self.wait_timer.timeout.connect(self._cycle_wait_hint)

        # 常驻时钟：宠物下方一条半透明时间牌
        self.clock_enabled = True
        self.clock_font = QFont("Microsoft YaHei", 12)
        self.clock_font.setBold(True)
        self.clock_height = QFontMetrics(self.clock_font).height() + 14
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self.update)
        self.clock_timer.start(500)

        if not ASSET_PATH.exists():
            logging.error(f"找不到图片文件：{ASSET_PATH}")
            sys.exit(1)
            
        self.pixmap = QPixmap(str(ASSET_PATH))
        # 强制缩小图片到 200x200 以内
        self.pixmap = self.pixmap.scaled(200, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.resize(self.pixmap.width(), self.pixmap.height() + self.clock_height)
        self.bubble = Bubble()
        
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.width() - self.width() - 80, screen.height() - self.height() - 80)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_menu)

        self.init_tray()

    def _can_trigger_ai(self):
        """检查当前是否允许触发 AI 对话"""
        now = time.time()
        if self.is_busy or (now - self.last_ai_trigger_time < self.ai_cooldown):
            return False
        self.last_ai_trigger_time = now
        return True

    def check_idle(self):
        if self.is_busy or not self.isVisible():
            return
            
        now = time.time()
        if now - self.last_interact > self.idle_threshold:
            if random.random() < 0.2:
                if self._can_trigger_ai():
                    self.trigger_idle_chat()
                    self.last_interact = now

    def trigger_water_reminder(self):
        if not self.water_reminder_enabled:
            return
            
        self.water_timer.start(self.water_interval)
        
        if not self._can_trigger_ai():
            logging.info("喝水提醒撞车，已挂起，将在当前聊天结束后提醒。")
            self.pending_water_reminder = True
            return

        self._do_water_reminder()

    def _do_water_reminder(self):
        self.tray_icon.showMessage(
            "喝水时间到啦！",
            "你的桌宠正在提醒你补充水分~",
            QSystemTrayIcon.Information,
            3000
        )

        prompt = (
            "这是一个特殊的系统提醒事件：主人已经连续工作45分钟了。"
            "请用你当前的桌宠人设，主动提醒主人喝一杯水，稍微关心一下他的身体。"
            "可以说两句俏皮的话，不要太死板。"
        )
        self.ask_ai(prompt)
        
    def check_pending_water(self):
        if self.pending_water_reminder:
            logging.info("当前对话结束，2.1秒后准备补发喝水提醒。")
            QTimer.singleShot(2100, self._do_pending_water_reminder)

    def _do_pending_water_reminder(self):
        if self._can_trigger_ai():
            self.pending_water_reminder = False
            logging.info("成功补发喝水提醒！")
            self._do_water_reminder()
        else:
            logging.info("补发时再次撞车，继续挂起。")

    def toggle_water_reminder(self, state):
        self.water_reminder_enabled = bool(state)
        if self.water_reminder_enabled:
            self.water_timer.start(self.water_interval)
            logging.info("喝水提醒已开启")
        else:
            self.water_timer.stop()
            self.pending_water_reminder = False 
            logging.info("喝水提醒已关闭")

    def init_tray(self):
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(QIcon(str(ASSET_PATH)))
        self.tray_icon.setToolTip("我的 AI 桌宠")

        tray_menu = QMenu()
        
        self.action_toggle = QAction("隐藏桌宠", self)
        self.action_toggle.triggered.connect(self.toggle_pet_visibility)
        tray_menu.addAction(self.action_toggle)

        action_chat = QAction("聊聊天", self)
        action_chat.triggered.connect(self.trigger_chat)
        tray_menu.addAction(action_chat)

        self.action_clock = QAction("显示时间", self)
        self.action_clock.setCheckable(True)
        self.action_clock.setChecked(True)
        self.action_clock.triggered.connect(self.toggle_clock)
        tray_menu.addAction(self.action_clock)

        tray_menu.addMenu(self._build_music_menu(tray_menu))

        tray_menu.addSeparator()
        self.action_water = QAction("开启喝水提醒", self)
        self.action_water.setCheckable(True)
        self.action_water.setChecked(True)
        self.action_water.triggered.connect(self.toggle_water_reminder)
        tray_menu.addAction(self.action_water)

        tray_menu.addSeparator()
        action_quit = QAction("彻底退出", self)
        action_quit.triggered.connect(self.quit_app)
        tray_menu.addAction(action_quit)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.on_tray_activated)
        self.tray_icon.show()

    def toggle_clock(self, state):
        self.clock_enabled = bool(state)
        if self.clock_enabled:
            self.resize(self.pixmap.width(), self.pixmap.height() + self.clock_height)
        else:
            self.resize(self.pixmap.size())
        self.update()

    def _build_music_menu(self, parent):
        menu = QMenu("QQ 音乐", parent)
        items = [
            ("现在放什么", "status"),
            ("播放 / 暂停", "toggle"),
            ("上一首", "prev"),
            ("下一首", "next"),
            ("音量 +", "vol_up"),
            ("音量 -", "vol_down"),
            ("静音", "mute"),
        ]
        for label, action in items:
            act = QAction(label, self)
            act.triggered.connect(lambda checked=False, a=action: self.music_action(a))
            menu.addAction(act)
        menu.addSeparator()
        act_open = QAction("打开 QQ 音乐", self)
        act_open.triggered.connect(lambda: self.music_action("launch"))
        menu.addAction(act_open)
        return menu

    def music_action(self, action):
        if self.music_busy:
            return
        self.music_busy = True
        self.last_interact = time.time()
        self.bubble.show_text("……", self.geometry())
        self.music_worker = MusicWorker(action)
        self.music_worker.done.connect(self.on_music_done)
        self.music_worker.start()

    def on_music_done(self, text):
        self.music_busy = False
        self.bubble.show_text(text, self.geometry())

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.toggle_pet_visibility()

    def toggle_pet_visibility(self):
        if self.isVisible():
            self.hide()
            self.bubble.hide()
            self.action_toggle.setText("显示桌宠")
        else:
            self.show()
            self.action_toggle.setText("隐藏桌宠")
            self.last_interact = time.time()

    def quit_app(self):
        self.tray_icon.hide()
        self.bubble.close()
        music_plugin._bridge.close()
        QApplication.quit()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.drawPixmap(0, 0, self.pixmap)

        if not self.clock_enabled:
            return

        # 时间牌：宠物下方居中，宽度不超过宠物
        plate_w = min(self.width() - 12, 128)
        plate = QRectF((self.width() - plate_w) / 2, self.pixmap.height(),
                       plate_w, self.clock_height - 4)
        painter.setPen(QColor(255, 255, 255, 45))
        painter.setBrush(QColor(30, 30, 30, 165))
        painter.drawRoundedRect(plate, 10, 10)

        painter.setFont(self.clock_font)
        painter.setPen(QColor(255, 255, 255, 235))
        painter.drawText(plate, Qt.AlignCenter, time.strftime("%H:%M:%S"))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_offset = event.pos()
            self.bubble.hide()
            self.last_interact = time.time()

    def mouseMoveEvent(self, event):
        if self.drag_offset and event.buttons() & Qt.LeftButton:
            self.move(self.pos() + event.pos() - self.drag_offset)
            self.last_interact = time.time()

    def mouseReleaseEvent(self, event):
        self.drag_offset = None
        self.last_interact = time.time()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.trigger_chat()

    def trigger_chat(self):
        self.last_interact = time.time()

        # 弹出输入框，让你打字
        text, ok = QInputDialog.getText(self, "和女鹅聊天", "你想对她说点什么？")

        # 如果你点了确定，并且输入了内容
        if ok and text.strip():
            # 音乐指令本地执行，不花 AI 的等待时间
            action = parse_music_command(text)
            if action:
                self.music_action(action)
                return
            if not self._can_trigger_ai():
                return
            # 普通聊天再交给 AI
            self.ask_ai(text)
        else:
            if not self._can_trigger_ai():
                return
            # 如果你没打字直接关了，就当做是摸了她一下
            prompt = random.choice([
                "用户戳了你一下，请用桌宠的语气回应。", 
                "用户摸了摸你，请撒娇回应。", 
            ])
            self.ask_ai(prompt)

    def trigger_idle_chat(self):
        current_time = time.strftime("%H:%M", time.localtime())
        prompts = [
            f"现在时间是 {current_time}，主人已经很久没理你了。你感到很无聊，请主动找个话题吸引主人的注意。",
            f"现在时间是 {current_time}，主人很久没动静了。请主动关心一下主人是不是在忙，或者提醒主人休息。",
            f"现在时间是 {current_time}。你发呆了一会儿，突然想到一个有趣的小问题或者吐槽，请主动说给主人听。"
        ]
        self.ask_ai(random.choice(prompts))

    def ask_ai(self, user_text):
        self.is_busy = True
        self.wait_hint_index = 0
        self.bubble.show_text("……", self.geometry())
        self.wait_timer.start()
        self.worker = ChatWorker(user_text, self.config)
        self.worker.finished.connect(self.on_reply)
        self.worker.error.connect(self.on_error)
        self.worker.start()

    def _cycle_wait_hint(self):
        """等待期间每 6 秒换一句，气泡既不会空着也不会自动消失。"""
        if not self.is_busy:
            self.wait_timer.stop()
            return
        hint = self.wait_hints[self.wait_hint_index % len(self.wait_hints)]
        self.wait_hint_index += 1
        self.bubble.show_text(hint, self.geometry())

    def on_reply(self, text):
        self.is_busy = False
        self.wait_timer.stop()
        self.bubble.show_text(text, self.geometry())
        self.last_interact = time.time()
        self.check_pending_water() 

    def on_error(self, error_msg):
        self.is_busy = False
        self.wait_timer.stop()
        fallback = random.choice(["喵……信号好像飘走了", "等一下下，我再想想", "哼，暂时不想说话"])
        self.bubble.show_text(fallback, self.geometry())
        
        self.tray_icon.showMessage(
            "AI 报错啦！",
            f"详细错误已写入 pet_error.log\n错误摘要: {error_msg[:30]}...",
            QSystemTrayIcon.Warning,
            3000
        )
        self.check_pending_water() 

    def show_menu(self, pos):
        menu = QMenu(self)
        act_chat = QAction("聊聊天", self)
        act_chat.triggered.connect(self.trigger_chat)
        menu.addAction(act_chat)
        menu.addAction(self.action_clock)
        menu.addMenu(self._build_music_menu(menu))
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self.quit_app)
        menu.addAction(act_quit)
        menu.exec_(self.mapToGlobal(pos))

    def closeEvent(self, event):
        event.ignore()
        self.hide()
        self.bubble.hide()
        self.action_toggle.setText("显示桌宠")
        self.tray_icon.showMessage(
            "桌宠已隐藏",
            "我躲在右下角的托盘里啦，随时点我出来哦~",
            QSystemTrayIcon.Information,
            2000
        )

# ---------------- 启动入口 ----------------
def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(QIcon(str(ASSET_PATH)))

    config = load_config()
    if not config.get("api_key"):
        logging.warning("config.json 中的 api_key 还没填，请填写后重新运行。")
        time.sleep(2)
        return

    logging.info("桌宠程序已启动。")
    pet = PetWindow(config)
    pet.show()
    music_plugin.warm_up()   # 后台预热媒体桥，第一次说“下一首”也能秒回
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()