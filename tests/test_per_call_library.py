import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fusion_mcp import server as S
from fusion_mcp.library import Library
from test_datasheet_parts import FP
class PerCallLibraryTest(unittest.TestCase):
    def test_two_design_libraries_do_not_change_default_or_each_other(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); fp=root/'test.kicad_mod';fp.write_text(FP)
            a=root/'a'; b=root/'b'; default=Library(str(root/'default'))
            with patch.object(S,'library',default):
                for path,ds in [(a,'A'),(b,'B')]:
                    S.create_library_part(str(fp),'same-id',ds,'U',ds,library_directory=str(path))
                self.assertEqual(S.get_library_part('same-id',library_directory=str(a))['deviceset'],'A')
                self.assertEqual(S.get_library_part('same-id',library_directory=str(b))['deviceset'],'B')
                self.assertEqual(S.search_library('',library_directory=str(a))[0]['id'],'same-id')
                self.assertEqual(default.parts,{})
                self.assertFalse((root/'default').exists())
if __name__=='__main__': unittest.main()
