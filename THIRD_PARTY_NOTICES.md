# 第三方组件声明 / Third-Party Notices

本项目（FT-710 Remote Controller）以 **MIT** 协议开源。以下第三方组件随本项目**打包的 EXE** 一起分发，版权归其各自作者所有，请遵守其各自的许可证条款。

This project (FT-710 Remote Controller) is released under the **MIT** license. The following third-party components are distributed with the **packaged EXE** and remain under their respective licenses.

## 随 EXE 分发的二进制 / Bundled binaries

| 组件 | 许可证 | 说明 |
| --- | --- | --- |
| ffmpeg (Gyan.dev build) | **GPL-2.0+ / version3** | 视频采集工具，由程序以子进程方式调用。构建配置含 `--enable-gpl --enable-version3`。源码与构建说明：https://www.gyan.dev/ffmpeg/builds/ ，许可证全文：https://www.gnu.org/licenses/old-licenses/gpl-2.0.html |
| libopus / Opus | BSD-3-Clause | Opus 音频编解码库：https://opus-codec.org/ |

## 网页端引用的在线资源 / Web resources (loaded from CDN)

| 组件 | 许可证 | 说明 |
| --- | --- | --- |
| socket.io client (via cdnjs) | MIT | 网页端实时通信：https://socket.io/ |

## Python 依赖 / Python dependencies

见 `requirements.txt`。各包版权归其作者所有：aiortc (BSD-3-Clause)、PyAV (BSD-3-Clause)、Flask / Flask-SocketIO (BSD-3-Clause)、scipy (BSD-3-Clause)、numpy (BSD-3-Clause)、opencv-python (Apache-2.0)、netifaces (MIT)、psutil (BSD-3-Clause)、pyserial (BSD-3-Clause)、sounddevice (MIT)、cryptography (Apache-2.0 / BSD)。

## 注意事项 / Notes

- 源码仓库（GitHub）**不包含** ffmpeg 与 opus DLL，仅打包 EXE 时一并分发。
- 分发 EXE 时请随附本文件，以便 ffmpeg（GPL）等组件的许可证要求得到满足。
- 应用图标 `favicon (1).ico` 若含有第三方素材，请确认其授权后再分发。
