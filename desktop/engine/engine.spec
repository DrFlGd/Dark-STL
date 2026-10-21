# PyInstaller build of the app's engine: darkstl(.exe), the command line, which the app runs as
# "darkstl engine" (src/darkstl/app/engine.py). From the repository root, with the package and
# the remesher installed (pip install ".[remesh]" pyinstaller):
#   pyinstaller desktop/engine/engine.spec --noconfirm --distpath build --workpath build/pyinstaller
# It lands in build/engine, where the app's bundle takes it from (tauri.conf.json, resources).
#
# Of VTK, the remesher only needs the few modules pyvista loads for a PolyData; the rest (rendering,
# file readers, plotting) would make the app several times the size, so they are left out. The
# smoke test (smoke.py) runs the remesher in the built engine to prove the cut.
import pkgutil
from pathlib import Path

import vtkmodules
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

HERE = Path(SPECPATH)
VTK_KEEP = {"util", "numpy_interface", "vtkCommonCore", "vtkCommonDataModel", "vtkCommonExecutionModel",
            "vtkCommonMath", "vtkCommonSystem", "vtkCommonTransforms", "vtkFiltersSources", "vtkImagingSources",
            "vtkParallelCore"}
VTK_CUT = [f"vtkmodules.{m.name}" for m in pkgutil.iter_modules(vtkmodules.__path__) if m.name not in VTK_KEEP]
EXCLUDES = ["pyvista.plotting", "pyvista.trame", "pyvista.examples", "pyvista.demos", "pyvista.ext", "matplotlib", "tkinter", "webview", "PySide6", "PyQt5", "PyQt6", "IPython", "pytest", *VTK_CUT]

a = Analysis([str(HERE / "main.py")], datas=collect_data_files("darkstl"), hiddenimports=collect_submodules("darkstl"),
             excludes=EXCLUDES)
exe = EXE(PYZ(a.pure), a.scripts, [], exclude_binaries=True, name="darkstl", icon=str(HERE.parent / "icons" / "icon.ico"),
          console=True)
COLLECT(exe, a.binaries, a.datas, name="engine")
