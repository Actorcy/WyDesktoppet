# -*- coding: utf-8 -*-
"""
QQ 音乐控制插件（Windows SMTC 媒体会话）

原理：
    新版 QQ 音乐会向 Windows 注册“系统媒体传输控件”(SMTC) 会话，
    通过它就能读当前歌曲、播放/暂停、上一首/下一首，不需要 QQ 音乐插件或账号。

进程结构：
    pet.py  ──> 常驻 powershell.exe（嵌入脚本，走 stdin/stdout 传 JSON）
             └─ 用 WinRT 的 GlobalSystemMediaTransportControlsSessionManager 控制播放

音量：
    SMTC 不提供音量接口，这里用系统音量键（VK_VOLUME_*），对音乐同样生效。
"""

import ctypes
import json
import os
import subprocess
import tempfile
import threading
import time

PS_EXE = "powershell.exe"
CREATE_NO_WINDOW = 0x08000000

# QQ 音乐常见安装位置（用于“帮你打开”）
QQMUSIC_PATHS = [
    r"C:\Program Files (x86)\Tencent\QQMusic\QQMusic.exe",
    r"C:\Program Files\Tencent\QQMusic\QQMusic.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Tencent\QQMusic\QQMusic.exe"),
]

BRIDGE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

Add-Type -AssemblyName System.Runtime.WindowsRuntime

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]

function Await($op, $type) {
    $asTask = $asTaskGeneric.MakeGenericMethod($type)
    $task = $asTask.Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}

function Get-QQSession {
    [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime] | Out-Null
    $mgr = Await ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
    ($mgr.GetSessions() | Where-Object { $_.SourceAppUserModelId -like '*QQMusic*' } | Select-Object -First 1)
}

function Send-Result($obj) {
    [Console]::Out.WriteLine(($obj | ConvertTo-Json -Compress -Depth 4))
    [Console]::Out.Flush()
}

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    $cmd = $line.Trim().ToLowerInvariant()
    $result = @{ ok = $false }
    try {
        if ($cmd -eq 'ping') {
            $result = @{ ok = $true; pong = $true }
        } else {
            $s = Get-QQSession
            if (-not $s) {
                $result = @{ ok = $false; error = 'no-session' }
            } else {
                switch ($cmd) {
                    'status' {
                        $props = Await ($s.TryGetMediaPropertiesAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties])
                        $info = $s.GetPlaybackInfo()
                        $tl = $s.GetTimelineProperties()
                        $result = @{
                            ok = $true
                            playing = ($info.PlaybackStatus -eq 'Playing')
                            title = $props.Title
                            artist = $props.Artist
                            album = $props.AlbumTitle
                            position = [math]::Round($tl.Position.TotalSeconds, 1)
                            duration = [math]::Round($tl.EndTime.TotalSeconds, 1)
                        }
                    }
                    { $_ -in 'toggle', 'play', 'pause' } {
                        $info = $s.GetPlaybackInfo()
                        $isPlaying = ($info.PlaybackStatus -eq 'Playing')
                        $target = $cmd
                        if ($target -eq 'toggle') { if ($isPlaying) { $target = 'pause' } else { $target = 'play' } }
                        if ($target -eq 'play' -and $isPlaying) {
                            $result = @{ ok = $true; playing = $true; noop = $true }
                        } elseif ($target -eq 'pause' -and (-not $isPlaying)) {
                            $result = @{ ok = $true; playing = $false; noop = $true }
                        } else {
                            $done = Await ($s.TryTogglePlayPauseAsync()) ([bool])
                            $result = @{ ok = [bool]$done; playing = (-not $isPlaying) }
                        }
                    }
                    'next' {
                        $done = Await ($s.TrySkipNextAsync()) ([bool])
                        $result = @{ ok = [bool]$done }
                    }
                    'prev' {
                        $done = Await ($s.TrySkipPreviousAsync()) ([bool])
                        $result = @{ ok = [bool]$done }
                    }
                    default { $result = @{ ok = $false; error = 'unknown-command' } }
                }
            }
        }
    } catch {
        $result = @{ ok = $false; error = $_.Exception.Message }
    }
    Send-Result $result
}
"""


class _Bridge:
    """常驻 PowerShell 进程 + 一问一答。首次启动约 0.6s，之后每次约 20-50ms。"""

    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()
        self._script_path = None

    def _script(self):
        if not self._script_path:
            path = os.path.join(tempfile.gettempdir(), "pet_music_bridge.ps1")
            with open(path, "w", encoding="utf-8-sig") as f:
                f.write(BRIDGE_SCRIPT)
            self._script_path = path
        return self._script_path

    def _start(self):
        self._proc = subprocess.Popen(
            [PS_EXE, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", self._script()],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )
        return self._proc

    def _kill(self):
        if self._proc:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None

    def ensure(self):
        if self._proc is None or self._proc.poll() is not None:
            self._start()
        return self._proc is not None

    def call(self, cmd, timeout=6):
        with self._lock:
            if not self.ensure():
                return {"ok": False, "error": "bridge-start-failed"}
            try:
                self._proc.stdin.write(cmd + "\n")
                self._proc.stdin.flush()
            except Exception as e:
                self._kill()
                return {"ok": False, "error": f"write-failed: {e}"}

            box = {}
            reader = threading.Thread(
                target=lambda: box.setdefault("line", self._proc.stdout.readline()),
                daemon=True,
            )
            reader.start()
            reader.join(timeout)

            if reader.is_alive() or "line" not in box:
                self._kill()
                return {"ok": False, "error": "timeout"}
            line = (box["line"] or "").strip()
            if not line:
                self._kill()
                return {"ok": False, "error": "bridge-exited"}
            try:
                return json.loads(line)
            except Exception:
                return {"ok": False, "error": f"bad-json: {line[:80]}"}

    def close(self):
        with self._lock:
            try:
                if self._proc and self._proc.stdin:
                    self._proc.stdin.write("__quit__\n")
                    self._proc.stdin.flush()
            except Exception:
                pass
            self._kill()


_bridge = _Bridge()


def warm_up():
    """后台预热，避免第一次说“下一首”时多等半秒。"""
    threading.Thread(target=lambda: _bridge.call("ping", timeout=10), daemon=True).start()


# ---------------- 系统音量 ----------------
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
VK_VOLUME_MUTE = 0xAD
KEYEVENTF_KEYUP = 0x0002


def _press(vk, times=1):
    user32 = ctypes.windll.user32
    for _ in range(times):
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.04)


def volume_up(step=5):
    _press(VK_VOLUME_UP, step)


def volume_down(step=5):
    _press(VK_VOLUME_DOWN, step)


def toggle_mute():
    _press(VK_VOLUME_MUTE, 1)


# ---------------- 对外动作 ----------------
def qqmusic_path():
    for p in QQMUSIC_PATHS:
        if os.path.exists(p):
            return p
    return None


def launch_qqmusic():
    path = qqmusic_path()
    if not path:
        return False
    try:
        subprocess.Popen([path], creationflags=CREATE_NO_WINDOW)
        return True
    except Exception:
        return False


def _fmt_time(seconds):
    try:
        seconds = int(float(seconds))
    except Exception:
        return ""
    return f"{seconds // 60}:{seconds % 60:02d}"


def status():
    r = _bridge.call("status")
    if not r.get("ok"):
        return r
    r["position_text"] = _fmt_time(r.get("position", 0))
    r["duration_text"] = _fmt_time(r.get("duration", 0))
    return r


def toggle():
    return _bridge.call("toggle")


def play():
    return _bridge.call("play")


def pause():
    return _bridge.call("pause")


def next_track():
    return _bridge.call("next")


def prev_track():
    return _bridge.call("prev")
