# FT-710 Remote Controller

基于 **WebRTC** 的 Yaesu FT-710 短波电台远程控制器。本机运行 tkinter 图形界面作为服务端，任意浏览器通过 HTTPS 网页即可远程操作电台（音频/视频/PTT/频率）。

> WebRTC-based remote controller for the **Yaesu FT-710** HF transceiver. A local tkinter GUI runs as the server; any browser can control the radio remotely over HTTPS (audio / video / PTT / frequency).

## 特性 / Features

- **本地图形界面**：tkinter，中/英双语（自动跟随系统语言）
- **WebRTC 低延迟音频/视频**：网页端实时收发语音（Opus/PCMU），视频（MJPEG/VP8）
- **PTT 控制**：CAT 指令控制发射/接收，300ms 超时自动重发
- **串口自适应**：自动枚举 COM 口、自动重连、波特率/电台地址可配置
- **DSP 处理**：抗混叠低通抽取、VAD 语音门控、麦克风采样率自动对齐（16k/44.1k/48k）
- **多客户端隔离**：每个 WebRTC 连接独立音频队列，慢客户端只丢自己的帧
- **视频自愈**：假死看门狗（8 秒无帧自动重启 ffmpeg）、残留进程清理、快速失败退避
- **空闲连接回收**：无数据流动 180 秒自动清理 WebRTC 连接，不留僵尸连接
- **HTTPS 自签名证书**：首次运行自动生成，支持局域网 IP/域名

## 环境要求 / Requirements

- 64 位 Windows（打包验证环境）
- Python **3.8**（也兼容 3.9–3.12，未经逐一验证）
- 依赖见 `requirements.txt`
- 视频功能需要 **ffmpeg**（随 exe 分发，或加入系统 PATH）

## 安装运行 / Install & Run

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 运行（需连接电台与音频设备）
python ft710_controller.py
```

首次运行会自动生成 `ft710_config.ini` 默认配置与自签名 SSL 证书。程序启动后，在局域网内任意设备浏览器访问：

```
https://<本机IP>:5000
```

> 浏览器提示证书不受信任时，选择"继续访问"（自签名证书）。建议使用 Chrome / Edge。

## 配置说明 / Configuration

配置文件 `ft710_config.ini`（参照 `ft710_config.example.ini`）：

| 配置节 | 说明 |
| --- | --- |
| `[SERIAL]` | COM 口、波特率 |
| `[AUDIO]` | 采样率、块大小、输入/输出声卡设备号 |
| `[NETWORK]` | 服务端口（默认 5000） |
| `[PTT]` | PTT 模式（momentary） |
| `[VIDEO]` | 视频开关、摄像头索引、分辨率/帧率/质量 |
| `[WEBRTC]` | WebRTC 音频/视频编码 |
| `[CHANNELS]` | 快捷频率按钮（160m–10m） |
| `[LANGUAGE]` | 界面语言 `zh` / `en` |

## 打包为 EXE / Build EXE

使用 PyInstaller（本项目基于 **Python 3.8** + PyInstaller 5.13.2 构建）：

```bash
pip install pyinstaller
pyinstaller --clean --noconfirm ft710.spec
```

产物位于 `dist\ft710\`。`ft710.spec` 会把 `ffmpeg/` 目录（含全部 DLL）一并打包，exe 自包含、不依赖系统 ffmpeg。

## 目录结构 / Project Structure

```
ft710_controller.py        # 主程序（GUI 服务端 + WebRTC + DSP + Flask/SocketIO）
ft710.spec                 # PyInstaller 打包配置
build.spec                 # 备用打包配置
ft710_config.example.ini   # 配置示例
requirements.txt           # Python 依赖
LICENSE                    # MIT
THIRD_PARTY_NOTICES.md     # 第三方组件许可证声明
favicon (1).ico            # 应用图标
```

## 安全提示 / Security Notes

- 程序使用**自签名证书**与 WebRTC，适合局域网/可信网络；**请勿**直接暴露到公网
- 串口、音频、摄像头设备号为本机相关配置，请勿提交真实 `ft710_config.ini`
- 请在所在地法律法规允许范围内使用（业余无线电操作需持有相应执照）

## 协议 / License

本项目以 [MIT](LICENSE) 协议开源。

Copyright (c) 2026 [Robby Wang](https://github.com/ruibaow)

---

## English

**FT-710 Remote Controller** is a WebRTC-based remote controller for the Yaesu FT-710 HF transceiver. A tkinter GUI runs locally as the server; any browser on the LAN can operate the radio over HTTPS.

### Features
- Local tkinter GUI, bilingual (zh/en)
- WebRTC low-latency audio (Opus/PCMU) and video (MJPEG/VP8)
- PTT via CAT with 300 ms retry
- Auto COM-port enumeration / reconnect
- DSP: anti-aliasing decimation, VAD gating, mic sample-rate auto-align (16k/44.1k/48k)
- Per-client WebRTC audio isolation (slow clients only drop their own frames)
- Video self-healing: 8 s no-frame watchdog restarts ffmpeg; residual-process cleanup; backoff retry
- Idle WebRTC connections recycled after 180 s of no data flow
- Auto-generated self-signed HTTPS certificate

### Requirements
- 64-bit Windows, Python **3.8**+, ffmpeg (for video; bundled with the exe build)

### Run
```bash
pip install -r requirements.txt
python ft710_controller.py
```
Then open `https://<LAN-IP>:5000` in Chrome/Edge (accept the self-signed cert warning).

### Build EXE
```bash
pip install pyinstaller
pyinstaller --clean --noconfirm ft710.spec
```
Output: `dist\ft710\` (self-contained, includes the whole `ffmpeg/` folder).

### License
[MIT](LICENSE). Use within your local laws (amateur radio operation requires a license where applicable).
