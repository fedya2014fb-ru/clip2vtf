# PyInstaller spec: one-file clip2vtf.exe.  Build:  py -3 -m PyInstaller --noconfirm clip2vtf.spec
import os
import tkinterdnd2

tkdnd = os.path.join(os.path.dirname(tkinterdnd2.__file__), "tkdnd")
# Only the Windows builds of tkdnd; which one gets loaded depends on the Tk version.
datas = [("clip2vtf.ico", ".")]
datas += [(os.path.join("lang", f), "lang") for f in os.listdir("lang")
          if f.endswith(".json") and not f.startswith("_")]
for sub in ("win-x64", "win-x64-tcl9"):
    if os.path.isdir(os.path.join(tkdnd, sub)):
        datas.append((os.path.join(tkdnd, sub), os.path.join("tkinterdnd2", "tkdnd", sub)))

a = Analysis(
    ["clip2vtf.pyw"],
    datas=datas,
    # srctools imports its Cython libsquish encoder inside try/except: without it DXT
    # compression silently falls back to pure Python (orders of magnitude slower).
    hiddenimports=["srctools._cy_vtf_readwrite", "srctools._math", "srctools._tokenizer"],
    # scipy is not used any more (numpy replacements, see border_connected / bleed_colors);
    # excluded explicitly so an installed copy never sneaks back in (it was 69 MB of 150).
    excludes=["scipy", "matplotlib", "pandas", "IPython", "pytest", "PyQt5", "PyQt6", "PySide6",
              "notebook", "sphinx", "setuptools"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="clip2vtf",
    icon="clip2vtf.ico",
    console=False,
    upx=False,
)
