"""
Writes a PyInstaller version resource so the .exe shows its version in
Windows Explorer (right-click > Properties > Details).

  python tools/version_file.py <build number> > version_info.txt
"""
import os
import re
import sys

here = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(here, "..", "app", "psu_studio.py"), encoding="utf-8").read()
version = re.search(r'^VERSION = "([\d.]+)"', src, re.M).group(1)
build = int(sys.argv[1]) if len(sys.argv) > 1 else 0
parts = (version.split(".") + ["0", "0", "0"])[:3]
nums = tuple(int(p) for p in parts) + (build,)
dotted = ".".join(str(n) for n in nums)

print(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'NB6RULES'),
      StringStruct('FileDescription', 'PSU Studio - bench power supply control'),
      StringStruct('FileVersion', '{dotted}'),
      StringStruct('InternalName', 'PSU-Studio'),
      StringStruct('OriginalFilename', 'PSU-Studio.exe'),
      StringStruct('ProductName', 'PSU Studio'),
      StringStruct('ProductVersion', '{version} (build {build})')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)""")
