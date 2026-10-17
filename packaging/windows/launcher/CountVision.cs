// CountVision.exe - the program behind the desktop and Start-menu icons of CountVision Local.
//
// It only starts the bundled Python without a console window:
//     <install>\python\pythonw.exe -m countvision_desktop <arguments>
// and waits for it, so "CountVision.exe --quit" (used by the setup and the uninstaller) returns when
// CountVision has stopped. All the real work (database, service, app, counting, window, tray icon)
// is the Python package countvision_desktop.
//
// Built by packaging\windows\build.ps1 with the C# compiler that is part of every Windows
// (.NET Framework 4, C# 5: no newer language features here), with the CountVision icon.

using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(dir, "python", "pythonw.exe");
        if (!File.Exists(python))
        {
            MessageBox.Show(
                "A part of CountVision is missing:\n" + python + "\n\nPlease run the CountVision setup again.",
                "CountVision", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 2;
        }

        StringBuilder line = new StringBuilder("-m countvision_desktop");
        foreach (string arg in args)
        {
            line.Append(' ').Append(Quote(arg));
        }

        ProcessStartInfo start = new ProcessStartInfo(python, line.ToString());
        start.UseShellExecute = false;
        start.WorkingDirectory = dir;
        // The bundled Python must not pick up settings of another Python on this PC.
        start.EnvironmentVariables.Remove("PYTHONHOME");
        start.EnvironmentVariables.Remove("PYTHONPATH");
        start.EnvironmentVariables.Remove("PYTHONSTARTUP");
        start.EnvironmentVariables["PYTHONUTF8"] = "1";

        try
        {
            using (Process child = Process.Start(start))
            {
                child.WaitForExit();
                return child.ExitCode;
            }
        }
        catch (Exception error)
        {
            MessageBox.Show("CountVision could not start:\n" + error.Message, "CountVision",
                MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 3;
        }
    }

    // Quote one argument the way the C runtime (and Python) splits a command line.
    private static string Quote(string arg)
    {
        if (arg.Length > 0 && arg.IndexOfAny(new[] { ' ', '\t', '"' }) < 0)
        {
            return arg;
        }
        StringBuilder sb = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in arg)
        {
            if (c == '\\')
            {
                slashes++;
                continue;
            }
            if (c == '"')
            {
                sb.Append('\\', slashes * 2 + 1);
            }
            else
            {
                sb.Append('\\', slashes);
            }
            slashes = 0;
            sb.Append(c);
        }
        sb.Append('\\', slashes * 2);
        sb.Append('"');
        return sb.ToString();
    }
}
