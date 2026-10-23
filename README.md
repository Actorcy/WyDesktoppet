# WyDesktoppet · AI 桌面宠物

一只住在 Windows 桌面上的小猫桌宠：会陪你聊天、提醒喝水、显示时间，还能帮你控制 QQ 音乐。

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![PyQt5](https://img.shields.io/badge/PyQt5-5.x-green)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)

## 功能

- **AI 对话** — 双击桌宠输入文字，由 DeepSeek 以桌宠人设回复；空闲几分钟会主动搭话。
- **常驻时钟** — 宠物下方一条半透明时间牌，跟随拖动、隐藏，精确到秒。
- **QQ 音乐控制** — 播放/暂停、上一首、下一首、音量、静音、查看当前歌曲。走 Windows 系统媒体会话（SMTC），不需要 QQ 音乐插件。
- **喝水提醒** — 每 45 分钟提醒一次，可在托盘菜单关闭。
- **托盘常驻** — 隐藏、显示、彻底退出；关窗口只隐藏不退出。

## 截图

> 建议自行截一张放到 `docs/screenshot.png`，README 里引用即可。

## 快速开始

```bash
# 1. 安装依赖
pip install PyQt5 openai

# 2. 复制配置模板，填入你的 API Key
copy config.example.json config.json

# 3. 运行
python pet.py
```

`config.json` 已在 `.gitignore` 中，不会被提交。

### 配置项

| 字段 | 说明 |
| --- | --- |
| `api_key` | DeepSeek API Key（必填） |
| `base_url` | 接口地址，默认 `https://api.deepseek.com` |
| `model` | 模型名，默认 `deepseek-flash` |
| `system_prompt` | 桌宠人设提示词，可随意改 |

## 交互

| 操作 | 效果 |
| --- | --- |
| 左键拖动 | 移动桌宠 |
| 双击 | 打开聊天输入框 |
| 右键 | 快捷菜单（聊天 / 显示时间 / QQ 音乐 / 退出） |
| 托盘左键 | 显示或隐藏桌宠 |
| 托盘右键 | 完整菜单 |

在聊天框输入这些词会**本地执行**，不消耗 AI 请求：

```
下一首 / 上一首 / 暂停 / 继续 / 静音
大声点 / 小声点 / 现在放什么歌
```

## 实现要点

- **AI 请求**：关闭模型的思考过程（`thinking: disabled`），并把 `max_tokens` 提到 200——否则推理 token 会吃掉回复预算，导致空回复或半句话。单次最多等 45 秒，总预算 50 秒，超时先重试一次，仍失败则说一句本地台词。
- **不卡界面**：所有网络与 PowerShell 调用都在 `QThread` 里跑，结果通过信号回主线程更新气泡。
- **QQ 音乐**：QQ 音乐对外没有 API，但它注册了 Windows 媒体会话，因此通过 WinRT 的 `GlobalSystemMediaTransportControlsSessionManager` 控制。Python 3.13+ 安装 WinRT 绑定比较麻烦，所以用一个常驻的 `powershell.exe` 作为桥，通过 stdin/stdout 传 JSON，单次调用约 10–70 ms。
- **时钟**：直接画在桌宠窗口的 `paintEvent` 里，不额外开窗口，因此拖动、隐藏天然同步。

## 打包

```bash
pip install pyinstaller
pyinstaller pet.spec
```

产物在 `dist/pet.exe`。注意 `assets/` 与 `config.json` 需要和 exe 放在同一目录（程序按 exe 所在路径查找）。

## 隐私

- API Key 只存在于本地 `config.json`，不会上传到任何地方。
- `pet_error.log` 记录运行日志，同样只在本地。
- 本项目不收集任何使用数据。

## 已知限制

- 仅支持 Windows（依赖 Win32 窗口标志与 SMTC）。
- 桌宠图片 `assets/pet.png` 请确认你有权使用；如来自第三方，请替换为你自己的素材。
- QQ 音乐控制依赖它运行且支持系统媒体会话；未启动时只会提示，不会报错。

## License

MIT，见 [LICENSE](LICENSE)。
