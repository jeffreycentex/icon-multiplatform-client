# PyInstaller spec:  pyinstaller icomremote.spec
# Produces dist/Icom Remote/ (Windows, Linux) or dist/Icom Remote.app (macOS).
import sys

hidden = []
try:
    import samplerate  # noqa: F401
    hidden.append("samplerate")
except ImportError:
    print("NOTE: optional package 'samplerate' not installed; build will use linear resampling")

a = Analysis(["run_icomremote.py"], pathex=["."], hiddenimports=hidden,
             excludes=["tkinter"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Icom Remote",
          console=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="Icom Remote", upx=False)
if sys.platform == "darwin":
    app = BUNDLE(coll, name="Icom Remote.app", bundle_identifier="net.icomremote.app",
                 info_plist={"NSHighResolutionCapable": True,
                             "NSMicrophoneUsageDescription": "Transmit audio is read from a sound input."})
