"""Check release language contracts without translating private user content."""
import ast,json,subprocess,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
class LocalizationTest(unittest.TestCase):
 def test_embedded_catalog_matches_source(self):
  subprocess.run([sys.executable,str(ROOT/'scripts/sync_localization.py'),'--check'],check=True)
 def test_all_observation_messages_have_english_segments(self):
  catalog=json.loads((ROOT/'locales/en.json').read_text())
  source=ast.parse((ROOT/'codex_autoresume/observations.py').read_text())
  for node in ast.walk(source):
   if isinstance(node,ast.Constant) and isinstance(node.value,str) and any('\u4e00'<=c<='\u9fff' for c in node.value):
    text=node.value
    for k in sorted(catalog,key=len,reverse=True):text=text.replace(k,catalog[k])
    self.assertFalse(any('\u4e00'<=c<='\u9fff' for c in text),node.value)
 def test_user_content_not_localized(self):
  source=(ROOT/'macos/RelayApp.swift').read_text()
  self.assertIn('inspectorTitle.stringValue = t["name"] as? String',source)
  self.assertIn('text.string = evidenceText()',source)
 def test_english_default_and_explicit_language_switch(self):
  source=(ROOT/'macos/Localization.swift').read_text()
  self.assertIn('static var current = "en"',source)
  self.assertIn('["en","zh"].contains',source)
