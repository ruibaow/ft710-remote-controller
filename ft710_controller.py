# FT-710 Remote Controller v2.1
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
from tkinter import ttk, scrolledtext, messagebox
import threading as _gui_threading
import threading as _threading
import queue as _queue
import logging
import logging.handlers
from datetime import datetime
import collections
from concurrent.futures import ThreadPoolExecutor

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

# ===================== 日志系统 =====================
import queue as _gui_log_q
_gui_log_queue = _gui_log_q.Queue()

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
        'DEVICE_OUT': '-1'
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
        'WIDTH': '320',
        'HEIGHT': '240',
        'FPS': '15',
        'QUALITY': '15'
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

def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        config.write(f)

config = load_config()

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
    'ws.gain.rx':         {'zh': '🔊 接收增益（5级，0.5x~2.5x）', 'en': '🔊 RX Gain (5 levels, 0.5x~2.5x)'},
    'ws.gain.mic':        {'zh': '🎙️ 麦克风增益',   'en': '🎙️ Mic Gain'},
    'ws.noise':           {'zh': '🔇 DSP降噪',       'en': '🔇 DSP NR'},
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
GAIN_LEVELS = [0.5, 1.0, 1.5, 2.0, 2.5]
current_gain_level = 1

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

# ===================== 降噪状态 =====================
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

_RING_SIZE = max(4, int(0.5 * SAMPLING_RATE / AUDIO_BLOCK_SIZE))
_ring_buf = np.zeros((_RING_SIZE, AUDIO_BLOCK_SIZE), dtype='float32')
_ring_write = 0
_ring_read  = 0
_ring_lock  = _threading.Lock()
_ring_event = _threading.Event()

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

def _check_vad(audio, vad_hang):
    rms = float(np.sqrt(np.mean(audio ** 2)))
    db = 20 * np.log10(rms + 1e-10)
    if db > _VAD_THRESHOLD_DB:
        return _VAD_HANG_FRAMES, True, rms
    elif vad_hang > 0:
        return vad_hang - 1, True, rms
    else:
        return 0, False, rms

def _dsp_worker():
    global _ring_read, _downsampler, _rx_sos, _rx_zi, SAMPLING_RATE, _webrtc_audio_accum
    global _dsp_error_count

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
                _drain_send_queue()
                continue

            try:
                with noise_lock:
                    noise_level = current_noise_level
                with gain_lock:
                    gain_value  = GAIN_LEVELS[current_gain_level]

                # 下采样：48kHz -> 16kHz
                if _downsampler is not None:
                    audio = _downsampler.process(audio)
                    if audio is None:
                        continue

                # 带通滤波（持续状态，消除帧边界不连续）
                with _dsp_state_lock:
                    audio, _rx_zi = sosfilt(_rx_sos, audio, zi=_rx_zi)
                audio = audio.astype('float32')

                # VAD 检测：静音期直接丢弃（rms 复用，避免重复计算）
                vad_hang, is_speech, frame_rms = _check_vad(audio, vad_hang)
                if not is_speech:
                    _dsp_skip_vad += 1
                    continue

                # 增益前 RMS 门控
                if frame_rms < 0.004:
                    _dsp_skip_rms += 1
                    continue

                if nb_enabled:
                    audio = apply_noise_blanker(audio)

                if noise_level > 0:
                    audio = apply_noise_reduction(audio, noise_level)

                if gain_value != 1.0:
                    audio = audio * gain_value

                audio = np.clip(audio, -0.95, 0.95).astype('float32')

                # 降噪后二次 RMS 检查
                if float(np.sqrt(np.mean(audio ** 2))) < 0.002:
                    continue

                # WebRTC 音频路径：累积到160-sample帧（10ms @ 16kHz）
                _webrtc_audio_accum = np.concatenate([_webrtc_audio_accum, audio])
                while len(_webrtc_audio_accum) >= 160:
                    chunk_f32 = _webrtc_audio_accum[:160]
                    _webrtc_audio_accum = _webrtc_audio_accum[160:]
                    chunk_payload = b'\x01' + chunk_f32.tobytes()
                    _webrtc_audio_broadcast(chunk_payload)
                _dsp_processed += 1
                _dsp_error_count = 0  # 成功处理即清零：瞬时错误不再累积到致命阈值
            except Exception as e:
                _dsp_error_count += 1
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

def _dsp_supervisor():
    """DSP线程守护：异常退出后自动重启，确保RX音频永不静默死亡"""
    global _dsp_thread, _dsp_error_count
    while not _running.is_set():
        time.sleep(5)
        try:
            if not _dsp_thread.is_alive():
                _dsp_error_count = 0
                log("DSP线程已退出，自动重启")
                _dsp_thread = _threading.Thread(target=_dsp_worker, daemon=True, name='AudioDSP')
                _dsp_thread.start()
        except Exception:
            pass

_threading.Thread(target=_dsp_supervisor, daemon=True, name='AudioDSPSupervisor').start()

# ===================== Flask初始化 =====================
app = Flask(__name__)
app.config['SECRET_KEY'] = 'ft710-audio'

def get_all_network_ips():
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

_all_ips = get_all_network_ips()
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='threading',
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
    ffmpeg_path = os.path.join(exe_dir, 'ffmpeg', 'ffmpeg.exe')
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
    global _ffmpeg_process, video_running, _current_frame, _video_frame_seq, _video_fps

    fps = int(config['VIDEO'].get('FPS', 10))
    _video_fps = fps
    quality = int(config['VIDEO'].get('QUALITY', 3))
    width = int(config['VIDEO'].get('WIDTH', 320))
    height = int(config['VIDEO'].get('HEIGHT', 240))
    dev_name = config['VIDEO'].get('DEVICE_NAME', 'USB Video')

    cmd = [
        None,  # ffmpeg path, filled below
        '-y',
        '-f', 'dshow',
        '-rtbufsize', '64M',          # 采集缓冲加大，避免CPU繁忙时丢帧/缓冲溢出
        '-video_size', '{}x{}'.format(width, height),
        '-framerate', str(fps),
        '-i', f'video={dev_name}',
        '-vf', 'scale={}:{}'.format(width, height),
        '-c:v', 'mjpeg',              # 显式指定输出 MJPEG，避免 image2pipe 默认编码器漂移
        '-q:v', str(quality),
        '-fflags', 'nobuffer',        # 禁用输出缓冲
        '-f', 'image2pipe',
        '-'
    ]

    CREATE_NO_WINDOW = 0x08000000
    stderr_lines = []
    restart_backoff = 1  # 连续快速失败退避系数：1,2,3,4 封顶4（对应等待 1.5-6s）

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

    while _video_stream_active:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            log("未找到ffmpeg")
            return
        cmd[0] = ffmpeg
        stderr_lines.clear()

        log("FFmpeg: {}x{} {}fps MJPEG q={} (低延迟)".format(width, height, fps, quality))
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
                    if frame_count % 300 == 0:
                        log("视频采集: 累计{}帧, seq={}".format(frame_count, _video_frame_seq))
                # 保护：异常数据流下避免缓冲区无限增长
                if len(buffer) > (4 << 20):
                    del buffer[:]
        finally:
            proc = _ffmpeg_process
            if proc is not None:
                code = proc.poll()
                log("视频循环退出, 共{}帧".format(frame_count))
                log("ffmpeg exit code={}".format(code))
                if stderr_lines:
                    tail = stderr_lines[-30:]
                    log("--- ffmpeg stderr 最近{}行 ---".format(len(tail)))
                    for l in tail:
                        log("  " + l)
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

        # 退避策略：运行时间过短(设备未释放/异常)则重试；但设备断流后应立即尝试恢复，
        # 避免退避时间过长导致视频长时间黑屏。等待期间不断探测，设备释放即尽快重启。
        if frame_count < 30:
            restart_backoff = min(restart_backoff + 1, 4)  # 1,2,3,4 封顶4
            wait_secs = min(1.5 * restart_backoff, 6.0)
            log("视频捕获启动后很快失败({}帧)，{}s 后重试(第{}次)".format(
                frame_count, wait_secs, restart_backoff))
        else:
            restart_backoff = 1
            wait_secs = 3.0  # 正常结束后稳定等待，避免设备未释放
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

        if _ffmpeg_process:
            try:
                if _ffmpeg_process.poll() is None:
                    _ffmpeg_process.terminate()
                    try:
                        _ffmpeg_process.wait(timeout=3.0)
                    except Exception:
                        _ffmpeg_process.kill()
                        _ffmpeg_process.wait(timeout=1.0)
            except Exception:
                pass
            with _ffmpeg_pids_lock:
                _ffmpeg_spawned_pids.discard(_ffmpeg_process.pid)
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
                ser = serial.Serial(COM_PORT, BAUD_RATE, timeout=1)
                time.sleep(0.5)
                init_yaesu_radio()
            except Exception as e:
                log(f"串口自动重连失败：{e}")
                return False
        
        # Yaesu FT-710 频率设置需要先选择VFO-A
        if cmd == "FA":
            with lock:
                vs_cmd = build_yaesu_cmd("VS", "0")
                ser.write(vs_cmd.encode('ascii'))
                time.sleep(0.02)
                raw_cmd = build_yaesu_cmd(cmd, value)
                ser.write(raw_cmd.encode('ascii'))
                time.sleep(delay)
                resp = b""
                try:
                    read_count = 0
                    while ser.in_waiting > 0 and read_count < 20:
                        resp += ser.read(ser.in_waiting)
                        time.sleep(0.05)
                        read_count += 1
                except:
                    pass
        else:
            raw_cmd = build_yaesu_cmd(cmd, value)
            with lock:
                ser.write(raw_cmd.encode('ascii'))
                time.sleep(delay)
                resp = b""
                try:
                    read_count = 0
                    while ser.in_waiting > 0 and read_count < 20:
                        resp += ser.read(ser.in_waiting)
                        time.sleep(0.05)
                        read_count += 1
                except:
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
            ser = serial.Serial(use_port, use_baud, timeout=1)
            time.sleep(0.5)
            radio_state["com_port"] = use_port
            radio_state["baud_rate"] = use_baud
        except Exception as e:
            log(_t('log.com.init.fail', e=e))
            radio_state["audio_status"] = _t('srv.audio.errcmd', e=str(e))
            return False
    init_yaesu_radio()
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
    frame_rms = float(np.sqrt(np.mean(audio ** 2)))
    
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

    rms = float(np.sqrt(np.mean(audio ** 2)))
    gate = noise_config["gate"]
    aggr = noise_config["aggressiveness"]

    global _noise_profile
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


def init_audio_streams():
    global audio_output_stream, audio_input_stream, SAMPLING_RATE, _downsampler, _rx_sos, _rx_zi, AUDIO_BLOCK_SIZE

    if not get_audio_devices():
        return False

    configured_dev_in = config['AUDIO'].get('DEVICE_IN', '-1')
    configured_dev_out = config['AUDIO'].get('DEVICE_OUT', '-1')
    
    dev_in  = int(configured_dev_in) if configured_dev_in != '-1' else -1
    dev_out = int(configured_dev_out) if configured_dev_out != '-1' else -1
    
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
    global _ring_write
    with _ring_lock:
        samples = indata[:, 0]
        if samples.dtype != np.float32:
            samples = samples.astype(np.float32) / 32768.0
        _ring_buf[_ring_write % _RING_SIZE] = samples
        _ring_write = (_ring_write + 1) % _RING_SIZE
    _ring_event.set()

def _reset_dsp_state():
    global _rx_zi, _noise_profile
    _rx_zi = _sosfilt_zi(_rx_sos) * 0.0
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
        socketio.emit('trx_updated', {'trx': 'RX'}, namespace='/', broadcast=True)

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
        while not _running.is_set():
            time.sleep(5)
            if tx_active.is_set():
                continue
            try:
                input_ok = audio_input_stream is not None and audio_input_stream.active
                output_ok = audio_output_stream is not None and audio_output_stream.active
                if not input_ok:
                    log("看门狗：接收音频流异常，尝试恢复...")
                    restart_audio_streams()
                elif not output_ok:
                    log("看门狗：发送音频流异常，尝试恢复...")
                    restart_audio_streams()
            except Exception:
                pass

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
                socketio.emit('trx_updated', {'trx': 'RX'}, namespace='/', broadcast=True)
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
        'ptt_mode': PTT_MODE,
        'power': radio_state.get('power', 25),
        'com_port': radio_state['com_port'],
        'baud_rate': radio_state['baud_rate'],
        'meter': radio_state.get('meter', {"pwr": 0, "ref": 0, "swr": 1.0, "eff": 0, "connected": False})
    }, namespace='/')


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

        global audio_output_stream, audio_input_stream, _rx_start_seq, tx_start_time

        if t == "TX":
            tx_active.set()
            tx_start_time = time.monotonic()
            _rx_start_seq = (_rx_start_seq + 1) % 65536
            global _tx_squelch_until
            _tx_squelch_until = time.monotonic() + 0.05
            _reset_ring_buffer()
            _drain_send_queue()
        else:
            tx_active.clear()
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
    _noise_profile["spectrum"] = None
    _noise_profile["history"] = []
    
    status_text = "关闭" if level == 0 else f"{level}级"
    log(f"降噪等级已设置为：{status_text}")
    emit('noise_level_updated', {
        'level': level,
        'status': status_text
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
        f = request.json.get('f', 14.270)
        # FT-710 需要9位Hz数字，如14.270MHz = 014270000
        freq_hz = int(f * 1000000)
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
        
        if m in mode_map:
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
    global tx_start_time, _rx_start_seq, _tx_squelch_until
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
            tx_active.set()
            tx_start_time = time.monotonic()
            _rx_start_seq = (_rx_start_seq + 1) % 65536
            _tx_squelch_until = time.monotonic() + 0.05
            _reset_ring_buffer()
            _drain_send_queue()
        else:
            tx_active.clear()
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

        band = get_band_from_freq(float(freq))
        if not band:
            return jsonify({'status': 'error', 'msg': _t('srv.channel.unsup')}), 400

        for existing_key, existing_value in config['CHANNELS'].items():
            parts = existing_value.split(',')
            if parts and abs(float(parts[0]) - float(freq)) < 0.0001:
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
        mode_code = mode_map.get(mode, '02')
        send_radio("MODE", mode_code)

        f = float(freq)
        freq_hz = int(f * 1000000)
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
            f = float(new_freq)
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
@app.route('/')
def page():
    ip = _all_ips[0] if _all_ips else socket.gethostbyname(socket.gethostname())
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
</style>
</head>
<body>
<div class="card">
<div class="title-container">
    <h1 class="main-title">FT-710 Remote Controller</h1>
    <div class="subtitle">Yaesu FT-710</div>
</div>

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

<div style="display:flex; gap:8px; margin-top:10px;">
    <button id="activate-audio" class="activate-btn confirm-btn" style="flex:1;" data-i18n="ws.btn.act">🎧 激活音频</button>
    <button class="restart-btn" onclick="restartAudio()" style="flex:1; margin-top:0;" data-i18n="ws.btn.restart">🔄 重启音频</button>
</div>

<div class="level-control">
    <div class="level-title"><span data-i18n="ws.gain.rx">🔊 接收增益（5级，0.5x~2.5x）</span><span class="level-value" id="gain-status">1.0x</span></div>
    <div class="level-buttons">
        <button class="level-btn gain-0" onclick="setGainLevel(0)">0.5x</button>
        <button class="level-btn gain-1" onclick="setGainLevel(1)">1.0x</button>
        <button class="level-btn gain-2" onclick="setGainLevel(2)">1.5x</button>
        <button class="level-btn gain-3" onclick="setGainLevel(3)">2.0x</button>
        <button class="level-btn gain-4" onclick="setGainLevel(4)">2.5x</button>
    </div>
</div>

<div class="level-control">
    <div class="level-title"><span data-i18n="ws.gain.mic">🎙️ 麦克风增益</span><span class="level-value" id="mic-gain-status">100%</span></div>
    <div class="level-buttons">
        <button class="level-btn mic-0" onclick="setMicGainLevel(0)">100%</button>
        <button class="level-btn mic-1" onclick="setMicGainLevel(1)">125%</button>
        <button class="level-btn mic-2" onclick="setMicGainLevel(2)">150%</button>
        <button class="level-btn mic-3" onclick="setMicGainLevel(3)">175%</button>
        <button class="level-btn mic-4" onclick="setMicGainLevel(4)">200%</button>
    </div>
</div>

<div class="level-control">
    <div class="level-title"><span data-i18n="ws.noise">🔇 DSP降噪</span><span class="level-value" id="noise-status">关闭</span></div>
    <div class="level-buttons">
        <button class="level-btn noise-0" onclick="setNoiseLevel(0)" data-i18n="noise.off">关闭</button>
        <button class="level-btn noise-1" onclick="setNoiseLevel(1)" data-i18n="noise.lvl1">1级</button>
        <button class="level-btn noise-2" onclick="setNoiseLevel(2)" data-i18n="noise.lvl2">2级</button>
        <button class="level-btn noise-3" onclick="setNoiseLevel(3)" data-i18n="noise.lvl3">3级</button>
        <button class="level-btn noise-4" onclick="setNoiseLevel(4)" data-i18n="noise.lvl4">4级</button>
        <button class="level-btn noise-5" onclick="setNoiseLevel(5)" data-i18n="noise.lvl5">5级</button>
    </div>
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

<div style="margin-top:14px; padding-top:10px; border-top:1px solid #333; font-size:11px; color:#555; text-align:center;">FT-710 Remote Controller</div>

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
let currentGainLevel = 1;
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
const SAMPLING_RATE = {SAMPLING_RATE};
const AUDIO_BLOCK_SIZE = {AUDIO_BLOCK_SIZE};
const SERVER_PORT = {SERVER_PORT};
const el = document.getElementById('access-ip');
if (el) el.textContent = window.location.hostname + ':' + SERVER_PORT;
let channelData = {{}};

function initSocket() {{
    socket = io(window.location.origin, {{
        reconnection: true,
        reconnectionAttempts: 10,
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
    document.querySelectorAll('.gain-0, .gain-1, .gain-2, .gain-3, .gain-4').forEach((btn, idx) => {{
        btn.classList.remove('active');
        if (idx === currentGainLevel) btn.classList.add('active');
    }});
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
    document.querySelectorAll('.noise-0, .noise-1, .noise-2, .noise-3, .noise-4, .noise-5').forEach((btn, idx) => {{
        btn.classList.remove('active');
        if (idx === currentNoiseLevel) btn.classList.add('active');
    }});
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
    document.querySelectorAll('.mic-0, .mic-1, .mic-2, .mic-3, .mic-4').forEach((btn, idx) => {{
        btn.classList.remove('active');
        if (idx === currentMicGainLevel) btn.classList.add('active');
    }});
}}

// ===================== WebRTC =====================
let webrtc_pc = null;
let webrtc_remoteStream = null;
let _webrtc_audioEl = null;
let _webrtc_reconnectTimer = null;
let _webrtc_reconnectAttempts = 0;
const _WEBRTC_MAX_RECONNECT = 10;

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
                if (!_webrtc_audioEl) {{
                    _webrtc_audioEl = document.createElement('audio');
                    _webrtc_audioEl.id = 'webrtc-audio';
                    _webrtc_audioEl.autoplay = true;
                    document.body.appendChild(_webrtc_audioEl);
                }}
                _webrtc_audioEl.srcObject = webrtc_remoteStream;
                _webrtc_audioEl.play().catch(e => console.warn('音频自动播放被阻止，需用户交互:', e));
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
    if (_webrtc_audioEl) {{ _webrtc_audioEl.srcObject = null; }}
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
                item.innerHTML = `
                    <div class="channel-item-info" onclick="selectChannelByKey('${{ch.key}}')">
                        <div class="channel-item-name">${{ch.freq}} MHz / ${{ch.mode}}</div>
                    </div>
                    <div class="channel-item-btns">
                        <button onclick="event.stopPropagation();editChannelUI('${{ch.key}}')">✏️</button>
                        <button onclick="event.stopPropagation();deleteChannelConfirm('${{ch.key}}')">🗑️</button>
                    </div>
                `;
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
class FT710ServerGUI:
    VERSION = "2.1"
    
    C = {
        "bg":         "#dce8f5",
        "glass":      "#eef4fc",
        "glass2":     "#e2eef8",
        "border":     "#b8d0ea",
        "sep":        "#c8ddf0",
        "accent":     "#1a7fcb",
        "accent2":    "#0a5fa0",
        "text":       "#1a2a3a",
        "text2":      "#3a5570",
        "text3":      "#6a8aaa",
        "ok":         "#12875c",
        "err":        "#cc2222",
        "warn":       "#b06a00",
        "btn":        "#1a7fcb",
        "btn_fg":     "#ffffff",
    }

    def __init__(self):
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        self.root = tk.Tk()
        self.root.title(f"FT-710 Remote Controller  v{self.VERSION}")
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
            socketio.emit('set_lang', {'code': code}, namespace='/', broadcast=True)
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
        tk.Frame(dlg, bg=self.C["accent"], height=3).pack(fill="x")
        return dlg

    def _dialog_com(self):
        C = self.C
        dlg = self._styled_dialog(_t('dlg.com.title'), 340, 210)

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
                               width=13, state="readonly", font=("Microsoft YaHei", 10))
        cb_port.grid(row=0, column=1, padx=8, pady=6)

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
        tk.Button(card, text="🔄", command=_refresh,
                  bg=C["btn"], fg=C["btn_fg"], font=("Microsoft YaHei", 9),
                  relief="flat", padx=6, cursor="hand2").grid(row=0, column=2, padx=2)

        tk.Label(card, text=_t('lbl.baud'), bg=C["glass"], fg=C["text2"],
                 font=("Microsoft YaHei", 9)).grid(row=1, column=0, sticky="w", padx=4, pady=6)
        e_baud = tk.Entry(card, font=("Microsoft YaHei", 10), width=14,
                          bg="#fff", fg=C["text"], insertbackground=C["text"],
                          relief="flat",
                          highlightbackground=C["border"], highlightthickness=1)
        e_baud.insert(0, config['SERIAL']['BAUD_RATE'])
        e_baud.grid(row=1, column=1, padx=8, pady=6)

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

        btn = tk.Button(dlg, text=_t('btn.apply.reconn'), command=_apply,
                        bg=C["btn"], fg=C["btn_fg"],
                        font=("Microsoft YaHei", 10), relief="flat",
                        padx=16, pady=5, cursor="hand2",
                        activebackground=C["accent2"], activeforeground="#fff")
        btn.pack(pady=4)

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
        dlg = self._styled_dialog(_t('dlg.audio.title'), 650, 260)

        card = tk.Frame(dlg, bg=C["glass"],
                        highlightbackground=C["border"], highlightthickness=1,
                        pady=12, padx=18)
        card.pack(fill="x", padx=12, pady=10)

        try:
            devices = sd.query_devices()
            input_devices = []
            output_devices = []
            for i, d in enumerate(devices):
                dev_name = d.get('name', f'Device#{i}')
                if d.get('max_input_channels', 0) > 0:
                    input_devices.append((i, dev_name))
                if d.get('max_output_channels', 0) > 0:
                    output_devices.append((i, dev_name))
        except Exception as e:
            messagebox.showerror(_t('err.title'), _t('err.audio.dev', e=e), parent=dlg)
            return

        cur_device_in = int(config['AUDIO'].get('DEVICE_IN', -1))
        cur_device_out = int(config['AUDIO'].get('DEVICE_OUT', -1))

        tk.Label(card, text=_t('lbl.input.dev'), bg=C["glass"], fg=C["text2"],
                 font=("Microsoft YaHei", 9)).grid(row=0, column=0, sticky="w", padx=4, pady=6)
        
        sys_default = _t('lbl.sys.default')
        in_values = [sys_default] + [name for i, name in input_devices]
        in_indices = [-1] + [i for i, name in input_devices]
        
        cur_in_idx = 0
        if cur_device_in in in_indices:
            cur_in_idx = in_indices.index(cur_device_in)
        
        cb_in = ttk.Combobox(card, values=in_values,
                             width=55, state="readonly", font=("Microsoft YaHei", 10))
        cb_in.grid(row=0, column=1, columnspan=2, sticky="ew", padx=8, pady=6)
        try:
            cb_in.current(cur_in_idx)
        except:
            cb_in.current(0)

        tk.Label(card, text=_t('lbl.output.dev'), bg=C["glass"], fg=C["text2"],
                 font=("Microsoft YaHei", 9)).grid(row=1, column=0, sticky="w", padx=4, pady=6)
        
        out_values = [sys_default] + [name for i, name in output_devices]
        out_indices = [-1] + [i for i, name in output_devices]
        
        cur_out_idx = 0
        if cur_device_out in out_indices:
            cur_out_idx = out_indices.index(cur_device_out)
        
        cb_out = ttk.Combobox(card, values=out_values,
                              width=55, state="readonly", font=("Microsoft YaHei", 10))
        cb_out.grid(row=1, column=1, columnspan=2, sticky="ew", padx=8, pady=6)
        try:
            cb_out.current(cur_out_idx)
        except:
            cb_out.current(0)

        def _apply():
            in_sel = cb_in.get()
            out_sel = cb_out.get()
            
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

        btn = tk.Button(dlg, text=_t('btn.apply.audio'), command=_apply,
                        bg=C["btn"], fg=C["btn_fg"],
                        font=("Microsoft YaHei", 10), relief="flat",
                        padx=16, pady=5, cursor="hand2",
                        activebackground=C["accent2"], activeforeground="#fff")
        btn.pack(pady=8)

    def _dialog_video(self):
        """视频设备配置对话框"""
        C = self.C
        dlg = self._styled_dialog(_t('dlg.video.title'), 420, 320)

        if not CV2_AVAILABLE:
            tk.Label(dlg, text=_t('lbl.video.nocv'),
                     bg=C["glass"], fg=C["err"], font=("Microsoft YaHei", 10)).pack(pady=20)
            tk.Button(dlg, text=_t('btn.close'), command=dlg.destroy,
                      bg=C["glass"], fg=C["text"], font=("Microsoft YaHei", 10)).pack()
            return

        card = tk.Frame(dlg, bg=C["glass"],
                        highlightbackground=C["border"], highlightthickness=1,
                        pady=12, padx=18)
        card.pack(fill="x", padx=12, pady=10)

        # 存储控件引用
        widgets = {'res_var': None, 'quality_var': None, 'chk_enabled': None}
        device_var = tk.StringVar(value=config['VIDEO'].get('DEVICE_INDEX', '0'))
        res_var = tk.StringVar(value=f"{config['VIDEO'].get('WIDTH', '640')}x{config['VIDEO'].get('HEIGHT', '480')}")
        quality_var = tk.StringVar(value=config['VIDEO'].get('QUALITY', '15'))
        chk_enabled_var = tk.BooleanVar(value=config['VIDEO'].get('ENABLED', 'false').lower() == 'true')

        result = {'devices': [], 'ready': False}

        def scan_and_show():
            # 后台扫描
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
            # 更新UI
            dlg.after(0, lambda: update_device_list(devices))

        def update_device_list(devices):
            # 清除扫描提示
            for w in card.grid_slaves():
                if w.grid_info().get('row') == 0:
                    w.destroy()
            
            # 设备列表
            tk.Label(card, text=_t('lbl.camera'), bg=C["glass"], fg=C["text2"],
                     font=("Microsoft YaHei", 9)).grid(row=0, column=0, sticky="w", padx=4, pady=6)
            
            # 读取上次保存的设备索引
            saved_device = int(config['VIDEO'].get('DEVICE_INDEX', 0))
            
            if devices:
                values = [_t('lbl.device', i=i) for i in devices]
                cb = ttk.Combobox(card, values=values, state="readonly", font=("Microsoft YaHei", 9))
                cb.grid(row=0, column=1, sticky="ew", padx=8, pady=6)
                
                # 选中和上次相同的设备，如果没有则选第一个
                if saved_device in devices:
                    cb.current(devices.index(saved_device))
                    device_var.set(str(saved_device))
                else:
                    cb.current(0)
                    device_var.set(str(devices[0]))
                
                cb.bind('<<ComboboxSelected>>', lambda e: device_var.set(str(devices[cb.current()])))
            else:
                tk.Label(card, text=_t('lbl.no.dev'), bg=C["glass"], fg=C["err"],
                         font=("Microsoft YaHei", 9)).grid(row=0, column=1, sticky="w", padx=8, pady=6)
            
            card.columnconfigure(1, weight=1)

        # 显示扫描中
        tk.Label(card, text=_t('lbl.scanning'), bg=C["glass"], fg=C["text3"],
                 font=("Microsoft YaHei", 9)).grid(row=0, column=0, columnspan=2, pady=10)

        # 分辨率 - 改为下拉框，只提供两个选项
        tk.Label(card, text=_t('lbl.res'), bg=C["glass"], fg=C["text2"],
                 font=("Microsoft YaHei", 9)).grid(row=1, column=0, sticky="w", padx=4, pady=6)
        res_var = tk.StringVar(value="{}x{}".format(
            config['VIDEO'].get('WIDTH', '320'),
            config['VIDEO'].get('HEIGHT', '240')))
        res_cb = ttk.Combobox(card, values=["320x240", "800x600"], 
                            textvariable=res_var, state="readonly", font=("Microsoft YaHei", 9))
        res_cb.grid(row=1, column=1, sticky="w", padx=8, pady=6)
        res_cb.bind('<<ComboboxSelected>>', lambda e: res_var.set(res_cb.get()))

        # 帧率
        tk.Label(card, text=_t('lbl.fps'), bg=C["glass"], fg=C["text2"],
                 font=("Microsoft YaHei", 9)).grid(row=2, column=0, sticky="w", padx=4, pady=6)
        fps_var = tk.StringVar(value=config['VIDEO'].get('FPS', '15'))
        fps_frame = tk.Frame(card, bg=C["glass"])
        fps_frame.grid(row=2, column=1, sticky="w", padx=8, pady=6)
        tk.Entry(fps_frame, textvariable=fps_var, font=("Microsoft YaHei", 10), width=10).pack(side="left")
        tk.Label(fps_frame, text="(1-60)", bg=C["glass"], fg=C["text3"],
                 font=("Microsoft YaHei", 8)).pack(side="left", padx=4)

        # 质量
        tk.Label(card, text=_t('lbl.jpeg'), bg=C["glass"], fg=C["text2"],
                 font=("Microsoft YaHei", 9)).grid(row=3, column=0, sticky="w", padx=4, pady=6)
        quality_frame = tk.Frame(card, bg=C["glass"])
        quality_frame.grid(row=3, column=1, sticky="w", padx=8, pady=6)
        tk.Entry(quality_frame, textvariable=quality_var, font=("Microsoft YaHei", 10), width=10).pack(side="left")
        tk.Label(quality_frame, text=_t('lbl.quality.hint'), bg=C["glass"], fg=C["text3"],
                 font=("Microsoft YaHei", 8)).pack(side="left", padx=4)

        # 启用
        tk.Checkbutton(card, text=_t('lbl.enable.video'), bg=C["glass"], fg=C["text"],
                       font=("Microsoft YaHei", 10), variable=chk_enabled_var).grid(
                           row=4, column=0, columnspan=2, sticky="w", pady=8)

        # 启动扫描线程
        _gui_threading.Thread(target=scan_and_show, daemon=True).start()

        def _apply():
            device_idx = int(device_var.get() or 0)
            try:
                w, h = res_var.get().split('x')
                width, height = int(w), int(h)
            except:
                width, height = 320, 240
            
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

        btn = tk.Button(dlg, text=_t('btn.apply'), command=_apply,
                        bg=C["btn"], fg=C["btn_fg"],
                        font=("Microsoft YaHei", 10), relief="flat",
                        padx=20, pady=5, cursor="hand2",
                        activebackground=C["accent2"], activeforeground="#fff")
        btn.pack(pady=8)

    def _clear_console(self):
        self._console.configure(state="normal")
        self._console.delete("1.0", "end")
        self._console.configure(state="disabled")

    def _open_log_file(self):
        try:    os.startfile(LOG_FILE)
        except: messagebox.showinfo(_t('log.path.title'), LOG_FILE, parent=self.root)

    def _dialog_about(self):
        C = self.C
        dlg = self._styled_dialog(_t('dlg.about.title'), 380, 180)

        card = tk.Frame(dlg, bg=C["glass"],
                        highlightbackground=C["border"], highlightthickness=1,
                        pady=20, padx=24)
        card.pack(fill="x", padx=12, pady=10)

        tk.Label(card, text="FT-710 Remote Controller",
                 bg=C["glass"], fg=C["accent2"],
                 font=("Microsoft YaHei", 13, "bold")).pack()
        tk.Label(card, text=_t('lbl.about.ver', ver=self.VERSION),
                 bg=C["glass"], fg=C["text"],
                 font=("Microsoft YaHei", 10)).pack(pady=(6,2))
        tk.Label(card, text=_t('lbl.about.log'),
                 bg=C["glass"], fg=C["text3"],
                 font=("Microsoft YaHei", 9)).pack(pady=(6,0))

        tk.Button(dlg, text=_t('btn.close'), command=dlg.destroy,
                  bg=C["glass2"], fg=C["text"],
                  font=("Microsoft YaHei", 10), relief="flat",
                  padx=20, pady=4, cursor="hand2",
                  activebackground=C["border"]).pack(pady=8)

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