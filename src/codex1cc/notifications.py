"""Local desktop delivery; the database inbox remains authoritative."""

from __future__ import annotations

import asyncio
import base64
import html
import json
import os
from pathlib import Path
import shutil
import sys


def notification_command(title: str, message: str) -> list[str] | None:
    title, message = title[:100], message[:1500]
    if sys.platform == "darwin" and shutil.which("osascript"):
        return ["osascript", "-e", "display notification " + json.dumps(message, ensure_ascii=False) +
                " with title " + json.dumps(title, ensure_ascii=False)]
    if shutil.which("notify-send"):
        return ["notify-send", "--app-name=Codex1CC", "--", title, message]
    # WSL has no Linux notification daemon. Use the Windows host's toast service.
    powershell = shutil.which("powershell.exe")
    if not powershell and sys.platform.startswith("linux"):
        candidate = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
        if candidate.is_file():
            powershell = str(candidate)
    if powershell:
        xml = ('<toast><visual><binding template="ToastGeneric"><text>' + html.escape(title) +
               '</text><text>' + html.escape(message) + '</text></binding></visual></toast>')
        encoded_xml = base64.b64encode(xml.encode()).decode()
        script = (
            "$ErrorActionPreference='Stop';"
            "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]>$null;"
            "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime]>$null;"
            "$doc=New-Object Windows.Data.Xml.Dom.XmlDocument;"
            "$doc.LoadXml([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + encoded_xml + "')));"
            "$toast=[Windows.UI.Notifications.ToastNotification]::new($doc);"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Microsoft.Windows.PowerShell').Show($toast);"
        )
        return [powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand",
                base64.b64encode(script.encode("utf-16-le")).decode()]
    if shutil.which("gdbus"):
        return ["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
                "--object-path", "/org/freedesktop/Notifications", "--method",
                "org.freedesktop.Notifications.Notify", "Codex1CC", "0", "",
                json.dumps(title, ensure_ascii=False), json.dumps(message, ensure_ascii=False),
                "[]", "{}", "-1"]
    return None


async def deliver_notification(title: str, message: str) -> str | None:
    if os.environ.get("CODEX1CC_DESKTOP_NOTIFICATIONS") == "0":
        return "Desktop notifications disabled; notification remains in the inbox"
    command = notification_command(title, message)
    if command is None:
        return "Desktop delivery unavailable; see codex1cc notifications or codex1cc follow TASK_ID"
    try:
        proc = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return "Desktop delivery timed out; notification remains in the inbox"
        return None if proc.returncode == 0 else "Desktop notification service rejected delivery; see the inbox"
    except OSError:
        return "Desktop notification service unavailable; see the inbox"
