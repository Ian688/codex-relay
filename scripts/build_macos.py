"""Build a local, double-clickable macOS AppKit bundle. No pip dependencies."""
from pathlib import Path
import json
import argparse
import os
import platform
import plistlib
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parent.parent
if sys.platform != 'darwin': raise SystemExit('macOS is required to build the native app.')
build = root / '.build'
build.mkdir(exist_ok=True)
binary = build / 'CodexRelay'
source=build/'RelayCombined.swift'
source.write_text((root/'macos/GoalAccessibility.swift').read_text()+'\n'+(root/'macos/Localization.swift').read_text()+'\n'+(root/'macos/RelayApp.swift').read_text())
subprocess.run(['swiftc', str(source), '-o', str(binary), '-framework', 'Cocoa', '-framework', 'ApplicationServices'], check=True)
parser = argparse.ArgumentParser()
parser.add_argument('--output', default='dist/Codex Relay 0.7.0.app')
args = parser.parse_args()
app = root / args.output
contents = app / 'Contents'
resources = contents / 'Resources'
(resources).mkdir(parents=True, exist_ok=True)
(contents/'MacOS').mkdir(exist_ok=True)
shutil.copy2(binary, contents/'MacOS/CodexRelay')
package = resources/'codex_autoresume'
if package.exists(): shutil.rmtree(package)
shutil.copytree(root/'codex_autoresume', package, ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
(resources/'runtime.json').write_text(json.dumps({'python':str(Path(sys.executable).resolve()), 'codex':shutil.which('codex'), 'architecture':platform.machine()}, indent=2))
shutil.copy2(root/'README.md', resources/'README.md')
iconset = build/'Relay.iconset'
iconset.mkdir(exist_ok=True)
icon = build/'Relay-1024.png'
subprocess.run([str(binary),'--render-icon',str(icon)],check=True)
for size in [16,32,128,256,512]:
    for scale in [1,2]:
        name = f'icon_{size}x{size}' + ('@2x' if scale==2 else '') + '.png'
        subprocess.run(['sips','-z',str(size*scale),str(size*scale),str(icon),'--out',str(iconset/name)],check=True,stdout=subprocess.DEVNULL)
subprocess.run(['iconutil','-c','icns',str(iconset),'-o',str(resources/'Relay.icns')],check=True)
info = {'CFBundleIconFile':'Relay.icns','CFBundleName':'Codex Relay','CFBundleDisplayName':'Codex Relay','CFBundleIdentifier':'com.local.codex-relay.desktop',
        'CFBundleExecutable':'CodexRelay','CFBundlePackageType':'APPL','CFBundleShortVersionString':'0.7.0','CFBundleVersion':'8',
        'LSMinimumSystemVersion':'10.15','NSHighResolutionCapable':True,'NSHumanReadableCopyright':'Codex Relay',
        'NSPrincipalClass':'NSApplication'}
(contents/'Info.plist').write_bytes(plistlib.dumps(info))
# Local ad-hoc signature, no external signing identity or notarization claim.
subprocess.run(['codesign','--force','--deep','--sign','-',str(app)],check=True)
print(app)
print('Built for this Mac. Requires the installed Python 3.11+ and Codex Desktop/CLI; no terminal activation or pip install needed to launch.')
