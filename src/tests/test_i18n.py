import unittest

from i18n import DEFAULT_LANGUAGE, set_language, t


class I18nTest(unittest.TestCase):
    def tearDown(self):
        set_language(DEFAULT_LANGUAGE)

    def test_default_language_is_simplified_chinese(self):
        set_language(DEFAULT_LANGUAGE)
        self.assertEqual("抖音直播录制", t("app_title"))
        self.assertEqual("开始监控", t("start_monitoring"))

    def test_english_catalog(self):
        set_language("en")
        self.assertEqual("Douyin Live Recorder", t("app_title"))
        self.assertEqual("Start Monitoring", t("start_monitoring"))


if __name__ == "__main__":
    unittest.main()
