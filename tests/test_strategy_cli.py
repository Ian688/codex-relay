import asyncio
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from codex_autoresume.cli import parser, run_cli
from codex_autoresume.store import Store


class StrategyCLI(unittest.TestCase):
    def test_local_strategy_and_legacy_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / 'state.sqlite3')
            store = Store(db)
            store.watch('t', 'account', 'test')
            store.close()
            def run(*argv):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    asyncio.run(run_cli(parser().parse_args(['--db', db, *argv])))
                return out.getvalue()
            run('strategy', 'set', 't', '--mode', 'reserve', '--stop-remaining', '20',
                '--resume-mode', 'remaining', '--resume-remaining', '50')
            store = Store(db)
            self.assertEqual(store.policy('t')['pause_used'], 80)
            store.close()
            self.assertIn("'mode': 'reserve'", run('strategy', 'list'))
            run('budget', 'set', 't', '--pause-used', '90', '--resume-used', '0')
            store = Store(db)
            self.assertEqual(store.policy('t')['resume_remaining'], 100)
            self.assertEqual(store.policy('t')['resume_mode'], 'remaining')
            store.close()
            run('strategy', 'set', 't')
            store = Store(db)
            self.assertEqual(store.policy('t')['mode'], 'quota')
            store.close()
            with self.assertRaises(ValueError):
                run('strategy', 'set', 't', '--mode', 'reserve', '--resume-mode',
                    'remaining', '--stop-remaining', '60', '--resume-remaining', '50')
