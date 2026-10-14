"""Synchronize the embedded English catalog. Run before building after catalog edits."""
from pathlib import Path
import json
import argparse
root=Path(__file__).resolve().parent.parent
catalog=json.loads((root/'locales/en.json').read_text())
p=root/'macos/Localization.swift';source=p.read_text()
a=source.index('    static let catalog:');b=source.index('    static let keys',a)
entries=',\n'.join('    '+json.dumps(k,ensure_ascii=False)+': '+json.dumps(v,ensure_ascii=False) for k,v in catalog.items())
updated=source[:a]+'    static let catalog:[String:String] = [\n'+entries+'\n    ]\n'+source[b:]
parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');args=parser.parse_args()
if args.check:
 if updated != source:raise SystemExit('Embedded catalog differs. Run scripts/sync_localization.py')
else:p.write_text(updated)
