"""Create a Windows desktop shortcut through native Windows Script Host (no policy changes)."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def vb(value):
    return str(value).replace('"', '""')


def main():
    if os.name != "nt":
        raise SystemExit("Desktop shortcut installation requires Windows")
    root = Path(__file__).resolve().parent.parent
    launcher = root / "scripts" / "launch-router.vbs"
    launcher.write_text(f'''Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "{vb(root)}"
shell.Run Chr(34) & "{vb(sys.executable)}" & Chr(34) & " -B -m model_router.desktop", 0, False
''', encoding="utf-16")
    content = f'''Set shell = CreateObject("WScript.Shell")
linkPath = shell.SpecialFolders("Desktop") & "\\Model Router.lnk"
Set shortcut = shell.CreateShortcut(linkPath)
shortcut.TargetPath = shell.ExpandEnvironmentStrings("%WINDIR%") & "\\System32\\wscript.exe"
shortcut.Arguments = Chr(34) & "{vb(launcher)}" & Chr(34)
shortcut.WorkingDirectory = "{vb(root)}"
shortcut.Description = "Model Router - Local engineering workspace"
shortcut.IconLocation = "{vb(root / 'model_router/web/router.ico')}"
shortcut.WindowStyle = 7
shortcut.Save
WScript.Echo linkPath
'''
    with tempfile.NamedTemporaryFile(suffix=".vbs", delete=False) as temporary:
        temp_path = Path(temporary.name)
    try:
        temp_path.write_text(content, encoding="utf-16")
        result = subprocess.run([str(Path(os.environ["WINDIR"]) / "System32/cscript.exe"), "//NoLogo", str(temp_path)],
                                capture_output=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
        print(result.stdout.decode("mbcs").strip())
    finally:
        temp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
