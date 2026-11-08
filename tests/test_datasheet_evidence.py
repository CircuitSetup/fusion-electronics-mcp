import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fusion_mcp import datasheets as DS


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        import pymupdf
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), 'Synthetic regulator. Keep CIN near VIN. Revision A.')
        self.pdf = doc.tobytes()
        doc.close()

    def acquire(self, raw=None):
        with patch.object(DS, '_download', return_value=(raw or self.pdf, 'https://vendor.example/ic.pdf')):
            return DS.acquire('https://vendor.example/ic.pdf', ['vendor.example'],
                              'TEST-IC', 'QFN', 'A', 'datasheet', self.tmp.name)

    def test_acquisition_is_not_inspection_or_approval(self):
        result = self.acquire()
        manifest = DS.load(result['manifest_path'])
        self.assertEqual(manifest['sha256'], hashlib.sha256(self.pdf).hexdigest())
        self.assertEqual(manifest['inspected_pages'], {})
        self.assertNotIn('approved', str(manifest))
        inspected = DS.inspect(result['manifest_path'], [1])
        self.assertIn('Keep CIN near VIN', inspected['pages'][0]['text'])
        self.assertTrue(Path(inspected['pages'][0]['image_path']).is_file())
        self.assertIn('1', DS.load(result['manifest_path'])['inspected_pages'])

    def test_stale_pdf_and_bad_pages_are_refused(self):
        result = self.acquire()
        with self.assertRaisesRegex(ValueError, 'page'):
            DS.inspect(result['manifest_path'], [2])
        Path(result['pdf_path']).write_bytes(self.pdf + b'changed')
        with self.assertRaisesRegex(ValueError, 'hash'):
            DS.load(result['manifest_path'])

    def test_malformed_and_oversize_are_refused(self):
        for raw in [b'<html>login required</html>', b'%PDF-' + b'x' * DS.MAX_BYTES]:
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                self.acquire(raw)

    def test_private_urls_and_unlisted_redirects_are_refused(self):
        for url in ['http://vendor.example/x', 'https://user:pw@vendor.example/x',
                    'https://127.0.0.1/x', 'https://other.example/x']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                DS.validate_url(url, ['vendor.example'])
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('10.0.0.1', 443))]):
            with self.assertRaisesRegex(ValueError, 'public'):
                DS.validate_url('https://vendor.example/x', ['vendor.example'])


if __name__ == '__main__':
    unittest.main()
