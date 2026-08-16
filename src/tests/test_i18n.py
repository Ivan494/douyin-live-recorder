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
        self.assertEqual("Record live streams", t("record_live"))
        self.assertEqual("Not recording", t("live_recording_off"))

    def test_live_recording_toggle_strings_exist_in_both_languages(self):
        set_language("zh-CN")
        self.assertEqual("录制直播", t("record_live"))
        self.assertEqual("不录制", t("live_recording_off"))
        self.assertEqual("已关闭直播录制。", t("live_recording_off_detail"))

    def test_app_login_strings_cover_works_and_stories(self):
        set_language("en")
        self.assertIn("app-capable session", t("session_help"))
        self.assertIn("anonymous", t("session_note"))
        self.assertEqual("Douyin App Login", t("session_title"))
        set_language("zh-CN")
        self.assertIn("日常", t("session_help"))
        self.assertIn("匿名", t("session_note"))
        self.assertEqual("抖音 App 登录", t("session_title"))


if __name__ == "__main__":
    unittest.main()
