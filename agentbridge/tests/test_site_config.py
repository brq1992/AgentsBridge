import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import build_release  # noqa: E402
import check_public  # noqa: E402


def run_check(text, deny=None):
    with tempfile.TemporaryDirectory() as d:
        f = Path(d, 'f.txt')
        f.write_text(text, encoding='utf8')
        args = [str(f)]
        if deny is not None:
            site = Path(d, 'site.json')
            site.write_text(json.dumps({'deny': deny}), encoding='utf8')
            args = ['--deny-file', str(site)] + args
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = check_public.main(args)
        return code, out.getvalue()


class CheckPublic(unittest.TestCase):
    def test_flags_site_values_without_echoing_them(self):
        # Assembled at run time so this file itself passes the check.
        ip = '.'.join(['10', '1', '2', '3'])
        sid = '-'.join(['S', '1', '5', '21', '1', '2', '3', '4'])
        mail = 'a.b' + '@corp.test.org'
        code, out = run_check(f'hub wss://{ip}:9741\nsid {sid}\nmail {mail}\n')
        self.assertEqual(code, 1)
        self.assertIn('private_ip', out)
        self.assertIn('account_sid', out)
        self.assertIn('email', out)
        self.assertNotIn(ip, out)

    def test_documentation_values_pass(self):
        code, _ = run_check('wss://192.0.2.10:9741 user@users.noreply.github.com version 3.14.10')
        self.assertEqual(code, 0)

    def test_deny_list_from_site_file(self):
        self.assertEqual(run_check('ran on NodeSecret today', ['nodesecret'])[0], 1)
        self.assertEqual(run_check('ran on node-a today', ['nodesecret'])[0], 0)


class SiteOverlay(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.dest = self.tmp / 'out'
        (self.dest / 'bridge').mkdir(parents=True)
        (self.dest / 'agentbridge.py').write_text('code', encoding='utf8')
        self.site = self.tmp / 'site'
        (self.site / 'package').mkdir(parents=True)

    def test_adds_site_files(self):
        (self.site / 'package' / 'site.json').write_text('{}', encoding='utf8')
        added = build_release.overlay_site(self.site, self.dest, self.tmp / 'repo')
        self.assertEqual(added, ['site.json'])
        self.assertEqual((self.dest / 'agentbridge.py').read_text(encoding='utf8'), 'code')

    def test_never_replaces_released_file(self):
        (self.site / 'package' / 'agentbridge.py').write_text('evil', encoding='utf8')
        with self.assertRaises(SystemExit):
            build_release.overlay_site(self.site, self.dest, self.tmp / 'repo')
        self.assertEqual((self.dest / 'agentbridge.py').read_text(encoding='utf8'), 'code')

    def test_site_inside_repository_refused(self):
        with self.assertRaises(SystemExit):
            build_release.overlay_site(self.site, self.dest, self.tmp)


if __name__ == '__main__':
    unittest.main()
