"""Markup integrity checks for presentation-only changes."""
from html.parser import HTMLParser
import re
import unittest

from webui.server import STATIC


class Document(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.controls = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.ids.append(attrs['id'])
            self.controls[attrs['id']] = (tag, attrs)


class FrontendTests(unittest.TestCase):
    def test_markup_preserves_unique_script_hooks_and_password_control(self):
        document = Document()
        document.feed((STATIC / 'index.html').read_text(encoding='utf-8'))
        self.assertEqual(len(document.ids), len(set(document.ids)), 'Duplicate HTML IDs')
        script = (STATIC / 'app.js').read_text(encoding='utf-8')
        hooks = set(re.findall(r'\$\("([^"\n]+)"\)', script))
        self.assertTrue(hooks <= set(document.ids), f'Missing HTML hooks: {hooks - set(document.ids)}')
        tag, password = document.controls['password']
        self.assertEqual(tag, 'input')
        self.assertEqual(password['type'], 'password')
        self.assertEqual(password['autocomplete'], 'current-password')
