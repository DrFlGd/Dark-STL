# PyInstaller build of the Windows app: "Dark STL.exe" (the window) and darkstl.exe (the
# command line), sharing one folder. From the repository root, with the package and
# pywebview installed (pip install ".[gui]" pyinstaller):
#   pyinstaller packaging/windows/darkstl.spec --noconfirm --distpath build/windows/dist --workpath build/windows/work
# The isotropic remesher (pyvista, VTK) is left out: it would make the app several times the
# size, and the app falls back to subdivision without it.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

HERE = Path(SPECPATH)
ICON = str(HERE / "darkstl.ico")
DATAS = collect_data_files("darkstl")  # the page: darkstl/web
HIDDEN = collect_submodules("darkstl")
EXCLUDES = ["pyvista", "vtk", "vtkmodules", "pyacvd", "matplotlib", "tkinter", "PySide6", "PyQt5", "PyQt6",
            "IPython", "pytest"]

app = Analysis([str(HERE / "app.py")], datas=DATAS, hiddenimports=HIDDEN, excludes=EXCLUDES)
cli = Analysis([str(HERE / "cli.py")], datas=DATAS, hiddenimports=HIDDEN, excludes=EXCLUDES)

app_exe = EXE(PYZ(app.pure), app.scripts, [], exclude_binaries=True, name="Dark STL", icon=ICON, console=False)
cli_exe = EXE(PYZ(cli.pure), cli.scripts, [], exclude_binaries=True, name="darkstl", icon=ICON, console=True)

COLLECT(app_exe, app.binaries, app.datas, cli_exe, cli.binaries, cli.datas, name="Dark STL")
