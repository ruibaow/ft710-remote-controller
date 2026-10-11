# FT-710 Remote Controller v2.2.2
# GUI Server - tkinter + Flask/SocketIO
#
# Copyright (c) 2026  Robby Wang
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import engineio.async_drivers.threading
import engineio.server
import socketio as _socketio
import socketio.server
import serial
import time
import socket
import subprocess
import psutil
import numpy as np
import sounddevice as sd
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit
from threading import Lock, Event
import json
import traceback
import sys
import os
import configparser
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, font as tkfont
import threading as _gui_threading
import threading as _threading
import queue as _queue
import logging
import logging.handlers
from datetime import datetime
import collections
from concurrent.futures import ThreadPoolExecutor

# ===================== F1: GIL 切换间隔（可配置，默认 0.5ms） =====================
# CPython 默认 5ms：其它线程占住 GIL 时，DSP 线程在每个 Python↔C 边界
# （sosfilt/ONNX/soxr/numpy 共约 15~20 次/帧）最多要干等 5ms。缩短间隔能降 DSP 等待，
# 但过小（如 0.5ms）会让本机多线程频繁上下文切换空转、反而拖慢整个软件（GUI 迟钝）。
# v2.2.2 起由 [AUDIO] GIL_SWITCH_INTERVAL_MS 控制：默认 0.5ms（沿用 v2.2 实时调优，
# 实测它在 DSP/WebRTC 音频下丢帧最少），0 = 用系统默认 5ms。
def _apply_gil_switch_interval():
    try:
        ms = float(config['AUDIO'].get('GIL_SWITCH_INTERVAL_MS', '0.5'))
        if ms > 0:
            sys.setswitchinterval(ms / 1000.0)
        else:
            sys.setswitchinterval(0.005)
    except Exception:
        pass

# ===================== H-a: 线程/进程优先级（进程恢复 ABOVE_NORMAL） =====================
# 只调调度优先级，不改任何处理逻辑、不影响音质。
# v2.2.2 起：整进程默认 ABOVE_NORMAL（0x8000，沿用 v2.2 实时调优），保留可配置项
# [SYSTEM] PROCESS_PRIORITY（normal/above_normal/high，缺省 above_normal）。
# 关键音频线程（DSP/TX写声卡）仍提权 THREAD_PRIORITY_HIGHEST(2)；视频线程取消提权，
# 避免与 ffmpeg 子进程抢占、加剧卡顿。
# 注意：x64 下必须显式声明 restype/argtypes——GetCurrentProcess 伪句柄(-1)
# 默认按 c_int 处理会截断成无效句柄，调用静默失败。
_PROCESS_PRIORITY_CLASS = {
    'normal': 0x0020,
    'above_normal': 0x8000,
    'high': 0x0080,
}

def _set_process_priority_class(cls):
    if sys.platform != 'win32' or not cls:
        return False
    try:
        import ctypes
        import ctypes.wintypes as _wt
        _k = ctypes.windll.kernel32
        _k.GetCurrentProcess.restype = _wt.HANDLE
        _k.SetPriorityClass.argtypes = (_wt.HANDLE, _wt.DWORD)
        _k.SetPriorityClass.restype = _wt.BOOL
        return bool(_k.SetPriorityClass(_k.GetCurrentProcess(), cls))
    except Exception:
        return False

def _boost_thread_priority():
    if sys.platform != 'win32':
        return False
    try:
        import ctypes
        import ctypes.wintypes as _wt
        _k = ctypes.windll.kernel32
        _k.GetCurrentThread.restype = _wt.HANDLE
        _k.SetThreadPriority.argtypes = (_wt.HANDLE, ctypes.c_int)
        _k.SetThreadPriority.restype = _wt.BOOL
        return bool(_k.SetThreadPriority(_k.GetCurrentThread(), 2))
    except Exception:
        return False

def _apply_process_priority():
    _set_process_priority_class(_PROCESS_PRIORITY_CLASS.get(
        str(config['SYSTEM'].get('PROCESS_PRIORITY', 'above_normal')).lower(), 0x8000))

# 视频捕获（可选）
try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False
    print("OpenCV不可用，视频功能已禁用")

# ===================== SSL BAD_LENGTH 修复 =====================
def _patch_ssl_socket_send():
    import ssl as _ssl_mod
    _orig_send = _ssl_mod.SSLSocket.send
    def _safe_send(self, data, flags=0):
        try:
            return _orig_send(self, data, flags)
        except _ssl_mod.SSLError as e:
            if 'BAD_LENGTH' in str(e) or 'bad length' in str(e).lower():
                logging.getLogger("ft710").warning(f"SSL BAD_LENGTH 错误，数据长度={len(data)}")
                raise
            raise
    _ssl_mod.SSLSocket.send = _safe_send
_patch_ssl_socket_send()

# ===================== 配置管理 =====================
_exe_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))

_config_dir = _exe_dir
_test_file = os.path.join(_config_dir, ".ft710_test")
try:
    with open(_test_file, 'w') as f:
        f.write("test")
    os.remove(_test_file)
except Exception:
    _config_dir = os.path.join(os.path.expanduser("~"), ".ft710")
    os.makedirs(_config_dir, exist_ok=True)

CONFIG_FILE = os.path.join(_config_dir, "ft710_config.ini")
LOG_FILE    = os.path.join(_config_dir, "ft710_server.log")
APP_DIR = _exe_dir

# PyInstaller 6 的 onedir 布局把随包资源放在 <exe>\_internal\ 下
_RSC_DIR = os.path.join(_exe_dir, '_internal') if os.path.isdir(os.path.join(_exe_dir, '_internal')) else _exe_dir

# ===================== 日志系统 =====================
import queue as _gui_log_q
_gui_log_queue = _gui_log_q.Queue(maxsize=5000)

def _setup_logger():
    logger = logging.getLogger("ft710")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        fh = logging.handlers.TimedRotatingFileHandler(
            LOG_FILE, when='h', interval=24, backupCount=2, encoding='utf-8'
        )
        fh.setFormatter(logging.Formatter('%(asctime)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
        logger.addHandler(fh)
    except Exception as e:
        print(f"日志文件初始化失败：{e}")
    return logger

_logger = _setup_logger()

# 日志写入队列：文件/控制台 IO 放到独立守护线程，
# 避免磁盘慢/管道阻塞等瞬时 IO 卡顿拖死所有调用线程（WebRTC 事件循环、采集线程、Flask）
_log_writer_queue = _queue.Queue(maxsize=10000)

def _log_writer_loop():
    while True:
        try:
            msg = _log_writer_queue.get()
        except Exception:
            return
        if msg is None:
            break
        try:
            _logger.info(msg)
        except Exception:
            pass
        try:
            print(msg)
        except Exception:
            pass

_threading.Thread(target=_log_writer_loop, daemon=True, name='LogWriter').start()

def log(msg):
    try:
        _log_writer_queue.put_nowait(msg)
    except Exception:
        try:
            _log_writer_queue.get_nowait()
        except Exception:
            pass
        try:
            _log_writer_queue.put_nowait(msg)
        except Exception:
            pass
    try:
        _gui_log_queue.put_nowait(msg)
    except Exception:
        # 队列满：丢最旧一条，保证 GUI 日志始终保留最新内容
        try:
            _gui_log_queue.get_nowait()
        except Exception:
            pass
        try:
            _gui_log_queue.put_nowait(msg)
        except Exception:
            pass

# ===================== 默认配置 =====================
DEFAULT_CONFIG = {
    'LANGUAGE': {
        'CODE': 'zh'
    },
    'SERIAL': {
        'COM_PORT': 'COM8',
        'BAUD_RATE': 38400
    },
    'AUDIO': {
        'SAMPLING_RATE': 48000,
        'CHANNELS': 1,
        'BLOCK_SIZE': 512,
        'DEVICE_IN': '-1',
        'DEVICE_OUT': '-1',
        'DEVICE_IN_NAME': '',
        'DEVICE_OUT_NAME': '',
        'RX_VAD_THRESHOLD_DB': '-55',
        'AI_ATTEN_LIM_DB': '12',
        'AI_INTRA_THREADS': '1',
        'GIL_SWITCH_INTERVAL_MS': '0.5'
    },
    'SYSTEM': {
        'PROCESS_PRIORITY': 'above_normal'
    },
    'NETWORK': {
        'PORT': 5000
    },
    'PTT': {
        'MODE': 'momentary'
    },
    'VIDEO': {
        'ENABLED': 'false',
        'DEVICE_INDEX': '0',
        'DEVICE_NAME': 'USB Video',
        'WIDTH': '640',
        'HEIGHT': '480',
        'FPS': '15',
        'QUALITY': '3'
    },
    'CHANNELS': {
        'ch_160m_1': '1.840,LSB,',
        'ch_160m_2': '1.860,LSB,',
        'ch_160m_3': '1.900,LSB,',
        'ch_80m_1': '3.500,LSB,',
        'ch_80m_2': '3.800,LSB,',
        'ch_80m_3': '3.900,LSB,',
        'ch_40m_1': '7.050,LSB,',
        'ch_40m_2': '7.068,LSB,',
        'ch_40m_3': '7.100,LSB,',
        'ch_20m_1': '14.270,USB,',
        'ch_20m_2': '14.300,USB,',
        'ch_20m_3': '14.350,USB,',
        'ch_15m_1': '21.400,USB,',
        'ch_15m_2': '21.450,USB,',
        'ch_15m_3': '21.350,USB,',
        'ch_10m_1': '28.400,USB,'
    }
}

def load_config():
    config = configparser.ConfigParser()
    if not os.path.exists(CONFIG_FILE):
        config.read_dict(DEFAULT_CONFIG)
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            config.write(f)
        log(f"创建默认配置文件：{CONFIG_FILE}")
    else:
        config.read(CONFIG_FILE, encoding='utf-8')
        log(f"加载配置文件：{CONFIG_FILE}")
    
    for section in DEFAULT_CONFIG:
        if section not in config:
            config[section] = DEFAULT_CONFIG[section]
        for key in DEFAULT_CONFIG[section]:
            if key not in config[section]:
                config[section][key] = DEFAULT_CONFIG[section][key]
    
    return config

_config_save_lock = _threading.Lock()

def save_config(config):
    # 原子写：临时文件 + fsync + os.replace，消除并发写与写一半崩溃导致的 ini 截损
    with _config_save_lock:
        tmp = CONFIG_FILE + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                config.write(f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, CONFIG_FILE)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            raise

config = load_config()

# ===================== 运行时调优（读取配置后生效） =====================
_apply_process_priority()
_apply_gil_switch_interval()

# ===================== 中英文文案表（I18N） =====================
LANG = config['LANGUAGE'].get('CODE', 'zh').lower()

I18N = {
    # ---- GUI: 菜单 ----
    'menu.serial':        {'zh': '  串口设置  ',     'en': '  Serial  '},
    'menu.serial.edit':   {'zh': '  修改串口配置',   'en': '  Edit Serial Port'},
    'menu.serial.reconn': {'zh': '  重新连接串口',   'en': '  Reconnect Serial'},
    'menu.audio':         {'zh': '  音频设置  ',     'en': '  Audio  '},
    'menu.audio.edit':    {'zh': '  修改音频设备',   'en': '  Edit Audio Device'},
    'menu.audio.restart': {'zh': '  重启音频流',     'en': '  Restart Audio Stream'},
    'menu.video':         {'zh': '  视频设置  ',     'en': '  Video  '},
    'menu.video.edit':    {'zh': '  视频设备配置',   'en': '  Configure Video Device'},
    'menu.log':           {'zh': '  日志  ',         'en': '  Log  '},
    'menu.log.clear':     {'zh': '  清空控制台',     'en': '  Clear Console'},
    'menu.log.file':      {'zh': '  打开日志文件',   'en': '  Open Log File'},
    'menu.about':         {'zh': '  关于  ',         'en': '  About  '},
    'menu.about.info':    {'zh': '  关于',           'en': '  About'},
    'menu.lang':          {'zh': '  语言  ',         'en': '  Language  '},
    'menu.lang.zh':       {'zh': '  中文',           'en': '  Chinese'},
    'menu.lang.en':       {'zh': '  英文',           'en': '  English'},

    # ---- GUI: 状态 pill / 状态栏 / 标题 ----
    'st.starting':        {'zh': '● 服务 启动中',   'en': '● Service starting'},
    'st.running':         {'zh': '● 服务 运行中',   'en': '● Service running'},
    'st.audio.init':      {'zh': '● 音频 未初始化', 'en': '● Audio not init'},
    'st.audio.ready':     {'zh': '● 音频 就绪',     'en': '● Audio ready'},
    'st.com.disc':        {'zh': '● 串口 未连接',   'en': '● Serial disconnected'},
    'st.com.port':        {'zh': '● 串口 {port}',   'en': '● Serial {port}'},
    'st.radio.disc':      {'zh': '● 电台 未连接',   'en': '● Rig disconnected'},
    'st.radio.port':      {'zh': '● 电台 {port}',   'en': '● Rig {port}'},
    'st.access.init':     {'zh': '启动中...',       'en': 'Starting...'},
    'lbl.access':         {'zh': '访问地址',         'en': 'Access Address'},
    'lbl.com':            {'zh': '串口配置',         'en': 'Serial Config'},
    'lbl.console':        {'zh': '控制台输出',       'en': 'Console Output'},

    # ---- GUI: 对话框 ----
    'dlg.com.title':      {'zh': '串口配置',         'en': 'Serial Config'},
    'lbl.comport':        {'zh': 'COM 端口',         'en': 'COM Port'},
    'lbl.baud':           {'zh': '波特率',           'en': 'Baud Rate'},
    'btn.apply.reconn':   {'zh': '应用并重连',       'en': 'Apply & Reconnect'},
    'dlg.audio.title':    {'zh': '音频设备配置',     'en': 'Audio Device Config'},
    'lbl.input.dev':      {'zh': '输入设备:',        'en': 'Input Device:'},
    'lbl.output.dev':     {'zh': '输出设备:',        'en': 'Output Device:'},
    'lbl.sys.default':    {'zh': '系统默认',         'en': 'System Default'},
    'btn.apply.audio':    {'zh': '应用并重启音频',   'en': 'Apply & Restart Audio'},
    'dlg.video.title':    {'zh': '视频设备配置',     'en': 'Video Device Config'},
    'lbl.camera':         {'zh': '摄像头:',          'en': 'Camera:'},
    'lbl.device':         {'zh': '设备 {i}',         'en': 'Device {i}'},
    'lbl.no.dev':         {'zh': '未检测到',         'en': 'Not found'},
    'lbl.scanning':       {'zh': '正在扫描...',      'en': 'Scanning...'},
    'lbl.res':            {'zh': '分辨率:',          'en': 'Resolution:'},
    'lbl.fps':            {'zh': '帧率(FPS):',       'en': 'Frame Rate(FPS):'},
    'lbl.jpeg':           {'zh': 'JPEG质量:',        'en': 'JPEG Quality:'},
    'lbl.quality.hint':   {'zh': '(2-31, 越小越好)', 'en': '(2-31, lower is better)'},
    'lbl.enable.video':   {'zh': '启用视频捕获',     'en': 'Enable Video Capture'},
    'dlg.about.title':    {'zh': '关于',             'en': 'About'},
    'lbl.about.ver':      {'zh': '版本  v{ver}',     'en': 'Version  v{ver}'},
    'lbl.about.email':    {'zh': '邮箱  {e}',       'en': 'Email  {e}'},
    'lbl.about.qq':       {'zh': 'QQ交流群  {q}',  'en': 'QQ Group  {q}'},
    'lbl.about.log':      {'zh': '日志保存：24小时循环滚动', 'en': 'Logs kept: 24h rolling'},
    'btn.close':          {'zh': '关闭',             'en': 'Close'},
    'btn.apply':          {'zh': '应用',             'en': 'Apply'},
    'lbl.video.nocv':     {'zh': 'OpenCV未安装，无法使用视频功能', 'en': 'OpenCV not installed, video unavailable'},
    'err.title':          {'zh': '错误',             'en': 'Error'},
    'err.invalid.com':    {'zh': '请选择有效的端口和波特率', 'en': 'Please select a valid port & baud'},
    'err.audio.dev':      {'zh': '获取音频设备失败：{e}', 'en': 'Failed to list audio devices: {e}'},
    'log.path.title':     {'zh': '日志路径',         'en': 'Log Path'},
    'exit.title':         {'zh': '退出',             'en': 'Exit'},
    'exit.confirm':       {'zh': '确定要关闭服务器吗？', 'en': 'Are you sure to shut down the server?'},

    # ---- GUI/服务端: 主要日志 ----
    'log.init':           {'zh': '正在初始化 FT-710 控制程序...', 'en': 'Initializing FT-710 controller...'},
    'log.start.video':    {'zh': '正在启动视频流...', 'en': 'Starting video stream...'},
    'log.video.ok':       {'zh': '视频流已自动启动', 'en': 'Video stream auto-started'},
    'log.video.circuit':  {'zh': '视频设备连续多次无法打开，已暂停自动重试。请检查摄像头电源/USB连接（或是否被其它程序占用），然后在界面重新开启视频', 'en': 'Video device repeatedly failed to open; auto-retry paused. Check camera power/USB (or whether it is in use), then re-enable video'},
    'log.access':         {'zh': '访问地址：{u}',   'en': 'Access URL: {u}'},
    'log.serial':         {'zh': '串口：{p} @ {b}', 'en': 'Serial: {p} @ {b}'},
    'log.audio.link':     {'zh': '音频：{r}Hz → 16kHz DSP', 'en': 'Audio: {r}Hz → 16kHz DSP'},
    'log.serial.st':      {'zh': '串口状态：{s}',   'en': 'Serial status: {s}'},
    'log.audio.st':       {'zh': '音频状态：{s}',   'en': 'Audio status: {s}'},
    'log.video.st':       {'zh': '视频状态：{s}',   'en': 'Video status: {s}'},
    'log.logfile':        {'zh': '日志：{f}',       'en': 'Log file: {f}'},
    'log.server.fail':    {'zh': '服务器启动失败：{e}', 'en': 'Server start failed: {e}'},
    'st.ok.conn':         {'zh': '✅ 已连接',        'en': '✅ Connected'},
    'st.no.conn':         {'zh': '❌ 未连接',        'en': '❌ Not connected'},
    'st.ok.init':         {'zh': '✅ 已初始化',      'en': '✅ Initialized'},
    'st.no.init':         {'zh': '❌ 未初始化',      'en': '❌ Not initialized'},
    'st.ok.start':        {'zh': '✅ 已启动',        'en': '✅ Started'},
    'st.no.start':        {'zh': '❌ 未启动',        'en': '❌ Not started'},
    'log.com.reconn.ok':  {'zh': '✅ 串口重连成功',  'en': '✅ Serial reconnected'},
    'log.com.reconn.fail':{'zh': '❌ 串口重连失败',  'en': '❌ Serial reconnect failed'},
    'log.audio.ok':       {'zh': '✅ 音频流重启成功', 'en': '✅ Audio stream restarted'},
    'log.audio.fail':     {'zh': '❌ 音频流重启失败', 'en': '❌ Audio stream restart failed'},
    'log.save.audio':     {'zh': '保存音频设备配置：输入={a} 输出={b}', 'en': 'Audio devices saved: IN={a} OUT={b}'},
    'log.video.set':      {'zh': '视频：{s} 设备={d} {w}x{h}x{fps}fps q={q}', 'en': 'Video: {s} dev={d} {w}x{h}x{fps}fps q={q}'},
    'log.video.on':       {'zh': '启用',             'en': 'enabled'},
    'log.video.off':      {'zh': '禁用',             'en': 'disabled'},
    'log.reconn.ok':      {'zh': '✅ 串口连接成功',  'en': '✅ Serial connected'},
    'log.reconn.fail':    {'zh': '❌ 串口连接失败',  'en': '❌ Serial connect failed'},
    'log.lang.switch':    {'zh': '界面语言已切换为 中文', 'en': 'Language switched to English'},
    'log.port.refresh':   {'zh': '刷新串口列表：{l}', 'en': 'Refreshed port list: {l}'},
    'log.port.refresh.fail': {'zh': '串口列表刷新失败：{e}', 'en': 'Port list refresh failed: {e}'},
    'log.audio.refresh':  {'zh': '音频设备已刷新：输入={a} 输出={b}', 'en': 'Audio devices refreshed: IN={a} OUT={b}'},
    'log.audio.refresh.fail': {'zh': '音频设备刷新失败：{e}', 'en': 'Audio device refresh failed: {e}'},
    'log.video.refresh':  {'zh': '视频设备已刷新：{l}', 'en': 'Video devices refreshed: {l}'},
    'btn.refresh':        {'zh': '刷新设备',         'en': 'Refresh Devices'},
    'btn.refresh.short':  {'zh': '刷新',             'en': 'Refresh'},
    'lbl.refresh.hint':   {'zh': '新插入的声卡/摄像头无需重启程序，点此刷新即可显示', 'en': 'Newly plugged-in devices appear after refresh'},
    'log.port.ok':        {'zh': '✅ 串口 {p}@{b} 成功', 'en': '✅ Serial {p}@{b} ok'},
    'log.port.fail':      {'zh': '❌ 串口 {p}@{b} 失败', 'en': '❌ Serial {p}@{b} failed'},
    'log.com.init.fail':  {'zh': '串口初始化失败：{e}', 'en': 'Serial init failed: {e}'},
    'log.com.init.ok':    {'zh': '串口初始化成功：{p} @ {b}', 'en': 'Serial init ok: {p} @ {b}'},

    # ---- 服务端状态(经 socket/api 下发给 WebUI) ----
    'srv.audio.uninit':   {'zh': '未初始化',         'en': 'Not initialized'},
    'srv.audio.nodev':    {'zh': '无可用音频设备',   'en': 'No audio device available'},
    'srv.audio.device':   {'zh': '声卡：{n}',        'en': 'Audio: {n}'},
    'srv.audio.err':      {'zh': '设备获取失败：{e}', 'en': 'Device list failed: {e}'},
    'srv.audio.ready':    {'zh': '声卡就绪（{d}）→ 16kHz DSP', 'en': 'Sound card ready ({d}) → 16kHz DSP'},
    'srv.audio.failed':   {'zh': '音频流失败',       'en': 'Audio stream failed'},
    'srv.audio.initfail': {'zh': '音频流初始化失败', 'en': 'Audio stream init failed'},
    'srv.audio.errcmd':   {'zh': '串口错误：{e}',    'en': 'Serial error: {e}'},
    'noise.off':          {'zh': '关闭',             'en': 'Off'},
    'noise.level':        {'zh': '{n}级',            'en': 'Level {n}'},
    'noise.lvl1':         {'zh': '1级',              'en': '1'},
    'noise.lvl2':         {'zh': '2级',              'en': '2'},
    'noise.lvl3':         {'zh': '3级',              'en': '3'},
    'noise.lvl4':         {'zh': '4级',              'en': '4'},
    'noise.lvl5':         {'zh': '5级',              'en': '5'},
    'srv.channel.unsup':  {'zh': '频率不在支持波段内', 'en': 'Frequency not in a supported band'},
    'srv.channel.dup':    {'zh': '频率 {f} MHz 已存在', 'en': 'Frequency {f} MHz already exists'},
    'srv.channel.empty':  {'zh': '频率不能为空',     'en': 'Frequency cannot be empty'},
    'srv.channel.none':   {'zh': '信道不存在',       'en': 'Channel not found'},
    'srv.tx.no_restart':  {'zh': '发射中，无法重启音频', 'en': 'Transmitting, cannot restart audio'},

    # ---- WebUI: 静态 ----
    'ws.status-freq':     {'zh': '频率',             'en': 'Freq'},
    'ws.label-mode':      {'zh': '模式',             'en': 'Mode'},
    'ws.label-tuner':     {'zh': '天调',             'en': 'Tuner'},
    'ws.socket.disc':     {'zh': '未连接',           'en': 'Disconnected'},
    'ws.audio.init':      {'zh': '未激活',           'en': 'Not activated'},
    'ws.video.screen':    {'zh': '📷 电台屏幕',      'en': '📷 Radio Screen'},
    'ws.video.screen.title': {'zh': '显示电台屏幕',  'en': 'Show radio screen'},
    'ws.channel':         {'zh': '信道',             'en': 'Channel'},
    'ws.channel.none':    {'zh': '-- 当前信道 --',   'en': '-- None --'},
    'ws.chan.manage':     {'zh': '信道管理',         'en': 'Channel Manager'},
    'ws.chan.add':        {'zh': '添加信道',         'en': 'Add Channel'},
    'ws.chan.freq':       {'zh': '频率 (MHz):',      'en': 'Frequency (MHz):'},
    'ws.chan.ph':         {'zh': '如 14.270',        'en': 'e.g. 14.270'},
    'ws.chan.mode':       {'zh': '模式:',            'en': 'Mode:'},
    'ws.chan.cancel':     {'zh': '取消',             'en': 'Cancel'},
    'ws.chan.confirm':    {'zh': '添加',             'en': 'Add'},
    'ws.freq.ph':         {'zh': '频率',             'en': 'Frequency'},
    'ws.btn.confirm':     {'zh': '✅ 确认频率',      'en': '✅ Set Freq'},
    'ws.btn.ptt':         {'zh': 'PTT 按住说话',     'en': 'PTT Hold to Talk'},
    'ws.btn.act':         {'zh': '🎧 激活音频',      'en': '🎧 Activate Audio'},
    'ws.btn.restart':     {'zh': '🔄 重启音频',      'en': '🔄 Restart Audio'},
    'ws.gain.rx':         {'zh': '🔊 接收增益',       'en': '🔊 RX Gain'},
    'ws.gain.mic':        {'zh': '🎙️ 麦克风增益',   'en': '🎙️ Mic Gain'},
    'ws.noise':           {'zh': '🔇 DSP降噪',       'en': '🔇 DSP NR'},
    'ws.ai.title':        {'zh': '✨ 神经网络降噪',  'en': '✨ AI Denoise'},
    'ws.ai.on':           {'zh': '开启',             'en': 'On'},
    'ws.ai.off':          {'zh': '关闭',             'en': 'Off'},
    'ws.ai.hint':         {'zh': 'DFN3 · 开启后谱减法暂停', 'en': 'DFN3 · spectral NR paused'},
    'ws.ai.atten':        {'zh': '降噪强度',         'en': 'Strength'},
    'ws.ai.circuit':      {'zh': '已熔断，冷却后可重试', 'en': 'Circuit open, retry later'},
    'ws.tuner.title':     {'zh': '📡 天线调谐器',    'en': '📡 Antenna Tuner'},
    'ws.tuner.toggle':    {'zh': '开关',             'en': 'On / Off'},
    'ws.tuner.start':     {'zh': '🔄 调谐',          'en': '🔄 Tune'},
    'ws.power':           {'zh': '⚡ 发射功率',      'en': '⚡ TX Power'},
    'ws.title':           {'zh': 'FT-710 远程控制器', 'en': 'FT-710 Remote Controller'},

    # ---- WebUI: JS ----
    'js.connected':       {'zh': '已连接',           'en': 'Connected'},
    'js.soundcard.fail':  {'zh': '声卡失败',         'en': 'Sound card failed'},
    'js.disconnected':    {'zh': '已断开',           'en': 'Disconnected'},
    'js.connect.fail':    {'zh': '连接失败',         'en': 'Connection failed'},
    'js.tuner.tuning':    {'zh': '调谐中...',        'en': 'Tuning...'},
    'js.connecting':      {'zh': '连接中...',        'en': 'Connecting...'},
    'js.webrtc.fail':     {'zh': 'WebRTC失败',       'en': 'WebRTC failed'},
    'js.activating':      {'zh': '激活中...',        'en': 'Activating...'},
    'js.usb.ready':       {'zh': 'USB声卡就绪',      'en': 'USB audio ready'},
    'js.restarting':      {'zh': '重启中...',        'en': 'Restarting...'},
    'js.restart.done':    {'zh': '重启完成，请重新激活', 'en': 'Restart done, reactivate'},
    'js.activate.fail':   {'zh': '激活失败：{}',     'en': 'Activation failed: {}'},
    'js.restart.fail':    {'zh': '重启失败：{}',     'en': 'Restart failed: {}'},
    'js.tx':              {'zh': '🔴 发射中...',     'en': '🔴 Transmitting...'},
    'js.channel.empty':   {'zh': '暂无信道',         'en': 'No channels'},
    'js.video.connecting':{'zh': '正在连接...',      'en': 'Connecting...'},
    'js.video.streaming': {'zh': '视频传输中',       'en': 'Streaming video'},
    'js.video.failed':    {'zh': '视频播放失败',     'en': 'Video playback failed'},
    'js.video.wait':      {'zh': '等待视频轨道...',  'en': 'Waiting for video track...'},
    'js.video.stopped':   {'zh': '视频设备不可用，已停止。请检查摄像头电源/USB连接后重新开启', 'en': 'Video device unavailable; stopped. Check camera power/USB, then re-enable'},
    'js.alert.activate_first': {'zh': '请先激活音频！', 'en': 'Please activate audio first!'},
    'js.alert.activate_full':  {'zh': '音频激活失败：{}\n1. 检查麦克风权限\n2. 使用Chrome/Edge\n3. 关闭其他占用麦克风的程序', 'en': 'Audio activation failed: {}\n1. Check microphone permission\n2. Use Chrome/Edge\n3. Close other apps using the mic'},
    'js.alert.freq1':     {'zh': '请输入有效的频率！', 'en': 'Please enter a valid frequency!'},
    'js.alert.freq2':     {'zh': '请输入0.030-54之间的有效频率！', 'en': 'Enter a frequency between 0.030 - 54 MHz!'},
    'js.alert.no_audio':  {'zh': '音频未就绪，请先激活！', 'en': 'Audio not ready, activate first!'},
    'js.alert.freq3':     {'zh': '请输入有效的频率 (1.8-30 MHz)', 'en': 'Enter a valid frequency (1.8-30 MHz)'},
    'js.alert.update_fail':{'zh': '更新失败: {}',    'en': 'Update failed: {}'},
    'js.alert.delete_fail':{'zh': '删除失败: {}',    'en': 'Delete failed: {}'},
    'js.alert.enter_freq':{'zh': '请输入频率',       'en': 'Please enter frequency'},
    'js.alert.save_fail': {'zh': '保存失败: {}',     'en': 'Save failed: {}'},
    'js.alert.add_fail':  {'zh': '添加信道失败',     'en': 'Add channel failed'},
    'js.confirm.del':     {'zh': '确定要删除这个信道吗？', 'en': 'Delete this channel?'},
    'js.prompt.freq':     {'zh': '请输入新的频率 (MHz):', 'en': 'Enter new frequency (MHz):'},
}

def _t(key, *args, **kw):
    text = I18N.get(key, {}).get(LANG) or I18N.get(key, {}).get('zh', key)
    if args or kw:
        try:
            return text.format(*args, **kw)
        except Exception:
            return text
    return text

def _noise_text(level):
    if level == 0:
        return _t('noise.off')
    return _t('noise.level', n=level)

# ===================== 从配置读取参数 =====================
COM_PORT = config['SERIAL']['COM_PORT']
BAUD_RATE = int(config['SERIAL']['BAUD_RATE'])
SAMPLING_RATE = int(config['AUDIO']['SAMPLING_RATE'])
CHANNELS = int(config['AUDIO']['CHANNELS'])
AUDIO_BLOCK_SIZE = int(config['AUDIO']['BLOCK_SIZE'])
SERVER_PORT = int(config['NETWORK']['PORT'])
PTT_MODE = config['PTT'].get('MODE', 'momentary').lower()

# ===================== 音频参数配置 =====================
GAIN_LEVELS = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0, 3.25, 3.5, 3.75, 4.0, 4.25, 4.5, 4.75, 5.0]
current_gain_level = 2

MIC_GAIN_LEVELS = [1.0, 1.25, 1.5, 1.75, 2.0]
current_mic_gain_level = 4

NOISE_LEVELS = [
    {"enabled": False, "aggressiveness": 0.0, "gate": 0.000},
    {"enabled": True,  "aggressiveness": 0.2, "gate": 0.008},
    {"enabled": True,  "aggressiveness": 0.3, "gate": 0.010},
    {"enabled": True,  "aggressiveness": 0.5, "gate": 0.012},
    {"enabled": True,  "aggressiveness": 0.7, "gate": 0.014},
    {"enabled": True,  "aggressiveness": 0.9, "gate": 0.016},
]
current_noise_level = 0
nb_enabled = False

_dsp_state_lock = Lock()
_audio_restart_lock = Lock()
_audio_restarting = Event()
_audio_restarting.clear()

FREQ_STEP = 0.001
freq_step = FREQ_STEP
freq_lock = Lock()
noise_lock = Lock()
gain_lock = Lock()

from scipy.signal import butter, sosfilt

# WebRTC（可选）
try:
    from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, RTCConfiguration, RTCIceServer
    from aiortc.contrib.media import MediaBlackhole
    import av
    import asyncio
    import fractions
    WEBRTC_AVAILABLE = True
    log("WebRTC: aiortc loaded")
except ImportError as e:
    WEBRTC_AVAILABLE = False
    print(f"WebRTC不可用: {e}")

# WebRTC 连接管理
_webrtc_pcs = set()

# WebRTC 持久化事件循环（解决协程在 Flask 请求中被关闭的问题）
_webrtc_loop = None

def _start_webrtc_loop():
    global _webrtc_loop
    _webrtc_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_webrtc_loop)
    _webrtc_loop.run_forever()

if WEBRTC_AVAILABLE:
    _webrtc_thread = _threading.Thread(target=_start_webrtc_loop, daemon=True, name="WebRTCEventLoop")
    _webrtc_thread.start()

# WebRTC PC 空闲清理：存活信号=实际数据流动（音视频轨道被前端持续拉取/麦克风帧到达），
# 而非 ICE 活动（建连后即停止）。刷新页面的旧连接会因数据停止而被回收。
_WEBRTC_PC_IDLE_TIMEOUT = 180.0
_webrtc_pc_ts = {}
_webrtc_pc_ts_lock = _threading.Lock()

def _webrtc_touch_pc(pc):
    if pc is None:
        return
    with _webrtc_pc_ts_lock:
        _webrtc_pc_ts[id(pc)] = time.monotonic()

def _webrtc_pc_sweeper():
    while True:
        time.sleep(30)
        now = time.monotonic()
        for pc in list(_webrtc_pcs):
            with _webrtc_pc_ts_lock:
                last = _webrtc_pc_ts.get(id(pc), now)
            try:
                state = pc.iceConnectionState
            except Exception:
                state = None
            if (now - last) >= _WEBRTC_PC_IDLE_TIMEOUT and state in ("connected", "completed"):
                log("WebRTC PC空闲超时（无数据流动），清理连接")
                try:
                    asyncio.run_coroutine_threadsafe(pc.close(), _webrtc_loop).result(timeout=3)
                    log("WebRTC PC已关闭")
                except Exception as e:
                    log(f"WebRTC PC关闭异常：{e}")
                _webrtc_pcs.discard(pc)
                with _webrtc_pc_ts_lock:
                    _webrtc_pc_ts.pop(id(pc), None)

if WEBRTC_AVAILABLE:
    _threading.Thread(target=_webrtc_pc_sweeper, daemon=True, name="WebRTCPcSweeper").start()

# 半双工控制
tx_active = Event()
tx_active.clear()
tx_start_time = 0.0

# PTT 持有者：持有发射的 SocketIO 客户端 sid，断线时自动回收发射
_ptt_owner_sid = None
# 麦克风丢帧计数（无可追溯的静默丢弃）
_mic_skip_count = 0
# 客户端播放链路诊断：kind -> 上次记录时间(monotonic)，60s 节流 + TTL/上限防内存 DoS
_client_diag_last = {}
_CLIENT_DIAG_TTL = 3600.0
_CLIENT_DIAG_MAX = 128

_running = Event()

_VAD_THRESHOLD_DB = -45.0
_VAD_HANG_FRAMES = 10
_dsp_error_count = 0
_MAX_ERRORS = 10

# ===================== 滤波器预计算 =====================
def make_sos(lowcut, highcut, fs, order=4):
    nyq = fs / 2
    lo = max(0.001, min(lowcut  / nyq, 0.999))
    hi = max(0.001, min(highcut / nyq, 0.998))
    if hi <= lo:
        hi = min(lo + 0.001, 0.999)
    return butter(order, [lo, hi], btype='band', output='sos')

# 接收路径：250~4000Hz 带通
_rx_sos = make_sos(250, 4000, SAMPLING_RATE)

from scipy.signal import sosfilt_zi as _sosfilt_zi
_rx_zi = _sosfilt_zi(_rx_sos) * 0.0

# ===================== DeepFilterNet3 AI 降噪 =====================
# 与 1-5 级谱减法互斥：AI 开启时 DSP 走 DFN3（48k），否则保持原谱减法（16k）
_DFN_MODEL_DIR = os.path.join(_RSC_DIR, 'dfn3_model')
try:
    import deepfilter_stream as _dfs
    import deepfilter_stream.resample as _dfs_rs
    _DFN_AVAILABLE = True
except Exception:
    _dfs = None
    _dfs_rs = None
    _DFN_AVAILABLE = False

def _patch_soxr_quality(default="HQ"):
    """16k↔48k soxr 重采样质量 VHQ→HQ，省约0.1~0.3ms/块（幂等）。"""
    if not _DFN_AVAILABLE:
        return False
    if getattr(_dfs_rs.StreamResampler.__init__, "_ft710_hq_patched", False):
        return True
    try:
        _orig_init = _dfs_rs.StreamResampler.__init__
        def _init_hq(self, in_rate, out_rate, quality=default):
            _orig_init(self, in_rate, out_rate, quality=quality)
        _init_hq._ft710_hq_patched = True
        _dfs_rs.StreamResampler.__init__ = _init_hq
        return True
    except Exception:
        return False

def _patch_dfn_hot_path():
    """DFN3 每块热路径减拷贝/减分配（monkeypatch，幂等，仅改数据搬运不改算法）。"""
    if not _DFN_AVAILABLE:
        return False
    try:
        from deepfilter_stream import denoiser as _dfd
        if getattr(_dfd.Denoiser, "_ft710_hot_patched", False):
            return True

        def _process_frame(self, frame):
            frame = frame.reshape(-1)
            feeds = {"input_frame": frame}
            feeds.update(self._states)
            res = self._model.session.run(self._model.output_names, feeds)
            enhanced = res[0].reshape(-1)
            for name, val in zip(self._model.input_names[1:], res[1:]):
                self._states[name] = val
            if self.atten_lim_db is not None:
                self._dry_delay.write(frame)
                dry = self._dry_delay.pop(self.frame_size)
                dry *= self._lim
                enhanced *= (1.0 - self._lim)
                enhanced += dry
            return enhanced

        def _process(self, samples, sr):
            self._guard()
            try:
                x = np.ascontiguousarray(samples, dtype=np.float32)
                if x.ndim == 2:
                    x = x.mean(axis=1) if x.shape[1] <= x.shape[0] else x.mean(axis=0)
                x = x.reshape(-1)
                self._ensure_resamplers(sr)
                if self._in_rs is not None:
                    x = self._in_rs.process(x)
                self._in_buf.write(x)
                f = self.frame_size
                out_frames = []
                while self._in_buf.available >= f:
                    out_frames.append(self.process_frame(self._in_buf.pop(f)))
                if not out_frames:
                    y = np.zeros(0, dtype=np.float32)
                elif len(out_frames) == 1:
                    y = out_frames[0]
                else:
                    y = np.concatenate(out_frames)
                if self._out_rs is not None:
                    y = self._out_rs.process(y)
                return y
            finally:
                self._lock.release()

        _dfd.Denoiser.process_frame = _process_frame
        _dfd.Denoiser.process = _process
        _dfd.Denoiser._ft710_hot_patched = True
        return True
    except Exception:
        return False

_patched_soxr = _patch_soxr_quality()
_patched_dfn_hot = _patch_dfn_hot_path()

_ai_denoise_enabled = False   # 神经网络降噪开关（set_ai_denoise 控制）
_ai_model = None              # 共享 DeepFilterModel（懒加载）
_ai_stream = None             # Denoiser 流对象（仅 _dsp_worker 线程调用 process）
_ai_load_error = None         # 最近一次加载失败原因
_ai_tx_reset_done = True      # TX静默期内是否已重置流状态
_ai_atten_lim_db = float(config['AUDIO'].get('AI_ATTEN_LIM_DB', '12'))  # 衰减上限dB（6-30）
_ai_params_pending = False    # 参数热应用标志
_ai_intra_threads = int(config['AUDIO'].get('AI_INTRA_THREADS', '1'))   # ORT intra-op 线程数
_ai_load_lock = _threading.Lock()
_ai_loading = False
_ai_enable_want = False
_ai_fail_count = 0
_ai_circuit_open = False
_ai_circuit_until = 0.0
_AI_FAIL_LIMIT = 3
_AI_CIRCUIT_COOLDOWN = 60.0


class _TunedDFNModel(_dfs.DeepFilterModel if _DFN_AVAILABLE else object):
    """调优版 DeepFilterModel：限制线程数 + 自旋，降低 CPU 占用（仅覆盖 __init__）。"""

    def __init__(self, intra_op_num_threads: int = 2) -> None:
        if not _DFN_AVAILABLE:
            raise RuntimeError("deepfilter-stream 未安装")
        from deepfilter_stream import assets as _df_assets
        from deepfilter_stream import _meta as _df_meta
        import onnxruntime as ort
        paths = _df_assets.ensure_assets()

        so = ort.SessionOptions()
        so.intra_op_num_threads = int(intra_op_num_threads)
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.add_session_config_entry("session.intra_op.allow_spinning", "1")
        self.session = ort.InferenceSession(
            str(paths["onnx"]), sess_options=so, providers=["CPUExecutionProvider"]
        )
        self.input_names = [i.name for i in self.session.get_inputs()]
        self.output_names = [o.name for o in self.session.get_outputs()]
        self.frame_size = int(self.session.get_inputs()[0].shape[0])
        self.sample_rate = _df_meta.SAMPLE_RATE
        with np.load(paths["initial_states"]) as z:
            self._init = {name: z[name].astype(np.float32) for name in self.input_names[1:]}


def _ai_warmup(stream, n=30):
    """新流预热（best-effort）：ORT 内存 arena/算子选择前几次 run 才懒初始化。"""
    try:
        zeros = np.zeros(512, dtype=np.float32)
        for _ in range(int(n)):
            stream.process(zeros, SAMPLING_RATE)
        log(f"DFN3 已预热{n}帧（推理+重采样冷启动完成）")
        return True
    except Exception as e:
        log(f"DFN3 预热跳过（不影响使用）：{e!r}")
        return False
    finally:
        try:
            stream.reset()
        except Exception:
            pass


def _ai_ensure_loaded():
    """懒加载 DFN3 模型与流对象。成功返回 True，失败记录原因并返回 False。"""
    global _ai_model, _ai_stream, _ai_load_error
    if not _DFN_AVAILABLE:
        _ai_load_error = "deepfilter-stream 未安装"
        return False
    try:
        if _ai_model is None:
            if os.path.isdir(_DFN_MODEL_DIR):
                os.environ['DEEPFILTER_STREAM_MODEL_DIR'] = _DFN_MODEL_DIR
            _ai_model = _TunedDFNModel(intra_op_num_threads=_ai_intra_threads)
            log(f"DFN3 模型已加载：{_DFN_MODEL_DIR if os.path.isdir(_DFN_MODEL_DIR) else '缓存目录'}"
                f"（ORT线程={_ai_intra_threads} 自旋=开）")
        if _ai_stream is None:
            _st = _ai_model.new_stream(atten_lim_db=_ai_atten_lim_db)
            _ai_warmup(_st)
            _ai_stream = _st
        _ai_load_error = None
        return True
    except Exception as e:
        _ai_load_error = str(e)
        return False


def _ai_load_worker():
    """后台加载 DFN3（ONNX 会话创建可达数秒，不能占用 SocketIO 事件线程）。"""
    global _ai_denoise_enabled, _ai_loading, _ai_fail_count, _ai_circuit_open
    global _ai_enable_want, _ai_circuit_until, _ai_load_error
    try:
        ok = _ai_ensure_loaded()
    except Exception as e:
        _ai_load_error = str(e)
        ok = False
    with _ai_load_lock:
        _ai_loading = False
        if not ok:
            _ai_fail_count += 1
            if _ai_fail_count >= _AI_FAIL_LIMIT:
                _ai_circuit_open = True
                _ai_circuit_until = time.monotonic() + _AI_CIRCUIT_COOLDOWN
            _ai_denoise_enabled = False
            _ai_enable_want = False
        elif _ai_enable_want:
            _ai_fail_count = 0
            _ai_denoise_enabled = True
        else:
            _ai_denoise_enabled = False
        radio_state["ai_denoise"] = _ai_denoise_enabled
        state_err = None if ok else _ai_load_error
        tripped = _ai_circuit_open and not ok
    if ok and _ai_denoise_enabled:
        log("神经网络降噪已开启（DFN3 后台加载完成，谱减法暂停）")
        socketio.emit('ai_denoise_updated', {'enabled': True}, namespace='/')
    elif not ok:
        msg = f"连续失败{_ai_fail_count}次已熔断，稍后可重试" if tripped else f"加载失败（第{_ai_fail_count}次）"
        log(f"神经网络降噪{msg}：{state_err}")
        socketio.emit('ai_denoise_updated', {'enabled': False, 'error': state_err,
                                             'circuit_open': tripped}, namespace='/')
    else:
        log("神经网络降噪加载中被关闭，保持关闭状态")
        socketio.emit('ai_denoise_updated', {'enabled': False}, namespace='/')

# ===================== 降噪状态 =====================
# 保护 _noise_profile：DSP 线程逐帧读写，音频重启/换模式线程会整体清零，需互斥
_noise_profile_lock = _threading.Lock()
_noise_profile = {
    "spectrum": None,
    "history": [],
    "history_len": 15,
    "smoothed_spectrum": None,
    "init_frames_needed": 8,
    "init_buffer": []
}

# ===================== 下采样器：从48kHz降到16kHz =====================
class Downsampler:
    def __init__(self, input_rate, output_rate, block_size):
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.block_size = block_size
        self.ratio = input_rate // output_rate  # 3
        # 抗混叠低通：截止取输出奈奎斯特的85%，抑制抽取后折返的高频噪声
        _nyq = output_rate / 2.0
        _cutoff = min(0.85 * _nyq, input_rate / 2.0 - 1.0)
        self._aa_sos = butter(4, _cutoff, fs=input_rate, output='sos')
        self._aa_zi = _sosfilt_zi(self._aa_sos) * 0.0

    def process(self, audio):
        # 先低通抗混叠（保持跨块状态，消除帧边界不连续），再整数抽取
        audio, self._aa_zi = sosfilt(self._aa_sos, audio, zi=self._aa_zi)
        return audio[::self.ratio].astype(np.float32)

# 初始化下采样器
_downsampler = None
_TARGET_RATE = 16000  # 下采样目标

# ===================== 三级流水线 =====================
_stream_lock = _threading.Lock()
_rx_start_seq = 0

_RING_SIZE = max(4, int(2.0 * SAMPLING_RATE / AUDIO_BLOCK_SIZE))
log(f"运行时调优: GIL切换间隔={sys.getswitchinterval()*1000:.2f}ms RING={_RING_SIZE}帧({_RING_SIZE*AUDIO_BLOCK_SIZE/SAMPLING_RATE:.2f}s)")
_ring_buf = np.zeros((_RING_SIZE, AUDIO_BLOCK_SIZE), dtype='float32')
_ring_write = 0
_ring_read  = 0
_ring_lock  = _threading.Lock()
_ring_event = _threading.Event()
_ring_overflow_count = 0
_ring_last_overflow_log = 0.0

_webrtc_audio_accum = np.zeros((0,), dtype='float32')
_tx_squelch_until  = 0.0

# WebRTC 接收音频广播：每个客户端Track拥有独立小队列，互不抢占
_webrtc_listener_lock = _threading.Lock()
_webrtc_listener_qs = {}  # id(track) -> queue.Queue(maxsize)
# 队列越小延迟越低；满时丢最旧帧。4帧=40ms最坏额外延迟
_WEBRTC_LISTENER_MAX = 4

def _webrtc_audio_broadcast(payload):
    """向所有音频监听者分发一帧数据；慢客户端只丢自己的旧帧"""
    with _webrtc_listener_lock:
        qs = list(_webrtc_listener_qs.values())
    if not qs:
        return
    for q in qs:
        try:
            q.put_nowait(payload)
        except _queue.Full:
            try: q.get_nowait()
            except _queue.Empty: pass
            try: q.put_nowait(payload)
            except _queue.Full: pass

def _webrtc_listener_max_qsize():
    with _webrtc_listener_lock:
        qs = list(_webrtc_listener_qs.values())
    return max([q.qsize() for q in qs]) if qs else 0

_tx_audio_queue = _queue.Queue(maxsize=8)

_tx_audio_frame_count = 0

def _tx_audio_worker():
    global _tx_audio_frame_count
    _boost_thread_priority()
    while not _running.is_set():
        try:
            audio_frame = _tx_audio_queue.get(timeout=0.5)
            if not tx_active.is_set():
                continue
            if audio_output_stream is None:
                continue
            _tx_audio_frame_count += 1
            if _tx_audio_frame_count > 0 and _tx_audio_frame_count % 2000 == 0:
                log(f"TX声卡: 已写入{_tx_audio_frame_count}帧, 队列={_tx_audio_queue.qsize()}")
            try:
                if audio_output_stream is not None:
                    info = audio_output_stream.dtype
                    if 'int16' in str(info):
                        audio_frame = (np.clip(audio_frame, -1.0, 1.0) * 32767).astype('int16')
                audio_output_stream.write(audio_frame)
            except Exception as e:
                log(f"TX写声卡失败：{e}")
                while not _tx_audio_queue.empty():
                    try: _tx_audio_queue.get_nowait()
                    except: break
        except _queue.Empty:
            continue
        except Exception:
            pass

_tx_audio_thread = _threading.Thread(target=_tx_audio_worker, daemon=True, name='TxAudio')
_tx_audio_thread.start()

def _drain_send_queue():
    with _webrtc_listener_lock:
        qs = list(_webrtc_listener_qs.values())
    for q in qs:
        while not q.empty():
            try: q.get_nowait()
            except _queue.Empty: break

def _reset_ring_buffer():
    global _ring_read
    with _ring_lock:
        _ring_read = _ring_write

_rms_work = None
def _frame_rms(audio):
    """返回帧能量 RMS（float）。复用 _rms_work 缓冲按需重分配，无每帧临时数组。"""
    global _rms_work
    if (_rms_work is None or _rms_work.shape != audio.shape
            or _rms_work.dtype != audio.dtype):
        _rms_work = np.empty(audio.shape, dtype=audio.dtype)
    np.multiply(audio, audio, out=_rms_work)
    return float(np.sqrt(np.mean(_rms_work)))

def _check_vad(audio, vad_hang):
    """轻量级能级检测 VAD。返回 (新hang, 是否语音, rms)——rms 由调用方复用。"""
    rms = _frame_rms(audio)
    db = 20 * np.log10(rms + 1e-10)
    if db > _VAD_THRESHOLD_DB:
        return _VAD_HANG_FRAMES, True, rms
    elif vad_hang > 0:
        return vad_hang - 1, True, rms
    else:
        return 0, False, rms


# ===================== F2: DSP 分段耗时观测 =====================
# 在真机上量出每帧：sosfilt / 门控 / 降噪(DFN3) / 编码 各占多少，
# 以及「胶水+GIL等待」= 总耗时 - 四段之和。每 _PERF_EVERY 个完整帧聚合打印一行，
# 绝不逐帧刷日志——观测太吵会反过来加重 GIL 争抢、污染测量结果。
_pc = time.perf_counter
_PERF_EVERY = 200
_perf = {
    "n": 0, "n_skip": 0, "pend_max": 0, "pend_sum": 0.0,
    "sos": 0.0, "vad": 0.0, "ai": 0.0, "enc": 0.0, "total": 0.0,
}

def _perf_skip():
    """被 VAD/RMS 门控跳过的帧：只计数，不计入耗时均值（否则拉低均值、掩盖真实开销）"""
    _perf["n_skip"] += 1
    if _perf["n"] == 0 and _perf["n_skip"] >= _PERF_EVERY:
        log(f"DSP分段：连续{_PERF_EVERY}帧全被门控跳过，无完整帧可统计")
        _perf["n_skip"] = 0

def _perf_add(t_sos, t_vad, t_ai, t_enc, t_total, pending):
    """累积一个完整帧的分段耗时；满 _PERF_EVERY 帧打印一行汇总"""
    _perf["n"] += 1
    _perf["sos"] += t_sos
    _perf["vad"] += t_vad
    _perf["ai"] += t_ai
    _perf["enc"] += t_enc
    _perf["total"] += t_total
    _perf["pend_sum"] += pending
    if pending > _perf["pend_max"]:
        _perf["pend_max"] = pending
    if _perf["n"] < _PERF_EVERY:
        return
    n = _perf["n"]
    def ms(k):
        return _perf[k] / n * 1000.0
    wait = ms("total") - ms("sos") - ms("vad") - ms("ai") - ms("enc")
    if wait < 0.0:
        wait = 0.0
    log(f"DSP分段({n}帧): "
        f"总{ms('total'):.1f}ms | "
        f"sos{ms('sos'):.2f} 门控{ms('vad'):.2f} 降噪{ms('ai'):.1f} 编码{ms('enc'):.2f} | "
        f"胶水+GIL等待{wait:.1f} | "
        f"ring积压 max{_perf['pend_max']}/avg{_perf['pend_sum'] / n:.1f}"
        f"(满{_RING_SIZE - 1}) | 门控跳过{_perf['n_skip']}")
    _perf["n"] = _perf["n_skip"] = _perf["pend_max"] = 0
    _perf["sos"] = _perf["vad"] = _perf["ai"] = _perf["enc"] = 0.0
    _perf["total"] = _perf["pend_sum"] = 0.0

def _dsp_worker():
    global _ring_read, _downsampler, _rx_sos, _rx_zi, SAMPLING_RATE, _webrtc_audio_accum
    global _dsp_error_count, _ai_tx_reset_done, _ai_params_pending, _VAD_THRESHOLD_DB

    _boost_thread_priority()
    # RX 广播 VAD 门限可配置：默认 -55dB。设备输入电平偏低/弱信号时调低（如 -60），
    # 否则低于门限的电台音频会被当静音丢弃，表现为接收无声/偶尔冒一声。
    try:
        _VAD_THRESHOLD_DB = float(config['AUDIO'].get('RX_VAD_THRESHOLD_DB', '-55'))
        log(f"RX VAD 门限：{_VAD_THRESHOLD_DB:.1f} dB")
    except Exception:
        pass
    # 两个 RMS 门控跟随 VAD 门限（分别低 3dB / 6dB），保证弱信号不被二次卡掉
    try:
        _vad_rms = 10 ** (_VAD_THRESHOLD_DB / 20.0)
    except Exception:
        _vad_rms = 10 ** (-55.0 / 20.0)
    _rx_gate1 = _vad_rms * (10 ** (-3.0 / 20.0))
    _rx_gate2 = _vad_rms * (10 ** (-6.0 / 20.0))
    vad_hang = 0
    _dsp_skip_tx = 0
    _dsp_skip_vad = 0
    _dsp_skip_rms = 0
    _dsp_processed = 0
    _dsp_last_log = 0

    while not _running.is_set():
        _ring_event.wait(timeout=0.1)
        _ring_event.clear()
        while not _running.is_set():
            with _ring_lock:
                pending = (_ring_write - _ring_read) % _RING_SIZE
                if pending == 0:
                    break
                audio = _ring_buf[_ring_read % _RING_SIZE].copy()
                _ring_read = (_ring_read + 1) % _RING_SIZE

            if tx_active.is_set() or time.monotonic() < _tx_squelch_until:
                _dsp_skip_tx += 1
                _perf_skip()
                _drain_send_queue()
                # TX期间输入不连续：重置 DFN3 流状态，避免 RX 恢复时输出残留
                if _ai_stream is not None and not _ai_tx_reset_done:
                    try:
                        _ai_stream.reset()
                    except Exception:
                        pass
                    _ai_tx_reset_done = True
                continue

            try:
                t_blk0 = _pc()
                with noise_lock:
                    noise_level = current_noise_level
                with gain_lock:
                    gain_value  = GAIN_LEVELS[current_gain_level]

                # ① AI 神经网络降噪（DFN3，48k 输入）——与谱减法互斥
                t0 = _pc()
                ai_active = _ai_denoise_enabled and _ai_stream is not None
                if ai_active:
                    if _ai_params_pending:
                        _ai_params_pending = False
                        try:
                            _ai_stream.atten_lim_db = _ai_atten_lim_db
                            _ai_stream._lim = float(10.0 ** (-_ai_atten_lim_db / 20.0))
                        except Exception:
                            pass
                    try:
                        out48 = _ai_stream.process(audio, SAMPLING_RATE)
                    except Exception as _e:
                        log(f"DFN3 处理异常：{_e!r}")
                        out48 = audio
                    _ai_tx_reset_done = False
                    if out48 is None or len(out48) == 0:
                        t_ai = _pc() - t0
                        _perf_skip()
                        continue
                    audio = np.ascontiguousarray(out48, dtype=np.float32)
                t_ai = _pc() - t0

                # ② 下采样：48kHz -> 16kHz
                if _downsampler is not None:
                    audio = _downsampler.process(audio)
                    if audio is None:
                        _perf_skip()
                        continue

                # ③ 带通滤波（持续状态，消除帧边界不连续）
                t0 = _pc()
                with _dsp_state_lock:
                    audio, _rx_zi = sosfilt(_rx_sos, audio, zi=_rx_zi)
                audio = audio.astype('float32')
                t_sos = _pc() - t0

                # ④ VAD 检测：静音期直接丢弃（rms 复用，避免重复计算）
                t0 = _pc()
                vad_hang, is_speech, frame_rms = _check_vad(audio, vad_hang)
                if not is_speech:
                    _dsp_skip_vad += 1
                    _perf_skip()
                    continue

                # 增益前 RMS 门控
                if frame_rms < _rx_gate1:
                    _dsp_skip_rms += 1
                    _perf_skip()
                    continue
                t_vad = _pc() - t0

                # ⑤ 降噪：AI 已在上游处理，此处仅跑谱减法（互斥）
                if not ai_active:
                    t0 = _pc()
                    if nb_enabled:
                        audio = apply_noise_blanker(audio)

                    if noise_level > 0:
                        audio = apply_noise_reduction(audio, noise_level)
                    t_ai += _pc() - t0

                if gain_value != 1.0:
                    audio = audio * gain_value

                audio = np.clip(audio, -0.95, 0.95).astype('float32')

                # ⑥ 降噪后二次 RMS 检查（复用工作缓冲原地计算，不新建临时数组）
                if _frame_rms(audio) < _rx_gate2:
                    _perf_skip()
                    continue

                # ⑦ WebRTC 音频路径：累积到160-sample帧（10ms @ 16kHz）
                t0 = _pc()
                _webrtc_audio_accum = np.concatenate([_webrtc_audio_accum, audio])
                while len(_webrtc_audio_accum) >= 160:
                    chunk_f32 = _webrtc_audio_accum[:160]
                    _webrtc_audio_accum = _webrtc_audio_accum[160:]
                    chunk_payload = b'\x01' + chunk_f32.tobytes()
                    _webrtc_audio_broadcast(chunk_payload)
                t_enc = _pc() - t0

                _dsp_processed += 1
                _dsp_error_count = 0  # 成功处理即清零：瞬时错误不再累积到致命阈值
                _perf_add(t_sos, t_vad, t_ai, t_enc, _pc() - t_blk0, pending)
            except Exception as e:
                _dsp_error_count += 1
                if _dsp_error_count % 10 == 1:
                    log(f"DSP线程异常（累计{_dsp_error_count}）: {e!r}")
                if _dsp_error_count >= _MAX_ERRORS:
                    log(f"DSP线程异常次数过多（{_dsp_error_count}），即将退出重启")
                    return
        now = time.monotonic()
        if now - _dsp_last_log > 30:
            _dsp_last_log = now
            total = _dsp_processed + _dsp_skip_tx + _dsp_skip_vad + _dsp_skip_rms
            if total > 0:
                log(f"DSP诊断: 处理={_dsp_processed} 跳过TX={_dsp_skip_tx} VAD={_dsp_skip_vad} RMS门控={_dsp_skip_rms} 队列={_webrtc_listener_max_qsize()}")

_dsp_thread  = _threading.Thread(target=_dsp_worker,  daemon=True, name='AudioDSP')
_dsp_thread.start()

def _monitor_threads():
    """线程健康监控：DSP / TX写声卡 异常退出后自动重启，确保音频链路永不静默死亡"""
    global _dsp_thread, _dsp_error_count, _tx_audio_thread
    while not _running.is_set():
        time.sleep(3)
        try:
            if not _dsp_thread.is_alive():
                _dsp_error_count = 0
                log("DSP线程已退出，自动重启")
                _dsp_thread = _threading.Thread(target=_dsp_worker, daemon=True, name='AudioDSP')
                _dsp_thread.start()
            if not _tx_audio_thread.is_alive() and not _running.is_set():
                log("TX写声卡线程已退出，自动重启")
                _tx_audio_thread = _threading.Thread(target=_tx_audio_worker, daemon=True, name='TxAudio')
                _tx_audio_thread.start()
        except Exception:
            pass

_threading.Thread(target=_monitor_threads, daemon=True, name='ThreadMonitor').start()

# ===================== Flask初始化 =====================
app = Flask(__name__)
app.config['SECRET_KEY'] = 'ft710-audio'

def _enumerate_network_ips():
    """枚举本机所有非回环 IPv4 地址"""
    try:
        try:
            import netifaces
            ips = set()
            for iface in netifaces.interfaces():
                try:
                    for info in netifaces.ifaddresses(iface).get(netifaces.AF_INET, []):
                        addr = info.get('addr', '')
                        if addr and not addr.startswith(('127.', '169.254.')):
                            ips.add(addr)
                except Exception:
                    continue
            if ips:
                return list(ips)
        except ImportError:
            pass
        try:
            import psutil
            ips = set()
            for iface, addrs in psutil.net_if_addrs().items():
                try:
                    for addr in addrs:
                        if addr.family == socket.AF_INET:
                            ip = addr.address
                            if ip and not ip.startswith(('127.', '169.254.')):
                                ips.add(ip)
                except Exception:
                    continue
            if ips:
                return list(ips)
        except ImportError:
            pass
    except Exception:
        pass
    return [socket.gethostbyname(socket.gethostname())]

_IPS_TTL = 30.0
_ips_cache = None
_ips_cache_ts = 0.0

def get_all_network_ips(force=False):
    """枚举本机所有非回环 IPv4 地址（30s TTL 缓存，避免每请求遍历网卡）"""
    global _ips_cache, _ips_cache_ts
    now = time.time()
    if not force and _ips_cache is not None and (now - _ips_cache_ts) < _IPS_TTL:
        return _ips_cache
    ips = _enumerate_network_ips()
    _ips_cache = ips
    _ips_cache_ts = now
    return ips

_all_ips = get_all_network_ips(force=True)
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='threading',
    async_handlers=False,
    ping_timeout=20,
    ping_interval=8,
    logger=False,
    engineio_logger=False,
    max_http_buffer_size=1024*1024,
    allow_upgrades=True
)

# 全局变量
ser = None
lock = Lock()
radio_state = {
    "frequency": 14.270,
    "mode": "USB",
    "trx": "RX",
    "tuner": "OFF",
    "nb": False,
    "audio_status": _t('srv.audio.uninit'),
    "freq_step": "1kHz",
    "gain_level": current_gain_level,
    "gain_value": GAIN_LEVELS[current_gain_level],
    "noise_level": current_noise_level,
    "noise_status": _t('noise.off'),
    "ai_denoise": _ai_denoise_enabled,
    "ai_atten_lim_db": _ai_atten_lim_db,
    "power": 25,
    "com_port": COM_PORT,
    "baud_rate": BAUD_RATE,
    "ptt_mode": PTT_MODE,
    "meter": {"pwr": 0, "ref": 0, "swr": 1.0, "eff": 0, "connected": False}
}

audio_output_stream = None
audio_input_stream = None
audio_devices = {}

# ===================== 视频捕获（FFmpeg）=====================
_current_frame = None
_frame_lock = _threading.Lock()
_video_fps = 10
_video_frame_seq = 0  # 帧序列号，视频捕获线程递增
_ffmpeg_process = None
video_running = False
_video_stream_active = True  # 前端是否在显示视频，false时track不发帧省带宽
video_thread = None
_ffmpeg_spawned_pids = set()  # 本程序启动的ffmpeg PID，用于清理残留进程释放设备
_ffmpeg_pids_lock = _threading.Lock()

# 视频假死看门狗：设备存活但长时间无新帧时强制重启
_VIDEO_FRAME_TIMEOUT = 8.0
# 视频启动/停止互斥：避免竞态导致多个ffmpeg同时抢占设备
_video_op_lock = _threading.Lock()

def find_ffmpeg():
    """查找ffmpeg路径"""
    # 程序目录
    exe_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
    internal_dir = os.path.join(exe_dir, '_internal')
    for base in (internal_dir, exe_dir):
        ffmpeg_path = os.path.join(base, 'ffmpeg', 'ffmpeg.exe')
        if os.path.exists(ffmpeg_path):
            return ffmpeg_path
    # 系统PATH
    for p in os.environ.get('PATH', '').split(os.pathsep):
        ffmpeg_path = os.path.join(p, 'ffmpeg.exe')
        if os.path.exists(ffmpeg_path):
            return ffmpeg_path
    return None

def init_video_capture():
    """初始化视频捕获（只检查ffmpeg存在）"""
    global video_running
    if video_running:
        return True
    
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        log("未找到ffmpeg.exe")
        return False
    
    log("视频初始化完成")
    return True

def _video_watchdog(process, holder):
    """独立看门狗：ffmpeg存活但长时间无新帧 → 强制终止（触发worker自动重启）"""
    try:
        # 启动宽限期：ffmpeg 冷启动/设备枚举可能超过超时阈值，先等一个超时周期再判假死
        deadline = time.time() + _VIDEO_FRAME_TIMEOUT
        while _video_stream_active and video_running and process.poll() is None:
            time.sleep(1.0)
            if time.time() < deadline:
                continue
            stale = (time.time() - holder['t']) >= _VIDEO_FRAME_TIMEOUT
            if stale:
                log("视频看门狗：{}秒无新帧，判定设备假死，强制重启".format(_VIDEO_FRAME_TIMEOUT))
                try:
                    process.terminate()
                except Exception:
                    pass
                return
    except Exception:
        pass

def video_capture_worker_v2():
    """视频捕获线程 - FFmpeg版本 - 优化版（含错误诊断、假死看门狗与自动重启）"""
    global _ffmpeg_process, video_running, _current_frame, _video_frame_seq, _video_fps, _video_stream_active

    fps = int(config['VIDEO'].get('FPS', 10))
    _video_fps = fps
    quality = int(config['VIDEO'].get('QUALITY', 3))
    width = int(config['VIDEO'].get('WIDTH', 320))
    height = int(config['VIDEO'].get('HEIGHT', 240))
    dev_name = config['VIDEO'].get('DEVICE_NAME', 'USB Video')

    # 格式阶梯：先用用户配置，再尝试 640x480，最后用设备默认（不指定分辨率/帧率）。
    # 部分 USB 采集卡只支持特定格式，配置值不匹配时会 "Could not set video options"。
    size_ladder = [
        (width, height, fps),
        (640, 480, fps),
        (None, None, None),
    ]

    CREATE_NO_WINDOW = 0x08000000
    stderr_lines = []
    restart_backoff = 1      # 连续快速失败退避系数：1,2,3,4 封顶4（对应等待 1.5-6s）
    ladder_idx = 0           # 当前格式阶梯档位
    zero_frame_fails = 0     # 连续"启动即0帧"失败次数（熔断计数）
    banner_logged = False    # 日志节流：避免每次重试都刷 14 行 ffmpeg banner
    CIRCUIT_LIMIT = 5        # 连续 0 帧失败上限：达到即暂停自动重试

    def _drain_stderr(pipe, buf):
        for raw in pipe:
            try:
                line = raw.decode('utf-8', 'replace').rstrip()
            except Exception:
                line = ''
            if line:
                buf.append(line)
                if len(buf) > 200:
                    del buf[:100]

    def _build_cmd(ffmpeg, w, h, f):
        c = [ffmpeg, '-y', '-f', 'dshow', '-rtbufsize', '64M']
        if w and h:
            c += ['-video_size', '{}x{}'.format(w, h)]
        if f:
            c += ['-framerate', str(f)]
        c += ['-i', 'video={}'.format(dev_name)]
        c += ['-vf', 'scale={}:{}'.format(width, height)]
        c += ['-c:v', 'mjpeg', '-q:v', str(quality), '-fflags', 'nobuffer',
              '-f', 'image2pipe', '-']
        return c

    while _video_stream_active:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            log("未找到ffmpeg")
            return
        stderr_lines.clear()

        w, h, f = size_ladder[ladder_idx]
        cmd = _build_cmd(ffmpeg, w, h, f)

        if w and h:
            log("FFmpeg: {}x{} {}fps MJPEG q={} (低延迟)".format(w, h, f, quality))
        else:
            log("FFmpeg: 设备默认格式 MJPEG q={} (低延迟)".format(quality))
        try:
            _ffmpeg_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
            with _ffmpeg_pids_lock:
                _ffmpeg_spawned_pids.add(_ffmpeg_process.pid)
        except Exception as e:
            log("FFmpeg启动失败：{}".format(e))
            _ffmpeg_process = None
            if not _video_stream_active:
                break
            wait_secs = min(1.5 * restart_backoff, 6.0)
            log("{}s 后自动重启视频捕获...".format(wait_secs))
            restart_backoff = min(restart_backoff + 1, 4)
            wait_until = time.time() + wait_secs
            while _video_stream_active and time.time() < wait_until:
                time.sleep(0.2)
            continue
        _threading.Thread(target=_drain_stderr, args=(_ffmpeg_process.stderr, stderr_lines), daemon=True).start()
        log("FFmpeg started, PID={}".format(_ffmpeg_process.pid))

        holder = {'t': time.time()}
        _threading.Thread(target=_video_watchdog, args=(_ffmpeg_process, holder), daemon=True, name='VideoWatchdog').start()

        frame_count = 0
        last_frame_time = 0
        buffer = bytearray()
        CHUNK_SIZE = 16384

        try:
            # 用独立守护线程读取 ffmpeg 输出管道：即使 ffmpeg 设备假死/被看门狗强杀，
            # 主循环也能通过超时轮询退出并自动重启，不会因 read() 永久阻塞而卡死
            reader_q = _queue.Queue()
            def _pipe_reader(p):
                try:
                    while True:
                        chunk = p.stdout.read(CHUNK_SIZE)
                        if not chunk:
                            reader_q.put(None)
                            return
                        reader_q.put(chunk)
                except Exception:
                    reader_q.put(None)
            _threading.Thread(target=_pipe_reader, args=(_ffmpeg_process,),
                              daemon=True, name='FfmpegStdoutReader').start()

            while video_running:
                try:
                    chunk = reader_q.get(timeout=0.5)
                except _queue.Empty:
                    if _ffmpeg_process.poll() is not None:
                        break  # 进程已退出且队列排空
                    continue
                if chunk is None:
                    break  # EOF（正常退出或被强杀后管道关闭）

                buffer += chunk
                while True:
                    start_idx = buffer.find(b'\xFF\xD8')
                    if start_idx == -1:
                        break
                    end_idx = buffer.find(b'\xFF\xD9', start_idx)
                    if end_idx == -1:
                        if start_idx > 0:
                            del buffer[:start_idx]
                        break

                    frame = bytes(buffer[start_idx:end_idx + 2])
                    del buffer[:end_idx + 2]

                    current_time = time.time()
                    holder['t'] = current_time  # 任意帧到达即视为心跳
                    if last_frame_time > 0 and current_time - last_frame_time < (1.0 / fps):
                        continue

                    last_frame_time = current_time
                    with _frame_lock:
                        _current_frame = frame
                        _video_frame_seq += 1
                    frame_count += 1
                    if frame_count == 1:
                        log("视频出帧正常（{}x{}）".format(w or '默认', h or '默认'))
                    if frame_count % 300 == 0:
                        log("视频采集: 累计{}帧, seq={}".format(frame_count, _video_frame_seq))
                # 保护：异常数据流下避免缓冲区无限增长
                if len(buffer) > (4 << 20):
                    del buffer[:]
        finally:
            proc = _ffmpeg_process
            if proc is not None:
                code = proc.poll()
                if frame_count == 0:
                    # 日志节流：首次失败打印完整 stderr；后续只给一条摘要，避免刷屏
                    if not banner_logged:
                        log("视频循环退出, 共0帧 (设备无法打开/无信号)")
                        log("ffmpeg exit code={}".format(code))
                        if stderr_lines:
                            tail = stderr_lines[-14:]
                            log("--- ffmpeg stderr 最近{}行 ---".format(len(tail)))
                            for l in tail:
                                log("  " + l)
                        banner_logged = True
                    else:
                        err = ''
                        for l in reversed(stderr_lines):
                            if l.strip():
                                err = l.strip()
                                break
                        log("视频仍无帧(第{}次)：{}".format(zero_frame_fails + 1, err or '无 stderr'))
                else:
                    log("视频循环退出, 共{}帧".format(frame_count))
                    log("ffmpeg exit code={}".format(code))
                try:
                    if proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=2.0)
                        except Exception:
                            pass
                    if proc.poll() is None:
                        proc.kill()
                        try:
                            proc.wait(timeout=2.0)
                        except Exception:
                            pass
                except Exception:
                    pass
                with _ffmpeg_pids_lock:
                    _ffmpeg_spawned_pids.discard(proc.pid)
            _ffmpeg_process = None

        if not _video_stream_active:
            break

        # 熔断与格式阶梯：0 帧=设备打不开/无信号；有帧=正常结束或中途断流。
        if frame_count == 0:
            zero_frame_fails += 1
            if ladder_idx < len(size_ladder) - 1:
                ladder_idx += 1
                log("改用 {} 格式重试".format('640x480' if ladder_idx == 1 else '设备默认'))
            if zero_frame_fails >= CIRCUIT_LIMIT:
                msg = _t('log.video.circuit')
                log(msg)
                try:
                    socketio.emit('video_status_updated',
                                  {'ok': False, 'status': 'stopped', 'msg': msg},
                                  namespace='/', broadcast=True)
                except Exception:
                    pass
                _video_stream_active = False
                video_running = False
                return
            restart_backoff = min(restart_backoff + 1, 4)
            wait_secs = min(1.5 * restart_backoff, 6.0)
            log("视频捕获启动后很快失败(0帧)，{}s 后重试(第{}次)".format(wait_secs, zero_frame_fails))
        elif frame_count < 30:
            restart_backoff = min(restart_backoff + 1, 4)  # 1,2,3,4 封顶4
            wait_secs = min(1.5 * restart_backoff, 6.0)
            log("视频捕获启动后很快失败({}帧)，{}s 后重试(第{}次)".format(
                frame_count, wait_secs, restart_backoff))
        else:
            # 成功运行过：复位阶梯/熔断/退避，并通知前端已恢复出帧
            ladder_idx = 0
            zero_frame_fails = 0
            banner_logged = False
            restart_backoff = 1
            wait_secs = 3.0  # 正常结束后稳定等待，避免设备未释放
            try:
                socketio.emit('video_status_updated',
                              {'ok': True, 'status': 'streaming'},
                              namespace='/', broadcast=True)
            except Exception:
                pass
        wait_until = time.time() + wait_secs
        while _video_stream_active and time.time() < wait_until:
            time.sleep(0.2)


def start_video_stream():
    """启动视频流"""
    global video_thread, video_running, _video_stream_active

    if not _video_op_lock.acquire(blocking=False):
        log("视频流操作进行中，跳过启动")
        return video_running
    try:
        log("=== start_video_stream START ===")
        log("video_running={}".format(video_running))
        log("config VIDEO ENABLED={}".format(config['VIDEO'].get('ENABLED', 'false')))
        log("config VIDEO WIDTH={}, HEIGHT={}, FPS={}, QUALITY={}".format(
            config['VIDEO'].get('WIDTH', '320'),
            config['VIDEO'].get('HEIGHT', '240'),
            config['VIDEO'].get('FPS', '10'),
            config['VIDEO'].get('QUALITY', '3')))

        _video_stream_active = True

        if video_running:
            log("already running, return True")
            return True

        if not config['VIDEO'].get('ENABLED', 'false').lower() == 'true':
            log("video not enabled, return False")
            return False

        if not init_video_capture():
            log("init_video_capture failed")
            return False

        video_running = True
        log("Creating thread...")
        video_thread = _threading.Thread(target=video_capture_worker_v2, daemon=True)
        video_thread.start()

        log("Thread started, about to return True")
        return True
    finally:
        _video_op_lock.release()


def stop_video_stream():
    """停止视频流"""
    global video_running, _ffmpeg_process, video_thread, _video_stream_active

    if not _video_op_lock.acquire(blocking=False):
        log("视频流操作进行中，跳过停止")
        return

    try:
        log("stop_video_stream called, video_running={}".format(video_running))

        _video_stream_active = False
        video_running = False

        proc = _ffmpeg_process
        if proc:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3.0)
                    except Exception:
                        proc.kill()
                        proc.wait(timeout=1.0)
            except Exception:
                pass
            with _ffmpeg_pids_lock:
                _ffmpeg_spawned_pids.discard(proc.pid)
            _ffmpeg_process = None

        # 清理可能残留的本程序ffmpeg进程（设备未释放会导致后续启动失败）
        with _ffmpeg_pids_lock:
            stale_pids = list(_ffmpeg_spawned_pids)
            _ffmpeg_spawned_pids.clear()
        for pid in stale_pids:
            try:
                if psutil.pid_exists(pid):
                    psutil.Process(pid).kill()
                    log("已清理残留ffmpeg进程 PID={}".format(pid))
            except Exception:
                pass

        try:
            if video_thread and video_thread.is_alive():
                video_thread.join(timeout=3.0)
        except Exception:
            pass
        video_thread = None

        _threading.Event().wait(0.5)

        log("视频流已停止")
    finally:
        _video_op_lock.release()

# ===================== Yaesu CAT 通信 =====================
def build_yaesu_cmd(cmd, value=""):
    """构建Yaesu CAT命令"""
    if value:
        return f"{cmd}{value};"
    return f"{cmd};"

def send_yaesu_cmd(cmd, value="", delay=0.05):
    """发送Yaesu CAT命令"""
    global ser
    try:
        if not ser or not ser.is_open:
            log(f"串口未连接，尝试自动重连...")
            try:
                ser = serial.Serial(COM_PORT, BAUD_RATE, timeout=1, write_timeout=1)
                time.sleep(0.5)
                init_yaesu_radio()
            except Exception as e:
                log(f"串口自动重连失败：{e}")
                return False

        # 写操作在锁内（微秒级原子）；响应读取与 sleep 移出锁执行，
        # 避免慢响应头阻塞 PTT/频率等所有指令（原最坏持锁 ≈1s）
        if cmd == "FA":
            with lock:
                ser.write(build_yaesu_cmd("VS", "0").encode('ascii'))
                time.sleep(0.02)
                ser.write(build_yaesu_cmd(cmd, value).encode('ascii'))
        else:
            with lock:
                ser.write(build_yaesu_cmd(cmd, value).encode('ascii'))

        time.sleep(delay)
        resp = b""
        try:
            read_count = 0
            while ser.in_waiting > 0 and read_count < 20:
                resp += ser.read(ser.in_waiting)
                time.sleep(0.05)
                read_count += 1
        except Exception:
            pass

        log(f"发送电台指令：{cmd} = {value}")
        if resp:
            log(f"电台响应：{resp.decode('ascii', errors='ignore').strip()}")

        return True
    except Exception as e:
        log(f"发送指令失败：{e}")
        try:
            if ser: ser.close()
        except: pass
        ser = None
        return False

def send_ex_menu(p1, p2, p3, value="", delay=0.05):
    """发送或读取EX菜单命令 (FT-710)
    p1=01-05, p2=01-07, p3=01-26
    value=""时读取（等待响应），否则只发送不等待
    例: send_ex_menu(1,1,14) = 读取SSB→MOD SOURCE
         send_ex_menu(1,1,14,"1") = SSB→MOD SOURCE=USB
         send_ex_menu(1,1,11,"050") = SSB→USB OUT LEVEL=50
    返回: 电台响应(str) 或 ""
    """
    raw = f"EX{p1:02d}{p2:02d}{p3:02d}{value};"
    if value == "":
        log(f"读取电台设置：{raw}")
    else:
        log(f"发送电台指令：{raw}")
    with lock:
        try:
            global ser
            if ser and ser.is_open:
                try: ser.reset_input_buffer()
                except: pass
                ser.write(raw.encode('ascii'))
                time.sleep(delay)
                if value == "":
                    resp = ser.read_until(b';')
                    if resp:
                        resp_str = resp.decode('ascii', errors='replace').strip()
                        log(f"电台响应：{resp_str}")
                        return resp_str
        except Exception as e:
            log(f"EX命令失败：{e}")
    return ""

def read_yaesu(cmd, delay=0.25):
    """发送CAT查询命令并返回电台响应字符串（失败或超时返回""）"""
    global ser
    try:
        if not ser or not ser.is_open:
            return ""
        raw = build_yaesu_cmd(cmd, "")
        with lock:
            try:
                ser.reset_input_buffer()
            except:
                pass
            ser.write(raw.encode('ascii'))
            time.sleep(delay)
            resp = ser.read_until(b';')
        if resp:
            txt = resp.decode('ascii', errors='ignore').strip()
            log(f"读取电台：{txt}")
            return txt
    except Exception as e:
        log(f"读取电台失败：{e}")
    return ""

_MODE_REVERSE = {
    '01': 'LSB', '02': 'USB', '03': 'CW', '04': 'FM',
    '05': 'AM', '09': 'CWR', '0C': 'RTTY',
}

def sync_radio_state():
    """从电台读取当前频率/模式/功率并同步到状态（启动与自动重连时调用）"""
    if not (ser and ser.is_open):
        return False
    ok = False

    fa = read_yaesu("FA")
    if fa.startswith("FA") and len(fa) >= 11:
        try:
            freq_hz = int(fa[2:11])
            if 0 < freq_hz <= 54000000:
                radio_state['frequency'] = freq_hz / 1e6
                log(f"同步频率: {radio_state['frequency']:.3f} MHz")
                ok = True
        except Exception:
            pass

    md = read_yaesu("MD")
    if md.startswith("MD") and len(md) >= 4:
        mode = _MODE_REVERSE.get(md[2:4].upper())
        if mode:
            radio_state['mode'] = mode
            log(f"同步模式: {mode}")
            ok = True

    pc = read_yaesu("PC")
    if pc.startswith("PC") and len(pc) >= 5:
        try:
            radio_state['power'] = max(0, min(100, int(pc[2:5])))
            log(f"同步功率: {radio_state['power']}%")
            ok = True
        except Exception:
            pass

    return ok

def init_yaesu_radio():
    """初始化Yaesu电台"""
    try:
        # 关闭AI，设置VFO模式
        send_yaesu_cmd("AI", "0", 0.05)
        send_yaesu_cmd("VM", "0", 0.05)
        # 切换到VFO-A
        send_yaesu_cmd("VS", "0", 0.05)

        # 设置SSB模式下USB音频为调制源
        time.sleep(0.2)
        # 先读取当前值
        cur = send_ex_menu(1, 1, 14, delay=0.15)
        log(f"SSB MOD SOURCE当前值: [{cur}]")
        # 设置MOD SOURCE = USB
        send_ex_menu(1, 1, 14, "1", 0.15)
        # 读取确认
        time.sleep(0.10)
        cur2 = send_ex_menu(1, 1, 14, delay=0.15)
        log(f"SSB MOD SOURCE设置后: [{cur2}]")
        # 设置USB OUT LEVEL = 50 (默认仅3，太低)
        send_ex_menu(1, 1, 11, "050", 0.10)
        # 设置USB MOD GAIN = 50
        send_ex_menu(1, 1, 15, "050", 0.10)
        # 读回确认
        time.sleep(0.15)
        out_level = send_ex_menu(1, 1, 11, delay=0.10)
        log(f"SSB USB OUT LEVEL: [{out_level}]")
        mod_gain = send_ex_menu(1, 1, 15, delay=0.10)
        log(f"SSB USB MOD GAIN: [{mod_gain}]")

        # 也设置其他常见模式的MOD SOURCE
        for p2_val, mode_name in [(3, "AM"), (4, "FM")]:
            send_ex_menu(1, p2_val, 13, "1", 0.05)  # MOD SOURCE = USB
            send_ex_menu(1, p2_val, 11, "050", 0.05)  # USB OUT LEVEL = 50

        log("Yaesu FT-710 初始化完成")
        sync_radio_state()
        return True
    except Exception as e:
        log(f"Yaesu初始化失败：{e}")
        return False



def init_com(port=None, baud=None):
    global ser
    use_port = port if port else COM_PORT
    use_baud = baud if baud else BAUD_RATE
    
    with lock:
        try:
            if ser and ser.is_open:
                ser.close()
            ser = serial.Serial(use_port, use_baud, timeout=1, write_timeout=1)
            time.sleep(0.5)
            radio_state["com_port"] = use_port
            radio_state["baud_rate"] = use_baud
        except Exception as e:
            log(_t('log.com.init.fail', e=e))
            radio_state["audio_status"] = _t('srv.audio.errcmd', e=str(e))
            return False
    init_yaesu_radio()
    # 启动时强制默认功率 25W（25%），覆盖电台原有功率设置
    send_yaesu_cmd("PC", "025", 0.05)
    radio_state['power'] = 25
    log("默认功率: 25% (25W)")
    log(_t('log.com.init.ok', p=use_port, b=use_baud))
    return True

def send_radio(cmd, val):
    """发送电台命令"""
    # Yaesu CAT 命令映射
    cmd_map = {
        "TRX": ("TX", val),        # TX1/TX0
        "TXF": ("FA", val),       # 设置频率
        "RXF": ("FA", val),
        "MODE": ("MD", val),      # 模式
        "TUNER": ("TU", val),     # 天调开启/关闭
        "TUNE": ("RC", val),      # 天调启动（RC=Remote Control）
        "NB": ("NB", val),        # 噪声抑制
        "POWER": ("PC", val),     # 功率 0-100, 需要3位如010
    }
    
    if cmd in cmd_map:
        yaesu_cmd, value = cmd_map[cmd]
        # 天调和频率命令需要更长延迟
        delay = 0.15 if cmd in ["TXF", "RXF", "TUNER", "TUNE"] else 0.05
        return send_yaesu_cmd(yaesu_cmd, value, delay)
    else:
        log(f"未知命令：{cmd}")
        return False

# ===================== 音频处理 =====================
def _find_default_devices():
    try:
        dev_in, dev_out = sd.default.device
    except Exception:
        dev_in, dev_out = -1, -1

    try:
        all_devs = sd.query_devices()
    except Exception as e:
        log(f"sd.query_devices() 失败：{e}")
        all_devs = []
        for i in range(64):
            try:
                d = sd.query_devices(i)
                all_devs.append(d)
            except Exception:
                break

    if not all_devs:
        log("无法枚举任何音频设备")
        return -1, -1

    def _resolve(idx, need_input):
        if 0 <= idx < len(all_devs):
            d = all_devs[idx]
            key = 'max_input_channels' if need_input else 'max_output_channels'
            if d.get(key, 0) > 0:
                return idx
        for i, d in enumerate(all_devs):
            key = 'max_input_channels' if need_input else 'max_output_channels'
            if d.get(key, 0) > 0:
                return i
        return 0

    dev_in  = _resolve(dev_in,  need_input=True)
    dev_out = _resolve(dev_out, need_input=False)
    return dev_in, dev_out, all_devs


def get_audio_devices():
    try:
        result = _find_default_devices()
        if result[0] == -1:
            log("get_audio_devices: no device" if LANG == 'en' else "获取音频设备失败：无可用设备")
            radio_state["audio_status"] = _t('srv.audio.nodev')
            return False
        dev_in, dev_out, all_devs = result
        name_in  = all_devs[dev_in].get('name',  f'Device#{dev_in}')  if dev_in  < len(all_devs) else f'Device#{dev_in}'
        name_out = all_devs[dev_out].get('name', f'Device#{dev_out}') if dev_out < len(all_devs) else f'Device#{dev_out}'
        audio_devices["input"]  = name_in
        audio_devices["output"] = name_out
        log(f"Audio: IN[{dev_in}]={name_in} | OUT[{dev_out}]={name_out}")
        radio_state["audio_status"] = _t('srv.audio.device', n=name_out)
        return True
    except Exception as e:
        log(f"get_audio_devices: {e}" if LANG == 'en' else f"获取音频设备失败：{e}")
        radio_state["audio_status"] = _t('srv.audio.err', e=str(e))
        return False


# ===================== 脉冲抑制 =====================
_nb_history = collections.deque(maxlen=20)
_NB_THRESHOLD_MULT = 6.0
_NB_SAMPLE_MULT   = 4.0

def apply_noise_blanker(audio):
    global _nb_history
    audio = audio.copy()
    frame_rms = _frame_rms(audio)
    
    if len(_nb_history) >= 3:
        bg_rms = float(np.median(list(_nb_history)))
        
        if bg_rms > 0.001 and frame_rms > bg_rms * _NB_THRESHOLD_MULT:
            return np.zeros_like(audio)
        
        if frame_rms > 0.001:
            sample_thresh = frame_rms * _NB_SAMPLE_MULT
            mask = np.abs(audio) > sample_thresh
            if np.any(mask):
                audio[mask] = 0.0
    
    if frame_rms > 0.001:
        _nb_history.append(frame_rms)
    
    return audio.astype('float32')

def apply_noise_reduction(audio, level):
    noise_config = NOISE_LEVELS[level]
    if not noise_config["enabled"]:
        return audio

    rms = _frame_rms(audio)
    gate = noise_config["gate"]
    aggr = noise_config["aggressiveness"]

    global _noise_profile
    with _noise_profile_lock:
        fft = np.fft.rfft(audio)
        power = np.abs(fft) ** 2

        if _noise_profile["spectrum"] is None:
            buf = _noise_profile["init_buffer"]
            needed = _noise_profile["init_frames_needed"]
            if len(buf) < needed:
                buf.append(power.copy())
            if len(buf) >= needed:
                init_spectrum = np.min(buf, axis=0)
                _noise_profile["spectrum"]          = init_spectrum.copy()
                _noise_profile["history"]           = [init_spectrum.copy()] * _noise_profile["history_len"]
                _noise_profile["smoothed_spectrum"]  = init_spectrum.copy()
                _noise_profile["init_buffer"]       = []
                log("噪声谱初始化完成")
            else:
                return audio
        else:
            if rms < gate * 1.5:
                alpha = 0.1
            else:
                alpha = 0.03
            updated = (1 - alpha) * _noise_profile["spectrum"] + alpha * power
            _noise_profile["spectrum"] = updated
            _noise_profile["history"].pop(0)
            _noise_profile["history"].append(updated.copy())
            _noise_profile["smoothed_spectrum"] = np.min(_noise_profile["history"], axis=0)

        if level == 5:
            noise_est = _noise_profile["smoothed_spectrum"] * 2.0
        else:
            noise_est = _noise_profile["smoothed_spectrum"] * 1.05

    clean_power = np.maximum(power - aggr * noise_est, 0.0)
    gain = np.where(power > 1e-12, np.sqrt(clean_power / (power + 1e-12)), 0.0)

    gain_floor = max(0.01, 0.15 - aggr * 0.02)
    gain = np.clip(gain, gain_floor, 1.0)

    if rms < gate:
        if rms < gate * 0.3:
            frame_gain = 0.01
        else:
            frame_gain = 0.01 + (rms - gate * 0.3) / (gate * 0.7) * 0.25
        gain = gain * frame_gain

    fft_clean = fft * gain
    return np.fft.irfft(fft_clean, n=len(audio)).astype('float32')


def _try_open_streams(device_in, device_out, samplerate, dtype):
    in_stream = sd.InputStream(
        samplerate=samplerate,
        channels=CHANNELS,
        blocksize=AUDIO_BLOCK_SIZE,
        dtype=dtype,
        device=device_in,
        callback=audio_input_callback,
        latency='low',
        never_drop_input=False
    )
    try:
        in_stream.start()
        out_stream = sd.OutputStream(
            samplerate=samplerate,
            channels=CHANNELS,
            blocksize=AUDIO_BLOCK_SIZE,
            dtype=dtype,
            device=device_out,
            latency='low'
        )
        out_stream.start()
        return in_stream, out_stream
    except Exception:
        # 失败回滚：输出流失败时，已打开的输入流必须关闭，否则回调持续写 RingBuffer 泄漏
        try:
            in_stream.stop()
        except Exception:
            pass
        try:
            in_stream.close()
        except Exception:
            pass
        raise


def init_audio_streams():
    global audio_output_stream, audio_input_stream, SAMPLING_RATE, _downsampler, _rx_sos, _rx_zi, AUDIO_BLOCK_SIZE

    if not get_audio_devices():
        return False

    configured_dev_in = config['AUDIO'].get('DEVICE_IN', '-1')
    configured_dev_out = config['AUDIO'].get('DEVICE_OUT', '-1')
    
    dev_in  = int(configured_dev_in) if configured_dev_in != '-1' else -1
    dev_out = int(configured_dev_out) if configured_dev_out != '-1' else -1

    # 按名称优先匹配设备：Windows 设备索引会随 USB 重枚举漂移，
    # 只存索引会导致重启后抓到错误设备（RX 静音）。名称稳定，优先使用。
    name_in  = str(config['AUDIO'].get('DEVICE_IN_NAME', '')).strip()
    name_out = str(config['AUDIO'].get('DEVICE_OUT_NAME', '')).strip()
    if name_in or name_out:
        try:
            _devs = sd.query_devices()
        except Exception:
            _devs = []
        if name_in:
            for i, d in enumerate(_devs):
                if d.get('max_input_channels', 0) > 0 and name_in.lower() in str(d.get('name', '')).lower():
                    dev_in = i
                    log(f"按名称匹配输入设备：{name_in} -> [{i}] {d.get('name')}")
                    break
        if name_out:
            for i, d in enumerate(_devs):
                if d.get('max_output_channels', 0) > 0 and name_out.lower() in str(d.get('name', '')).lower():
                    dev_out = i
                    log(f"按名称匹配输出设备：{name_out} -> [{i}] {d.get('name')}")
                    break

    if dev_in >= 0 or dev_out >= 0:
        log(f"使用配置的音频设备：输入={dev_in} 输出={dev_out}")

    if dev_in == -1 or dev_out == -1:
        try:
            default_in, default_out = sd.default.device
            if dev_in == -1:
                dev_in = default_in
            if dev_out == -1:
                dev_out = default_out
        except:
            pass

    if dev_in == -1 or dev_out == -1:
        try:
            devices = sd.query_devices()
            log(f"设备列表共 {len(devices)} 个")
            for i, d in enumerate(devices):
                log(f"  设备{i}: {d['name']} in={d['max_input_channels']} out={d['max_output_channels']}")
                if dev_in == -1 and d['max_input_channels'] > 0:
                    dev_in = i
                if dev_out == -1 and d['max_output_channels'] > 0:
                    dev_out = i
                if dev_in != -1 and dev_out != -1:
                    break
            log(f"自动选择设备：输入={dev_in} 输出={dev_out}")
        except Exception as e:
            log(f"设备枚举失败：{e}")
            if dev_in == -1:
                dev_in = 0
            if dev_out == -1:
                dev_out = 0

    # 初始化下采样器
    _downsampler = Downsampler(SAMPLING_RATE, _TARGET_RATE, AUDIO_BLOCK_SIZE)
    
    # 更新DSP滤波器的采样率为目标采样率
    _rx_sos = make_sos(250, 4000, _TARGET_RATE)
    _rx_zi = _sosfilt_zi(_rx_sos) * 0.0

    fallback_list = [
        ('float32', SAMPLING_RATE, dev_in, dev_out, f"float32 {SAMPLING_RATE}Hz"),
        ('int16',   SAMPLING_RATE, dev_in, dev_out, f"int16   {SAMPLING_RATE}Hz"),
        ('int16',   48000,         dev_in, dev_out, "int16   48000Hz"),
    ]

    for dtype, samplerate, try_dev_in, try_dev_out, desc in fallback_list:
        try:
            log(f"尝试音频模式：{desc}")
            in_s, out_s = _try_open_streams(try_dev_in, try_dev_out, samplerate, dtype)
            audio_input_stream  = in_s
            audio_output_stream = out_s

            log(f"音频流初始化成功：{desc}")
            radio_state["audio_status"] = _t('srv.audio.ready', d=desc)
            save_config(config)
            return True

        except Exception as e:
            log(f"模式 [{desc}] 失败：{e}")
            for s in [audio_input_stream, audio_output_stream]:
                try:
                    if s is not None:
                        s.stop(); s.close()
                except Exception:
                    pass
            audio_input_stream  = None
            audio_output_stream = None
            continue

    # 配置的设备全部失败，尝试自动检测
    log("配置的设备无效，尝试自动检测音频设备...")
    try:
        default_in, default_out = sd.default.device
        fallback_auto = [
            ('float32', SAMPLING_RATE, default_in, default_out, f"auto float32 {SAMPLING_RATE}Hz"),
            ('int16',   SAMPLING_RATE, default_in, default_out, f"auto int16   {SAMPLING_RATE}Hz"),
        ]
        for dtype, samplerate, try_dev_in, try_dev_out, desc in fallback_auto:
            try:
                log(f"尝试自动音频模式：{desc}")
                in_s, out_s = _try_open_streams(try_dev_in, try_dev_out, samplerate, dtype)
                audio_input_stream  = in_s
                audio_output_stream = out_s
                log(f"音频流自动初始化成功：{desc}")
                radio_state["audio_status"] = _t('srv.audio.ready', d=desc)
                config['AUDIO']['DEVICE_IN'] = str(try_dev_in)
                config['AUDIO']['DEVICE_OUT'] = str(try_dev_out)
                save_config(config)
                return True
            except Exception as e:
                log(f"自动模式 [{desc}] 失败：{e}")
                for s in [audio_input_stream, audio_output_stream]:
                    try:
                        if s is not None:
                            s.stop(); s.close()
                    except Exception:
                        pass
                audio_input_stream  = None
                audio_output_stream = None
                continue
    except Exception as e:
        log(f"自动检测音频设备失败：{e}")

    log("Audio stream init failed" if LANG == 'en' else "音频流初始化失败")
    radio_state["audio_status"] = _t('srv.audio.failed')
    try: socketio.emit('audio_failed', {'status': _t('srv.audio.initfail')}, namespace='/')
    except: pass
    return False

def audio_input_callback(indata, frames, time_info, status):
    if tx_active.is_set():
        return
    if status and status.input_underflow:
        return
    global _ring_write, _ring_read, _ring_overflow_count, _ring_last_overflow_log
    do_log = False
    with _ring_lock:
        samples = indata[:, 0]
        if samples.dtype != np.float32:
            samples = samples.astype(np.float32) / 32768.0
        # 环满保护：写指针将追平读指针时，丢弃最旧一帧并计数（避免撕裂读）
        if (_ring_write + 1) % _RING_SIZE == _ring_read:
            _ring_read = (_ring_read + 1) % _RING_SIZE
            _ring_overflow_count += 1
            now = time.monotonic()
            if now - _ring_last_overflow_log >= 2.0:
                _ring_last_overflow_log = now
                do_log = True
        _ring_buf[_ring_write % _RING_SIZE] = samples
        _ring_write = (_ring_write + 1) % _RING_SIZE
    if do_log:
        try:
            log(f"RingBuffer溢出，丢最旧帧（累计{_ring_overflow_count}）")
        except Exception:
            pass
    _ring_event.set()

def _reset_dsp_state():
    global _rx_zi, _noise_profile
    _rx_zi = _sosfilt_zi(_rx_sos) * 0.0
    with _noise_profile_lock:
        _noise_profile["spectrum"] = None
        _noise_profile["history"] = []
        _noise_profile["smoothed_spectrum"] = None
        _noise_profile["init_buffer"] = []
    _reset_ring_buffer()
    _drain_send_queue()
    log("DSP状态已重置")

def _force_safe_rx():
    """强制电台回到RX，用于音频重启/恢复后的安全保护"""
    if tx_active.is_set():
        log("音频恢复：强制切回RX")
        tx_active.clear()
        radio_state['trx'] = 'RX'
        send_radio("TRX", "0")
        socketio.emit('trx_updated', {'trx': 'RX'}, namespace='/')

def restart_audio_streams():
    if not _audio_restart_lock.acquire(blocking=False):
        log("音频流重启已在进行中，跳过")
        return False
    try:
        if tx_active.is_set():
            log("发射中，跳过音频流重启")
            return False
        
        _audio_restarting.set()
        log("正在重启音频流...")
        try:
            if audio_input_stream:
                try: audio_input_stream.stop()
                except: pass
                try: audio_input_stream.close()
                except: pass
            if audio_output_stream:
                try: audio_output_stream.stop()
                except: pass
                try: audio_output_stream.close()
                except: pass
        except: pass
        time.sleep(0.3)
        _reset_dsp_state()
        result = init_audio_streams()
        if result:
            log("音频流已自动恢复")
            _force_safe_rx()
        else:
            log("音频流恢复失败")
        return result
    finally:
        _audio_restarting.clear()
        _audio_restart_lock.release()

def audio_watchdog():
    def _watch():
        fail_streak = 0
        while not _running.is_set():
            # 指数退避：正常 5s 巡检；连续失败逐步放宽到 60s，避免坏设备下反复重启风暴
            time.sleep(min(5 * (2 ** fail_streak), 60))
            if tx_active.is_set():
                continue
            try:
                input_ok = audio_input_stream is not None and audio_input_stream.active
                output_ok = audio_output_stream is not None and audio_output_stream.active
                if not input_ok:
                    log("看门狗：接收音频流异常，尝试恢复...")
                    restart_audio_streams()
                    fail_streak += 1
                elif not output_ok:
                    log("看门狗：发送音频流异常，尝试恢复...")
                    restart_audio_streams()
                    fail_streak += 1
                else:
                    fail_streak = 0
            except Exception:
                fail_streak += 1

    t = _threading.Thread(target=_watch, daemon=True)
    t.start()

def _tx_timeout_watchdog():
    while not _running.is_set():
        if tx_active.is_set():
            elapsed = time.monotonic() - tx_start_time
            if elapsed >= 180:
                log(f"TX超时{elapsed:.0f}s，自动切回RX")
                send_radio("TRX", "0")
                tx_active.clear()
                radio_state['trx'] = 'RX'
                socketio.emit('trx_updated', {'trx': 'RX'}, namespace='/')
        time.sleep(5)

def _get_audio_latency_ms():
    rx_latency = _webrtc_listener_max_qsize() * 10
    tx_latency = _tx_audio_queue.qsize() * 10
    return max(rx_latency, tx_latency)

def _audio_latency_emitter():
    while not _running.is_set():
        latency = _get_audio_latency_ms()
        socketio.emit('latency_update', {'latency': latency}, namespace='/')
        time.sleep(2)

# ===================== 证书生成 =====================
def generate_ssl_certs():
    cert_file = os.path.join(APP_DIR, "cert.pem")
    key_file = os.path.join(APP_DIR, "key.pem")

    # 检查现有证书的 SAN 是否覆盖当前所有 IP
    if os.path.exists(cert_file) and os.path.exists(key_file):
        try:
            from cryptography import x509
            cert = x509.load_pem_x509_certificate(open(cert_file, "rb").read())
            cert_san_ips = set()
            for ext in cert.extensions:
                if ext.oid == x509.oid.ExtensionOID.SUBJECT_ALTERNATIVE_NAME:
                    for san in ext.value:
                        if isinstance(san, x509.IPAddress):
                            cert_san_ips.add(str(san.value))
                    break
            current_ips = set(_all_ips) if _all_ips else {socket.gethostbyname(socket.gethostname())}
            if cert_san_ips.issuperset(current_ips):
                log(f"SSL证书已存在，覆盖所有当前IP：{cert_file}")
                return cert_file, key_file
            missing = current_ips - cert_san_ips
            log(f"SSL证书缺少当前IP，重建中（新增={missing}）")
            os.remove(cert_file)
            os.remove(key_file)
        except Exception:
            try: os.remove(cert_file)
            except: pass
            try: os.remove(key_file)
            except: pass

    log("生成自签名SSL证书...")
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime
        import ipaddress

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, u"ft710")])

        san_names = [
            x509.DNSName(u"localhost"),
            x509.DNSName(u"ft710"),
            x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        ]
        for ip_str in _all_ips:
            try:
                san_names.append(x509.IPAddress(ipaddress.IPv4Address(ip_str)))
            except:
                try:
                    san_names.append(x509.IPAddress(ipaddress.IPv6Address(ip_str)))
                except:
                    pass

        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
            .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365))
            .add_extension(
                x509.SubjectAlternativeName(san_names),
                critical=False,
            )
            .sign(private_key, hashes.SHA256())
        )
        
        with open(key_file, "wb") as f:
            f.write(private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()
            ))
        with open(cert_file, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        
        log(f"SSL证书生成成功：{cert_file}")
        return cert_file, key_file
    except ImportError:
        log("缺少cryptography库！")
        raise
    except Exception as e:
        log(f"生成SSL证书失败：{e}")
        raise

# ===================== SocketIO事件 =====================
@socketio.on('connect', namespace='/')
def handle_connect():
    log("客户端已连接")
    gain_lv = current_gain_level
    noise_lv = current_noise_level
    noise_status = _noise_text(noise_lv)
    emit('connect_ack', {
        'status': 'connected',
        'audio_status': radio_state["audio_status"],
        'freq_step': radio_state['freq_step'],
        'frequency': radio_state['frequency'],
        'mode': radio_state.get('mode', 'USB'),
        'trx': radio_state.get('trx', 'RX'),
        'tuner': radio_state.get('tuner', 'OFF'),
        'nb': radio_state.get('nb', False),
        'gain_level': gain_lv,
        'gain_value': GAIN_LEVELS[gain_lv],
        'mic_gain_level': current_mic_gain_level,
        'noise_level': noise_lv,
        'noise_status': noise_status,
        'ai_denoise': _ai_denoise_enabled,
        'ai_atten_lim_db': _ai_atten_lim_db,
        'ptt_mode': PTT_MODE,
        'power': radio_state.get('power', 25),
        'com_port': radio_state['com_port'],
        'baud_rate': radio_state['baud_rate'],
        'meter': radio_state.get('meter', {"pwr": 0, "ref": 0, "swr": 1.0, "eff": 0, "connected": False})
    }, namespace='/')


@socketio.on('disconnect', namespace='/')
def handle_disconnect():
    log("客户端已断开")
    # 射频安全：持有PTT的客户端掉线（浏览器崩溃/断网）时立即回收发射，不等180s看门狗
    global _ptt_owner_sid
    try:
        sid = request.sid
        if tx_active.is_set() and _ptt_owner_sid is not None and sid == _ptt_owner_sid:
            log("发射客户端断开，自动切回RX")
            send_radio("TRX", "0")
            tx_active.clear()
            radio_state['trx'] = 'RX'
            _ptt_owner_sid = None
            socketio.emit('trx_updated', {'trx': 'RX'}, namespace='/')
    except Exception as e:
        log(f"断开清理失败：{e}")


@socketio.on('client_diag', namespace='/')
def handle_client_diag(data):
    """客户端播放链路诊断上报（WebRTC/音频激活失败等）。按类型 60s 节流。"""
    try:
        kind = str(data.get('kind', 'unknown'))[:40]
        detail = str(data.get('detail', ''))[:300]
    except Exception:
        return
    now = time.monotonic()
    if now - _client_diag_last.get(kind, 0.0) < 60.0:
        return
    if len(_client_diag_last) >= _CLIENT_DIAG_MAX:
        for k in [k for k, t in _client_diag_last.items() if now - t > _CLIENT_DIAG_TTL]:
            del _client_diag_last[k]
        while len(_client_diag_last) >= _CLIENT_DIAG_MAX:
            del _client_diag_last[min(_client_diag_last, key=_client_diag_last.get)]
    _client_diag_last[kind] = now
    try:
        log(f"客户端诊断[{kind}]: {detail}")
    except Exception:
        pass


@socketio.on('ptt_change', namespace='/')
def handle_ptt_change(data):
    try:
        mode = data.get('mode', PTT_MODE)
        action = data.get('t', 'RX')
        
        if mode == 'latching' and action == 'TOGGLE':
            current_trx = radio_state.get('trx', 'RX')
            t = 'TX' if current_trx == 'RX' else 'RX'
        else:
            t = action if action in ['TX', 'RX'] else 'RX'
        
        # PTT保护：音频重启中禁止TX，RX放行
        if t == 'TX' and _audio_restarting.is_set():
            log("音频重启中，PTT TX被拦截")
            emit('trx_updated', {'trx': radio_state.get('trx', 'RX')}, namespace='/', broadcast=True)
            return
        
        send_radio("TRX", "1" if t == "TX" else "0")
        radio_state['trx'] = t
        log(f"PTT切换：{t} (模式: {mode})")

        global audio_output_stream, audio_input_stream, _rx_start_seq, tx_start_time, _ptt_owner_sid

        if t == "TX":
            _ptt_owner_sid = request.sid
            tx_active.set()
            tx_start_time = time.monotonic()
            _rx_start_seq = (_rx_start_seq + 1) % 65536
            global _tx_squelch_until
            _tx_squelch_until = time.monotonic() + 0.05
            _reset_ring_buffer()
            _drain_send_queue()
        else:
            tx_active.clear()
            _ptt_owner_sid = None
            _tx_squelch_until = time.monotonic() + 0.10
            _drain_send_queue()
            _reset_ring_buffer()
            with _dsp_state_lock:
                _rx_zi = _sosfilt_zi(_rx_sos) * 0.0
            _rx_start_seq = (_rx_start_seq + 1) % 65536
            my_seq = _rx_start_seq
            def _delayed_rx_start(seq):
                time.sleep(0.02)
                if seq != _rx_start_seq or tx_active.is_set():
                    return
                with _stream_lock:
                    if audio_input_stream and not audio_input_stream.active:
                        try:
                            audio_input_stream.start()
                        except Exception as e:
                            log(f"启动接收流失败：{e}")
            _threading.Thread(target=_delayed_rx_start, args=(my_seq,),
                              daemon=True, name='RxStart').start()
        
        emit('trx_updated', {'trx': t}, namespace='/', broadcast=True)
    except Exception as e:
        log(f"PTT SocketIO切换失败：{e}")


@socketio.on('set_gain_level', namespace='/')
def handle_set_gain_level(data):
    global current_gain_level
    level = int(data.get('level', 2))
    if level < 0 or level >= len(GAIN_LEVELS):
        level = 2
    
    with gain_lock:
        current_gain_level = level
        radio_state["gain_level"] = level
        radio_state["gain_value"] = GAIN_LEVELS[level]
    
    log(f"增益等级已设置为：{level}级 ({GAIN_LEVELS[level]}x)")
    emit('gain_level_updated', {
        'level': level,
        'value': GAIN_LEVELS[level],
        'percent': f"{GAIN_LEVELS[level]}x"
    }, namespace='/', broadcast=True)


@socketio.on('set_noise_level', namespace='/')
def handle_set_noise_level(data):
    global current_noise_level
    level = int(data.get('level', 0))
    if level < 0 or level >= len(NOISE_LEVELS):
        level = 0
    
    with noise_lock:
        current_noise_level = level
        radio_state["noise_level"] = level
        radio_state["noise_status"] = _noise_text(level)
    
    global _noise_profile
    with _noise_profile_lock:
        _noise_profile["spectrum"] = None
        _noise_profile["history"] = []
    
    status_text = "关闭" if level == 0 else f"{level}级"
    log(f"降噪等级已设置为：{status_text}")
    emit('noise_level_updated', {
        'level': level,
        'status': status_text
    }, namespace='/', broadcast=True)


@socketio.on('set_ai_denoise', namespace='/')
def handle_set_ai_denoise(data):
    """神经网络降噪开关（DeepFilterNet3）。开启时 DSP 走 DFN3，
    与 1-5 级谱减法互斥（AI 关闭后自动恢复原等级）。"""
    global _ai_denoise_enabled, _ai_enable_want, _ai_loading, _ai_circuit_open
    global _ai_fail_count, _ai_circuit_until
    want = bool(data.get('enabled', False))
    log_msg = None
    emit_msg = None
    _start_worker = False
    with _ai_load_lock:
        _ai_enable_want = want
        if not want:
            _ai_denoise_enabled = False
            radio_state["ai_denoise"] = False
            if _ai_loading:
                log_msg = "关闭神经网络降噪（后台加载完成后保持关闭）"
            else:
                log_msg = "神经网络降噪已关闭（恢复 1-5 级谱减法）"
            emit_msg = {'enabled': False}
        elif _ai_denoise_enabled:
            emit_msg = {'enabled': True}
        elif _ai_circuit_open:
            if time.monotonic() >= _ai_circuit_until:
                _ai_circuit_open = False
                _ai_fail_count = 0
                log_msg = "神经网络降噪熔断冷却到期，允许重试"
                emit_msg = {'enabled': False, 'loading': True}
                _ai_loading = True
                _start_worker = True
            else:
                emit_msg = {'enabled': False, 'error': _ai_load_error,
                            'circuit_open': True}
        elif _ai_loading:
            emit_msg = {'enabled': False, 'loading': True}
        else:
            emit_msg = {'enabled': False, 'loading': True}
            _ai_loading = True
            _start_worker = True
    if log_msg:
        log(log_msg)
    emit('ai_denoise_updated', emit_msg, namespace='/', broadcast=True)
    if _start_worker:
        _threading.Thread(target=_ai_load_worker, daemon=True, name='AiLoad').start()


@socketio.on('set_ai_denoise_params', namespace='/')
def handle_set_ai_denoise_params(data):
    """神经网络降噪微调：atten_lim_db（衰减上限dB，6-30，越小越保护弱语音）。"""
    global _ai_atten_lim_db, _ai_params_pending
    try:
        atten = float(data.get('atten_lim_db', _ai_atten_lim_db))
    except (TypeError, ValueError):
        return
    atten = round(min(30.0, max(6.0, atten)), 1)
    _ai_atten_lim_db = atten
    _ai_params_pending = True
    config['AUDIO']['AI_ATTEN_LIM_DB'] = str(atten)
    save_config(config)
    log(f"神经网络降噪参数：衰减上限 {atten:g}dB")
    emit('ai_denoise_params_updated', {
        'atten_lim_db': atten
    }, namespace='/', broadcast=True)


@socketio.on('set_ptt_mode', namespace='/')
def handle_set_ptt_mode(data):
    global PTT_MODE
    mode = data.get('mode', 'momentary').lower()
    if mode not in ['momentary', 'latching']:
        mode = 'momentary'
    
    PTT_MODE = mode
    radio_state['ptt_mode'] = mode
    
    config['PTT']['MODE'] = mode
    save_config(config)
    
    log(f"PTT模式已切换为：{'Hold' if mode == 'momentary' else 'Tap'}")
    emit('ptt_mode_updated', {
        'mode': mode,
        'label': 'Hold' if mode == 'momentary' else 'Tap'
    }, namespace='/', broadcast=True)


# ===================== Flask API =====================
@app.route('/api/list_ports', methods=['GET'])
def api_list_ports():
    try:
        import serial.tools.list_ports
        ports = []
        for p in serial.tools.list_ports.comports():
            ports.append({
                'port': p.device,
                'desc': p.description or p.device
            })
        ports.sort(key=lambda x: x['port'])
        return jsonify({'status': 'ok', 'ports': ports})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e), 'ports': []}), 500


@socketio.on('set_com_port', namespace='/')
def handle_set_com_port(data):
    port = data.get('port', COM_PORT)
    baud = int(data.get('baud', BAUD_RATE))
    
    config['SERIAL']['COM_PORT'] = port
    config['SERIAL']['BAUD_RATE'] = str(baud)
    save_config(config)
    
    success = init_com(port, baud)
    
    emit('com_port_updated', {
        'success': success,
        'port': port,
        'baud': baud,
        'msg': f"串口{port}@{baud}初始化{'成功' if success else '失败'}"
    }, namespace='/', broadcast=True)


@app.route('/api/list_audio_devices', methods=['GET'])
def api_list_audio_devices():
    try:
        devices = sd.query_devices()
        input_devices = []
        output_devices = []
        
        for i, d in enumerate(devices):
            dev_info = {
                'index': i,
                'name': d.get('name', f'Device#{i}'),
                'channels': d.get('max_input_channels', 0) if 'max_input_channels' in d else 0
            }
            if d.get('max_input_channels', 0) > 0:
                input_devices.append(dev_info)
            if d.get('max_output_channels', 0) > 0:
                output_devices.append(dev_info)
        
        return jsonify({
            'status': 'ok', 
            'input_devices': input_devices,
            'output_devices': output_devices
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@socketio.on('set_audio_device', namespace='/')
def handle_set_audio_device(data):
    device_in = data.get('device_in')
    device_out = data.get('device_out')
    
    if device_in is not None:
        config['AUDIO']['DEVICE_IN'] = str(device_in)
    if device_out is not None:
        config['AUDIO']['DEVICE_OUT'] = str(device_out)
    save_config(config)
    
    log(f"设置音频设备：输入={device_in} 输出={device_out}")
    
    success = restart_audio_streams()
    
    emit('audio_device_updated', {
        'success': success,
        'device_in': device_in,
        'device_out': device_out,
        'msg': f"音频设备设置{'成功' if success else '失败'}"
    }, namespace='/', broadcast=True)


@socketio.on('set_mic_gain', namespace='/')
def handle_set_mic_gain(data):
    global current_mic_gain_level
    level = int(data.get('level', 4))
    if level < 0 or level >= len(MIC_GAIN_LEVELS):
        level = 4
    current_mic_gain_level = level
    radio_state["mic_gain_level"] = level
    gain_pct = f"{int(MIC_GAIN_LEVELS[level] * 100)}%"
    log(f"麦克风增益已设置为：{gain_pct}")
    emit('mic_gain_updated', {
        'level': level,
        'gain': MIC_GAIN_LEVELS[level],
        'percent': gain_pct
    }, namespace='/', broadcast=True)


@socketio.on('rtt_ping', namespace='/')
def handle_rtt_ping(data):
    socketio.emit('rtt_pong', {'ts': data.get('ts', 0)}, namespace='/')


@app.route('/api/nb', methods=['POST'])
def api_nb():
    global nb_enabled
    try:
        enabled = request.json.get('enabled', False)
        send_radio("NB", "ON" if enabled else "OFF")
        radio_state['nb'] = enabled
        nb_enabled = enabled
        return jsonify({'status': 'ok', 'nb': enabled})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500


@app.route('/api/freq', methods=['POST'])
def api_freq():
    try:
        f = request.json.get('f')
        if f is None:
            return jsonify({'status': 'error', 'msg': 'missing f'}), 400
        try:
            f = float(f)
        except (TypeError, ValueError):
            return jsonify({'status': 'error', 'msg': 'invalid f'}), 400
        if not (0.030 <= f <= 999.999999):
            return jsonify({'status': 'error', 'msg': 'f out of range'}), 400
        f = round(f, 6)
        # FT-710 需要9位Hz数字，如14.270MHz = 014270000
        freq_hz = int(round(f * 1000000))
        freq_str = f"{freq_hz:09d}"
        send_radio("TXF", freq_str)
        radio_state['frequency'] = f
        return jsonify({'status': 'ok', 'frequency': f})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500


@app.route('/api/freq_adjust', methods=['POST'])
def api_freq_adjust():
    try:
        direction = request.json.get('dir', 'up')
        global freq_step
        with freq_lock:
            step = freq_step
        
        current_freq = radio_state['frequency']
        if direction == 'up':
            new_freq = current_freq + step
        else:
            new_freq = current_freq - step
        
        new_freq = max(0.030, min(54.0, new_freq))
        new_freq = round(new_freq, 3)
        
        # FT-710 需要9位Hz数字
        freq_hz = int(new_freq * 1000000)
        freq_str = f"{freq_hz:09d}"
        send_radio("TXF", freq_str)
        radio_state['frequency'] = new_freq
        
        return jsonify({'status': 'ok', 'frequency': new_freq})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500


@app.route('/api/set_step', methods=['POST'])
def api_set_step():
    try:
        step_type = request.json.get('step', '1kHz')
        global freq_step
        with freq_lock:
            if step_type == '1kHz':
                freq_step = 0.001
                radio_state['freq_step'] = '1kHz'
            elif step_type == '5kHz':
                freq_step = 0.005
                radio_state['freq_step'] = '5kHz'
            elif step_type == '10kHz':
                freq_step = 0.010
                radio_state['freq_step'] = '10kHz'
            elif step_type == '25kHz':
                freq_step = 0.025
                radio_state['freq_step'] = '25kHz'
        
        return jsonify({'status': 'ok', 'step': radio_state['freq_step']})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500


@app.route('/api/mode', methods=['POST'])
def api_mode():
    try:
        m = request.json.get('m', 'USB')

        mode_map = {
            'USB': '02',
            'LSB': '01',
            'CW': '03',
            'CWR': '09',
            'AM': '05',
            'FM': '04',
            'RTTY': '0C',
        }

        if m not in mode_map:
            return jsonify({'status': 'error', 'msg': f'unsupported mode: {m}'}), 400

        send_radio("MODE", mode_map[m])
        radio_state['mode'] = m
        return jsonify({'status': 'ok', 'mode': m})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500


@app.route('/api/power', methods=['POST'])
def api_set_power():
    try:
        power = int(request.json.get('power', 100))
        power = max(5, min(100, power))
        send_radio("POWER", f"{power:03d}")
        radio_state['power'] = power
        log(f"发射功率设置为: {power}%")
        return jsonify({"status": "ok", "power": power})
    except Exception as e:
        log(f"设置发射功率失败: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/ptt', methods=['POST'])
def api_ptt():
    global tx_start_time, _rx_start_seq, _tx_squelch_until, _ptt_owner_sid
    try:
        data = request.get_json()
        mode = data.get('mode', PTT_MODE)
        action = data.get('t', 'RX')

        if mode == 'latching' and action == 'TOGGLE':
            current_trx = radio_state.get('trx', 'RX')
            t = 'TX' if current_trx == 'RX' else 'RX'
        else:
            t = action if action in ['TX', 'RX'] else 'RX'

        # PTT保护：音频重启中禁止TX，RX放行
        if t == 'TX' and _audio_restarting.is_set():
            log("音频重启中，PTT TX被拦截")
            return jsonify({"status": "blocked", "trx": radio_state.get('trx', 'RX')}), 503

        # 先设状态，再发串口（即使串口失败状态也正确）
        if t == "TX":
            _ptt_owner_sid = None
            tx_active.set()
            tx_start_time = time.monotonic()
            _rx_start_seq = (_rx_start_seq + 1) % 65536
            _tx_squelch_until = time.monotonic() + 0.05
            _reset_ring_buffer()
            _drain_send_queue()
        else:
            tx_active.clear()
            _ptt_owner_sid = None
            _tx_squelch_until = time.monotonic() + 0.10
            _drain_send_queue()
            _reset_ring_buffer()
            with _dsp_state_lock:
                _rx_zi = _sosfilt_zi(_rx_sos) * 0.0
            _rx_start_seq = (_rx_start_seq + 1) % 65536
            my_seq = _rx_start_seq
            def _delayed_rx_start(seq):
                time.sleep(0.02)
                if seq != _rx_start_seq or tx_active.is_set():
                    return
                with _stream_lock:
                    if audio_input_stream and not audio_input_stream.active:
                        try:
                            audio_input_stream.start()
                        except Exception as e:
                            log(f"启动接收流失败：{e}")
            _threading.Thread(target=_delayed_rx_start, args=(my_seq,),
                              daemon=True, name='RxStart').start()

        radio_state['trx'] = t
        log(f"PTT切换：{t} (模式: {mode})")

        # 串口命令（失败不影响软件状态）
        send_radio("TRX", "1" if t == "TX" else "0")

        emit('trx_updated', {'trx': t}, namespace='/', broadcast=True)
        return jsonify({"status": "ok", "trx": t})
    except Exception as e:
        log(f"PTT HTTP切换失败：{e}")
        emit('trx_updated', {'trx': radio_state.get('trx', 'RX')}, namespace='/', broadcast=True)
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/ex', methods=['POST'])
def api_ex():
    """发送/读取EX菜单命令"""
    try:
        data = request.get_json()
        p1 = int(data.get('p1', 1))
        p2 = int(data.get('p2', 1))
        p3 = int(data.get('p3', 14))
        value = data.get('value', None)
        if value is None or value == "":
            # 读取
            resp = send_ex_menu(p1, p2, p3, delay=0.15)
        else:
            # 写入
            resp = send_ex_menu(p1, p2, p3, str(value), 0.15)
        return jsonify({"status": "ok", "cmd": f"EX{p1:02d}{p2:02d}{p3:02d}{value or ''};", "response": resp})
    except Exception as e:
        log(f"EX命令失败：{e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/state', methods=['GET'])
def api_state():
    return jsonify(radio_state)


@app.route('/api/channels', methods=['GET'])
def api_channels():
    channels_by_band = {}
    for key, value in config['CHANNELS'].items():
        parts = value.split(',')
        if len(parts) >= 3:
            freq = parts[0]
            mode = parts[1]
            label = parts[2]
            band = get_band_from_freq(float(freq))
            if band not in channels_by_band:
                channels_by_band[band] = []
            channels_by_band[band].append({
                'key': key,
                'freq': freq,
                'mode': mode,
                'label': label
            })
    for band in channels_by_band:
        channels_by_band[band].sort(key=lambda x: float(x['freq']))
    return jsonify({'status': 'ok', 'channels': channels_by_band})

@app.route('/api/save_channel', methods=['POST'])
def api_save_channel():
    try:
        data = request.get_json()
        freq = data.get('freq') if data else None
        mode = data.get('mode') if data else None

        if not freq:
            freq = radio_state.get('frequency', 0)
        if not mode:
            mode = radio_state.get('mode', 'USB')

        if mode not in ('USB', 'LSB', 'CW', 'CWR', 'AM', 'FM', 'RTTY'):
            return jsonify({'status': 'error', 'msg': f'unsupported mode: {mode}'}), 400
        try:
            freq = float(freq)
        except (TypeError, ValueError):
            return jsonify({'status': 'error', 'msg': 'invalid freq'}), 400
        if not (0.030 <= freq <= 999.999999):
            return jsonify({'status': 'error', 'msg': 'freq out of range'}), 400
        freq = round(freq, 6)

        band = get_band_from_freq(freq)
        if not band:
            return jsonify({'status': 'error', 'msg': _t('srv.channel.unsup')}), 400

        for existing_key, existing_value in config['CHANNELS'].items():
            parts = existing_value.split(',')
            if parts and abs(float(parts[0]) - freq) < 0.0001:
                return jsonify({'status': 'error', 'msg': _t('srv.channel.dup', f=freq)}), 400

        existing_keys = [k for k in config['CHANNELS'].keys() if k.startswith(f'ch_{band}_')]
        existing_nums = []
        for k in existing_keys:
            try:
                existing_nums.append(int(k.split('_')[-1]))
            except:
                pass
        next_num = max(existing_nums) + 1 if existing_nums else 1

        ch_key = f'ch_{band}_{next_num}'
        config['CHANNELS'][ch_key] = f'{freq},{mode},'
        save_config(config)

        return jsonify({'status': 'ok', 'channel': {'key': ch_key, 'freq': freq, 'mode': mode, 'band': band}})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500

@app.route('/api/set_channel', methods=['POST'])
def api_set_channel():
    try:
        freq = request.json.get('freq')
        mode = request.json.get('mode', 'USB')

        if not freq:
            return jsonify({'status': 'error', 'msg': _t('srv.channel.empty')}), 400

        mode_map = {
            'USB': '02',
            'LSB': '01',
            'CW': '03',
            'CWR': '09',
            'AM': '05',
            'FM': '04',
            'RTTY': '0C',
        }
        if mode not in mode_map:
            return jsonify({'status': 'error', 'msg': f'unsupported mode: {mode}'}), 400
        mode_code = mode_map[mode]

        try:
            f = float(freq)
        except (TypeError, ValueError):
            return jsonify({'status': 'error', 'msg': 'invalid freq'}), 400
        if not (0.030 <= f <= 999.999999):
            return jsonify({'status': 'error', 'msg': 'freq out of range'}), 400
        f = round(f, 6)
        send_radio("MODE", mode_code)
        freq_hz = int(round(f * 1000000))
        freq_str = f"{freq_hz:09d}"
        send_radio("TXF", freq_str)
        send_radio("RXF", freq_str)
        radio_state['frequency'] = f
        radio_state['mode'] = mode

        return jsonify({'status': 'ok', 'frequency': f, 'mode': mode})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500

@app.route('/api/delete_channel', methods=['POST'])
def api_delete_channel():
    try:
        key = request.json.get('key', '')
        if key and key in config['CHANNELS']:
            del config['CHANNELS'][key]
            save_config(config)
            return jsonify({'status': 'ok', 'channel_deleted': True})
        return jsonify({'status': 'error', 'msg': _t('srv.channel.none')}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500

@app.route('/api/update_channel', methods=['POST'])
def api_update_channel():
    try:
        key = request.json.get('key', '')
        new_freq = request.json.get('freq')
        if not key or key not in config['CHANNELS']:
            return jsonify({'status': 'error', 'msg': _t('srv.channel.none')}), 400
        parts = config['CHANNELS'][key].split(',')
        freq = parts[0] if parts else ''
        mode = parts[1] if len(parts) > 1 else 'USB'
        label = parts[2] if len(parts) > 2 else ''
        if new_freq:
            try:
                f = float(new_freq)
            except (TypeError, ValueError):
                return jsonify({'status': 'error', 'msg': 'invalid freq'}), 400
            if not (0.030 <= f <= 999.999999):
                return jsonify({'status': 'error', 'msg': 'freq out of range'}), 400
            band = get_band_from_freq(f)
            if band:
                freq = f
        config['CHANNELS'][key] = f'{freq},{mode},{label}'
        save_config(config)
        return jsonify({'status': 'ok', 'channel': {'key': key, 'freq': freq, 'mode': mode}})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500

def get_band_from_freq(freq_mhz):
    if 1.8 <= freq_mhz <= 2.0:
        return "160m"
    elif 3.5 <= freq_mhz <= 4.0:
        return "80m"
    elif 7.0 <= freq_mhz <= 7.5:
        return "40m"
    elif 14.0 <= freq_mhz <= 14.35:
        return "20m"
    elif 21.0 <= freq_mhz <= 21.45:
        return "15m"
    elif 28.0 <= freq_mhz <= 30.0:
        return "10m"
    return None

@app.route('/api/audio_restart', methods=['POST'])
def api_audio_restart():
    if tx_active.is_set():
        return jsonify({'status': 'error', 'msg': _t('srv.tx.no_restart')}), 400
    try:
        result = restart_audio_streams()
        return jsonify({'status': 'ok' if result else 'error'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500


@app.route('/api/video_toggle', methods=['POST'])
def api_video_toggle():
    """切换视频流"""
    try:
        enabled = request.json.get('enabled')
        
        if enabled:
            success = start_video_stream()
            return jsonify({'status': 'ok' if success else 'error', 'enabled': True})
        else:
            stop_video_stream()
            return jsonify({'status': 'ok', 'enabled': False})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500


@app.route('/api/video_config', methods=['POST'])
def api_video_config():
    """配置视频设备"""
    try:
        config['VIDEO']['ENABLED'] = 'true' if request.json.get('enabled') else 'false'
        config['VIDEO']['DEVICE_INDEX'] = str(request.json.get('device_index', 0))
        config['VIDEO']['WIDTH'] = str(request.json.get('width', 640))
        config['VIDEO']['HEIGHT'] = str(request.json.get('height', 480))
        config['VIDEO']['FPS'] = str(request.json.get('fps', 15))
        config['VIDEO']['QUALITY'] = str(request.json.get('quality', 15))
        save_config(config)
        
        # 重启视频流
        stop_video_stream()
        if config['VIDEO'].get('ENABLED', 'false') == 'true':
            start_video_stream()
        
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500


# ===================== Web UI =====================
@app.after_request
def _gzip_and_etag(resp):
    """仅对 text/html 页面做 gzip 压缩 + ETag 304 协商缓存。
    刻意不碰 /socket.io 轮询与 /api/* ：304/压缩会破坏 engineio 长轮询
    与 JS 轮询调用的 JSON 解析，只对整页首次加载收益最大的场景启用。"""
    if (resp.status_code != 200
            or not resp.content_type
            or resp.content_type.split(';')[0].strip().lower() != 'text/html'):
        return resp
    try:
        data = resp.get_data()
    except Exception:
        return resp
    if not data:
        return resp
    import hashlib
    etag = '"' + hashlib.md5(data).hexdigest() + '"'
    resp.headers['ETag'] = etag
    if request.headers.get('If-None-Match') == etag:
        resp.set_data(b'')
        resp.status_code = 304
        return resp
    if 'gzip' in (request.headers.get('Accept-Encoding') or ''):
        import gzip
        try:
            data = gzip.compress(data)
        except Exception:
            data = None
        if data:
            resp.set_data(data)
            resp.headers['Content-Encoding'] = 'gzip'
            resp.headers['Vary'] = 'Accept-Encoding'
    return resp


@app.route('/')
def page():
    _ips = get_all_network_ips()
    ip = _ips[0] if _ips else socket.gethostbyname(socket.gethostname())
    return f'''
<!DOCTYPE html>
<html lang="{LANG}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<title>FT-710 Remote Controller</title>
<script>
let UI_LANG = '{LANG}';
const I18N = {json.dumps(I18N, ensure_ascii=False)};
function T(key, ...args) {{
    let v = (I18N[key] || {{}})[UI_LANG] || (I18N[key] || {{}}).zh || key;
    if (typeof v !== 'string') v = String(v);
    args.forEach(a => {{ try {{ v = v.replace('{{}}', String(a)); }} catch(e) {{}} }});
    return v;
}}
function applyI18n() {{
    document.documentElement.lang = UI_LANG;
    document.title = T('ws.title');
    document.querySelectorAll('[data-i18n]').forEach(el => {{
        el.textContent = T(el.getAttribute('data-i18n'));
    }});
    document.querySelectorAll('[data-i18n-ph]').forEach(el => {{
        el.placeholder = T(el.getAttribute('data-i18n-ph'));
    }});
    document.querySelectorAll('[data-i18n-title]').forEach(el => {{
        el.title = T(el.getAttribute('data-i18n-title'));
    }});
    document.querySelectorAll('[data-i18n-alt]').forEach(el => {{
        el.alt = T(el.getAttribute('data-i18n-alt'));
    }});
}}
document.addEventListener('DOMContentLoaded', applyI18n);
</script>
<style>
:root {{
  --glass-bg:        rgba(255,255,255,0.55);
  --glass-bg2:       rgba(255,255,255,0.35);
  --glass-border:    rgba(255,255,255,0.70);
  --glass-shadow:    rgba(80,120,200,0.18);
  --blur:            18px;
  --accent:          #1a7fcb;
  --accent2:         #0a5fa0;
  --accent-glow:     rgba(26,127,203,0.25);
  --text:            #1a2a3a;
  --text2:           #3a5a7a;
  --text3:           #7a9aba;
  --bg-grad-a:       #c8dff5;
  --bg-grad-b:       #e8f0fb;
  --bg-grad-c:       #d0eaff;
  --ok:              #0ea371;
  --err:             #e03535;
  --radius:          18px;
  --radius-sm:       12px;
}}

* {{ box-sizing:border-box; margin:0; padding:0; }}

body {{
  min-height:100vh;
  background: linear-gradient(135deg, var(--bg-grad-a) 0%, var(--bg-grad-b) 50%, var(--bg-grad-c) 100%);
  background-attachment: fixed;
  color: var(--text);
    font-family: -apple-system, "SF Pro Display", "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
  padding: 10px 8px 40px;
  -webkit-font-smoothing: antialiased;
  overflow-x: hidden;
}}

html {{ scroll-behavior: smooth; }}

body::before {{
  content:''; position:fixed; top:-120px; right:-80px;
  width:420px; height:420px;
  background: radial-gradient(circle, rgba(100,180,255,0.28) 0%, transparent 70%);
  pointer-events:none; z-index:0;
}}
body::after {{
  content:''; position:fixed; bottom:-100px; left:-60px;
  width:360px; height:360px;
  background: radial-gradient(circle, rgba(160,210,255,0.22) 0%, transparent 70%);
  pointer-events:none; z-index:0;
}}

.card {{
  background: var(--glass-bg);
  backdrop-filter: blur(var(--blur)) saturate(1.6);
  -webkit-backdrop-filter: blur(var(--blur)) saturate(1.6);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius);
  box-shadow: 0 8px 32px var(--glass-shadow), inset 0 1px 0 rgba(255,255,255,0.8);
  padding: 14px 14px;
  max-width: 420px;
  margin: 0 auto;
  position: relative;
  z-index: 1;
}}

.title-container {{ margin-bottom: 10px; }}

.main-title {{
  font-size: 19px;
  font-weight: 700;
  background: linear-gradient(90deg, var(--accent2), #3ab0ff);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  letter-spacing: 0.5px;
}}

.subtitle {{
  font-size: 11px;
  color: var(--text3);
  margin-top: 2px;
  letter-spacing: 1px;
}}

.status-box {{
  background: var(--glass-bg2);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius-sm);
  padding: 9px 12px;
  margin-bottom: 10px;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.6);
}}

.status-row {{
  display: flex;
  align-items: center;
  margin: 3px 0;
  font-size: 13px;
  gap: 4px;
}}

.status-label {{ color: var(--text3); font-size: 12px; min-width: 36px; }}
.status-value {{ color: var(--text); font-weight: 600; font-size: 13px; margin-right: 10px; }}

.freq-value {{
  font-size: 26px;
  font-weight: 800;
  background: linear-gradient(90deg, var(--accent2), #1a9fff);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  letter-spacing: 1px;
  line-height: 1;
}}

.freq-control-container {{
  background: var(--glass-bg2);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius-sm);
  padding: 10px 10px 8px;
  margin-bottom: 10px;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.6);
}}

.freq-input-group {{ display:flex; gap:6px; align-items:center; margin-bottom:7px; }}

.freq-input {{
  flex:1; padding:8px; font-size:16px; font-weight:700;
  text-align:center;
  background: rgba(255,255,255,0.7);
  backdrop-filter: blur(8px);
  border: 1px solid var(--glass-border);
  border-radius: 10px;
  color: var(--accent2);
  outline: none;
  box-shadow: inset 0 2px 6px rgba(100,160,220,0.10);
}}
.freq-input:focus {{
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-glow), inset 0 2px 6px rgba(100,160,220,0.10);
}}

.freq-btn {{
  width:40px; height:40px;
  background: rgba(255,255,255,0.65);
  border: 1px solid var(--glass-border);
  border-radius: 10px;
  font-size: 16px;
  cursor: pointer;
  color: var(--accent);
  box-shadow: 0 2px 8px rgba(80,140,200,0.15);
  transition: all 0.15s;
}}
.freq-btn:active {{ background: rgba(26,127,203,0.15); transform: scale(0.94); }}

.confirm-btn {{
  width:100%; padding:8px;
  background: linear-gradient(135deg, var(--accent) 0%, #3aabff 100%);
  color:#fff; border:none; border-radius:10px;
  font-size:14px; font-weight:600; cursor:pointer;
  box-shadow: 0 4px 16px var(--accent-glow);
  margin-bottom:7px;
  transition: all 0.15s;
  letter-spacing: 0.3px;
}}
.confirm-btn:active {{ transform:scale(0.97); opacity:0.9; }}

.step-btns, .mode-btns {{ display:flex; gap:5px; margin-bottom:6px; }}

.step-btn, .mode-btn {{
  flex:1; padding:7px 4px;
  background: rgba(255,255,255,0.55);
  border: 1px solid var(--glass-border);
  border-radius: 9px;
  font-size:13px; font-weight:500;
  cursor:pointer; color:var(--text2);
  box-shadow: 0 2px 6px rgba(80,140,200,0.10);
  transition: all 0.15s;
}}
.step-btn:active, .mode-btn:active {{ transform: scale(0.95); }}
.step-btn.active, .mode-btn.active {{
  background: linear-gradient(135deg, var(--accent) 0%, #3aabff 100%);
  color:#fff; border-color:transparent;
  box-shadow: 0 4px 14px var(--accent-glow);
}}

.ptt-btn {{
  width:100%; padding:18px;
  background: linear-gradient(135deg, rgba(255,255,255,0.6), rgba(220,235,255,0.6));
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1.5px solid var(--glass-border);
  border-radius: var(--radius);
  font-size:20px; font-weight:800; letter-spacing:2px;
  color: var(--text2);
  cursor:pointer;
  box-shadow: 0 6px 24px rgba(80,140,200,0.18), inset 0 1px 0 rgba(255,255,255,0.9);
  margin-bottom: 0;
  transition: all 0.12s;
  user-select:none; -webkit-user-select:none;
}}
.ptt-btn:disabled {{ opacity:0.5; cursor:not-allowed; }}
.ptt-btn.tx {{
  background: linear-gradient(135deg, rgba(255,60,60,0.75), rgba(255,120,80,0.65));
  border-color: rgba(255,100,80,0.6);
  color:#fff;
  box-shadow: 0 6px 28px rgba(255,60,60,0.40), inset 0 1px 0 rgba(255,200,200,0.5);
  animation: ptt-pulse 0.9s ease-in-out infinite;
}}
@keyframes ptt-pulse {{
  0%,100% {{ box-shadow: 0 6px 28px rgba(255,60,60,0.40), inset 0 1px 0 rgba(255,200,200,0.5); }}
  50%      {{ box-shadow: 0 6px 40px rgba(255,60,60,0.70), inset 0 1px 0 rgba(255,200,200,0.5); }}
}}

.activate-btn {{
  background: rgba(255,255,255,0.60) !important;
  color: var(--accent2) !important;
  border: 1px solid var(--glass-border) !important;
  box-shadow: 0 2px 10px rgba(80,140,200,0.12) !important;
}}
.activate-btn.active {{
  background: linear-gradient(135deg, var(--ok), #2dd4a0) !important;
  color:#fff !important; border-color:transparent !important;
}}
.restart-btn {{
  padding:8px; width:100%;
  background: rgba(255,255,255,0.55);
  border: 1px solid var(--glass-border);
  border-radius:10px; font-size:14px; font-weight:600;
  cursor:pointer; color:var(--text2);
  box-shadow: 0 2px 8px rgba(80,140,200,0.10);
  transition: all 0.15s;
}}
.restart-btn:active {{ transform:scale(0.97); }}

.ptt-mode-selector {{
  display:flex; align-items:center;
  margin-top:10px; padding:6px 8px;
  background: rgba(255,255,255,0.5);
  border-radius: 10px;
  border: 1px solid var(--glass-border);
}}
.ptt-mode-label {{ font-size:11px; font-weight:600; color:var(--text2); flex:1; }}
.ptt-mode-btn {{
  padding:4px 12px; font-size:11px; font-weight:600;
  border:1px solid var(--glass-border); border-radius:12px;
  background: rgba(255,255,255,0.6); color:var(--text2);
  cursor:pointer; transition: all 0.15s; flex:1;
}}
.ptt-mode-btn.active {{ background: var(--accent); color:#fff; border-color:var(--accent); }}
.ptt-mode-btn:active {{ transform:scale(0.95); }}
.ptt-status {{
  font-size:11px; font-weight:700;
  padding:4px 10px; border-radius:12px;
  flex:1; text-align:center;
}}
.ptt-status.tx {{ background:#e03535; color:#fff; }}
.ptt-status.rx {{ background:#0ea371; color:#fff; }}

.level-control {{
  background: var(--glass-bg2);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius-sm);
  padding: 9px 10px;
  margin-top: 8px;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.6);
}}

.level-title {{
  font-size:12px; color:var(--text2);
  margin-bottom:6px; font-weight:500;
  display:flex; align-items:center; gap:6px;
}}

.level-value {{
  margin-left:auto;
  background: linear-gradient(90deg, var(--accent), #3aabff);
  -webkit-background-clip:text; -webkit-text-fill-color:transparent;
  background-clip:text;
  font-weight:700; font-size:13px;
}}

.level-buttons {{ display:flex; gap:5px; }}

.level-btn {{
  flex:1; padding:6px 2px;
  background: rgba(255,255,255,0.55);
  border: 1px solid var(--glass-border);
  border-radius:8px; font-size:12px; font-weight:500;
  cursor:pointer; color:var(--text2);
  transition: all 0.15s;
}}
.level-btn:active {{ transform:scale(0.94); }}
.level-btn.active {{
  background: linear-gradient(135deg, var(--accent), #3aabff);
  color:#fff; border-color:transparent;
  box-shadow: 0 3px 12px var(--accent-glow);
}}

.level-slider {{
  -webkit-appearance:none; appearance:none;
  width:100%; height:6px; border-radius:3px;
  background: linear-gradient(90deg, var(--accent), #3aabff);
  outline:none; margin:8px 0 2px; cursor:pointer;
}}
.level-slider::-webkit-slider-thumb {{
  -webkit-appearance:none; appearance:none;
  width:18px; height:18px; border-radius:50%;
  background:#fff; border:2px solid var(--accent);
  box-shadow:0 2px 6px var(--accent-glow); cursor:pointer;
}}
.level-slider::-moz-range-thumb {{
  width:18px; height:18px; border-radius:50%;
  background:#fff; border:2px solid var(--accent);
  box-shadow:0 2px 6px var(--accent-glow); cursor:pointer;
}}
.level-ticks {{
  display:flex; justify-content:space-between;
  font-size:10px; color:var(--text3); margin-top:2px;
}}

/* 神经网络降噪（DeepFilterNet3） */
.sw {{
  position:relative; box-sizing:border-box; flex:none; width:48px; height:26px;
  border:1px solid var(--glass-border); border-radius:13px;
  background:rgba(120,130,140,.30); cursor:pointer;
  transition:background .2s, box-shadow .2s;
}}
.sw-knob {{
  position:absolute; top:2px; left:2px; box-sizing:border-box;
  width:20px; height:20px; border-radius:50%; background:#fff;
  box-shadow:0 1px 4px rgba(0,0,0,.35); transition:left .18s;
}}
.sw.on {{ background:linear-gradient(135deg,var(--accent),#3aabff); border-color:transparent; box-shadow:0 3px 12px var(--accent-glow); }}
.sw.on .sw-knob {{ left:24px; }}
.sw.loading {{ animation:swPulse 1s ease-in-out infinite; }}
@keyframes swPulse {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:.35; }} }}
.sw:active {{ transform:scale(0.96); }}
.sw-text {{ font-size:12px; color:var(--text2); opacity:.55; cursor:pointer; user-select:none; transition:color .15s, opacity .15s; }}
.sw-text.active {{ opacity:1; color:var(--accent); font-weight:700; }}
.ai-hint {{ flex:2; font-size:11px; color:var(--text2); opacity:.7; align-self:center; }}
.ai-row {{ display:flex; align-items:center; gap:10px; }}
.ai-atten-row {{ margin-top:8px; transition:opacity .15s; }}
.ai-atten-row.disabled {{ opacity:.45; pointer-events:none; }}

.status-freq-row {{
  display: flex;
  align-items: baseline;
  justify-content: center;
  gap: 8px;
  padding: 4px 0 8px;
}}
.status-freq-label {{ font-size: 11px; color: var(--text3); font-weight: 500; align-self: center; }}
.status-freq-unit {{ font-size: 14px; color: var(--text3); font-weight: 500; align-self: center; }}

.status-grid {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
  margin-bottom: 8px;
}}
.status-cell {{
  background: rgba(255,255,255,0.45);
  border: 1px solid var(--glass-border);
  border-radius: 10px;
  padding: 6px 10px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.7);
}}
.sc-label {{ font-size: 10px; color: var(--text3); font-weight: 500; letter-spacing: 0.5px; text-transform: uppercase; }}
.sc-value {{ font-size: 16px; font-weight: 700; color: var(--accent2); letter-spacing: 0.5px; }}

.status-divider {{ height: 1px; background: rgba(150,190,230,0.35); margin: 0 0 7px; }}

.status-info-row {{ display: flex; gap: 6px; }}
.si-item {{ flex: 1; font-size: 11px; color: var(--text3); background: rgba(255,255,255,0.45); border-radius: 6px; padding: 4px 6px; text-align: center; white-space: nowrap; letter-spacing: 0.3px; box-shadow: inset 0 1px 2px rgba(0,0,0,0.04); }}

.copyright {{ font-size:11px; color:var(--text3); margin-top:12px; padding-top:8px; border-top: 1px solid rgba(150,190,230,0.3); letter-spacing:0.5px; }}

.video-container {{
  background: var(--glass-bg);
  backdrop-filter: blur(var(--blur));
  border: 1px solid var(--glass-border);
  border-radius: var(--radius);
  margin: 10px auto;
  max-width: 100%;
  width: 100%;
  overflow: hidden;
  text-align: center;
}}

.video-header {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 12px;
  background: var(--glass-bg2);
  font-size: 13px;
  font-weight: 600;
}}

.video-close {{
  background: none;
  border: none;
  font-size: 16px;
  cursor: pointer;
  color: var(--text2);
  padding: 4px 8px;
}}

.video-toggle-btn {{
  position: fixed;
  bottom: 20px;
  right: 20px;
  width: 50px;
  height: 50px;
  border-radius: 50%;
  background: linear-gradient(135deg, var(--accent) 0%, #3aabff 100%);
  border: none;
  color: white;
  font-size: 20px;
  cursor: pointer;
  box-shadow: 0 4px 16px var(--accent-glow);
  z-index: 1000;
}}

.video-toggle-btn-long {{
  width: 100%;
  padding: 10px 20px;
  border-radius: 8px;
  background: linear-gradient(135deg, var(--accent) 0%, #3aabff 100%);
  border: none;
  color: white;
  font-size: 14px;
  cursor: pointer;
  box-shadow: 0 4px 16px var(--accent-glow);
}}

.video-toggle-row {{
  padding: 10px 15px 15px;
}}

/* ── 信道选择栏 ── */
.channel-bar {{
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
  padding: 8px 10px;
  background: var(--glass-bg2);
  border-radius: var(--radius-sm);
  border: 1px solid var(--glass-border);
  align-items: center;
}}
.channel-label {{
  padding: 6px 10px;
  background: var(--accent);
  color: #fff;
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-weight: 600;
}}
.channel-dropdown {{
  flex: 1;
  position: relative;
}}
.channel-dropdown-trigger {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 6px 10px;
  background: #dee8f0;
  border: 1px solid #b0c4d8;
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: 14px;
  color: var(--text);
}}
.channel-dropdown-trigger:hover {{
  background: #d0dce8;
}}
.channel-dropdown-arrow {{
  font-size: 10px;
  color: var(--text2);
}}
.channel-dropdown-list {{
  display: none;
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  right: 0;
  background: #dee8f0;
  border: 1px solid #b0c4d8;
  border-radius: var(--radius-sm);
  max-height: 250px;
  overflow-y: auto;
  z-index: 1000;
  box-shadow: 0 4px 12px rgba(0,0,0,0.15);
}}
.channel-dropdown-list.show {{
  display: block;
}}
.channel-dropdown-list .channel-band-header {{
  padding: 4px 10px;
  background: #c8d8e8;
  color: var(--accent2);
  font-size: 12px;
  font-weight: bold;
}}
.channel-dropdown-list .channel-item {{
  padding: 6px 10px;
  cursor: pointer;
  border-bottom: 1px solid #c8d8e8;
  font-size: 13px;
  color: var(--text);
}}
.channel-dropdown-list .channel-item:hover {{
  background: #c0d0e0;
}}
.channel-dropdown-list .channel-item:last-child {{
  border-bottom: none;
}}
.channel-dropdown-empty {{
  padding: 10px;
  text-align: center;
  color: var(--text2);
  font-size: 13px;
}}
.save-channel-btn {{
  padding: 8px 12px;
  background: linear-gradient(135deg, #28a745 0%, #34ce57 100%);
  color: #fff;
  border: none;
  border-radius: 8px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  box-shadow: 0 2px 8px rgba(40, 167, 69, 0.4);
}}
.save-channel-btn:hover {{
  background: linear-gradient(135deg, #218838 0%, #2bc94a 100%);
}}
.save-channel-btn:active {{
  transform: scale(0.97);
  opacity: 0.9;
}}
.channel-action-btn {{
  padding: 4px 8px;
  background: rgba(255,255,255,0.55);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: 14px;
}}
.channel-action-btn:hover {{
  background: rgba(255,255,255,0.75);
}}
/* ── 信道管理弹窗 ── */
.channel-manager-overlay {{
  position: fixed;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  background: rgba(0,0,0,0.5);
  z-index: 1999;
}}
.channel-manager {{
  position: fixed;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: 90%;
  max-width: 400px;
  max-height: 70vh;
  background: #dee8f0;
  border: 1px solid #b0c4d8;
  border-radius: var(--radius);
  z-index: 2000;
  overflow: hidden;
}}
.channel-manager-header {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  background: var(--accent);
  color: #fff;
  font-weight: bold;
}}
.channel-manager-header button {{
  background: none;
  border: none;
  color: #fff;
  font-size: 20px;
  cursor: pointer;
}}
.channel-list {{
  padding: 10px;
  max-height: calc(70vh - 50px);
  overflow-y: auto;
}}
.channel-item {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px;
  margin-bottom: 6px;
  background: rgba(255,255,255,0.45);
  border-radius: var(--radius-sm);
  border: 1px solid var(--glass-border);
}}
.channel-item-info {{
  flex: 1;
  cursor: pointer;
}}
.channel-item-name {{
  font-weight: bold;
  color: var(--text);
}}
.channel-item-freq {{
  font-size: 12px;
  color: var(--text3);
}}
.channel-item-btns button {{
  margin-left: 8px;
  padding: 4px 8px;
  background: rgba(255,255,255,0.55);
  border: 1px solid var(--glass-border);
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
}}
.channel-menu button {{
  display: block;
  width: 100%;
  padding: 8px 16px;
  background: #444;
  border: none;
  color: #fff;
  cursor: pointer;
  text-align: left;
  font-size: 14px;
}}
.channel-menu button:hover {{
  background: #555;
}}
/* ── 保存信道对话框 ── */
.save-channel-dialog-overlay {{
  position: fixed;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  background: rgba(0,0,0,0.5);
  z-index: 1999;
}}
.save-channel-dialog {{
  position: fixed;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: 90%;
  max-width: 350px;
  background: #dee8f0;
  border: 1px solid #b0c4d8;
  border-radius: var(--radius);
  z-index: 2000;
  padding: 16px;
}}
.save-channel-dialog h3 {{
  margin: 0 0 12px;
  color: var(--accent2);
  font-size: 16px;
}}
.save-channel-dialog input {{
  width: 100%;
  padding: 8px 10px;
  border: 1px solid var(--glass-border);
  border-radius: 8px;
  font-size: 14px;
  margin-bottom: 10px;
  background: rgba(255,255,255,0.7);
  color: var(--text);
}}
.save-channel-dialog .dialog-btns {{
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}}
.save-channel-dialog .dialog-btns button {{
  padding: 6px 16px;
  border: none;
  border-radius: 8px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
}}
.save-channel-dialog .dialog-btns .cancel-btn {{
  background: rgba(255,255,255,0.55);
  color: var(--text2);
}}
.save-channel-dialog .dialog-btns .ok-btn {{
  background: linear-gradient(135deg, var(--accent), #3aabff);
  color: #fff;
}}

/* 移动端/窄屏自适应 */
@media (max-width: 560px) {{
  body {{ padding: 6px; }}
  .card {{ padding: 12px 10px; border-radius: 14px; }}
  .main-title {{ font-size: 20px; }}
  .freq-value {{ font-size: 34px; }}
  .status-grid {{ grid-template-columns: 1fr 1fr; }}
  .level-slider {{ height: 8px; }}
  .level-slider::-webkit-slider-thumb {{ width: 22px; height: 22px; }}
  .level-btn {{ font-size: 11px; }}
}}
/* 宽屏居中收敛，避免卡片被拉得过宽 */
@media (min-width: 820px) {{
  .card {{ max-width: 680px; margin-left: auto; margin-right: auto; }}
}}
/* 主控制区：竖屏单列；横屏左右两列平均分配（左＝状态+增益/降噪/功率，右＝视频+频率/信道/PTT） */
.board {{ display: flex; flex-direction: column; }}
.board-col {{ display: contents; }}
.status-box {{ order: 1; }}
.video-container {{ order: 2; }}
.channel-bar {{ order: 3; }}
.freq-control-container {{ order: 4; }}
#ptt_btn {{ order: 5; }}
.ptt-mode-selector {{ order: 6; }}
.ctrl-row {{ order: 7; }}
.level-control {{ order: 8; }}
@media (orientation: landscape) and (min-width: 720px) {{
  .card {{ max-width: 1120px; }}
  .board {{ display: grid; grid-template-columns: 1fr 1fr; column-gap: 16px; align-items: start; }}
  .board-col {{ display: flex; flex-direction: column; gap: 12px; }}
  /* 两列模块间距统一交给 gap，避免左右两列首个模块因外边距不同而顶部错位 */
  .board-col > * {{ margin-top: 0 !important; margin-bottom: 0 !important; }}
}}
</style>
</head>
<body>
<div class="card">
<div class="title-container">
    <h1 class="main-title">FT-710 Remote Controller</h1>
    <div class="subtitle">Designed by BI4IXO</div>
</div>

<div class="board">
<div class="board-col board-left">
<div class="status-box">
    <div class="status-freq-row">
        <span class="status-freq-label" data-i18n="ws.status-freq">频率</span>
        <span class="freq-value" id="freq">14.270</span>
        <span class="status-freq-unit">MHz</span>
    </div>
    <div class="status-grid">
        <div class="status-cell">
            <span class="sc-label" data-i18n="ws.label-mode">模式</span>
            <span class="sc-value" id="mode">USB</span>
        </div>
        <div class="status-cell">
            <span class="sc-label" data-i18n="ws.label-tuner">天调</span>
            <span class="sc-value" id="tuner-status-bar">OFF</span>
        </div>
        <span class="sc-value" id="freq-step" style="display:none">1kHz</span>
    </div>
    <div class="status-divider"></div>
    <div class="status-info-row">
        <span class="si-item">🔌 <span id="com-status">{COM_PORT}@{BAUD_RATE}</span></span>
        <span class="si-item">🔗 <span id="socket-status" data-i18n="ws.socket.disc">未连接</span></span>
    </div>
    <div class="status-info-row" style="margin-top:2px;">
        <span class="si-item" id="rtt-item">⏱ <span id="rtt-status">-- ms</span></span>
        <span class="si-item">🌐 <span id="access-ip">{ip}:{SERVER_PORT}</span></span>
    </div>
    <span id="audio-status" style="display:none" data-i18n="ws.audio.init">未激活</span>
    <div class="video-toggle-row">
        <button class="video-toggle-btn-long" onclick="toggleVideo(true)" id="video-toggle-btn"
                data-i18n="ws.video.screen" data-i18n-title="ws.video.screen.title"
                title="显示电台屏幕">📷 电台屏幕</button>
    </div>
</div>

<div class="level-control">
    <div class="level-title"><span data-i18n="ws.gain.rx">🔊 接收增益</span><span class="level-value" id="gain-status">1.0x</span></div>
    <input type="range" class="level-slider" id="gain-slider" min="0" max="18" step="1" value="2" oninput="setGainLevel(parseInt(this.value))">
    <div class="level-ticks"><span>0.5x</span><span>2.75x</span><span>5.0x</span></div>
</div>

<div class="level-control">
    <div class="level-title"><span data-i18n="ws.gain.mic">🎙️ 麦克风增益</span><span class="level-value" id="mic-gain-status">200%</span></div>
    <input type="range" class="level-slider" id="mic-gain-slider" min="0" max="4" step="1" value="4" oninput="setMicGainLevel(parseInt(this.value))">
    <div class="level-ticks"><span>100%</span><span>150%</span><span>200%</span></div>
</div>

<div class="level-control">
    <div class="level-title"><span data-i18n="ws.ai.title">✨ 神经网络降噪</span></div>
    <div class="ai-row">
        <span class="sw-text active" id="ai-lbl-off" onclick="setAiDenoise(false)" data-i18n="ws.ai.off">关闭</span>
        <div class="sw" id="ai-switch" onclick="toggleAiDenoise()" title="神经网络降噪(DeepFilterNet3)"><span class="sw-knob"></span></div>
        <span class="sw-text" id="ai-lbl-on" onclick="setAiDenoise(true)" data-i18n="ws.ai.on">开启</span>
        <span class="ai-hint" data-i18n="ws.ai.hint">DFN3 · 开启后谱减法暂停</span>
    </div>
    <div class="ai-atten-row disabled" id="ai-atten-row">
        <div class="level-title" style="margin-bottom:2px;"><span data-i18n="ws.ai.atten">降噪强度</span><span class="level-value" id="ai-atten-val">-12dB</span></div>
        <input type="range" class="level-slider" id="ai-atten-slider" min="6" max="30" step="1" value="12" oninput="onAiParamInput()" disabled>
    </div>
</div>

<div class="level-control" id="noise-control">
    <div class="level-title"><span data-i18n="ws.noise">🔇 DSP降噪</span><span class="level-value" id="noise-status">关闭</span></div>
    <input type="range" class="level-slider" id="noise-slider" min="0" max="5" step="1" value="0" oninput="setNoiseLevel(parseInt(this.value))">
    <div class="level-ticks"><span>关闭</span><span>3级</span><span>5级</span></div>
</div>

<div class="level-control" style="display:none;">
    <div class="level-title"><span data-i18n="ws.tuner.title">📡 天线调谐器</span><span class="level-value" id="tuner-status">OFF</span></div>
    <div class="level-buttons">
        <button class="level-btn tuner-toggle" onclick="toggleTuner()" data-i18n="ws.tuner.toggle">开关</button>
        <button class="level-btn tuner-start" 
            ontouchstart="startTune(event)" 
            ontouchend="stopTune(event)"
            onmousedown="startTune(event)"
            onmouseup="stopTune(event)"
            onmouseleave="stopTune(event)" data-i18n="ws.tuner.start">🔄 调谐</button>
    </div>
</div>

<div class="level-control">
    <div class="level-title"><span data-i18n="ws.power">⚡ 发射功率</span><span class="level-value" id="power-status">25%</span></div>
    <div class="level-buttons">
        <button class="level-btn power-0" onclick="setPower(10)">10%</button>
        <button class="level-btn power-1" onclick="setPower(25)">25%</button>
        <button class="level-btn power-2" onclick="setPower(50)">50%</button>
        <button class="level-btn power-3" onclick="setPower(75)">75%</button>
        <button class="level-btn power-4" onclick="setPower(100)">100%</button>
    </div>
</div>
</div>
<div class="board-col board-right">
<div class="video-container" id="video-container" style="display:none;">
    <div class="video-header">
        <span data-i18n="ws.video.screen">📷 电台屏幕</span>
        <button class="video-close" onclick="toggleVideo(false)">✕</button>
    </div>
    <video id="video-display" autoplay playsinline muted style="width:100%; height:auto; object-fit:fill; background:#000; display:block;" 
         alt="等待视频信号" data-i18n-alt="js.video.wait">
    <div id="video-status" style="padding:5px; text-align:center; font-size:12px; color:#666; background:#eee;" data-i18n="js.video.wait">等待视频信号...</div>
</div>

<!-- 信道选择区 -->
<div class="channel-bar">
    <span class="channel-label" data-i18n="ws.channel">信道</span>
    <div class="channel-dropdown" id="channel-dropdown">
        <div class="channel-dropdown-trigger" id="channel-dropdown-trigger" onclick="toggleChannelDropdown()">
            <span id="channel-current-label" data-i18n="ws.channel.none">-- 信道 --</span>
            <span class="channel-dropdown-arrow">▼</span>
        </div>
        <div class="channel-dropdown-list" id="channel-dropdown-list"></div>
    </div>
    <button class="channel-action-btn" onclick="showChannelManager()">📋</button>
</div>

<!-- 信道管理弹窗 -->
<div id="channel-manager-overlay" class="channel-manager-overlay" style="display:none;" onclick="closeChannelManager()"></div>
<div id="channel-manager" class="channel-manager" style="display:none;">
    <div class="channel-manager-header">
        <span data-i18n="ws.chan.manage">信道管理</span>
        <div>
            <button onclick="showAddChannelDialog()" style="background:none;border:none;color:#fff;font-size:16px;cursor:pointer;margin-right:8px;" data-i18n-title="ws.chan.add" title="添加信道">➕</button>
            <button onclick="closeChannelManager()">✕</button>
        </div>
    </div>
    <div id="channel-list" class="channel-list"></div>
</div>

<!-- 添加信道对话框 -->
<div id="add-channel-dialog-overlay" class="save-channel-dialog-overlay" style="display:none;" onclick="closeAddChannelDialog()"></div>
<div id="add-channel-dialog" class="save-channel-dialog" style="display:none;">
    <h3 data-i18n="ws.chan.add">添加信道</h3>
    <div style="margin-bottom:10px;">
        <label style="font-size:12px;color:var(--text2);" data-i18n="ws.chan.freq">频率 (MHz):</label>
        <input type="text" id="add-ch-freq" data-i18n-ph="ws.chan.ph" placeholder="如 14.270" style="width:100%;padding:8px;border:1px solid var(--glass-border);border-radius:8px;font-size:14px;margin-top:4px;">
    </div>
    <div style="margin-bottom:10px;">
        <label style="font-size:12px;color:var(--text2);" data-i18n="ws.chan.mode">模式:</label>
        <select id="add-ch-mode" style="width:100%;padding:8px;border:1px solid var(--glass-border);border-radius:8px;font-size:14px;margin-top:4px;">
            <option value="USB">USB</option>
            <option value="LSB">LSB</option>
            <option value="AM">AM</option>
            <option value="FM">FM</option>
        </select>
    </div>
    <div class="dialog-btns">
        <button class="cancel-btn" onclick="closeAddChannelDialog()" data-i18n="ws.chan.cancel">取消</button>
        <button class="ok-btn" onclick="doAddChannel()" data-i18n="ws.chan.confirm">添加</button>
    </div>
</div>

<div class="freq-control-container">
    <div class="freq-input-group">
        <button class="freq-btn" onclick="adjustFreq('down')">➖</button>
        <input type="text" id="freq_in" value="14.270" class="freq-input" data-i18n-ph="ws.freq.ph" placeholder="频率" onkeydown="handleFreqKeyDown(event)">
        <button class="freq-btn" onclick="adjustFreq('up')">➕</button>
    </div>
    <button class="confirm-btn" onclick="setFreq()" data-i18n="ws.btn.confirm">✅ 确认频率</button>
    <div class="step-btns">
        <button class="step-btn active" id="step-1khz" onclick="setStep('1kHz')">1kHz</button>
        <button class="step-btn" id="step-5khz" onclick="setStep('5kHz')">5kHz</button>
        <button class="step-btn" id="step-10khz" onclick="setStep('10kHz')">10kHz</button>
        <button class="step-btn" id="step-25khz" onclick="setStep('25kHz')">25kHz</button>
    </div>
    <div class="mode-btns">
        <button class="mode-btn" onclick="setMode('USB')">USB</button>
        <button class="mode-btn" onclick="setMode('LSB')">LSB</button>
        <button class="mode-btn" onclick="setMode('AM')">AM</button>
        <button class="mode-btn" onclick="setMode('FM')">FM</button>
    </div>
</div>

<button id="ptt_btn" class="ptt-btn" disabled ontouchstart="ptt_on(event)" ontouchend="ptt_off(event)" ontouchcancel="ptt_off(event)" onmousedown="ptt_on(event)" onmouseup="ptt_off(event)" onmouseleave="ptt_off(event)" data-i18n="ws.btn.ptt">PTT 按住说话</button>

<div class="ptt-mode-selector">
    <span class="ptt-mode-label">PTT</span>
    <button class="ptt-mode-btn active" id="ptt-momentary" onclick="setPttMode('momentary')">Hold</button>
    <button class="ptt-mode-btn" id="ptt-latching" onclick="setPttMode('latching')">Tap</button>
    <span class="ptt-status" id="ptt-status">🔴 RX</span>
</div>

<div class="ctrl-row" style="display:flex; gap:8px; margin-top:10px;">
    <button id="activate-audio" class="activate-btn confirm-btn" style="flex:1;" data-i18n="ws.btn.act">🎧 激活音频</button>
    <button class="restart-btn" onclick="restartAudio()" style="flex:1; margin-top:0;" data-i18n="ws.btn.restart">🔄 重启音频</button>
</div>

</div>
</div>

<div class="copyright">
    Copyright &copy; 2026 BI4IXO. All rights reserved.
</div>

</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.min.js"></script>
<script>
let socket;
let audioContext;
let micStream;
let isPTTHeld = false;
let isAudioReady = false;
let freqStep = 0.001;
let isFreqInputEditing = false;
let currentGainLevel = 2;
let currentNoiseLevel = 0;
let currentMicGainLevel = 0; // 默认100%
let currentPttMode = 'momentary';
let currentTrx = 'RX';
let pttRxRetry = null;
let pttRxRetryCount = 0;
let pttTxRetry = null;
let pttTxRetryCount = 0;
const MIC_GAIN_LEVELS = [1.00, 1.25, 1.50, 1.75, 2.00];
const MIC_GAIN_LABELS = ['100%','125%','150%','175%','200%'];
const GAIN_LABELS = ['0.5x','0.75x','1.0x','1.25x','1.5x','1.75x','2.0x','2.25x','2.5x','2.75x','3.0x','3.25x','3.5x','3.75x','4.0x','4.25x','4.5x','4.75x','5.0x'];
let currentAiDenoise = false; // 神经网络降噪开关（DeepFilterNet3）
let aiLoading = false;        // 后台加载中
let currentAiAttenLim = 12;   // 衰减上限dB
const SAMPLING_RATE = {SAMPLING_RATE};
const AUDIO_BLOCK_SIZE = {AUDIO_BLOCK_SIZE};
const SERVER_PORT = {SERVER_PORT};
const el = document.getElementById('access-ip');
if (el) el.textContent = window.location.hostname + ':' + SERVER_PORT;
let channelData = {{}};

function initSocket() {{
    socket = io(window.location.origin, {{
            reconnection: true,
            reconnectionAttempts: Infinity,
            reconnectionDelay: 2000,
        transports: ['polling', 'websocket'],
        upgrade: true,
        upgradeTimeout: 15000,
        timeout: 10000,
        rememberUpgrade: true
    }});

    socket.on('connect', () => {{
        console.log('SocketIO连接成功, transport:', socket.io.engine.transport.name);
        try {{ socket.io.engine.transport.ws && (socket.io.engine.transport.ws.binaryType = 'arraybuffer'); }} catch(e) {{}}
        document.getElementById('socket-status').textContent = T('js.connected');
        document.getElementById('socket-status').style.color = '#20c997';
    }});

    socket.io.engine.on('upgrade', (transport) => {{
        console.log('Transport 升级到:', transport.name);
        try {{ transport.ws && (transport.ws.binaryType = 'arraybuffer'); }} catch(e) {{}}
    }});
    
    socket.on('connect_ack', (data) => {{
        console.log('服务器连接成功：', data);
        if (!isFreqInputEditing) {{
            document.getElementById('freq').textContent = data.frequency.toFixed(3);
            document.getElementById('freq_in').value = data.frequency.toFixed(3);
        }}
        document.getElementById('freq-step').textContent = data.freq_step;
        document.getElementById('com-status').textContent = `${{data.com_port}}@${{data.baud_rate}}`;
        
        updateStepButtons(data.freq_step);
        if (data.freq_step === '1kHz') freqStep = 0.001;
        else if (data.freq_step === '5kHz') freqStep = 0.005;
        else if (data.freq_step === '10kHz') freqStep = 0.010;
        else if (data.freq_step === '25kHz') freqStep = 0.025;
        
        updateModeButtons(data.mode);
        
        currentGainLevel = data.gain_level;
        updateGainButtons();
        document.getElementById('gain-status').textContent = `${{data.gain_value}}x`;
        
        if (data.mic_gain_level !== undefined) {{
            currentMicGainLevel = data.mic_gain_level;
            updateMicGainButtons();
            document.getElementById('mic-gain-status').textContent = MIC_GAIN_LABELS[data.mic_gain_level];
        }}
        
        if (data.tuner !== undefined) updateTunerUI(data.tuner);
        
        currentNoiseLevel = data.noise_level;
        updateNoiseButtons();
        document.getElementById('noise-status').textContent = data.noise_status;

        if (data.ai_denoise !== undefined) {{
            currentAiDenoise = data.ai_denoise;
            updateAiUI();
        }}
        if (data.ai_atten_lim_db !== undefined) {{
            currentAiAttenLim = data.ai_atten_lim_db;
            syncAiParamSliders();
        }}
        
        if (data.ptt_mode !== undefined) {{
            currentPttMode = data.ptt_mode;
            updatePttModeButtons();
        }}
        if (data.trx !== undefined) {{
            currentTrx = data.trx;
            updatePttStatus();
        }}
        if (data.power !== undefined) {{
            document.getElementById('power-status').textContent = data.power + '%';
            const powerLevels = [10, 25, 50, 75, 100];
            document.querySelectorAll('.power-0, .power-1, .power-2, .power-3, .power-4').forEach((btn, idx) => {{
                btn.classList.remove('active');
                if (powerLevels[idx] === data.power) btn.classList.add('active');
            }});
        }}
    }});
    
    socket.on('gain_level_updated', (data) => {{
        currentGainLevel = data.level;
        updateGainButtons();
        document.getElementById('gain-status').textContent = data.percent;
    }});
    
    socket.on('noise_level_updated', (data) => {{
        currentNoiseLevel = data.level;
        updateNoiseButtons();
        document.getElementById('noise-status').textContent = data.status;
    }});
    
    socket.on('mic_gain_updated', (data) => {{
        currentMicGainLevel = data.level;
        updateMicGainButtons();
        document.getElementById('mic-gain-status').textContent = data.percent;
    }});
    
    socket.on('ai_denoise_updated', (data) => {{
        if (data.loading !== undefined) aiLoading = data.loading;
        if (data.enabled !== undefined) {{
            currentAiDenoise = data.enabled;
            aiLoading = false;
        }}
        if (data.error !== undefined) setAiHint(String(data.error), true);
        if (data.circuit_open) setAiHint(T('ws.ai.circuit'), true);
        updateAiUI();
    }});
    
    socket.on('ai_denoise_params_updated', (data) => {{
        if (data.atten_lim_db !== undefined) {{
            currentAiAttenLim = data.atten_lim_db;
            syncAiParamSliders();
        }}
    }});
    
    socket.on('ptt_mode_updated', (data) => {{
        currentPttMode = data.mode;
        updatePttModeButtons();
    }});
    
    socket.on('trx_updated', (data) => {{
        currentTrx = data.trx;
        if (data.trx === 'RX') {{
            if (pttRxRetry) clearTimeout(pttRxRetry);
            pttRxRetry = null;
            pttRxRetryCount = 0;
        }} else if (data.trx === 'TX') {{
            if (pttTxRetry) clearTimeout(pttTxRetry);
            pttTxRetry = null;
            pttTxRetryCount = 0;
        }}
        updatePttStatus();
    }});
    
    let _webrtc_rtt_timer = null;
    let _rtt_webrtc_ok = false;

    async function _measureWebRTCRTT() {{
        if (!webrtc_pc) return;
        try {{
            const stats = await webrtc_pc.getStats();
            for (const report of stats.values()) {{
                if (report.type === 'candidate-pair' && report.state === 'succeeded') {{
                    const r = report.currentRoundTripTime !== undefined ? report.currentRoundTripTime : report.googRtt;
                    if (r !== undefined && r !== null) {{
                        const rtt = Math.round(r * 1000);
                        const el = document.getElementById('rtt-status');
                        if (el) {{
                            el.textContent = rtt + ' ms';
                            el.style.color = rtt < 100 ? '#20c997' : rtt < 300 ? '#ffc107' : '#dc3545';
                        }}
                        _rtt_webrtc_ok = true;
                        return;
                    }}
                }}
            }}
        }} catch(e) {{ console.error('getStats:', e); }}
    }}

    // WebSocket RTT回退
    socket.on('rtt_pong', (data) => {{
        const rtt = Date.now() - data.ts;
        const el = document.getElementById('rtt-status');
        if (el && !_rtt_webrtc_ok) {{
            el.textContent = rtt + ' ms';
            el.style.color = rtt < 100 ? '#20c997' : rtt < 300 ? '#ffc107' : '#dc3545';
        }}
    }});
    setInterval(() => {{
        socket.emit('rtt_ping', {{ts: Date.now()}});
    }}, 5000);

    function _startWebRTCRTT() {{
        _measureWebRTCRTT();
        if (_webrtc_rtt_timer) clearInterval(_webrtc_rtt_timer);
        _webrtc_rtt_timer = setInterval(_measureWebRTCRTT, 5000);
    }}
    
    socket.on('audio_failed', (data) => {{
        const el = document.getElementById('audio-status');
        el.textContent = T('js.soundcard.fail');
        el.style.color = '#dc3545';
    }});
    
    socket.on('video_status_updated', (data) => {{
        const status = document.getElementById('video-status');
        if (!status) return;
        if (data && data.ok) {{
            status.textContent = T('js.video.streaming');
            status.style.color = '#28a745';
        }} else {{
            status.textContent = T('js.video.stopped');
            status.style.color = '#dc3545';
        }}
    }});
    
    socket.on('disconnect', () => {{
        console.warn('SocketIO连接断开');
        document.getElementById('socket-status').textContent = T('js.disconnected');
        document.getElementById('socket-status').style.color = '#ffc107';
    }});
    
    socket.on('connect_error', (err) => {{
        console.error('SocketIO连接失败：', err);
        document.getElementById('socket-status').textContent = T('js.connect.fail');
        document.getElementById('socket-status').style.color = '#dc3545';
    }});

    socket.on('set_lang', (data) => {{
        console.log('语言切换:', data.code);
        UI_LANG = data.code;
        applyI18n();
        updatePttStatus();
        const pttBtn = document.getElementById("ptt_btn");
        if (pttBtn) {{ pttBtn.innerText = T('ws.btn.ptt'); }}
    }});
}}

function handleFreqKeyDown(event) {{
    if (event.key === 'Enter' || event.keyCode === 13) {{
        event.preventDefault();
        setFreq();
        const input = document.getElementById('freq_in');
        input.blur();
        input.setAttribute('readonly', true);
        setTimeout(() => {{ input.removeAttribute('readonly'); }}, 100);
        isFreqInputEditing = false;
    }}
}}

function setGainLevel(level) {{
    if (!socket || !socket.connected) {{ alert(T('js.alert.activate_first')); return; }}
    socket.emit('set_gain_level', {{level: level}});
}}

function updateGainButtons() {{
    const slider = document.getElementById('gain-slider');
    if (slider) slider.value = currentGainLevel;
    const st = document.getElementById('gain-status');
    if (st && GAIN_LABELS[currentGainLevel] !== undefined) st.textContent = GAIN_LABELS[currentGainLevel];
}}

function setTuner(state) {{
    fetch('/api/tuner', {{ method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{state: state}}) }})
    .then(r => r.json()).then(d => {{ if (d.status === 'ok') updateTunerUI(state); }}).catch(e => console.error('天调切换失败:', e));
}}

let tunerState = 'OFF';
let isTuning = false;

function toggleTuner() {{
    tunerState = tunerState === 'ON' ? 'OFF' : 'ON';
    const cmd = tunerState === 'ON' ? '1' : '0';
    fetch('/api/tuner', {{ 
        method: 'POST', 
        headers: {{'Content-Type': 'application/json'}}, 
        body: JSON.stringify({{state: cmd}}) 
    }}).then(r => r.json()).then(d => {{ 
        if (d.status === 'ok') updateTunerUI(tunerState);
    }}).catch(e => console.error('天调切换失败:', e));
}}

function startTune(e) {{
    e.preventDefault();
    isTuning = true;
    // 长按启动调谐，电台会自动完成
    fetch('/api/tune_start', {{ method: 'POST' }})
    .then(r => r.json())
    .then(d => {{
        if (d.status === 'ok') {{
            document.querySelector('.tuner-start').textContent = T('js.tuner.tuning');
            document.querySelector('.tuner-start').classList.add('active');
        }}
    }}).catch(e => console.error('天调启动失败:', e));
}}

function stopTune(e) {{
    e.preventDefault();
    // 调谐是自动完成的，松开更新UI
    setTimeout(() => {{
        document.querySelector('.tuner-start').textContent = T('ws.tuner.start');
        document.querySelector('.tuner-start').classList.remove('active');
        isTuning = false;
    }}, 500);
}}

function updateTunerUI(state) {{
    const el = document.getElementById('tuner-status');
    if (el) el.textContent = state === 'ON' ? 'ON' : 'OFF';
    const bar = document.getElementById('tuner-status-bar');
    if (bar) {{ bar.textContent = state === 'ON' ? 'ON' : 'OFF'; bar.style.color = state === 'ON' ? '#22a050' : ''; }}
}}

function setPower(power) {{
    fetch('/api/power', {{ method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{power: power}}) }})
    .then(r => r.json()).then(d => {{ 
        if (d.status === 'ok') {{
            document.getElementById('power-status').textContent = power + '%';
            document.querySelectorAll('.power-0, .power-1, .power-2, .power-3, .power-4').forEach((btn, idx) => {{
                btn.classList.remove('active');
            }});
            const powerLevels = [10, 25, 50, 75, 100];
            const idx = powerLevels.indexOf(power);
            if (idx >= 0) document.querySelector('.power-' + idx).classList.add('active');
        }}
    }}).catch(e => console.error('功率设置失败:', e));
}}

function setNoiseLevel(level) {{
    if (!socket || !socket.connected) {{ alert(T('js.alert.activate_first')); return; }}
    socket.emit('set_noise_level', {{level: level}});
}}

function updateNoiseButtons() {{
    const slider = document.getElementById('noise-slider');
    if (slider) slider.value = currentNoiseLevel;
}}

// 神经网络降噪（DeepFilterNet3，与1-5级谱减法互斥）
function setAiHint(text, isError) {{
    const hint = document.querySelector('.ai-hint');
    if (!hint) {{ return; }}
    if (text) {{
        hint.textContent = text;
        hint.style.color = isError ? '#cc2222' : '';
    }} else {{
        hint.textContent = T('ws.ai.hint');
        hint.style.color = '';
    }}
}}
function setAiDenoise(on) {{
    if (on === currentAiDenoise && !(aiLoading && !on)) {{ return; }}
    if (!socket || !socket.connected) {{ alert(T('js.alert.activate_first')); return; }}
    socket.emit('set_ai_denoise', {{enabled: on}});
}}
function toggleAiDenoise() {{ setAiDenoise(!currentAiDenoise); }}

function updateAiUI() {{
    const on = currentAiDenoise;
    const sw = document.getElementById('ai-switch');
    if (sw) {{ sw.classList.toggle('on', on); sw.classList.toggle('loading', aiLoading); }}
    const lo = document.getElementById('ai-lbl-off');
    const ln = document.getElementById('ai-lbl-on');
    if (lo) {{ lo.classList.toggle('active', !on); }}
    if (ln) {{ ln.classList.toggle('active', on); }}
    const ar = document.getElementById('ai-atten-row');
    if (ar) {{ ar.classList.toggle('disabled', !on); }}
    const asl = document.getElementById('ai-atten-slider');
    if (asl) {{ asl.disabled = !on; }}
    // 互斥：AI 开启时禁用谱减法滑块
    const nc = document.getElementById('noise-control');
    if (nc) {{
        nc.style.opacity = on ? '0.45' : '';
        nc.style.pointerEvents = on ? 'none' : '';
    }}
}}

let _aiParamTimer = null;
function onAiParamInput() {{
    const attenEl = document.getElementById('ai-atten-slider');
    if (!attenEl) {{ return; }}
    currentAiAttenLim = parseInt(attenEl.value, 10);
    syncAiParamLabels();
    if (!socket || !socket.connected) {{ return; }}
    if (_aiParamTimer) {{ clearTimeout(_aiParamTimer); }}
    const atten = currentAiAttenLim;
    _aiParamTimer = setTimeout(() => {{
        socket.emit('set_ai_denoise_params', {{atten_lim_db: atten}});
    }}, 150);
}}

function syncAiParamLabels() {{
    const a = document.getElementById('ai-atten-val');
    if (a) {{ a.textContent = '-' + Math.round(currentAiAttenLim) + 'dB'; }}
}}

function syncAiParamSliders() {{
    const a = document.getElementById('ai-atten-slider');
    if (a) {{ a.value = String(Math.round(currentAiAttenLim)); }}
    syncAiParamLabels();
}}

function setPttMode(mode) {{
    if (!socket || !socket.connected) {{ alert(T('js.alert.activate_first')); return; }}
    socket.emit('set_ptt_mode', {{mode: mode}});
}}

function updatePttModeButtons() {{
    document.getElementById('ptt-momentary').classList.remove('active');
    document.getElementById('ptt-latching').classList.remove('active');
    if (currentPttMode === 'momentary') document.getElementById('ptt-momentary').classList.add('active');
    else document.getElementById('ptt-latching').classList.add('active');
}}

function updatePttStatus() {{
    const statusEl = document.getElementById('ptt-status');
    if (currentTrx === 'TX') {{ statusEl.textContent = '🟢 TX'; statusEl.className = 'ptt-status tx'; }}
    else {{ statusEl.textContent = '🔴 RX'; statusEl.className = 'ptt-status rx'; }}
}}

function setMicGainLevel(level) {{
    currentMicGainLevel = level;
    updateMicGainButtons();
    document.getElementById('mic-gain-status').textContent = MIC_GAIN_LABELS[level];
    if (socket && socket.connected) socket.emit('set_mic_gain', {{level: level}});
}}

function updateMicGainButtons() {{
    const slider = document.getElementById('mic-gain-slider');
    if (slider) slider.value = currentMicGainLevel;
    const st = document.getElementById('mic-gain-status');
    if (st && MIC_GAIN_LABELS[currentMicGainLevel] !== undefined) st.textContent = MIC_GAIN_LABELS[currentMicGainLevel];
}}

// ===================== WebRTC =====================
let webrtc_pc = null;
let webrtc_remoteStream = null;
let _webrtc_audioEl = null;
let _webrtc_playSrcIOS = null;
let _webrtc_playGainIOS = null;
let _webrtc_reconnectTimer = null;
let _webrtc_reconnectAttempts = 0;
        const _WEBRTC_MAX_RECONNECT = Infinity;

const _IOS_PLAY_BOOST = 1.8;   // iOS 部件状态栏音量下低于其他设备，Web Audio 增益补偿

function _isIOS() {{
    return /iPad|iPhone|iPod/.test(navigator.userAgent) ||
           (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
}}

function _detachRemoteIOS() {{
    try {{ if (_webrtc_playSrcIOS) _webrtc_playSrcIOS.disconnect(); }} catch(e) {{}}
    try {{ if (_webrtc_playGainIOS) _webrtc_playGainIOS.disconnect(); }} catch(e) {{}}
    _webrtc_playSrcIOS = null;
    _webrtc_playGainIOS = null;
}}

function _attachRemoteAudio(stream) {{
    if (!_isIOS()) {{
        if (!_webrtc_audioEl) {{
            _webrtc_audioEl = document.createElement('audio');
            _webrtc_audioEl.id = 'webrtc-audio';
            _webrtc_audioEl.autoplay = true;
            document.body.appendChild(_webrtc_audioEl);
        }}
        _webrtc_audioEl.srcObject = stream;
        _webrtc_audioEl.play().catch(e => console.warn('音频自动播放被阻止，需用户交互:', e));
        return;
    }}
    // iOS：HTMLAudio 播放在 iOS 明显偏小，改走 Web Audio + GainNode 增益补偿
    window.AudioContext = window.AudioContext || window.webkitAudioContext;
    if (!audioContext || audioContext.state === 'closed') {{
        audioContext = new AudioContext({{ sampleRate: SAMPLING_RATE, latencyHint: 'interactive' }});
    }}
    if (audioContext.state === 'suspended') {{
        audioContext.resume().catch(e => console.warn('iOS AudioContext 未激活:', e));
    }}
    _detachRemoteIOS();   // 重连/换流时断开旧接线
    _webrtc_playSrcIOS = audioContext.createMediaStreamSource(stream);
    _webrtc_playGainIOS = audioContext.createGain();
    _webrtc_playGainIOS.gain.value = _IOS_PLAY_BOOST;
    _webrtc_playSrcIOS.connect(_webrtc_playGainIOS);
    _webrtc_playGainIOS.connect(audioContext.destination);
}}

function _webrtc_scheduleReconnect() {{
    if (_webrtc_reconnectTimer) clearTimeout(_webrtc_reconnectTimer);
    if (_webrtc_reconnectAttempts >= _WEBRTC_MAX_RECONNECT) {{
        console.error('WebRTC 重连已达上限');
        return;
    }}
    _webrtc_reconnectAttempts++;
    const delay = Math.min(1000 * Math.pow(2, _webrtc_reconnectAttempts - 1), 30000);
    console.log(`WebRTC ${{delay/1000}}秒后重连 (第${{_webrtc_reconnectAttempts}}次)`);
    _webrtc_reconnectTimer = setTimeout(() => {{
        _webrtc_reconnectTimer = null;
        initWebRTC();
    }}, delay);
}}

async function initWebRTC() {{
    // 清理旧连接
    if (webrtc_pc) {{
        try {{ webrtc_pc.close(); }} catch(e) {{}}
        webrtc_pc = null;
    }}
    if (_webrtc_reconnectTimer) {{
        clearTimeout(_webrtc_reconnectTimer);
        _webrtc_reconnectTimer = null;
    }}
    try {{
        const config = {{
            iceServers: [
                {{urls: "stun:stun.miwifi.com:3478"}},
                {{urls: "stun:stun.chat.bilibili.com:3478"}},
                {{urls: "stun:stun.cloudflare.com:3478"}},
                {{urls: "stun:stun.l.google.com:19302"}}
            ]
        }};
        webrtc_pc = new RTCPeerConnection(config);
        
        // 发送 ICE candidates
        webrtc_pc.onicecandidate = (event) => {{
            if (event.candidate) {{
                fetch('/webrtc/ice', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify(event.candidate.toJSON())
                }}).catch(e => console.error('ICE发送失败:', e));
            }}
        }};
        
        // ICE 连接状态
        webrtc_pc.oniceconnectionstatechange = (event) => {{
            console.log('WebRTC ICE状态:', webrtc_pc.iceConnectionState);
            // disconnected 是临时的（RTP 短暂停顿/网络抖动可自愈），不应立即重建连接，
            // 否则会形成"服务端关pc->前端重连->又关"的死循环。仅 failed/closed 才重连。
            if (webrtc_pc.iceConnectionState === 'failed' ||
                webrtc_pc.iceConnectionState === 'closed') {{
                console.warn('WebRTC 连接断开，尝试重连...');
                _webrtc_scheduleReconnect();
            }}
        }};
        
        // 获取本地麦克风（只需要音频，不需要摄像头）
        const localStream = await navigator.mediaDevices.getUserMedia({{ 
            audio: {{ sampleRate: 16000, channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false }},
            video: false
        }});
        
        // 只添加音频track（浏览器→服务端）
        localStream.getAudioTracks().forEach(track => {{
            webrtc_pc.addTrack(track, localStream);
        }});
        
        // 添加视频接收端（确保 offer 包含 video m= 段，修复 SDP 方向不匹配）
        webrtc_pc.addTransceiver('video', {{ direction: 'recvonly' }});
        
        // 接收远程音视频
        webrtc_pc.ontrack = (event) => {{
            console.log('WebRTC 收到轨道:', event.track.kind);
            webrtc_remoteStream = event.streams[0];
            
            if (event.track.kind === 'audio') {{
                _attachRemoteAudio(webrtc_remoteStream);
            }}
            if (event.track.kind === 'video') {{
                const videoEl = document.getElementById('video-display');
                if (videoEl) {{
                    videoEl.srcObject = webrtc_remoteStream;
                    const container = document.getElementById('video-container');
                    if (container && container.style.display !== 'none') {{
                        videoEl.play();
                    }}
                }}
            }}
        }};
        
        // 创建Offer
        const offer = await webrtc_pc.createOffer();
        await webrtc_pc.setLocalDescription(offer);
        
        // 发送到服务器
        const response = await fetch('/webrtc/offer', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{
                sdp: webrtc_pc.localDescription.sdp,
                type: webrtc_pc.localDescription.type
            }})
        }});
        
        const result = await response.json();
        await webrtc_pc.setRemoteDescription(new RTCSessionDescription({{
            sdp: result.sdp,
            type: result.type
        }}));
        
        console.log('WebRTC 连接成功');
        document.getElementById('audio-status').textContent = T('js.connecting');
        _webrtc_reconnectAttempts = 0;
        _startWebRTCRTT();
        
    }} catch(e) {{
        console.error('WebRTC连接失败：', e);
        document.getElementById('audio-status').textContent = T('js.webrtc.fail');
    }}
}}

async function _doActivateAudio() {{
    document.getElementById('audio-status').textContent = T('js.activating');
    document.getElementById('audio-status').style.color = '#ffc107';

    if (!socket) initSocket();

    micStream = await navigator.mediaDevices.getUserMedia({{ 
        audio: {{ sampleRate: {{ ideal: SAMPLING_RATE }}, channelCount: {{ ideal: 1 }}, echoCancellation: false, noiseSuppression: false, autoGainControl: false, latency: 0.01 }},
        video: false
    }});

    window.AudioContext = window.AudioContext || window.webkitAudioContext;
    if (!audioContext || audioContext.state === 'closed') {{
        audioContext = new AudioContext({{ sampleRate: SAMPLING_RATE, latencyHint: 'interactive' }});
    }}
    if (audioContext.state === 'suspended') await audioContext.resume();

    isAudioReady = true;
    document.getElementById('audio-status').textContent = T('js.usb.ready');
    document.getElementById('audio-status').style.color = '#20c997';
    document.getElementById('ptt_btn').disabled = false;
    document.getElementById('activate-audio').style.display = 'none';

    await initWebRTC();
}}

document.getElementById('activate-audio').addEventListener('click', async () => {{
    try {{
        await _doActivateAudio();
    }} catch (e) {{
        console.error('音频激活失败：', e);
        document.getElementById('audio-status').textContent = T('js.activate.fail', e.message);
        document.getElementById('audio-status').style.color = '#dc3545';
        alert(T('js.alert.activate_full', e.message));
    }}
}});

async function adjustFreq(dir) {{
    if (!socket || !socket.connected) {{ alert(T('js.alert.activate_first')); return; }}
    try {{
        const res = await fetch(`/api/freq_adjust`, {{ method:"POST", headers:{{"Content-Type":"application/json"}}, body:JSON.stringify({{dir: dir}}) }});
        const data = await res.json();
        if (data.status === 'ok') {{
            document.getElementById('freq').textContent = data.frequency.toFixed(3);
            if (!isFreqInputEditing) document.getElementById('freq_in').value = data.frequency.toFixed(3);
        }}
    }} catch (e) {{ console.error('调节频率失败：', e); }}
}}

async function setStep(step) {{
    try {{
        const res = await fetch(`/api/set_step`, {{ method:"POST", headers:{{"Content-Type":"application/json"}}, body:JSON.stringify({{step: step}}) }});
        const data = await res.json();
        if (data.status === 'ok') {{
            document.getElementById('freq-step').textContent = data.step;
            document.getElementById('step-1khz').classList.remove('active');
            document.getElementById('step-5khz').classList.remove('active');
            document.getElementById('step-10khz').classList.remove('active');
            document.getElementById('step-25khz').classList.remove('active');
            if (step === '1kHz') {{ freqStep = 0.001; document.getElementById('step-1khz').classList.add('active'); }}
            else if (step === '5kHz') {{ freqStep = 0.005; document.getElementById('step-5khz').classList.add('active'); }}
            else if (step === '10kHz') {{ freqStep = 0.010; document.getElementById('step-10khz').classList.add('active'); }}
            else if (step === '25kHz') {{ freqStep = 0.025; document.getElementById('step-25khz').classList.add('active'); }}
        }}
    }} catch (e) {{ console.error('设置步进失败：', e); }}
}}

function updateStepButtons(step) {{
    document.getElementById('step-1khz').classList.remove('active');
    document.getElementById('step-5khz').classList.remove('active');
    document.getElementById('step-10khz').classList.remove('active');
    document.getElementById('step-25khz').classList.remove('active');
    if (step === '1kHz') document.getElementById('step-1khz').classList.add('active');
    else if (step === '5kHz') document.getElementById('step-5khz').classList.add('active');
    else if (step === '10kHz') document.getElementById('step-10khz').classList.add('active');
    else if (step === '25kHz') document.getElementById('step-25khz').classList.add('active');
}}

async function setMode(m) {{
    await fetch(`/api/mode`, {{ method:"POST", headers:{{"Content-Type":"application/json"}}, body:JSON.stringify({{m:m}}) }});
    updateModeButtons(m);
}}

function updateModeButtons(mode) {{
    const modeBtns = document.querySelectorAll('.mode-btn');
    modeBtns.forEach(btn => {{ btn.classList.remove('active'); if (btn.textContent.trim() === mode) btn.classList.add('active'); }});
}}

async function setFreq() {{
    const fStr = document.getElementById('freq_in').value.trim();
    if (!fStr) {{ alert(T('js.alert.freq1')); return; }}
    const f = parseFloat(fStr);
    if (isNaN(f) || f < 0.030 || f > 54) {{ alert(T('js.alert.freq2')); return; }}
    try {{
        await fetch(`/api/freq`, {{ method:"POST", headers:{{"Content-Type":"application/json"}}, body:JSON.stringify({{f:f}}) }});
        document.getElementById('freq').textContent = f.toFixed(3);
        isFreqInputEditing = false;
        document.getElementById('freq_in').blur();
    }} catch (e) {{ console.error('设置频率失败：', e); }}
}}

document.getElementById('freq_in').addEventListener('focus', () => {{ isFreqInputEditing = true; }});
document.getElementById('freq_in').addEventListener('blur', () => {{
    isFreqInputEditing = false;
    let val = document.getElementById('freq_in').value;
    if (val) {{ const num = parseFloat(val); if (!isNaN(num) && num >= 0.030 && num <= 54) document.getElementById('freq_in').value = num.toFixed(3); }}
    document.activeElement.blur();
}});

async function restartAudio() {{
    document.getElementById('audio-status').textContent = T('js.restarting');
    document.getElementById('audio-status').style.color = '#ffc107';
    isAudioReady = false;
    if (audioContext) {{ audioContext.close(); audioContext = null; }}
    if (micStream) {{ micStream.getTracks().forEach(t => t.stop()); micStream = null; }}
    if (webrtc_pc) {{ try {{ webrtc_pc.close(); }} catch(e) {{}} webrtc_pc = null; }}
    if (_webrtc_reconnectTimer) {{ clearTimeout(_webrtc_reconnectTimer); _webrtc_reconnectTimer = null; }}
if (_webrtc_audioEl) {{
        _webrtc_audioEl.srcObject = null;
    }}
    _detachRemoteIOS();
    isPTTHeld = false;
    try {{
        await fetch(`/api/audio_restart`, {{method:"POST"}});
        document.getElementById('audio-status').textContent = T('js.restart.done');
        document.getElementById('activate-audio').style.display = 'block';
        document.getElementById('ptt_btn').disabled = true;
        document.getElementById('ptt_btn').classList.remove('tx');
        document.getElementById('ptt_btn').innerText = T('ws.btn.ptt');
    }} catch(e) {{ document.getElementById('audio-status').textContent = T('js.restart.fail', e.message); document.getElementById('audio-status').style.color = '#dc3545'; document.getElementById('activate-audio').style.display = 'block'; }}
}}

function ptt_on(e) {{
    e.preventDefault();
    if (!isAudioReady) {{ alert(T('js.alert.no_audio')); return; }}
    if (isPTTHeld) return;
    isPTTHeld = true;
    if (audioContext && audioContext.state === 'suspended') audioContext.resume();
    if (currentPttMode === 'latching') togglePtt();
    else doPttTx();
}}

function ptt_off(e) {{
    if (e) e.preventDefault();
    if (!isPTTHeld) return;
    if (currentPttMode === 'latching') {{ isPTTHeld = false; return; }}
    isPTTHeld = false;
    const pttBtn = document.getElementById("ptt_btn");
    pttBtn.innerText = T('ws.btn.ptt');
    pttBtn.classList.remove('tx');
    sendRx();
}}

function sendRx() {{
    fetch('/api/ptt', {{ method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{t: 'RX'}}) }}).catch(() => {{}});
}}

function sendTx() {{
    fetch('/api/ptt', {{ method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{t: 'TX'}}) }}).catch(() => {{}});
}}

function doPttTx() {{
    const pttBtn = document.getElementById("ptt_btn");
    pttBtn.innerText = T('js.tx');
    pttBtn.classList.add('tx');
    sendTx();
}}

function togglePtt() {{
    if (pttRxRetry) clearTimeout(pttRxRetry);
    if (pttTxRetry) clearTimeout(pttTxRetry);
    pttRxRetry = null;
    pttTxRetry = null;
    pttRxRetryCount = 0;
    pttTxRetryCount = 0;
    const newState = currentTrx === 'RX' ? 'TX' : 'RX';
    currentTrx = newState;
    updatePttStatus();
    const pttBtn = document.getElementById("ptt_btn");
    if (newState === 'TX') {{ pttBtn.innerText = T('js.tx'); pttBtn.classList.add('tx'); }}
    else {{ pttBtn.innerText = T('ws.btn.ptt'); pttBtn.classList.remove('tx'); }}
    fetch('/api/ptt', {{ method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{t: 'TOGGLE', mode: 'latching'}}) }}).catch(() => {{}});
}}

async function updateState() {{
    try {{
        const res = await fetch(`/api/state`);
        const data = await res.json();
        document.getElementById("freq").textContent = data.frequency.toFixed(3);
        if (!isFreqInputEditing) document.getElementById("freq_in").value = data.frequency.toFixed(3);
        document.getElementById("mode").textContent = data.mode;
        if (data.power !== undefined) {{
            document.getElementById("power-status").textContent = data.power + '%';
            const powerLevels = [10, 25, 50, 75, 100];
            document.querySelectorAll('.power-0, .power-1, .power-2, .power-3, .power-4').forEach((btn, idx) => {{
                btn.classList.remove('active');
                if (powerLevels[idx] === data.power) btn.classList.add('active');
            }});
        }}
    }} catch (e) {{ console.error('更新状态失败：', e); }}
}}

// ===================== 信道管理 =====================
async function loadChannels() {{
    try {{
        const res = await fetch('/api/channels');
        const data = await res.json();
        if (data.status === 'ok') {{
            channelData = data.channels;
            populateChannelSelect(data.channels);
        }}
    }} catch (e) {{
        console.error('加载信道列表失败:', e);
    }}
}}

function populateChannelSelect(channels) {{
    const list = document.getElementById('channel-dropdown-list');
    const label = document.getElementById('channel-current-label');
    list.innerHTML = '';

    const bandOrder = ['160m', '80m', '40m', '20m', '15m', '10m'];
    let hasChannels = false;

    for (const band of bandOrder) {{
        if (channels[band] && channels[band].length > 0) {{
            hasChannels = true;
            const bandHeader = document.createElement('div');
            bandHeader.className = 'channel-band-header';
            bandHeader.textContent = band;
            list.appendChild(bandHeader);

            for (const ch of channels[band]) {{
                const item = document.createElement('div');
                item.className = 'channel-item';
                item.textContent = `${{ch.freq}} MHz / ${{ch.mode}}`;
                item.dataset.key = ch.key;
                item.dataset.freq = ch.freq;
                item.dataset.mode = ch.mode;
                item.onclick = () => selectChannelItem(ch.key, ch.freq, ch.mode);
                list.appendChild(item);
            }}
        }}
    }}

    if (!hasChannels) {{
        list.innerHTML = '<div class="channel-dropdown-empty">' + T('js.channel.empty') + '</div>';
    }}
}}

function selectChannelItem(key, freq, mode) {{
    document.getElementById('channel-current-label').textContent = `${{freq}} MHz / ${{mode}}`;
    document.getElementById('freq_in').value = freq;
    setFreqByChannel(freq, mode);
    closeChannelDropdown();
}}

function toggleChannelDropdown() {{
    const list = document.getElementById('channel-dropdown-list');
    list.classList.toggle('show');
}}

function closeChannelDropdown() {{
    const list = document.getElementById('channel-dropdown-list');
    list.classList.remove('show');
}}

document.addEventListener('click', (e) => {{
    const dropdown = document.getElementById('channel-dropdown');
    if (dropdown && !dropdown.contains(e.target)) {{
        closeChannelDropdown();
    }}
}});

async function setFreqByChannel(freq, mode) {{
    try {{
        await fetch('/api/set_channel', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{freq: freq, mode: mode}})
        }});
        document.getElementById('freq').textContent = parseFloat(freq).toFixed(3);
        updateModeButtons(mode);
    }} catch (e) {{
        console.error('设置信道失败:', e);
    }}
}}

function showChannelManager() {{
    const overlay = document.getElementById('channel-manager-overlay');
    const manager = document.getElementById('channel-manager');
    const list = document.getElementById('channel-list');

    list.innerHTML = '';

    const bandOrder = ['160m', '80m', '40m', '20m', '15m', '10m'];

    for (const band of bandOrder) {{
        if (channelData[band] && channelData[band].length > 0) {{
            const bandDiv = document.createElement('div');
            bandDiv.style.cssText = 'font-weight:bold;color:var(--accent2);margin:8px 0 4px;padding-left:4px;';
            bandDiv.textContent = band;
            list.appendChild(bandDiv);

            for (const ch of channelData[band]) {{
                const item = document.createElement('div');
                item.className = 'channel-item';

                const info = document.createElement('div');
                info.className = 'channel-item-info';
                info.onclick = () => selectChannelByKey(ch.key);
                const nameEl = document.createElement('div');
                nameEl.className = 'channel-item-name';
                nameEl.textContent = `${{ch.freq}} MHz / ${{ch.mode}}`;
                info.appendChild(nameEl);

                const btns = document.createElement('div');
                btns.className = 'channel-item-btns';
                const editBtn = document.createElement('button');
                editBtn.textContent = '✏️';
                editBtn.onclick = (e) => {{ e.stopPropagation(); editChannelUI(ch.key); }};
                const delBtn = document.createElement('button');
                delBtn.textContent = '🗑️';
                delBtn.onclick = (e) => {{ e.stopPropagation(); deleteChannelConfirm(ch.key); }};
                btns.appendChild(editBtn);
                btns.appendChild(delBtn);

                item.appendChild(info);
                item.appendChild(btns);
                list.appendChild(item);
            }}
        }}
    }}

    overlay.style.display = 'block';
    manager.style.display = 'block';
}}

function closeChannelManager() {{
    document.getElementById('channel-manager-overlay').style.display = 'none';
    document.getElementById('channel-manager').style.display = 'none';
}}

function selectChannelByKey(key) {{
    const bandOrder = ['160m', '80m', '40m', '20m', '15m', '10m'];
    for (const band of bandOrder) {{
        if (channelData[band]) {{
            for (const ch of channelData[band]) {{
                if (ch.key === key) {{
                    selectChannelItem(key, ch.freq, ch.mode);
                    closeChannelManager();
                    return;
                }}
            }}
        }}
    }}
}}

function editChannelUI(key) {{
    let currentFreq = '';
    let currentMode = '';
    const bandOrder = ['160m', '80m', '40m', '20m', '15m', '10m'];
    for (const band of bandOrder) {{
        if (channelData[band]) {{
            for (const ch of channelData[band]) {{
                if (ch.key === key) {{
                    currentFreq = ch.freq;
                    currentMode = ch.mode;
                    break;
                }}
            }}
        }}
    }}

    const newFreq = prompt(T('js.prompt.freq'), currentFreq);
    if (newFreq && newFreq.trim()) {{
        const f = parseFloat(newFreq.trim());
        if (!isNaN(f) && f >= 1.8 && f <= 30) {{
            updateChannelFreq(key, f);
        }} else {{
            alert(T('js.alert.freq3'));
        }}
    }}
}}

async function updateChannelFreq(key, freq) {{
    try {{
        const res = await fetch('/api/update_channel', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{key: key, freq: freq}})
        }});
        const data = await res.json();
        if (data.status === 'ok') {{
            await loadChannels();
            showChannelManager();
        }} else {{
            alert(T('js.alert.update_fail', data.msg));
        }}
    }} catch (e) {{
        console.error('更新信道失败:', e);
    }}
}}

function deleteChannelConfirm(key) {{
    if (confirm(T('js.confirm.del'))) {{
        deleteChannel(key);
    }}
}}

async function deleteChannel(key) {{
    try {{
        const res = await fetch('/api/delete_channel', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{key: key}})
        }});
        const data = await res.json();
        if (data.status === 'ok') {{
            await loadChannels();
            showChannelManager();
        }} else {{
            alert(T('js.alert.delete_fail', data.msg));
        }}
    }} catch (e) {{
        console.error('删除信道失败:', e);
    }}
}}

function showAddChannelDialog() {{
    const overlay = document.getElementById('add-channel-dialog-overlay');
    const dialog = document.getElementById('add-channel-dialog');
    document.getElementById('add-ch-freq').value = '';
    document.getElementById('add-ch-mode').value = 'USB';
    overlay.style.display = 'block';
    dialog.style.display = 'block';
}}

function closeAddChannelDialog() {{
    document.getElementById('add-channel-dialog-overlay').style.display = 'none';
    document.getElementById('add-channel-dialog').style.display = 'none';
}}

async function doAddChannel() {{
    const freq = document.getElementById('add-ch-freq').value.trim();
    const mode = document.getElementById('add-ch-mode').value;
    if (!freq) {{
        alert(T('js.alert.enter_freq'));
        return;
    }}
    const f = parseFloat(freq);
    if (isNaN(f) || f < 1.8 || f > 30) {{
        alert(T('js.alert.freq3'));
        return;
    }}
    try {{
        const saveRes = await fetch('/api/save_channel', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{freq: freq, mode: mode}})
        }});
        const saveData = await saveRes.json();
        if (saveData.status === 'ok') {{
            closeAddChannelDialog();
            await loadChannels();
            showChannelManager();
        }} else {{
            alert(T('js.alert.save_fail', saveData.msg));
        }}
    }} catch (e) {{
        console.error('添加信道失败:', e);
        alert(T('js.alert.add_fail'));
    }}
}}

window.addEventListener('load', () => {{
    initSocket();
    loadChannels();
    setInterval(() => {{ if (!isFreqInputEditing && socket && socket.connected) updateState(); }}, 5000);
}});

// ===================== 视频功能 =====================
function toggleVideo(show) {{
    const container = document.getElementById('video-container');
    const btn = document.getElementById('video-toggle-btn');
    const status = document.getElementById('video-status');
    const videoEl = document.getElementById('video-display');
    
    if (show) {{
        fetch('/api/video_toggle', {{ method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{enabled: true}}) }}).catch(() => {{}});
        container.style.display = 'block';
        btn.style.display = 'none';
        if (status) status.textContent = T('js.video.connecting');
        
        const iceState = webrtc_pc && (webrtc_pc.iceConnectionState === 'completed' || webrtc_pc.iceConnectionState === 'connected');
        if (iceState && webrtc_remoteStream && webrtc_remoteStream.getVideoTracks().length > 0) {{
            videoEl.srcObject = webrtc_remoteStream;
            videoEl.play().then(() => {{
                if (status) status.textContent = T('js.video.streaming');
            }}).catch(e => {{
                if (status) status.textContent = T('js.video.failed');
            }});
        }} else {{
            if (status) status.textContent = T('js.video.wait');
        }}
        
    }} else {{
        fetch('/api/video_toggle', {{ method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{enabled: false}}) }}).catch(() => {{}});
        videoEl.pause();
        container.style.display = 'none';
        btn.style.display = 'block';
    }}
}}

window.addEventListener('beforeunload', () => {{ if (socket) socket.disconnect(); if (audioContext) audioContext.close(); if (micStream) micStream.getTracks().forEach(track => track.stop()); }});
</script>
</body>
</html>
'''

# ===================== GUI 主窗口 =====================
class RoundedButton(tk.Canvas):
    """Apple 风格圆角矩形按钮（Canvas 绘制，支持悬停变色）"""

    def __init__(self, parent, text="", command=None,
                 bg="#007aff", fg="#ffffff", hover_bg=None,
                 font=("Microsoft YaHei", 9), padx=14, pady=5,
                 radius=10, min_width=0, cursor="hand2", **kw):
        self._font = tkfont.Font(font=font)
        h = self._font.metrics("linespace") + pady * 2
        w = max(min_width, self._font.measure(text) + padx * 2)
        try:
            pbg = parent.cget("bg")
        except Exception:
            pbg = "#ffffff"
        super().__init__(parent, width=w, height=h, highlightthickness=0,
                         bd=0, bg=pbg, cursor=cursor, **kw)
        self._command = command
        self._bg = bg
        self._hover = hover_bg or bg
        r = min(radius, h // 2)
        self._shape = self._round_rect(1, 1, w - 1, h - 1, r, fill=bg)
        self.create_text(w / 2, h / 2, text=text, fill=fg, font=font)
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", lambda e: self.itemconfigure(self._shape, fill=self._hover))
        self.bind("<Leave>", lambda e: self.itemconfigure(self._shape, fill=self._bg))

    def _round_rect(self, x1, y1, x2, y2, r, **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.create_polygon(pts, smooth=True, **kw)

    def _on_click(self, _e=None):
        if callable(self._command):
            self._command()


class FT710ServerGUI:
    VERSION = "2.2.2"
    EMAIL = "ruibaow@outlook.com"
    QQ_GROUP = "232934024"
    
    C = {
        "bg":         "#f2f2f7",
        "glass":      "#ffffff",
        "glass2":     "#f2f2f7",
        "border":     "#e3e3e8",
        "sep":        "#d1d1d6",
        "accent":     "#007aff",
        "accent2":    "#0060df",
        "text":       "#1c1c1e",
        "text2":      "#3a3a3c",
        "text3":      "#8e8e93",
        "ok":         "#34c759",
        "err":        "#ff3b30",
        "warn":       "#ff9500",
        "btn":        "#007aff",
        "btn_fg":     "#ffffff",
        "btn_sec":       "#e9e9ee",
        "btn_sec_fg":    "#007aff",
        "btn_sec_hover": "#dcdce3",
    }

    def __init__(self):
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        self.root = tk.Tk()
        self.root.title(f"FT-710 Remote Controller  v{self.VERSION}  Designed by BI4IXO")
        self.root.geometry("1000x680")
        self.root.minsize(700, 500)
        self.root.configure(bg=self.C["bg"])
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        root = self.root
        C = self.C

        self._menubar = tk.Menu(root, bg=C["glass"], fg=C["text"],
                          activebackground=C["accent"], activeforeground="#fff",
                          relief="flat", borderwidth=0)
        menubar = self._menubar
        self._menu_com = tk.Menu(menubar, tearoff=0, bg=C["glass"], fg=C["text"],
                           activebackground=C["accent"], activeforeground="#fff",
                           relief="flat")
        com_menu = self._menu_com
        com_menu.add_command(label=_t('menu.serial.edit'), command=self._dialog_com)
        com_menu.add_command(label=_t('menu.serial.reconn'), command=self._reconnect_com)
        menubar.add_cascade(label=_t('menu.serial'), menu=com_menu)

        self._menu_audio = tk.Menu(menubar, tearoff=0, bg=C["glass"], fg=C["text"],
                             activebackground=C["accent"], activeforeground="#fff",
                             relief="flat")
        audio_menu = self._menu_audio
        audio_menu.add_command(label=_t('menu.audio.edit'), command=self._dialog_audio)
        audio_menu.add_command(label=_t('menu.audio.restart'), command=self._restart_audio)
        menubar.add_cascade(label=_t('menu.audio'), menu=audio_menu)

        self._menu_video = tk.Menu(menubar, tearoff=0, bg=C["glass"], fg=C["text"],
                             activebackground=C["accent"], activeforeground="#fff",
                             relief="flat")
        video_menu = self._menu_video
        video_menu.add_command(label=_t('menu.video.edit'), command=self._dialog_video)
        menubar.add_cascade(label=_t('menu.video'), menu=video_menu)

        self._menu_log = tk.Menu(menubar, tearoff=0, bg=C["glass"], fg=C["text"],
                           activebackground=C["accent"], activeforeground="#fff",
                           relief="flat")
        log_menu = self._menu_log
        log_menu.add_command(label=_t('menu.log.clear'), command=self._clear_console)
        log_menu.add_command(label=_t('menu.log.file'), command=self._open_log_file)
        menubar.add_cascade(label=_t('menu.log'), menu=log_menu)

        self._menu_lang = tk.Menu(menubar, tearoff=0, bg=C["glass"], fg=C["text"],
                            activebackground=C["accent"], activeforeground="#fff",
                            relief="flat")
        self._lang_var = tk.StringVar(value=LANG)
        lang_menu = self._menu_lang
        lang_menu.add_radiobutton(label=_t('menu.lang.zh'), value='zh',
                                  variable=self._lang_var,
                                  command=lambda: self._set_lang('zh'))
        lang_menu.add_radiobutton(label=_t('menu.lang.en'), value='en',
                                  variable=self._lang_var,
                                  command=lambda: self._set_lang('en'))
        menubar.add_cascade(label=_t('menu.lang'), menu=lang_menu)

        self._menu_about = tk.Menu(menubar, tearoff=0, bg=C["glass"], fg=C["text"],
                             activebackground=C["accent"], activeforeground="#fff",
                             relief="flat")
        about_menu = self._menu_about
        about_menu.add_command(label=_t('menu.about.info'), command=self._dialog_about)
        menubar.add_cascade(label=_t('menu.about'), menu=about_menu)
        root.config(menu=menubar)

        top = tk.Frame(root, bg=C["glass2"], pady=0)
        top.pack(fill="x")

        top_inner = tk.Frame(top, bg=C["glass2"], pady=8, padx=14)
        top_inner.pack(fill="x")

        self._lbl_title = tk.Label(top_inner, text="📻  FT-710 Remote Controller",
                 bg=C["glass2"], fg=C["accent2"],
                 font=("Microsoft YaHei", 12, "bold"))
        self._lbl_title.pack(side="left")

        pill_frame = tk.Frame(top_inner, bg=C["glass2"])
        pill_frame.pack(side="right")

        self._lbl_url   = self._pill(pill_frame, _t('st.starting'), C["text3"])
        self._lbl_audio = self._pill(pill_frame, _t('st.audio.init'), C["text3"])
        self._lbl_com   = self._pill(pill_frame, _t('st.com.disc'),  C["text3"])
        self._lbl_url.pack(side="right", padx=4)
        self._lbl_audio.pack(side="right", padx=4)
        self._lbl_com.pack(side="right", padx=4)

        tk.Frame(root, bg=C["accent"], height=2).pack(fill="x")
        tk.Frame(root, bg=C["sep"],    height=1).pack(fill="x")

        card_outer = tk.Frame(root, bg=C["bg"], pady=6, padx=10)
        card_outer.pack(fill="x")

        card = tk.Frame(card_outer, bg=C["glass"],
                        highlightbackground=C["border"], highlightthickness=1,
                        pady=8, padx=14)
        card.pack(fill="x")

        row1 = tk.Frame(card, bg=C["glass"])
        row1.pack(fill="x")
        self._lbl_access_title = tk.Label(row1, text=_t('lbl.access'), bg=C["glass"], fg=C["text3"],
                 font=("Microsoft YaHei", 8))
        self._lbl_access_title.pack(side="left")
        self._lbl_access = tk.Label(row1, text=_t('st.access.init'),
                                    bg=C["glass"], fg=C["accent2"],
                                    font=("Microsoft YaHei", 10, "bold"))
        self._lbl_access.pack(side="left", padx=8)

        row2 = tk.Frame(card, bg=C["glass"])
        row2.pack(fill="x", pady=(2,0))
        self._lbl_com_title = tk.Label(row2, text=_t('lbl.com'), bg=C["glass"], fg=C["text3"],
                 font=("Microsoft YaHei", 8))
        self._lbl_com_title.pack(side="left")
        self._lbl_com_info = tk.Label(row2,
            text=f"{config['SERIAL']['COM_PORT']}  @  {config['SERIAL']['BAUD_RATE']} baud",
            bg=C["glass"], fg=C["text2"], font=("Microsoft YaHei", 10))
        self._lbl_com_info.pack(side="left", padx=8)

        tk.Frame(root, bg=C["sep"], height=1).pack(fill="x", pady=(4,0))

        hdr = tk.Frame(root, bg=C["glass2"], pady=3, padx=10)
        hdr.pack(fill="x")
        self._lbl_console_title = tk.Label(hdr, text=_t('lbl.console'), bg=C["glass2"], fg=C["text3"],
                 font=("Microsoft YaHei", 8))
        self._lbl_console_title.pack(side="left")

        self._console = scrolledtext.ScrolledText(
            root,
            bg="#f4f8fd", fg=C["text"],
            font=("Consolas", 9),
            insertbackground=C["text"],
            state="disabled",
            relief="flat",
            borderwidth=0,
            wrap="word",
            selectbackground=C["accent"],
            selectforeground="#fff"
        )
        self._console.pack(fill="both", expand=True, padx=0)
        self._console.tag_config("ok",    foreground=C["ok"])
        self._console.tag_config("err",   foreground=C["err"])
        self._console.tag_config("warn",  foreground=C["warn"])
        self._console.tag_config("info",  foreground=C["accent"])
        self._console.tag_config("plain", foreground=C["text"])
        self._console.tag_config("ts",    foreground=C["text3"])

        self._status_bar = tk.Frame(root, bg=C["glass2"], height=22)
        self._status_bar.pack(fill="x", side="bottom")
        self._status_bar.pack_propagate(False)
        tk.Frame(self._status_bar, bg=C["accent"], height=2).pack(fill="x")

        sb_inner = tk.Frame(self._status_bar, bg=C["glass2"])
        sb_inner.pack(fill="x", padx=10, pady=1)
        self._lbl_st_rig = tk.Label(sb_inner, text="● 电台 --", bg=C["glass2"],
            fg=C["text3"], font=("Microsoft YaHei", 8))
        self._lbl_st_rig.pack(side="left", padx=(0,12))
        self._lbl_st_audio = tk.Label(sb_inner, text="● 音频 --", bg=C["glass2"],
            fg=C["text3"], font=("Microsoft YaHei", 8))
        self._lbl_st_audio.pack(side="left", padx=(0,12))
        self._lbl_st_mode = tk.Label(sb_inner, text="MODE --", bg=C["glass2"],
            fg=C["text3"], font=("Microsoft YaHei", 8))
        self._lbl_st_mode.pack(side="left", padx=(0,12))
        tk.Label(sb_inner, text=f"v{self.VERSION}", bg=C["glass2"],
            fg=C["text3"], font=("Microsoft YaHei", 7)).pack(side="right")

    def _pill(self, parent, text, fg):
        f = tk.Frame(parent, bg=self.C["glass"], 
                     highlightbackground=self.C["border"], highlightthickness=1,
                     padx=8, pady=2)
        lbl = tk.Label(f, text=text, bg=self.C["glass"], fg=fg,
                       font=("Microsoft YaHei", 8))
        lbl.pack()
        f.config  = lbl.config
        f._lbl    = lbl
        return f

    def _tag_for(self, msg):
        if any(x in msg for x in ["✅", "成功", "就绪", "完成", "恢复",
                                  "Connected", "Ready", "Started", "Success", "SUCCESS"]):
            return "ok"
        if any(x in msg for x in ["❌", "失败", "错误", "ERROR", "Failed", "FAILED", "Error"]):
            return "err"
        if any(x in msg for x in ["⚠️", "警告", "WARN", "Warning"]):
            return "warn"
        if any(x in msg for x in ["📻","📱","🎯","🎧","📡","🔧","🔐","📁"]):
            return "info"
        return "plain"

    def _refresh_ui_pills(self, com_ok, audio_ok, url=None):
        C = self.C
        port = config['SERIAL']['COM_PORT']
        if url is not None:
            self._lbl_url._lbl.config(text=_t('st.running'), fg=C["ok"])
        else:
            self._lbl_url._lbl.config(text=_t('st.starting'), fg=C["text3"])
        self._lbl_com._lbl.config(
            text=_t('st.com.port', port=port) if com_ok else _t('st.com.disc'),
            fg=C["ok"] if com_ok else C["err"])
        self._lbl_st_rig.config(
            text=_t('st.radio.port', port=port) if com_ok else _t('st.radio.disc'),
            fg=C["ok"] if com_ok else C["err"])
        self._lbl_audio._lbl.config(
            text=_t('st.audio.ready') if audio_ok else _t('st.audio.init'),
            fg=C["ok"] if audio_ok else C["err"])
        self._lbl_st_audio.config(
            text=_t('st.audio.ready') if audio_ok else _t('st.audio.init'),
            fg=C["ok"] if audio_ok else C["err"])

    def _poll_log_queue(self):
        try:
            while True:
                msg = _gui_log_queue.get_nowait()
                self.append_log(msg)
        except Exception:
            pass
        com_ok = radio_state.get("frequency", 0) > 0
        try:
            input_ok = audio_input_stream is not None and audio_input_stream.active
        except Exception:
            input_ok = False
        try:
            output_ok = audio_output_stream is not None and audio_output_stream.active
        except Exception:
            output_ok = False
        audio_ok = input_ok or output_ok
        self._refresh_ui_pills(com_ok, audio_ok, getattr(self, '_st_url', None))
        m = radio_state.get("mode", "")
        self._lbl_st_mode.config(text=f"MODE {m}" if m else "MODE --")
        self.root.after(100, self._poll_log_queue)

    def _update_status(self, com_ok, audio_ok, url):
        self._st_com_ok = com_ok
        self._st_audio_ok = audio_ok
        self._st_url = url
        self._lbl_url._lbl.config(text=_t('st.running'), fg=self.C["ok"])
        self._lbl_access.config(text=url)
        self._lbl_com_info.config(
            text=f"{config['SERIAL']['COM_PORT']}  @  {config['SERIAL']['BAUD_RATE']} baud")
        self._refresh_ui_pills(com_ok, audio_ok, url)

    def _set_lang(self, code):
        global LANG
        if code not in ('zh', 'en'):
            code = 'zh'
        LANG = code
        self._lang_var.set(code)
        config['LANGUAGE']['CODE'] = code
        save_config(config)
        self._apply_language()
        try:
            socketio.emit('set_lang', {'code': code}, namespace='/')
        except Exception:
            pass
        log(_t('log.lang.switch', lang=code))

    def _apply_language(self):
        """根据当前 LANG 刷新全部 GUI 文案"""
        mb = self._menubar
        for i in range(mb.index('end') or 0, -1, -1):
            try:
                mb.delete(i)
            except Exception:
                pass
        mb.add_cascade(label=_t('menu.serial'), menu=self._menu_com)
        mb.add_cascade(label=_t('menu.audio'), menu=self._menu_audio)
        mb.add_cascade(label=_t('menu.video'), menu=self._menu_video)
        mb.add_cascade(label=_t('menu.log'), menu=self._menu_log)
        mb.add_cascade(label=_t('menu.lang'), menu=self._menu_lang)
        mb.add_cascade(label=_t('menu.about'), menu=self._menu_about)
        self._menu_com.entryconfigure(0, label=_t('menu.serial.edit'))
        self._menu_com.entryconfigure(1, label=_t('menu.serial.reconn'))
        self._menu_audio.entryconfigure(0, label=_t('menu.audio.edit'))
        self._menu_audio.entryconfigure(1, label=_t('menu.audio.restart'))
        self._menu_video.entryconfigure(0, label=_t('menu.video.edit'))
        self._menu_log.entryconfigure(0, label=_t('menu.log.clear'))
        self._menu_log.entryconfigure(1, label=_t('menu.log.file'))
        self._menu_about.entryconfigure(0, label=_t('menu.about.info'))
        self._lbl_access_title.config(text=_t('lbl.access'))
        self._lbl_com_title.config(text=_t('lbl.com'))
        self._lbl_console_title.config(text=_t('lbl.console'))
        self._refresh_ui_pills(
            getattr(self, '_st_com_ok', False),
            getattr(self, '_st_audio_ok', False),
            getattr(self, '_st_url', None))

    def append_log(self, msg):
        if any(x in msg for x in ["电台响应：", "WebRTC帧原始", "音频前10采样", "shape=", "min="]):
            return
        self._console.configure(state="normal")
        ts = datetime.now().strftime("%H:%M:%S")
        self._console.insert("end", f"[{ts}] ", "ts")
        self._console.insert("end", f"{msg}\n", self._tag_for(msg))
        lc = int(self._console.index("end-1c").split(".")[0])
        if lc > 2000:
            self._console.delete("1.0", f"{lc-2000}.0")
        self._console.configure(state="disabled")
        self._console.see("end")

    def _styled_dialog(self, title, w, h):
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.geometry(f"{w}x{h}")
        dlg.resizable(False, False)
        dlg.configure(bg=self.C["bg"])
        dlg.grab_set()
        try:
            dlg.update_idletasks()
            rx = self.root.winfo_rootx()
            ry = self.root.winfo_rooty()
            rw = self.root.winfo_width()
            rh = self.root.winfo_height()
            x = rx + max(0, (rw - w) // 2)
            y = ry + max(0, (rh - h) // 3)
            sw = dlg.winfo_screenwidth()
            sh = dlg.winfo_screenheight()
            x = min(max(0, x), max(0, sw - w))
            y = min(max(0, y), max(0, sh - h))
            dlg.geometry(f"+{x}+{y}")
        except Exception:
            pass
        # Windows 11：圆角化对话框（Win10 上无效但不报错）
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(dlg.winfo_id())
            pref = ctypes.c_int(2)  # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))  # DWMWA_WINDOW_CORNER_PREFERENCE
        except Exception:
            pass
        tk.Frame(dlg, bg=self.C["accent"], height=3).pack(fill="x")
        return dlg

    def _dialog_com(self):
        C = self.C
        dlg = self._styled_dialog(_t('dlg.com.title'), 400, 250)

        card = tk.Frame(dlg, bg=C["glass"],
                        highlightbackground=C["border"], highlightthickness=1,
                        pady=12, padx=18)
        card.pack(fill="x", padx=12, pady=10)

        try:
            import serial.tools.list_ports
            port_list = [p.device for p in serial.tools.list_ports.comports()]
            port_list.sort()
        except Exception:
            port_list = []
        cur_port = config['SERIAL']['COM_PORT']
        if cur_port not in port_list:
            port_list.insert(0, cur_port)

        tk.Label(card, text=_t('lbl.comport'), bg=C["glass"], fg=C["text2"],
                 font=("Microsoft YaHei", 9)).grid(row=0, column=0, sticky="w", padx=4, pady=6)
        port_var = tk.StringVar(value=cur_port)
        cb_port = ttk.Combobox(card, textvariable=port_var, values=port_list,
                               width=22, state="readonly", font=("Microsoft YaHei", 10))
        cb_port.grid(row=0, column=1, sticky="ew", padx=8, pady=6)
        card.columnconfigure(1, weight=1)

        def _refresh():
            try:
                import serial.tools.list_ports
                pl = [p.device for p in serial.tools.list_ports.comports()]
                pl.sort()
                if port_var.get() not in pl:
                    pl.insert(0, port_var.get())
                cb_port['values'] = pl
                log(_t('log.port.refresh', l=pl))
            except Exception as e:
                log(_t('log.port.refresh.fail', e=e))

        ref_row = tk.Frame(card, bg=C["glass"])
        ref_row.grid(row=1, column=0, columnspan=2, sticky="w", padx=4, pady=(0, 2))
        RoundedButton(ref_row, text="🔄 " + _t('btn.refresh.short'), command=_refresh,
                      bg=C["btn_sec"], fg=C["btn_sec_fg"], hover_bg=C["btn_sec_hover"],
                      font=("Microsoft YaHei", 8), padx=10, pady=3, radius=8).pack(side="left")

        tk.Label(card, text=_t('lbl.baud'), bg=C["glass"], fg=C["text2"],
                 font=("Microsoft YaHei", 9)).grid(row=2, column=0, sticky="w", padx=4, pady=6)
        e_baud = tk.Entry(card, font=("Microsoft YaHei", 10), width=14,
                          bg="#fff", fg=C["text"], insertbackground=C["text"],
                          relief="flat",
                          highlightbackground=C["border"], highlightthickness=1)
        e_baud.insert(0, config['SERIAL']['BAUD_RATE'])
        e_baud.grid(row=2, column=1, sticky="w", padx=8, pady=6)

        def _apply():
            port = port_var.get().strip()
            baud = e_baud.get().strip()
            if not port or not baud.isdigit():
                messagebox.showerror(_t('err.title'), _t('err.invalid.com'), parent=dlg)
                return
            config['SERIAL']['COM_PORT'] = port
            config['SERIAL']['BAUD_RATE'] = baud
            save_config(config)
            ok = init_com(port, int(baud))
            self._lbl_com_info.config(text=f"{port}  @  {baud} baud")
            self._lbl_com._lbl.config(
                text=_t('st.com.port', port=port) if ok else _t('st.com.disc'),
                fg=C["ok"] if ok else C["err"])
            log(_t('log.port.ok' if ok else 'log.port.fail', p=port, b=baud))
            dlg.destroy()

        RoundedButton(dlg, text=_t('btn.apply.reconn'), command=_apply,
                      bg=C["btn"], fg=C["btn_fg"], hover_bg=C["accent2"],
                      font=("Microsoft YaHei", 10), padx=20, pady=6,
                      radius=10).pack(pady=6)

    def _reconnect_com(self):
        C = self.C
        ok = init_com()
        self._lbl_com._lbl.config(
            text=_t('st.com.port', port=config['SERIAL']['COM_PORT']) if ok else _t('st.com.disc'),
            fg=C["ok"] if ok else C["err"])
        log(_t('log.com.reconn.ok' if ok else 'log.com.reconn.fail'))

    def _restart_audio(self):
        C = self.C
        ok = restart_audio_streams()
        self._lbl_audio._lbl.config(
            text=_t('st.audio.ready') if ok else _t('st.audio.init'),
            fg=C["ok"] if ok else C["err"])
        log(_t('log.audio.ok' if ok else 'log.audio.fail'))

    def _dialog_audio(self):
        C = self.C
        dlg = self._styled_dialog(_t('dlg.audio.title'), 780, 360)

        card = tk.Frame(dlg, bg=C["glass"],
                        highlightbackground=C["border"], highlightthickness=1,
                        pady=12, padx=18)
        card.pack(fill="x", padx=14, pady=8)
        card.columnconfigure(1, weight=1)

        sys_default = _t('lbl.sys.default')
        input_devices = []
        output_devices = []

        def _scan(silent=False):
            """重查声卡设备列表，返回是否成功；成功后更新 input/output 列表"""
            try:
                devices = sd.query_devices()
                ins, outs = [], []
                for i, d in enumerate(devices):
                    dev_name = d.get('name', f'Device#{i}')
                    if d.get('max_input_channels', 0) > 0:
                        ins.append((i, dev_name))
                    if d.get('max_output_channels', 0) > 0:
                        outs.append((i, dev_name))
            except Exception as e:
                msg = _t('log.audio.refresh.fail', e=e)
                log(msg)
                if not silent:
                    messagebox.showerror(_t('err.title'), msg, parent=dlg)
                return False
            nonlocal input_devices, output_devices
            input_devices = ins
            output_devices = outs
            return True

        cur_device_in = int(config['AUDIO'].get('DEVICE_IN', -1))
        cur_device_out = int(config['AUDIO'].get('DEVICE_OUT', -1))

        in_var = tk.StringVar()
        out_var = tk.StringVar()

        def _repopulate(cb, var, pairs, cur_dev):
            values = [sys_default] + [name for i, name in pairs]
            indices = [-1] + [i for i, name in pairs]
            cb['values'] = values
            idx = 0
            if cur_dev in indices:
                idx = indices.index(cur_dev)
            try:
                var.set(values[idx])
                cb.current(idx)
            except Exception:
                var.set(values[0])
                cb.current(0)

        if _scan() is False:
            return

        tk.Label(card, text=_t('lbl.input.dev'), bg=C["glass"], fg=C["text2"],
                 font=("Microsoft YaHei", 9)).grid(row=0, column=0, sticky="w", padx=4, pady=6)

        cb_in = ttk.Combobox(card, textvariable=in_var, width=42, state="readonly",
                             font=("Microsoft YaHei", 10))
        cb_in.grid(row=0, column=1, columnspan=2, sticky="ew", padx=8, pady=6)
        _repopulate(cb_in, in_var, input_devices, cur_device_in)

        tk.Label(card, text=_t('lbl.output.dev'), bg=C["glass"], fg=C["text2"],
                 font=("Microsoft YaHei", 9)).grid(row=1, column=0, sticky="w", padx=4, pady=6)

        cb_out = ttk.Combobox(card, textvariable=out_var, width=42, state="readonly",
                              font=("Microsoft YaHei", 10))
        cb_out.grid(row=1, column=1, columnspan=2, sticky="ew", padx=8, pady=6)
        _repopulate(cb_out, out_var, output_devices, cur_device_out)

        def _refresh_devices():
            if not _scan(silent=True):
                return
            prev_in, prev_out = in_var.get(), out_var.get()
            _repopulate(cb_in, in_var, input_devices, cur_device_in)
            _repopulate(cb_out, out_var, output_devices, cur_device_out)
            nv_in = [sys_default] + [name for i, name in input_devices]
            nv_out = [sys_default] + [name for i, name in output_devices]
            if prev_in in nv_in:
                in_var.set(prev_in)
                cb_in.current(nv_in.index(prev_in))
            if prev_out in nv_out:
                out_var.set(prev_out)
                cb_out.current(nv_out.index(prev_out))
            log(_t('log.audio.refresh', a=len(input_devices), b=len(output_devices)))

        ref_frame = tk.Frame(card, bg=C["glass"])
        ref_frame.grid(row=2, column=0, columnspan=3, sticky="w", padx=4, pady=(10, 2))
        RoundedButton(ref_frame, text="🔄 " + _t('btn.refresh.short'), command=_refresh_devices,
                      bg=C["btn_sec"], fg=C["btn_sec_fg"], hover_bg=C["btn_sec_hover"],
                      font=("Microsoft YaHei", 8), padx=10, pady=3, radius=8).pack(side="left")
        tk.Label(ref_frame, text=_t('lbl.refresh.hint'), bg=C["glass"], fg=C["text3"],
                 font=("Microsoft YaHei", 8)).pack(side="left", padx=8)

        def _apply():
            in_sel = in_var.get()
            out_sel = out_var.get()

            dev_in = -1
            dev_out = -1

            if in_sel and in_sel != sys_default:
                for i, name in input_devices:
                    if name == in_sel:
                        dev_in = i
                        break

            if out_sel and out_sel != sys_default:
                for i, name in output_devices:
                    if name == out_sel:
                        dev_out = i
                        break

            config['AUDIO']['DEVICE_IN'] = str(dev_in)
            config['AUDIO']['DEVICE_OUT'] = str(dev_out)
            save_config(config)

            log(_t('log.save.audio', a=dev_in, b=dev_out))

            ok = restart_audio_streams()

            self._lbl_audio._lbl.config(
                text=_t('st.audio.ready') if ok else _t('st.audio.init'),
                fg=C["ok"] if ok else C["err"])

            log(_t('log.audio.ok' if ok else 'log.audio.fail'))

            if ok:
                dlg.destroy()

        RoundedButton(dlg, text=_t('btn.apply.audio'), command=_apply,
                      bg=C["btn"], fg=C["btn_fg"], hover_bg=C["accent2"],
                      font=("Microsoft YaHei", 10), padx=20, pady=6,
                      radius=10).pack(pady=8)

    def _dialog_video(self):
        """视频设备配置对话框"""
        C = self.C
        dlg = self._styled_dialog(_t('dlg.video.title'), 540, 440)

        if not CV2_AVAILABLE:
            tk.Label(dlg, text=_t('lbl.video.nocv'),
                     bg=C["glass"], fg=C["err"], font=("Microsoft YaHei", 10)).pack(pady=20)
            RoundedButton(dlg, text=_t('btn.close'), command=dlg.destroy,
                          bg=C["btn_sec"], fg=C["btn_sec_fg"], hover_bg=C["btn_sec_hover"],
                          font=("Microsoft YaHei", 10), padx=18, pady=5,
                          radius=10).pack(pady=6)
            return

        card = tk.Frame(dlg, bg=C["glass"],
                        highlightbackground=C["border"], highlightthickness=1,
                        pady=12, padx=18)
        card.pack(fill="x", padx=14, pady=8)
        card.columnconfigure(1, weight=1)

        # 存储控件引用
        widgets = {'res_var': None, 'quality_var': None, 'chk_enabled': None}
        device_var = tk.StringVar(value=config['VIDEO'].get('DEVICE_INDEX', '0'))
        res_var = tk.StringVar(value=f"{config['VIDEO'].get('WIDTH', '640')}x{config['VIDEO'].get('HEIGHT', '480')}")
        quality_var = tk.StringVar(value=config['VIDEO'].get('QUALITY', '15'))
        chk_enabled_var = tk.BooleanVar(value=config['VIDEO'].get('ENABLED', 'false').lower() == 'true')

        result = {'devices': [], 'ready': False, 'scanning': False}

        def scan_and_show():
            # 后台扫描（可重复点“刷新设备”触发，扫描中防重入）
            if result['scanning']:
                return
            result['scanning'] = True
            lbl_scan.config(text=_t('lbl.scanning'))
            devices = []
            for i in range(5):
                try:
                    cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
                    if cap.isOpened():
                        devices.append(i)
                        cap.release()
                except:
                    pass
            result['devices'] = devices
            result['ready'] = True
            result['scanning'] = False
            # 更新UI
            dlg.after(0, lambda: update_device_list(devices))

        def update_device_list(devices):
            # 清除扫描提示/旧设备行
            for w in card.grid_slaves():
                if w.grid_info().get('row') == 1:
                    w.destroy()

            # 设备列表
            tk.Label(card, text=_t('lbl.camera'), bg=C["glass"], fg=C["text2"],
                     font=("Microsoft YaHei", 9)).grid(row=1, column=0, sticky="w", padx=4, pady=6)

            # 读取上次保存的设备索引
            saved_device = int(config['VIDEO'].get('DEVICE_INDEX', 0))

            if devices:
                values = [_t('lbl.device', i=i) for i in devices]
                cb = ttk.Combobox(card, values=values, state="readonly", font=("Microsoft YaHei", 9))
                cb.grid(row=1, column=1, sticky="ew", padx=8, pady=6)

                # 选中和上次相同的设备，如果没有则选第一个
                if saved_device in devices:
                    cb.current(devices.index(saved_device))
                    device_var.set(str(saved_device))
                else:
                    cb.current(0)
                    device_var.set(str(devices[0]))

                cb.bind('<<ComboboxSelected>>', lambda e: device_var.set(str(devices[cb.current()])))
                lbl_scan.config(text=f"{len(devices)} × " + _t('lbl.camera'))
            else:
                tk.Label(card, text=_t('lbl.no.dev'), bg=C["glass"], fg=C["err"],
                         font=("Microsoft YaHei", 9)).grid(row=1, column=1, sticky="w", padx=8, pady=6)
                lbl_scan.config(text=_t('lbl.no.dev'))

            card.columnconfigure(1, weight=1)

        def _start_scan():
            _gui_threading.Thread(target=scan_and_show, daemon=True).start()

        # 工具栏：刷新 + 状态提示（始终保留，不受设备列表刷新影响）
        toolbar = tk.Frame(card, bg=C["glass"])
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=4, pady=(0, 6))
        RoundedButton(toolbar, text="🔄 " + _t('btn.refresh.short'), command=_start_scan,
                      bg=C["btn_sec"], fg=C["btn_sec_fg"], hover_bg=C["btn_sec_hover"],
                      font=("Microsoft YaHei", 8), padx=10, pady=3, radius=8).pack(side="left")
        lbl_scan = tk.Label(toolbar, text=_t('lbl.scanning'), bg=C["glass"], fg=C["text3"],
                            font=("Microsoft YaHei", 8))
        lbl_scan.pack(side="left", padx=8)

        # 分辨率
        tk.Label(card, text=_t('lbl.res'), bg=C["glass"], fg=C["text2"],
                 font=("Microsoft YaHei", 9)).grid(row=2, column=0, sticky="w", padx=4, pady=6)
        res_cb = ttk.Combobox(card, values=["640x480", "800x600", "1280x720", "1920x1080"],
                            textvariable=res_var, state="readonly", font=("Microsoft YaHei", 9))
        res_cb.grid(row=2, column=1, sticky="ew", padx=8, pady=6)
        res_cb.bind('<<ComboboxSelected>>', lambda e: res_var.set(res_cb.get()))

        # 帧率
        tk.Label(card, text=_t('lbl.fps'), bg=C["glass"], fg=C["text2"],
                 font=("Microsoft YaHei", 9)).grid(row=3, column=0, sticky="w", padx=4, pady=6)
        fps_var = tk.StringVar(value=config['VIDEO'].get('FPS', '15'))
        fps_frame = tk.Frame(card, bg=C["glass"])
        fps_frame.grid(row=3, column=1, sticky="ew", padx=8, pady=6)
        tk.Entry(fps_frame, textvariable=fps_var, font=("Microsoft YaHei", 10), width=10).pack(side="left")
        tk.Label(fps_frame, text="(1-60)", bg=C["glass"], fg=C["text3"],
                 font=("Microsoft YaHei", 8)).pack(side="left", padx=4)

        # 质量
        tk.Label(card, text=_t('lbl.jpeg'), bg=C["glass"], fg=C["text2"],
                 font=("Microsoft YaHei", 9)).grid(row=4, column=0, sticky="w", padx=4, pady=6)
        quality_frame = tk.Frame(card, bg=C["glass"])
        quality_frame.grid(row=4, column=1, sticky="ew", padx=8, pady=6)
        tk.Entry(quality_frame, textvariable=quality_var, font=("Microsoft YaHei", 10), width=10).pack(side="left")
        tk.Label(quality_frame, text=_t('lbl.quality.hint'), bg=C["glass"], fg=C["text3"],
                 font=("Microsoft YaHei", 8)).pack(side="left", padx=4)

        # 启用
        tk.Checkbutton(card, text=_t('lbl.enable.video'), bg=C["glass"], fg=C["text"],
                       font=("Microsoft YaHei", 10), variable=chk_enabled_var).grid(
                           row=5, column=0, columnspan=2, sticky="w", pady=8)

        # 启动扫描线程
        _start_scan()

        def _apply():
            device_idx = int(device_var.get() or 0)
            try:
                w, h = res_var.get().split('x')
                width, height = int(w), int(h)
            except:
                width, height = 640, 480

            try:
                fps_val = max(1, min(60, int(fps_var.get())))
            except:
                fps_val = 15
            try:
                quality_val = max(2, min(31, int(quality_var.get())))
            except:
                quality_val = 15

            config['VIDEO']['ENABLED'] = 'true' if chk_enabled_var.get() else 'false'
            config['VIDEO']['DEVICE_INDEX'] = str(device_idx)
            config['VIDEO']['WIDTH'] = str(width)
            config['VIDEO']['HEIGHT'] = str(height)
            config['VIDEO']['FPS'] = str(fps_val)
            config['VIDEO']['QUALITY'] = str(quality_val)
            save_config(config)

            stop_video_stream()
            if chk_enabled_var.get():
                # 使用新配置启动视频流
                start_video_stream()

            log(_t('log.video.set', s=_t('log.video.on' if chk_enabled_var.get() else 'log.video.off'),
                    d=device_idx, w=width, h=height, fps=fps_val, q=quality_val))
            dlg.destroy()

        RoundedButton(dlg, text=_t('btn.apply'), command=_apply,
                      bg=C["btn"], fg=C["btn_fg"], hover_bg=C["accent2"],
                      font=("Microsoft YaHei", 10), padx=22, pady=6,
                      radius=10).pack(pady=8)

    def _clear_console(self):
        self._console.configure(state="normal")
        self._console.delete("1.0", "end")
        self._console.configure(state="disabled")

    def _open_log_file(self):
        try:    os.startfile(LOG_FILE)
        except: messagebox.showinfo(_t('log.path.title'), LOG_FILE, parent=self.root)

    def _dialog_about(self):
        C = self.C
        dlg = self._styled_dialog(_t('dlg.about.title'), 480, 320)

        card = tk.Frame(dlg, bg=C["glass"],
                        highlightbackground=C["border"], highlightthickness=1,
                        pady=20, padx=24)
        card.pack(fill="x", padx=12, pady=10)

        tk.Label(card, text="FT-710 Remote Controller",
                 bg=C["glass"], fg=C["accent2"],
                 font=("Microsoft YaHei", 15, "bold")).pack()
        tk.Label(card, text="Designed by BI4IXO",
                 bg=C["glass"], fg=C["text3"],
                 font=("Microsoft YaHei", 10)).pack(pady=(2, 4))
        tk.Label(card, text=_t('lbl.about.ver', ver=self.VERSION),
                 bg=C["glass"], fg=C["text"],
                 font=("Microsoft YaHei", 11)).pack(pady=(4, 2))
        tk.Label(card, text=_t('lbl.about.email', e=self.EMAIL),
                 bg=C["glass"], fg=C["text2"],
                 font=("Microsoft YaHei", 11)).pack()
        tk.Label(card, text=_t('lbl.about.qq', q=self.QQ_GROUP),
                 bg=C["glass"], fg=C["text2"],
                 font=("Microsoft YaHei", 11)).pack(pady=(2, 0))
        tk.Label(card, text=_t('lbl.about.log'),
                 bg=C["glass"], fg=C["text3"],
                 font=("Microsoft YaHei", 10)).pack(pady=(8, 0))

        RoundedButton(dlg, text=_t('btn.close'), command=dlg.destroy,
                      bg=C["btn_sec"], fg=C["btn_sec_fg"], hover_bg=C["btn_sec_hover"],
                      font=("Microsoft YaHei", 10), padx=18, pady=5,
                      radius=10).pack(pady=8)

    def _on_close(self):
        if messagebox.askokcancel(_t('exit.title'), _t('exit.confirm')):
            log("User closing server" if LANG == 'en' else "用户关闭服务器")
            self._cleanup()
            self.root.destroy()

    def _cleanup(self):
        global ser, audio_input_stream, audio_output_stream
        log("正在清理资源...")
        _running.set()
        try:
            stop_video_stream()
        except Exception:
            pass
        try:
            for pc in list(_webrtc_pcs):
                try:
                    asyncio.run_coroutine_threadsafe(pc.close(), _webrtc_loop).result(timeout=2)
                except Exception:
                    pass
            _webrtc_pcs.clear()
        except Exception:
            pass
        try:
            if ser and ser.is_open: ser.close()
        except: pass
        for s in [audio_input_stream, audio_output_stream]:
            try:
                if s: s.stop(); s.close()
            except: pass

    def start_server(self):
        def _run():
            try:
                log(_t('log.init'))
                cert_file, key_file = generate_ssl_certs()
                com_ok   = init_com()
                audio_ok = init_audio_streams()
                audio_watchdog()
                _threading.Thread(target=_tx_timeout_watchdog, daemon=True, name='TxTimeoutWatchdog').start()
                _threading.Thread(target=_audio_latency_emitter, daemon=True, name='AudioLatencyEmitter').start()

                # 自动启动视频流
                video_ok = False
                if config['VIDEO'].get('ENABLED', 'false').lower() == 'true':
                    log(_t('log.start.video'))
                    video_ok = start_video_stream()
                    if video_ok:
                        log(_t('log.video.ok'))
                
                urls = " / ".join([f"https://{ip_str}:{SERVER_PORT}" for ip_str in _all_ips])
                log("=" * 56)
                log(f"FT-710 Remote Controller  v{self.VERSION}")
                log(_t('log.access', u=urls))
                log(_t('log.serial', p=COM_PORT, b=BAUD_RATE))
                log(_t('log.audio.link', r=SAMPLING_RATE))
                log(_t('log.serial.st', s=_t('st.ok.conn') if com_ok else _t('st.no.conn')))
                log(_t('log.audio.st', s=_t('st.ok.init') if audio_ok else _t('st.no.init')))
                log(_t('log.video.st', s=_t('st.ok.start') if video_ok else _t('st.no.start')))
                log(_t('log.logfile', f=LOG_FILE))
                log("=" * 56)
                self.root.after(0, lambda: self._update_status(com_ok, audio_ok, urls))
                socketio.run(app, host='0.0.0.0', port=SERVER_PORT,
                             debug=False, use_reloader=False,
                             ssl_context=(cert_file, key_file),
                             allow_unsafe_werkzeug=True)
            except Exception as e:
                log(_t('log.server.fail', e=e))
                traceback.print_exc()
        _gui_threading.Thread(target=_run, daemon=True, name="FlaskServer").start()

    def run(self):
        self._poll_log_queue()
        self.start_server()
        self.root.mainloop()


# ===================== WebRTC Audio/Video Tracks =====================
class RadioAudioSourceTrack(MediaStreamTrack):
    """WebRTC 音频源：从独立队列读取处理后的音频发送给前端（每客户端隔离）"""
    kind = "audio"
    
    _NET_RATE = 16000
    _FRAME_SIZE = 160  # 10ms @ 16kHz
    
    def __init__(self, pc=None):
        super().__init__()
        self._pc = pc
        self._timestamp = 0
        self._start = 0.0
        self._buf = np.zeros((0,), dtype='int16')
        self._closed = False
        self._total_frames = 0
        self._silence_frames = 0
        self._q = _queue.Queue(maxsize=_WEBRTC_LISTENER_MAX)
        self._key = id(self)
        with _webrtc_listener_lock:
            _webrtc_listener_qs[self._key] = self._q

    def _unregister(self):
        if not self._closed:
            self._closed = True
            with _webrtc_listener_lock:
                _webrtc_listener_qs.pop(self._key, None)

    def stop(self):
        self._unregister()
        super().stop()

    def __del__(self):
        try:
            self._unregister()
        except Exception:
            pass
        
    async def recv(self):
        # 只要 aiortc 在拉取本音频轨道，WebRTC 连接即视为活跃。
        # 静音帧路径也必须刷新心跳，避免电台静音/无信号时连接被误判空闲而关闭。
        if self._pc is not None:
            _webrtc_touch_pc(self._pc)
        if self._start > 0:
            self._timestamp += self._FRAME_SIZE
            elapsed = time.monotonic() - self._start
            ideal = self._timestamp / self._NET_RATE
            wait = ideal - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
        else:
            self._start = time.monotonic()
            self._timestamp = 0

        _empty_retries = 0
        while len(self._buf) < self._FRAME_SIZE:
            payload = self._get_audio()
            if payload is not None:
                _empty_retries = 0
                audio_f32 = np.frombuffer(payload[1:], dtype='float32')
                audio_i16 = (np.clip(audio_f32, -1.0, 1.0) * 32767).astype('int16')
                self._buf = np.concatenate([self._buf, audio_i16])
            else:
                _empty_retries += 1
                if _empty_retries > 20:
                    break
                await asyncio.sleep(0.005)

        if len(self._buf) >= self._FRAME_SIZE:
            out = self._buf[:self._FRAME_SIZE]
            self._buf = self._buf[self._FRAME_SIZE:]
            self._total_frames += 1
            if self._pc is not None:
                _webrtc_touch_pc(self._pc)
        else:
            out = np.zeros(self._FRAME_SIZE, dtype='int16')
            self._silence_frames += 1

        frame = av.AudioFrame.from_ndarray(out.reshape(1, -1), format='s16', layout='mono')
        frame.pts = self._timestamp
        frame.sample_rate = self._NET_RATE
        frame.time_base = fractions.Fraction(1, self._NET_RATE)

        if self._total_frames == 0 and self._silence_frames == 500:
            log("WebRTC音频: 队列持续为空，请检查声卡输入")
        elif self._total_frames > 0 and self._total_frames % 1000 == 0:
            log(f"WebRTC音频: 总帧={self._total_frames}, 静音帧={self._silence_frames}, 队列={self._q.qsize()}")

        return frame

    def _get_audio(self):
        try:
            return self._q.get_nowait()
        except _queue.Empty:
            return None


# ============ 视频解码后台线程（避免阻塞 WebRTC 事件循环） ============
_VIDEO_DECODE_EXECUTOR = None

def _get_video_decode_executor():
    """共享的视频解码线程池（避免每个轨道各建线程导致资源累积）"""
    global _VIDEO_DECODE_EXECUTOR
    if _VIDEO_DECODE_EXECUTOR is None:
        _VIDEO_DECODE_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix='viddecode')
    return _VIDEO_DECODE_EXECUTOR

def _decode_video_jpeg(data):
    """在后台线程中解码 MJPEG 帧，失败返回 None"""
    try:
        return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return None


class RadioVideoTrack(MediaStreamTrack):
    """WebRTC 视频源：从当前帧读取视频发送给前端"""
    kind = "video"
    
    def __init__(self, pc=None):
        super().__init__()
        self._pc = pc
        self._pts = 0
        self._last_seq = -1
        self._last_frame_img = None
        self._reuse_slot = 0.0  # 缓存帧重发时间槽（低频心跳，省CPU）
        # 诊断统计：真实帧/缓存帧/黑帧
        self._stat_real = 0
        self._stat_cache = 0
        self._stat_black = 0
        self._stat_log_at = 500
        
    async def recv(self):
        try:
            global _current_frame, _video_frame_seq, _video_stream_active

            # 只要 aiortc 在拉取本轨道，就说明 WebRTC 连接活跃。
            # 黑帧/缓存帧路径也必须刷新心跳，否则 ffmpeg 断流时会因
            # "PC空闲超时" 被 sweeper 误杀（连接仍有效，只是画面暂时停滞）。
            if self._pc is not None:
                _webrtc_touch_pc(self._pc)

            if not _video_stream_active:
                await asyncio.sleep(5.0)
                return self._make_black()

            loop = asyncio.get_running_loop()
            # 轮询等待新帧（用 asyncio.sleep 避免阻塞事件循环）
            for _ in range(40):  # 最多等 40*5ms = 200ms
                with _frame_lock:
                    seq = _video_frame_seq
                    frame_data = _current_frame

                if frame_data is not None and seq != self._last_seq:
                    self._last_seq = seq
                    # 解码是CPU密集操作，放到后台线程执行，避免阻塞事件循环，
                    # 否则同循环上的音频接收/ICE/offer 都会被拖慢（音频受影响）
                    img = await loop.run_in_executor(_get_video_decode_executor(), _decode_video_jpeg, frame_data)
                    if img is not None:
                        self._last_frame_img = img
                        self._reuse_slot = time.monotonic() + 0.25
                        if self._pc is not None:
                            _webrtc_touch_pc(self._pc)
                        self._stat_real += 1
                        self._maybe_log_stats()
                        return self._make_frame(img)

                await asyncio.sleep(0.005)  # 让出事件循环

            # 无新帧：低频心跳返回缓存帧（否则以全帧率反复重编码浪费CPU）
            if self._last_frame_img is not None:
                now = time.monotonic()
                if now < self._reuse_slot:
                    await asyncio.sleep(self._reuse_slot - now)
                self._reuse_slot = time.monotonic() + 0.25
                self._stat_cache += 1
                self._maybe_log_stats()
                # 缓存帧也必须递增 PTS：复用旧 PTS 会让浏览器收到相同时间戳，
                # 判定时间未前进而不渲染，导致画面冻结直到手动刷新
                return self._make_frame(self._last_frame_img)

            # 从未收到过帧 → 返回黑帧
            return self._make_black()

        except Exception:
            pass

        return self._make_black()

    def _make_frame(self, img):
        video_frame = av.VideoFrame.from_ndarray(img, format='bgr24')
        video_frame.pts = self._pts
        video_frame.time_base = fractions.Fraction(1, _video_fps)
        self._pts += 1
        return video_frame

    def _make_black(self):
        self._stat_black += 1
        self._maybe_log_stats()
        w = int(config['VIDEO'].get('WIDTH', 320))
        h = int(config['VIDEO'].get('HEIGHT', 240))
        img = np.zeros((h, w, 3), dtype=np.uint8)
        return self._make_frame(img)

    def _maybe_log_stats(self):
        total = self._stat_real + self._stat_cache + self._stat_black
        if total >= self._stat_log_at:
            log("视频轨统计: 真实帧={} 缓存帧={} 黑帧={} pts={} seq={}".format(
                self._stat_real, self._stat_cache, self._stat_black, self._pts, self._last_seq))
            self._stat_log_at = total + 500


# ===================== WebRTC 信令接口 =====================
@app.route('/webrtc/offer', methods=['POST'])
def webrtc_offer():
    """处理 WebRTC Offer - 使用持久化事件循环"""
    log("收到 WebRTC Offer 请求")
    try:
        params = request.get_json()
        future = asyncio.run_coroutine_threadsafe(_webrtc_process(params), _webrtc_loop)
        result = future.result(timeout=30)
        return jsonify(result)
    except Exception as e:
        log(f"WebRTC Offer处理失败: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

async def _webrtc_process(params):
    """WebRTC 处理（在持久化事件循环中运行）"""
    global _webrtc_mic_restart, _webrtc_conn_gen
    _webrtc_mic_restart = 0
    _webrtc_conn_gen += 1
    conn_gen = _webrtc_conn_gen
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection(RTCConfiguration(
        iceServers=[
            RTCIceServer(urls="stun:stun.miwifi.com:3478"),
            RTCIceServer(urls="stun:stun.chat.bilibili.com:3478"),
            RTCIceServer(urls="stun:stun.qq.com:3478"),
            RTCIceServer(urls="stun:stun.cloudflare.com:3478"),
        ]
    ))
    _webrtc_pcs.add(pc)
    _webrtc_touch_pc(pc)
    log(f"WebRTC 新建连接, 活跃pc数={len(_webrtc_pcs)}")

    source_audio = RadioAudioSourceTrack(pc)
    pc.addTrack(source_audio)

    video_track = RadioVideoTrack(pc)
    pc.addTrack(video_track)

    gathering_done = asyncio.Event()

    @pc.on("icegatheringstatechange")
    async def on_ice_gather():
        if pc.iceGatheringState == "complete":
            gathering_done.set()

    @pc.on("iceconnectionstatechange")
    async def on_ice_state():
        log(f"WebRTC ICE状态: {pc.iceConnectionState}")
        _webrtc_touch_pc(pc)
        if pc.iceConnectionState in ("failed", "closed"):
            try: await pc.close()
            except: pass
            _webrtc_pcs.discard(pc)
            with _webrtc_pc_ts_lock:
                _webrtc_pc_ts.pop(id(pc), None)

    @pc.on("track")
    async def on_track(track):
        log(f"WebRTC 收到轨道: {track.kind}")
        if track.kind == "audio":
            asyncio.create_task(handle_webrtc_audio(track, pc, conn_gen))

    await pc.setRemoteDescription(offer)

    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    try:
        await asyncio.wait_for(gathering_done.wait(), timeout=3.0)
    except asyncio.TimeoutError:
        log("WebRTC ICE收集超时，使用已收集的候选")

    log("WebRTC 连接成功")
    return {
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type
    }


_webrtc_mic_frame_count = 0
_webrtc_mic_restart = 0
_webrtc_mic_sr_checked = set()
_webrtc_conn_gen = 0  # WebRTC 连接代数：新连接递增，旧连接的麦克风handler据此停止

def _resample_linear(x, in_rate, out_rate):
    """线性重采样：把单声道float32对齐到目标采样率（TX方向，防变调/倍速）"""
    if in_rate == out_rate:
        return x.astype(np.float32)
    n_out = max(1, int(round(x.shape[0] * out_rate / in_rate)))
    if x.shape[0] == 1:
        return np.full(n_out, x[0], dtype='float32')
    xp = np.arange(x.shape[0], dtype='float64')
    xi = np.linspace(0.0, x.shape[0] - 1, n_out)
    return np.interp(xi, xp, x.astype('float64')).astype('float32')

async def handle_webrtc_audio(track, pc=None, conn_gen=None):
    """处理收到的WebRTC音频（前端麦克风）"""
    global _webrtc_mic_frame_count, current_mic_gain_level, _webrtc_mic_restart, _webrtc_mic_sr_checked, _webrtc_conn_gen
    log("WebRTC 麦克风音频处理开始")
    try:
        while True:
            frame = await track.recv()
            # 若已被更新的连接取代（刷新页面/重新激活音频），立即停止，
            # 避免新旧连接同时写入 _tx_audio_queue 造成TX音频加倍
            if conn_gen is not None and conn_gen != _webrtc_conn_gen:
                log("WebRTC音频: 已被新连接取代，停止")
                break
            if pc is not None:
                _webrtc_touch_pc(pc)
            _webrtc_mic_frame_count += 1
            if _webrtc_mic_frame_count > 0 and _webrtc_mic_frame_count % 1000 == 0:
                log(f"WebRTC麦克风: 已收{_webrtc_mic_frame_count}帧")
            audioData = frame.to_ndarray()
            if audioData.ndim > 1:
                if audioData.shape[0] <= audioData.shape[1]:
                    audioData = audioData[0, :]
                else:
                    audioData = audioData[:, 0]
            # aiortc 解码Opus得到float32[-1,1]，若已float则不除32768
            if audioData.dtype in (np.float32, np.float64):
                audio_f32 = audioData.astype('float32')
            else:
                audio_f32 = audioData.astype('float32') / 32768.0
            # 采样率对齐：前端opus帧率依设备/协商而定(16k/44.1k/48k)，
            # 输出声卡按 SAMPLING_RATE 打开，必须先重采样否则TX倍速变调
            frame_rate = int(getattr(frame, 'sample_rate', 0) or 0)
            if frame_rate and frame_rate != SAMPLING_RATE:
                if frame_rate not in _webrtc_mic_sr_checked:
                    _webrtc_mic_sr_checked.add(frame_rate)
                    log(f"WebRTC麦克风采样率 {frame_rate}Hz → 输出 {SAMPLING_RATE}Hz 重采样")
                audio_f32 = _resample_linear(audio_f32, frame_rate, SAMPLING_RATE)
            # 应用麦克风增益
            audio_f32 *= MIC_GAIN_LEVELS[current_mic_gain_level]
            # 增益可能把幅度推出[-1,1]，必须先钳值再进入声卡输出（RX路径已做，此处补齐）
            audio_f32 = np.clip(audio_f32, -1.0, 1.0).astype('float32')
            
            try:
                _tx_audio_queue.put_nowait(audio_f32)
            except _queue.Full:
                # 队列满：正常情况下消费者应跟上；频繁触发说明TX写声卡卡顿，
                # 丢帧不再静默——计数并按 100 帧节流上报，便于排查发射变调/断续
                global _mic_skip_count
                _mic_skip_count += 1
                if _mic_skip_count % 100 == 1:
                    log(f"TX音频队列满，丢最旧帧（累计{_mic_skip_count}）")
                try:
                    _tx_audio_queue.get_nowait()
                except:
                    pass
                try:
                    _tx_audio_queue.put_nowait(audio_f32)
                except:
                    pass
    except Exception as e:
        log(f"WebRTC音频接收结束: {e}")
        _webrtc_mic_restart += 1
        # pc 已失效时不再重试：失效 track 的 recv() 会立即抛异常，
        # 无限重试只会堆积 asyncio 任务并刷屏日志
        still_alive = True
        if pc is not None:
            try:
                still_alive = pc.iceConnectionState not in ("failed", "closed")
            except Exception:
                still_alive = False
        if still_alive and _webrtc_mic_restart <= 1:
            log("WebRTC音频任务1秒后重试...")
            await asyncio.sleep(1.0)
            asyncio.create_task(handle_webrtc_audio(track, pc, conn_gen))


@app.route('/webrtc/ice', methods=['POST'])
def webrtc_ice():
    """处理 ICE Candidate"""
    try:
        params = request.get_json()
        future = asyncio.run_coroutine_threadsafe(_webrtc_add_ice(params), _webrtc_loop)
        future.result(timeout=10)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

async def _webrtc_add_ice(params):
    candidate = RTCIceCandidate(
        component=params.get("component", 1),
        foundation=params.get("foundation", ""),
        ip=params.get("ip", ""),
        port=params.get("port", 0),
        priority=params.get("priority", 0),
        protocol=params.get("protocol", "udp"),
        type=params.get("type", "host"),
        sdpMid=params.get("sdpMid"),
        sdpMLineIndex=params.get("sdpMLineIndex"),
    )
    for pc in list(_webrtc_pcs):
        _webrtc_touch_pc(pc)
        try:
            await pc.addIceCandidate(candidate)
        except Exception as e:
            log(f"添加ICE Candidate失败: {e}")
    return {"status": "ok"}


# ===================== 主入口 =====================
def main():
    gui = FT710ServerGUI()
    gui.run()


if __name__ == '__main__':
    main()