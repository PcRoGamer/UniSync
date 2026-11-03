import os
import re
import shutil
import sys


def naive_versiontuple(v):
    """
    This only works for version tuple with the same number of parts.
    Expects naive_versiontuple('xx.yy.zz') < naive_versiontuple('aa.bb.cc').
    """
    return tuple(map(int, (v.split("."))))


def get_project_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


LOCAL_DATA_DIR = "_local"


def local_path(*parts):
    return os.path.join(get_project_root(), LOCAL_DATA_DIR, *parts)


VENV_DIR = local_path("venv")
DOWNLOADS_DIR = local_path("downloads")
LOGS_DIR = local_path("logs")
PERSISTENT_SESSION_FOLDER = local_path("browser_session")
LOGIN_EMAIL_FILE = local_path("echo360_login_email.txt")

BY_LECTURE_SUBDIR = "by_lecture"

MP4_SUBDIR = "mp4"
TRANSCRIPT_SUBDIR = "transcript"

_INVALID_PATH_CHARS = re.compile(r'[\\/:*?"<>|]')


def sanitize_path_component(name):
    if not name:
        return "untitled"
    return _INVALID_PATH_CHARS.sub("_", str(name)).strip() or "untitled"


_WINDOWS_MAX_PATH = 258


def win_extended_path(path):
    """Prefix absolute paths for Windows long-path support (>260 chars)."""
    if sys.platform != "win32":
        return path
    path = os.path.normpath(os.path.abspath(path))
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def truncate_filename_for_section(section_dir, basename):
    """Truncate basename so paths fit under both mp4/ and transcript/ on Windows."""
    mp4_dir = os.path.join(section_dir, MP4_SUBDIR)
    transcript_dir = os.path.join(section_dir, TRANSCRIPT_SUBDIR)
    basename = truncate_filename_for_dir(mp4_dir, basename, "_video.mp4")
    return truncate_filename_for_dir(transcript_dir, basename, ".vtt")


def truncate_filename_for_dir(dir_path, basename, worst_suffix="_video.mp4"):
    """Truncate basename so dir/basename + worst_suffix fits Windows MAX_PATH."""
    if sys.platform != "win32":
        return basename
    full_dir = os.path.abspath(dir_path)
    max_base = _WINDOWS_MAX_PATH - len(full_dir) - 1 - len(worst_suffix)
    if max_base < 20:
        max_base = 20
    if len(basename) <= max_base:
        return basename
    if max_base > 3:
        return basename[: max_base - 3] + "..."
    return basename[:max_base]


def safe_move(src, dst):
    src_abs = os.path.abspath(src)
    dst_abs = os.path.abspath(dst)
    if src_abs == dst_abs:
        return dst_abs
    dst_parent = os.path.dirname(dst_abs)
    os.makedirs(win_extended_path(dst_parent), exist_ok=True)
    if not os.path.isfile(win_extended_path(src_abs)):
        raise FileNotFoundError(
            "HLS download finished but output file is missing: {0}".format(src_abs)
        )
    os.replace(win_extended_path(src_abs), win_extended_path(dst_abs))
    return dst_abs


def ensure_dir(path):
    os.makedirs(win_extended_path(os.path.abspath(path)), exist_ok=True)


def write_text_file(path, content, encoding="utf-8"):
    path_abs = os.path.abspath(path)
    ensure_dir(os.path.dirname(path_abs))
    with open(win_extended_path(path_abs), "w", encoding=encoding) as f:
        f.write(content)
    return path_abs


def ensure_section_media_dirs(section_dir):
    """Return (mp4_dir, transcript_dir) under a course section folder."""
    mp4_dir = os.path.join(section_dir, MP4_SUBDIR)
    transcript_dir = os.path.join(section_dir, TRANSCRIPT_SUBDIR)
    ensure_dir(mp4_dir)
    ensure_dir(transcript_dir)
    return mp4_dir, transcript_dir


def ensure_local_dirs():
    for path in (local_path(), DOWNLOADS_DIR, LOGS_DIR, PERSISTENT_SESSION_FOLDER):
        os.makedirs(path, exist_ok=True)


def load_saved_login_email():
    if not os.path.isfile(LOGIN_EMAIL_FILE):
        return None
    try:
        with open(LOGIN_EMAIL_FILE, "r", encoding="utf-8") as f:
            email = f.read().strip()
        return email or None
    except OSError:
        return None


def save_login_email(email):
    email = (email or "").strip()
    if not email:
        return
    ensure_local_dirs()
    with open(LOGIN_EMAIL_FILE, "w", encoding="utf-8") as f:
        f.write(email)


def clear_stale_browser_session_locks(profile_dir=None):
    """
    Remove Chrome lock files left after a crash, force-quit, or taskkill.
    Keeps cookies and login data; only drops stale process locks.
    """
    profile_dir = os.path.abspath(profile_dir or PERSISTENT_SESSION_FOLDER)
    if not os.path.isdir(profile_dir):
        return 0
    removed = 0
    lock_names = {
        "SingletonLock",
        "SingletonSocket",
        "SingletonCookie",
        "lockfile",
        "DevToolsActivePort",
    }
    scan_roots = [profile_dir]
    default_profile = os.path.join(profile_dir, "Default")
    if os.path.isdir(default_profile):
        scan_roots.append(default_profile)
    for root in scan_roots:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            if name not in lock_names and not name.startswith("Singleton"):
                continue
            path = os.path.join(root, name)
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
                removed += 1
            except OSError:
                pass
    return removed


def terminate_chrome_processes_for_profile(profile_dir=None):
    """Stop orphaned Chrome processes still using the Selenium profile (Windows)."""
    if sys.platform != "win32":
        return 0
    import subprocess

    profile_dir = os.path.abspath(profile_dir or PERSISTENT_SESSION_FOLDER)
    if not os.path.isdir(profile_dir):
        return 0
    escaped = profile_dir.replace("'", "''")
    script = (
        "$p = '{0}'; "
        "Get-CimInstance Win32_Process -Filter \"name = 'chrome.exe'\" | "
        "Where-Object {{ $_.CommandLine -and $_.CommandLine.Contains($p) }} | "
        "ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"
    ).format(escaped)
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return 0
    return 1


def safe_quit_webdriver(driver):
    """
    Close only this script's Selenium Chrome window (driver.quit()).
    Does not quit your normal Chrome browser or other apps' automation.
    With -P, uses a separate profile under _local/browser_session/, not your
    default Chrome user data.
    """
    if driver is None:
        return
    try:
        driver.quit()
    except Exception:
        try:
            driver.close()
        except Exception:
            pass


def format_downloads_location(section_dir):
    """Human-readable path hint for mp4/ and transcript/ under a section folder."""
    root = os.path.abspath(section_dir)
    return (
        "\n>> Open your downloads at:\n"
        "   {0}\n"
        "   mp4/         — videos\n"
        "   transcript/  — .vtt subtitles\n".format(root)
    )


def migrate_legacy_local_data():
    """Move pre-_local folders into _local/ when present (one-time)."""
    root = get_project_root()
    ensure_local_dirs()
    moves = (
        (os.path.join(root, "default_out_path"), DOWNLOADS_DIR),
        (os.path.join(root, "_browser_persistent_session"), PERSISTENT_SESSION_FOLDER),
        (os.path.join(root, "_echo360venv"), VENV_DIR),
    )
    for old_path, new_path in moves:
        if not os.path.isdir(old_path) or os.path.exists(new_path):
            continue
        try:
            shutil.move(old_path, new_path)
        except OSError:
            pass

    for log_name in ("echo360Downloader.log", "webdriver_service.log"):
        old_log = os.path.join(root, log_name)
        new_log = os.path.join(LOGS_DIR, log_name)
        if os.path.isfile(old_log) and not os.path.exists(new_log):
            try:
                shutil.move(old_log, new_log)
            except OSError:
                pass


def pick_with_line_highlight(options, title, **kwargs):
    """pick() with the cursor row fully highlighted (not just the indicator)."""
    import curses

    from pick import Picker, pick

    original_get_option_lines = Picker.get_option_lines

    def get_option_lines(self):
        lines = []
        for index, option in enumerate(self.options):
            if self.options_map_func:
                option = self.options_map_func(option)
            if index == self.index:
                prefix = self.indicator
            else:
                prefix = len(self.indicator) * " "
            line = "{0} {1}".format(prefix, option)
            if self.multiselect and index in self.all_selected:
                lines.append((line, curses.color_pair(1)))
            elif index == self.index:
                lines.append((line, curses.A_REVERSE))
            else:
                lines.append(line)
        return lines

    Picker.get_option_lines = get_option_lines
    try:
        return pick(options, title, **kwargs)
    finally:
        Picker.get_option_lines = original_get_option_lines


def pick_with_line_highlight(options, title, **kwargs):
    """pick() with the cursor row fully highlighted (not just the indicator)."""
    import curses

    from pick import Picker, pick

    original_get_option_lines = Picker.get_option_lines

    def get_option_lines(self):
        lines = []
        for index, option in enumerate(self.options):
            if self.options_map_func:
                option = self.options_map_func(option)
            if index == self.index:
                prefix = self.indicator
            else:
                prefix = len(self.indicator) * " "
            line = "{0} {1}".format(prefix, option)
            if self.multiselect and index in self.all_selected:
                lines.append((line, curses.color_pair(1)))
            elif index == self.index:
                lines.append((line, curses.A_REVERSE))
            else:
                lines.append(line)
        return lines

    Picker.get_option_lines = get_option_lines
    try:
        return pick(options, title, **kwargs)
    finally:
        Picker.get_option_lines = original_get_option_lines
