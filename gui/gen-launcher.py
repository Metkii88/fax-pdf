#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Генератор /usr/bin/fax-pdf-gui (вместо dh_python3, чтобы не требовать setup.py)."""
import pathlib
import sys

dest = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else None
if not dest:
    print("usage: gen-launcher.py <output-path>")
    sys.exit(2)
dest.write_text(
    "#!/usr/bin/python3\n"
    "# -*- coding: utf-8 -*-\n"
    "import sys\n"
    "from faxpdf.app import run\n"
    "sys.exit(run(sys.argv))\n",
    encoding="utf-8",
)
dest.chmod(0o755)
