"""Dark STL.exe: the app window, as darkstl-gui runs it."""

import sys

from darkstl.app.config import cache_dir
from darkstl.app.desktop import main

if sys.stderr is None:  # a windowed program has no console: messages go to darkstl.log beside the cache
    log = cache_dir().parent / "darkstl.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    sys.stdout = sys.stderr = open(log, "w", encoding="utf-8", buffering=1)  # noqa: SIM115 - open for the run

sys.exit(main())
