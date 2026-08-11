# FT-710 Remote Controller — 详细文档

> 详细功能介绍、硬件说明、系统架构、配置参考与使用说明。
> 中英文对照：中文在前，英文完整版见文末。

---

## 一、软件简介

**FT-710 Remote Controller** 是一个基于 **WebRTC** 的 Yaesu FT-710 短波电台远程控制器，运行于 **Windows 10/11（64 位）**。

- 本机运行 **tkinter 图形界面**作为服务端，同时提供 **Web 服务**（Flask + Socket.IO）
- 局域网内任意设备通过**浏览器（HTTPS）**即可远程操作电台：实时语音、视频、PTT、频率/模式/频道切换、噪声抑制、功率调节等
- 电台通过**串口 CAT 协议**与控制软件通信
- **音频**：电台 USB 口 → USB 线 → 电脑，Windows 识别为**虚拟声卡**，语音经 **WebRTC** 实时推送到网页端
- **视频**：电台 **DVI 视频输出口** → **USB 采集卡** → 电脑 USB 口，将电台屏幕画面经 WebRTC 视频轨实时推送到网页端
- 无需公网服务器，局域网直接访问；支持同时多个客户端

## 二、系统架构

```
┌─────────────── 本机（服务端，Windows） ───────────────┐
│                                                       │
│  tkinter GUI（状态栏/菜单/日志）                        │
│        │                                              │
│  Flask + Flask-SocketIO（HTTP API + 事件推送）          │
│        │  WebRTC 信令 / 媒体                            │
│  aiortc（音频/视频轨道）                                │
│        │                                               │
│  DSP 管线                                              │
│   RX：抗混叠低通 → 整数抽取 → VAD → RMS 门控 → 增益       │
│   TX：采样率对齐重采样 → 钳值 → 声卡输出                  │
│        │                                               │
│  ffmpeg（视频采集，DirectShow）                         │
│  pyserial（Yaesu CAT 串口）                             │
└───────────────────────────────────────────────────────┘
        │ CAT            │ 虚拟声卡              │ USB 采集卡
   ┌────▼────┐     ┌────▼─────┐        ┌────▼─────────┐
   │ Yaesu 电台 │     │ FT-710 USB │        │ DVI→USB 采集卡 │
   │ (USB/CAT) │     │  Audio     │        │ (电台屏幕画面) │
   └─────────┘     └───────────┘        └──────────────┘
                ▲
        ┌───────┴────────┐
        │ 浏览器（手机/电脑） │
        │ https://<本机IP>:5000 │
        └────────────────┘
```

**技术栈**：Python 3.8、tkinter、Flask、Flask-SocketIO、python-socketio、aiortc、PyAV、OpenCV、NumPy/SciPy、PySerial、sounddevice、cryptography、ffmpeg（外部程序）。

## 三、功能特性

1. **实时语音（WebRTC）**：低延迟双向音频，音频编码 Opus/PCMU；网页端一键激活麦克风
2. **实时视频（WebRTC）**：电台 DVI 视频输出 → USB 采集卡 → ffmpeg 采集 → 网页实时显示电台屏幕画面
3. **PTT 控制**：CAT 指令 TX1/TX0，支持瞬时（momentary）模式；网络抖动自动重发，不卡死
4. **频率与模式**：FA 命令设置频率、MD 命令设置模式（USB/LSB/CW/AM/FM/CWR/RTTY），步进可调
5. **频道预设**：内置 160m–10m 常用频率；网页端可保存/切换/删除自定义频道
6. **噪声抑制 / 功率**：NB 命令开关噪声抑制，PC 命令调节发射功率
7. **DSP 音频处理**：
   - RX：抗混叠低通滤波 + 整数抽取（48kHz→16kHz，消除折返噪声）、VAD 语音活动检测静音丢弃、RMS 门控
   - TX：麦克风采样率自动对齐（16k/44.1k/48k → 声卡），增益钳值防爆音
   - DSP 线程守护：异常退出自动重启，RX 永不静默死亡
8. **视频自愈**：假死看门狗（8 秒无新帧强制重启 ffmpeg）、残留进程清理、快速失败退避重试、启动/停止互斥
9. **多客户端隔离**：每个 WebRTC 连接独立音频队列，慢客户端只丢自己的帧，互不干扰
10. **空闲连接回收**：无数据流动 180 秒自动清理 WebRTC 连接，不留僵尸连接
11. **串口自适应**：自动枚举 COM 口、断开自动重连
12. **HTTPS 自签名证书**：首次运行自动生成，支持局域网 IP/域名，无需手动配置
13. **日志系统**：异步写入（独立线程），防磁盘 IO 卡顿；界面内日志窗口 + 文件日志
14. **中英文界面**：GUI 与网页均可中/英切换

## 四、目录结构与文件说明

### 源码仓库文件（GitHub）

| 文件 | 说明 |
| --- | --- |
| `ft710_controller.py` | 主程序（约 5700 行）：GUI、Web 服务、WebRTC、DSP、视频、串口全部在此单文件 |
| `ft710.spec` | PyInstaller 打包配置（将整个 `ffmpeg/` 目录打入 exe，自包含） |
| `build.spec` | 备用打包配置 |
| `README.md` | 项目简介（简短版） |
| `DOCUMENTATION.md` | 本文档 |
| `docs/screenshots/` | 界面截图（GUI / Web UI） |
| `LICENSE` | MIT 许可证 |
| `THIRD_PARTY_NOTICES.md` | 第三方组件许可证声明（ffmpeg/Opus/CDN 等） |
| `.gitignore` | 排除证书、日志、配置、构建产物、旧版本等 |
| `requirements.txt` | Python 依赖列表 |
| `ft710_config.example.ini` | 配置示例 |
| `favicon (1).ico` | 应用/网页图标 |

### 运行期自动生成的文件（不入库）

| 文件 | 说明 |
| --- | --- |
| `ft710_config.ini` | 实际配置（首次运行自动生成） |
| `cert.pem` / `key.pem` | 自签名 SSL 证书（首次运行自动生成） |
| `ft710_server.log*` | 运行日志（按日期轮转） |

## 五、硬件说明

> 本软件基于 **Windows 10/11（64 位）**。电台侧需要三路连接：音频、视频、控制。

### 5.1 音频 —— 电台 USB 虚拟声卡

- 用 **USB 线**把电台的 **USB 口**连接到电脑 USB 口
- Windows 会把电台识别为**虚拟声卡**（如 `FT-710 USB Audio` / `USB Audio CODEC`）
- **RX 路径**：电台收到的语音由该虚拟声卡输入电脑（`[AUDIO] device_in`）
- **TX 路径**：电脑把麦克风/网页语音由该虚拟声卡输出到电台（`[AUDIO] device_out`）
- 无需额外音频线；设备号可在 GUI"音频设置"或网页 `/api/list_audio_devices` 查看

### 5.2 视频 —— 电台 DVI 输出 + USB 采集卡

- 电台的 **DVI 视频输出口** → **DVI/HDMI 转 USB 采集卡** → 电脑 USB 口
- 采集卡为 UVC 免驱设备，Windows 中表现为 **DirectShow 视频设备**
- 软件用 ffmpeg 以 `dshow` 方式采集该设备，把**电台屏幕画面**实时推送到网页端
- 在 `[VIDEO] device_name` 中填写采集卡设备名、`device_index` 填索引

### 5.3 控制 —— CAT 串口

- 电台 CAT 串口 → 电脑（USB 转串口），波特率默认 **38400**（`[SERIAL] baud_rate`）
- 老机型可改为 9600 等，与电台菜单中 CAT 速率一致即可

## 六、安装与运行

```bash
# 1. 安装 Python 3.8（64 位 Windows）
# 2. 安装依赖
pip install -r requirements.txt

# 3. 运行
python ft710_controller.py
```

首次运行自动生成配置文件与 SSL 证书。启动后本机弹出 GUI 窗口，并提示访问地址：

```
https://<本机IP>:5000
```

在局域网任意设备浏览器（推荐 Chrome/Edge）打开该地址，出现证书警告时选择"继续访问"即可（详见 9.3）。

## 七、界面截图

**主界面（tkinter GUI）**：

![GUI 主界面](docs/screenshots/gui_main.png)

**网页端界面（Web UI）**：

![Web UI 界面](docs/screenshots/webui_main.png)

## 八、配置参考

配置文件 `ft710_config.ini`（与 `ft710_config.example.ini` 结构一致）：

| 配置节 | 键 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `[SERIAL]` | `com_port` | `COM8` | 电台串口 |
| | `baud_rate` | `38400` | CAT 波特率（老机型可改 9600） |
| `[AUDIO]` | `sampling_rate` | `48000` | 虚拟声卡采样率 |
| | `channels` | `1` | 声道数 |
| | `block_size` | `512` | 音频块大小 |
| | `device_in` | `-1` | 输入设备号（-1 自动，可用 `/api/list_audio_devices` 查询） |
| | `device_out` | `-1` | 输出设备号 |
| `[NETWORK]` | `port` | `5000` | 服务端口 |
| `[PTT]` | `mode` | `momentary` | PTT 模式 |
| `[VIDEO]` | `enabled` | `false` | 视频开关 |
| | `mode` | `mjpeg` | 采集模式 |
| | `device_index` | `0` | 采集卡/摄像头索引 |
| | `device_name` | `USB Video` | 采集卡设备名（DirectShow） |
| | `width` | `640` | 分辨率宽 |
| | `height` | `480` | 分辨率高 |
| | `fps` | `15` | 帧率 |
| | `quality` | `2` | MJPEG 质量 |
| `[WEBRTC]` | `enabled` | `true` | WebRTC 开关 |
| | `audio_codec` | `pcmu` | 音频编码（pcmu/opus） |
| | `video_codec` | `vp8` | 视频编码 |
| `[CHANNELS]` | `ch_160m_1` … | `1.840,LSB,` | 频道预设：`频率,模式,名称` |
| `[LANGUAGE]` | `code` | `zh` | 界面语言 `zh` / `en` |

## 九、使用说明

### 9.1 基础使用流程

1. 双击 `ft710.exe`（或 `python ft710_controller.py`）启动
2. 程序自动生成配置文件 `ft710_config.ini` 与 SSL 证书（首次访问需手动信任）
3. 在 GUI 窗口或控制台查看访问地址 `https://<电脑IP>:5000`
4. 同局域网其他设备浏览器打开该地址

### 9.2 首次访问证书信任

- 桌面浏览器（Chrome/Edge）：点"**高级**" → "**继续访问**"
- iOS Safari：设置 → 通用 → **VPN 与设备管理** → 信任证书

### 9.3 本机 GUI 菜单

| 菜单 | 功能 |
| --- | --- |
| 串口设置 | 修改串口配置、重新连接串口 |
| 音频设置 | 修改音频设备、重启音频引擎 |
| 视频设置 | 视频设备配置 |
| 日志 | 清空控制台、打开日志文件 |
| 语言 | 中文 / English 切换 |
| 关于 | 版本信息 |

状态栏实时显示：串口连接状态、电台状态、TX/RX、音频状态、视频状态等。

### 9.4 网页端操作

1. **连接**：页面自动建立 Socket.IO + WebRTC 连接
2. **激活音频**：点击"激活音频"，授权麦克风后即可双向语音
3. **PTT**：按住"PTT"说话（或选择瞬时模式）
4. **频率调节**：直接输入频率回车，或旋钮步进微调
5. **模式**：USB/LSB/CW/AM/FM/CWR/RTTY 一键切换
6. **频道**：点击预设频道跳转；可保存当前频率为频道、删除/更新频道
7. **天调**：点击"调谐"按钮启动/停止（见已知问题 #1）
8. **其他**：噪声抑制开关、发射功率、视频开关、增益/噪声电平调节、延迟测试（rtt_ping）

### 9.5 HTTP API 参考

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/list_ports` | 枚举串口 |
| GET | `/api/list_audio_devices` | 枚举音频设备 |
| POST | `/api/nb` | 噪声抑制开关 |
| POST | `/api/freq` | 设置频率 |
| POST | `/api/freq_adjust` | 频率步进微调 |
| POST | `/api/set_step` | 设置步进 |
| POST | `/api/mode` | 设置模式 |
| POST | `/api/power` | 设置功率 |
| POST | `/api/ptt` | PTT 控制 |
| POST | `/api/ex` | EX 菜单命令读/写（p1/p2/p3/value） |
| GET | `/api/state` | 电台/连接状态 |
| GET | `/api/channels` | 频道列表 |
| POST | `/api/save_channel` | 保存频道 |
| POST | `/api/set_channel` | 切换频道 |
| POST | `/api/delete_channel` | 删除频道 |
| POST | `/api/update_channel` | 更新频道 |
| POST | `/api/audio_restart` | 重启音频 |
| POST | `/api/video_toggle` | 视频开关 |
| POST | `/api/video_config` | 视频配置 |
| POST | `/webrtc/offer` | WebRTC 信令（offer/answer） |
| POST | `/webrtc/ice` | ICE 候选交换 |

### 9.6 Socket.IO 事件

客户端可发：`ptt_change`、`set_gain_level`、`set_noise_level`、`set_ptt_mode`、`set_com_port`、`set_audio_device`、`set_mic_gain`、`rtt_ping`。
服务端广播：`trx_updated`（TX/RX 状态变化）等。

### 9.7 常见问题

- **串口无法打开**：检查 COM 端口是否被其他程序占用，或 `baud_rate` 是否与电台一致
- **音频无声音**：检查 `device_in/device_out` 是否指向电台 USB 虚拟声卡、浏览器麦克风权限
- **视频黑屏**：确认 USB 采集卡未被其他程序占用、`[VIDEO] device_name` 是否正确
- **iOS/Safari 无法访问**：安装并信任自签名证书（见 9.2）

## 十、打包为 EXE

```bash
pip install pyinstaller
pyinstaller --clean --noconfirm ft710.spec
```

产物：`dist\ft710\`，**自包含**（ffmpeg 及全部 DLL 已打入，`ffmpeg/` 必须与 exe 同级），可拷到无 Python 环境的 Windows 机器运行。

## 十一、电台兼容性

控制层使用**标准 Yaesu CAT 协议**（FA/MD/TX/TU/RC/NB/PC 等通用命令），无 FT-710 专属命令，因此兼容：

- Yaesu **FT-710**（默认 38400）
- Yaesu **FTDX10**（38400）
- Yaesu **FT-991A**（38400）
- Yaesu **FTDX3000 / FTDX1200 / FT-891 / FT-450D / FT-950 / FT-2000 / FT-991** 等全系

**注意事项**：
1. 波特率在 `[SERIAL] baud_rate` 调整，与电台菜单中 CAT 速率一致即可
2. 电台需开启 CAT 口；音频走电台 USB 虚拟声卡
3. 视频依赖电台的 **DVI 视频输出**：无 DVI 输出的机型无法显示电台屏幕画面（视频功能需另配 USB 摄像头时请改 `[VIDEO] device_name`）

## 十二、已知问题

1. **网页端天调按钮**：前端调用 `/api/tuner`，但后端尚未实现该路由（当前返回 404）。天调相关 CAT 命令（`TU`/`RC`）已封装在 `send_radio` 中，可通过 `/api/ex` 或后续版本使用。
2. **平台**：音频/视频采集依赖 Windows（DirectShow、WASAPI），暂未适配 Linux/macOS。
3. **自签名证书**：浏览器需手动信任一次证书，属预期行为。

## 十三、安全与合规

- 使用**自签名证书**与 WebRTC，适合**局域网/可信网络**，请勿直接暴露公网
- 若需远程访问，建议通过 VPN/ZeroTier 等安全通道
- 业余无线电操作请遵守所在地法律法规（需持有相应操作执照）

## 十四、许可

本项目以 **MIT** 协议开源（见 `LICENSE`）。随 EXE 分发的第三方组件（ffmpeg 为 GPL、Opus 为 BSD）详见 `THIRD_PARTY_NOTICES.md`。

Copyright (c) 2026 [Robby Wang](https://github.com/ruibaow)

---

# FT-710 Remote Controller — Detailed Documentation (English)

## 1. Introduction

**FT-710 Remote Controller** is a **WebRTC-based** remote controller for the **Yaesu FT-710** HF transceiver, built for **Windows 10/11 (64-bit)**.

- A local **tkinter GUI** runs as the server, together with a **web server** (Flask + Socket.IO)
- Any device on the LAN can operate the radio from a **browser over HTTPS**: real-time audio, video, PTT, frequency/mode/channel, noise blanker, power, etc.
- The radio is controlled over **serial CAT protocol**
- **Audio**: radio USB port → USB cable → PC, exposed as a **virtual sound card**; streamed via **WebRTC**
- **Video**: the radio's **DVI video output** → **USB capture card** → PC USB port; the radio screen is streamed over a WebRTC video track
- No public server needed; multi-client capable

## 2. Architecture

- **GUI server**: tkinter window (status bar, menus, log)
- **Web**: Flask + Flask-SocketIO (HTTP API + real-time events)
- **Realtime media**: aiortc (WebRTC) — audio Opus/PCMU, video VP8/MJPEG
- **DSP chain**: RX: anti-aliasing low-pass → integer decimation (48k→16k) → VAD → RMS gate → gain; TX: sample-rate alignment → clamp → sound card out
- **Video pipeline**: ffmpeg (DirectShow) captures the radio screen from the USB capture card → MJPEG frames → WebRTC video track (decoded in a background thread so the event loop is never blocked)
- **Control**: PySerial — Yaesu CAT (8N1, configurable baud)
- **Security**: HTTPS with auto-generated self-signed certificate

**Tech stack**: Python 3.8, tkinter, Flask, Flask-SocketIO, python-socketio, aiortc, PyAV, OpenCV, NumPy/SciPy, PySerial, sounddevice, cryptography, ffmpeg (external binary).

## 3. Features

1. Real-time bidirectional audio over WebRTC (Opus/PCMU)
2. Real-time video: radio DVI output → USB capture card → ffmpeg → radio screen in the browser
3. PTT via CAT (TX1/TX0), momentary mode, retry on jitter
4. Frequency (FA) and mode (MD: USB/LSB/CW/AM/FM/CWR/RTTY) control with adjustable step
5. Channel presets (160m–10m) — save / switch / delete via web UI
6. Noise blanker (NB) and TX power (PC)
7. DSP: anti-aliasing decimation, VAD gating, RMS gating; TX sample-rate auto-align (16k/44.1k/48k) and clipping protection; DSP supervisor auto-restart
8. Video self-healing: 8 s no-frame watchdog restarts ffmpeg; residual-process cleanup; backoff retry; start/stop mutex
9. Per-client WebRTC audio isolation (slow clients only drop their own frames)
10. Idle connection recycle: 180 s without data flow → clean up
11. Serial auto-reconnect and COM-port enumeration
12. Auto-generated self-signed HTTPS certificate
13. Async logging (dedicated writer thread) — no IO stalls
14. Bilingual UI (zh/en)

## 4. Project Structure

**Repository files**: `ft710_controller.py` (single-file main program), `ft710.spec` / `build.spec` (PyInstaller), `README.md`, `DOCUMENTATION.md`, `docs/screenshots/`, `LICENSE` (MIT), `THIRD_PARTY_NOTICES.md`, `.gitignore`, `requirements.txt`, `ft710_config.example.ini`, `favicon (1).ico`.

**Generated at runtime (not committed)**: `ft710_config.ini`, `cert.pem`, `key.pem`, `ft710_server.log*`.

## 5. Hardware Setup

Windows 10/11 (64-bit). Three connections to the radio:

### 5.1 Audio — radio USB virtual sound card
- Connect the radio's **USB port** to the PC with a **USB cable**
- Windows exposes the radio as a **virtual sound card** (e.g. `FT-710 USB Audio`)
- RX: radio audio → virtual sound card → PC input (`device_in`); TX: PC output (`device_out`) → virtual sound card → radio
- No extra audio cables needed

### 5.2 Video — radio DVI output + USB capture card
- Radio **DVI video output** → **DVI/HDMI-to-USB capture card** → PC USB port
- The capture card is a UVC device appearing as a **DirectShow video device**
- ffmpeg captures it via `dshow`; the **radio screen** is streamed to the browser
- Set the device name in `[VIDEO] device_name`, index in `device_index`

### 5.3 Control — CAT serial
- Radio CAT serial port → PC (USB-serial), baud **38400** by default (`[SERIAL] baud_rate`)
- Older radios may use 9600 etc. — match the radio's CAT menu setting

## 6. Install & Run

```bash
pip install -r requirements.txt
python ft710_controller.py
```

First run auto-generates the config file and SSL certificate. Then open `https://<LAN-IP>:5000` in Chrome/Edge (accept the self-signed certificate warning).

## 7. Screenshots

**Main GUI (tkinter)**:

![Main GUI](docs/screenshots/gui_main.png)

**Web UI**:

![Web UI](docs/screenshots/webui_main.png)

## 8. Configuration

See `ft710_config.example.ini`. Key sections: `[SERIAL]` (com_port, baud_rate=38400), `[AUDIO]` (sampling_rate=48000, channels, block_size, device_in/out=-1 auto), `[NETWORK]` (port=5000), `[PTT]` (mode), `[VIDEO]` (enabled, device_index, device_name, width/height/fps/quality), `[WEBRTC]` (enabled, audio_codec, video_codec), `[CHANNELS]` (freq, mode, name), `[LANGUAGE]` (code=zh/en).

## 9. Usage

- **Basic flow**: start the app → it auto-generates config + SSL cert → open `https://<LAN-IP>:5000` from another LAN device
- **Certificate trust**: desktop browsers — "Advanced" → "Proceed"; iOS Safari — Settings → General → VPN & Device Management → trust the cert
- **Local GUI menus**: Serial setup/reconnect, Audio device/restart, Video setup, Log clear/file, Language, About
- **Web UI**: activate audio (grant mic), hold PTT to talk, enter frequency or use step, switch mode, channel presets, tuner, noise blanker, power, video toggle, gain/noise level, latency test (rtt_ping)
- **Troubleshooting**: COM port busy → close other apps; no audio → check device_in/out point to the radio USB sound card and mic permission; black video → make sure the capture card is not held by another app and `device_name` is correct; iOS can't open → install & trust the self-signed cert
- **API**: see the route table in the Chinese section (9.5). Socket.IO events: `ptt_change`, `set_gain_level`, `set_noise_level`, `set_ptt_mode`, `set_com_port`, `set_audio_device`, `set_mic_gain`, `rtt_ping`; server emits `trx_updated`, etc.

## 10. Build EXE

```bash
pip install pyinstaller
pyinstaller --clean --noconfirm ft710.spec
```
Output: `dist\ft710\` — self-contained (bundles the whole `ffmpeg/` folder with its DLLs; keep `ffmpeg/` next to the exe); runs on Windows machines without Python.

## 11. Radio Compatibility

Uses **standard Yaesu CAT** (FA/MD/TX/TU/RC/NB/PC), so it works with any Yaesu CAT radio: FT-710, FTDX10, FT-991A, FTDX3000, FTDX1200, FT-891, FT-450D, FT-950, FT-2000, FT-991, etc. Set `baud_rate` to match the radio's CAT menu; audio uses the radio USB virtual sound card. Video requires a **DVI video output** on the radio (models without it cannot show the radio screen).

## 12. Known Issues

1. The web UI tuner button calls `/api/tuner`, but the backend route is not yet implemented (currently 404). CAT tuner commands (`TU`/`RC`) are already wrapped in `send_radio`.
2. Audio/video capture is Windows-specific (DirectShow/WASAPI); no Linux/macOS support yet.
3. Browsers must accept the self-signed certificate once (expected).

## 13. Security

Self-signed certificate + WebRTC → suitable for **trusted LANs only**; do not expose directly to the public internet. For remote access use VPN/ZeroTier. Operate the radio in accordance with local amateur-radio regulations.

## 14. License

MIT (see `LICENSE`). Third-party components bundled with the EXE (ffmpeg is GPL, Opus is BSD) are listed in `THIRD_PARTY_NOTICES.md`.

Copyright (c) 2026 [Robby Wang](https://github.com/ruibaow)
