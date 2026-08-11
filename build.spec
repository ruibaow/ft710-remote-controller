# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['ft710_controller.py'],
    pathex=[],
    binaries=[
        ('ffmpeg', 'ffmpeg'),
    ],
    datas=[
        ('ft710_config.ini', '.'),
        ('cert.pem', '.'),
        ('key.pem', '.'),
    ],
    hiddenimports=[
        'serial', 'sounddevice', 'flask', 'flask_socketio', 
        'numpy', 'cv2', 'cryptography',
        'flask', 'werkzeug', 'jinja2', 'click',
        'socketio', 'engineio', 'engineio.async_drivers',
        'engineio.async_drivers.threading',
        'python_socketio', 'python_socketio.asyncio_manager',
        'asyncio', 'aiohttp', 'aiohttp.websocket',
        'gevent', 'gevent.event', 'gevent.queue',
        'websocket',
        'aiortc', 'av', 'soundfile',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='FT710Remote',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)