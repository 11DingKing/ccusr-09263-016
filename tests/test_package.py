"""验证项目包可以在干净环境载入。"""
import unittest
from service_09252_008 import PROJECT_CODE, project_info

class PackageTests(unittest.TestCase):
    def test_identity(self) -> None:
        self.assertEqual(project_info()["code"], PROJECT_CODE)
        self.assertTrue(project_info()["title"])

if __name__ == "__main__":
    unittest.main()
