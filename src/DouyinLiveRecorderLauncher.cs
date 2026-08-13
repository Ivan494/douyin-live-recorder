using System;
using System.Diagnostics;
using System.IO;

public static class DouyinLiveRecorderLauncher
{
    // Wait long enough to catch lock-file "already running" exits, but do not
    // block for the full GUI lifetime.
    private const int ChildStartupWaitMilliseconds = 2500;

    public static int Main()
    {
        string appDir = AppDomain.CurrentDomain.BaseDirectory;
        string appPath = Path.Combine(appDir, "douyin_recorder_app.py");
        string logDir = Path.Combine(appDir, "logs");
        string logPath = Path.Combine(logDir, "launcher.log");

        try
        {
            Directory.CreateDirectory(logDir);
            if (!File.Exists(appPath))
            {
                File.AppendAllText(logPath, DateTime.Now + " Missing app script: " + appPath + Environment.NewLine);
                return 2;
            }

            string runner = FindPythonRunner();
            if (string.IsNullOrEmpty(runner))
            {
                File.AppendAllText(logPath, DateTime.Now + " pythonw/python was not found." + Environment.NewLine);
                return 3;
            }

            var startInfo = new ProcessStartInfo
            {
                FileName = runner,
                Arguments = "\"" + appPath + "\"",
                WorkingDirectory = appDir,
                UseShellExecute = false,
                // pythonw has no console; CreateNoWindow avoids a flash if python.exe is used.
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Normal
            };

            using (Process child = Process.Start(startInfo))
            {
                if (child == null)
                {
                    File.AppendAllText(logPath, DateTime.Now + " Failed to start the recorder process." + Environment.NewLine);
                    return 4;
                }

                File.AppendAllText(
                    logPath,
                    DateTime.Now + " Started recorder PID " + child.Id + " via " + runner + Environment.NewLine);

                if (child.WaitForExit(ChildStartupWaitMilliseconds))
                {
                    // Exit 0 often means "already running; show signal written".
                    File.AppendAllText(
                        logPath,
                        DateTime.Now + " Recorder exited early with code " + child.ExitCode +
                        " (0 usually means another instance was already running and was asked to show)." +
                        Environment.NewLine);
                    return child.ExitCode;
                }
            }
            return 0;
        }
        catch (Exception ex)
        {
            try
            {
                Directory.CreateDirectory(logDir);
                File.AppendAllText(logPath, DateTime.Now + " " + ex + Environment.NewLine);
            }
            catch
            {
            }
            return 1;
        }
    }

    private static string FindPythonRunner()
    {
        string appDir = AppDomain.CurrentDomain.BaseDirectory;
        string packRoot = Path.GetFullPath(Path.Combine(appDir, "..", ".."));
        string[] candidates =
        {
            // Prefer pythonw first so no console window is created.
            Path.Combine(packRoot, "_runtime", "python", "pythonw.exe"),
            Path.Combine(appDir, "_runtime", "python", "pythonw.exe"),
            Path.Combine(packRoot, "_runtime", "python", "python.exe"),
            Path.Combine(appDir, "_runtime", "python", "python.exe"),
            "pythonw.exe",
            "python.exe",
            "py.exe"
        };

        foreach (string candidate in candidates)
        {
            if (Path.IsPathRooted(candidate))
            {
                if (File.Exists(candidate))
                {
                    return candidate;
                }
                continue;
            }

            string found = FindOnPath(candidate);
            if (!string.IsNullOrEmpty(found))
            {
                return found;
            }
        }

        return "";
    }

    private static string FindOnPath(string fileName)
    {
        string path = Environment.GetEnvironmentVariable("PATH") ?? "";
        foreach (string directory in path.Split(Path.PathSeparator))
        {
            if (string.IsNullOrWhiteSpace(directory))
            {
                continue;
            }

            try
            {
                string candidate = Path.Combine(directory.Trim(), fileName);
                if (File.Exists(candidate))
                {
                    return candidate;
                }
            }
            catch
            {
            }
        }

        return "";
    }
}
