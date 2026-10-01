"""Desktop payloads stay data; failed delivery stays visible in the inbox."""

import base64
import unittest
from unittest import mock

from codex1cc.notifications import notification_command, deliver_notification


class NotificationTest(unittest.IsolatedAsyncioTestCase):
    def test_windows_message_is_encoded_xml_instead_of_executable_interpolation(self):
        payload = "中文 '; Remove-Item anything; $(secret) <tag>"
        with mock.patch("codex1cc.notifications.shutil.which", side_effect=lambda name: "/powershell.exe" if name == "powershell.exe" else None), \
             mock.patch("codex1cc.notifications.sys.platform", "linux"):
            command = notification_command("Review", payload)
        script = base64.b64decode(command[-1]).decode("utf-16-le")
        self.assertNotIn(payload, script)
        self.assertIn("FromBase64String", script)

    async def test_delivery_rejection_is_reported(self):
        process = mock.Mock(returncode=1)
        process.wait = mock.AsyncMock()
        with mock.patch.dict("os.environ", {"CODEX1CC_DESKTOP_NOTIFICATIONS": "1"}), \
             mock.patch("codex1cc.notifications.notification_command", return_value=["notify-send", "title", "body"]), \
             mock.patch("codex1cc.notifications.asyncio.create_subprocess_exec", mock.AsyncMock(return_value=process)):
            error = await deliver_notification("title", "body")
        self.assertIn("rejected", error)
