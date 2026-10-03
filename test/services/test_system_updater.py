import unittest
from app.services import system_updater


class TestSystemUpdater(unittest.TestCase):
    def test_get_system_health(self):
        health = system_updater.get_system_health()
        self.assertIn("python", health)
        self.assertIn("ffmpeg", health)
        self.assertIn("git", health)
        self.assertIn("storage", health)
        self.assertIn("api_keys", health)

        self.assertTrue(health["python"]["ok"])
        self.assertTrue(isinstance(health["storage"]["ok"], bool))

    def test_check_git_updates(self):
        result = system_updater.check_git_updates()
        self.assertIn("ok", result)
        self.assertIn("has_update", result)
