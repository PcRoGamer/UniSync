import argparse
import os
import sys
import re
import logging
import time
import selenium
from datetime import datetime

try:
    import pick
except ImportError as e:
    # check if this is windows, if so install windows curse on the fly
    if "win32" not in sys.platform:
        raise e
    import subprocess

    subprocess.check_call([sys.executable, "-m", "pip", "install", "windows-curses"])

from .enrollments import fetch_enrollments, pick_enrolled_courses, resolve_echo360_email, session_from_driver
from .downloader import EchoDownloader
from .course import EchoCourse, EchoCloudCourse
from .utils import PERSISTENT_SESSION_FOLDER, DOWNLOADS_DIR, LOGS_DIR, ensure_local_dirs, migrate_legacy_local_data, safe_quit_webdriver

_DEFAULT_BEFORE_DATE = datetime(2900, 1, 1).date()
_DEFAULT_AFTER_DATE = datetime(1100, 1, 1).date()

_LOGGER = logging.getLogger(__name__)


def try_parse_date(date_string, fmt):
    try:
        return datetime.strptime(date_string, fmt).date()
    except ValueError:
        print("Error parsing date input:", sys.exc_info())
        sys.exit(1)


def handle_args():
    parser = argparse.ArgumentParser(description="Download lectures from  portal.")
    parser.add_argument(
        "url",
        nargs="?",
        default=None,
        help="Full URL of the echo360 course page, \
              or only the UUID (which defaults to USYD). \
              The URL of the course's video lecture page, \
              for example: http://recordings.engineering.illinois.edu/ess/portal/section/115f3def-7371-4e98-b72f-6efe53771b2a). \
              Optional when using --pick-course.",
        metavar="ECHO360_URL",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Path to the desired output directory. The output \
                             directory must exist. Otherwise the current \
                             directory is used.",
        metavar="OUTPUT_PATH",
    )
    parser.add_argument(
        "--after-date",
        dest="after_date",
        help="Only download lectures newer than AFTER_DATE \
                             (inclusive). Note: this may be combined with \
                             --before-date.",
        metavar="AFTER_DATE(YYYY-MM-DD)",
    )
    parser.add_argument(
        "--before-date",
        dest="before_date",
        help="Only download lectures older than BEFORE_DATE \
                              (inclusive). Note: this may be combined with \
                              --after-date",
        metavar="BEFORE_DATE(YYYY-MM-DD)",
    )
    parser.add_argument(
        "--unikey",
        "-u",
        dest="unikey",
        help="Your unikey for your University of \
                              Sydney elearning account",
        metavar="UNIKEY",
    )
    parser.add_argument(
        "--password",
        "-p",
        dest="password",
        help="Your password for your University of \
                              Sydney elearning account",
        metavar="PASSWORD",
    )
    parser.add_argument(
        "--setup-credentials",
        action="store_true",
        default=False,
        dest="setup_credential",
        help="Open a chrome instance to expose an ability for user to log into \
                                any website to obtain credentials needed before proceeding. \
                                (implies using chrome-driver)",
    )
    parser.add_argument(
        "--persistent-session",
        "-P",
        action="store_true",
        default=False,
        dest="persistent_session",
        help="Starts a persistent session (helps to store credentials). Session uses \
            '{}' folder, and currently only supports chrome driver.".format(
            PERSISTENT_SESSION_FOLDER
        ),
    )
    parser.add_argument(
        "--download-phantomjs-binary",
        action="store_true",
        default=False,
        dest="download_binary",
        help="Force the echo360.py script to download a local \
                              binary file for phantomjs (will override system bin)",
    )
    parser.add_argument(
        "--stealth",
        action="store_true",
        default=False,
        dest="use_stealth",
        help="Use Stealth Chrome Driver to bypass some bot detection (e.g. useful for FIDO).",
    )
    parser.add_argument(
        "--chrome",
        action="store_true",
        default=False,
        dest="use_chrome",
        help="Use Chrome Driver instead of phantomjs webdriver. You \
                              must have chromedriver installed in your PATH.",
    )
    parser.add_argument(
        "--firefox",
        action="store_true",
        default=False,
        dest="use_firefox",
        help="Use Firefox Driver instead of phantomjs webdriver. You \
                              must have geckodriver installed in your PATH.",
    )
    parser.add_argument(
        "--echo360cloud",
        action="store_true",
        default=False,
        help="Treat the given hostname as echo360 cloud platform.",
    )
    parser.add_argument(
        "--pick-course",
        action="store_true",
        default=False,
        dest="pick_course",
        help="Echo360 Cloud only: list enrolled courses (via /user/enrollments) \
              and download all recording streams for each course you select.",
    )
    parser.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        default=False,
        help="Interactively pick the lectures you want, instead of download all \
                              (default) or based on dates .",
    )
    parser.add_argument(
        "--alternative_feeds",
        "-a",
        action="store_true",
        default=False,
        dest="alternative_feeds",
        help="Download first two video feeds. Since some university have multiple \
                video feeds, with this option on the downloader will also try to download \
                the second video, which could be the alternative feed. Might only work on \
                some 'echo360.org' hosts.",
    )
    parser.add_argument(
        "--short-names",
        action="store_true",
        default=False,
        dest="short_names",
        help="Use simple file names (Lecture 01, Lecture 02, ...) and save under "
        "_local/downloads/by_lecture/ instead of long course/section folder paths.",
    )
    media_mode = parser.add_mutually_exclusive_group(required=False)
    media_mode.add_argument(
        "--video-only",
        action="store_true",
        default=False,
        dest="video_only",
        help="Download MP4 video only; skip .vtt transcripts and subtitle muxing.",
    )
    media_mode.add_argument(
        "--transcript-only",
        action="store_true",
        default=False,
        dest="transcript_only",
        help="Download .vtt transcripts only; skip MP4 video files.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        dest="enable_degbug",
        help="Enable extensive logging.",
    )

    redirection_option = parser.add_mutually_exclusive_group(required=False)
    redirection_option.add_argument(
        "--auto",
        action="store_true",
        help="Only effective for 'echo360.org' host. When set, this script will attempts to \
                              automatically redirects after you had logged into your \
                              institution's SSO.",
    )
    redirection_option.add_argument(
        "--manual",
        "-m",
        action="store_true",
        help="[Deprecated] Only effective for 'echo360.org' host. When set, the script requires user to \
                              manually continue the script within the terminal. This is the \
                              default behaviour and exists only for backward compatibility reason.",
    )

    args = vars(parser.parse_args())
    course_url = args["url"]
    pick_course = args["pick_course"]

    if pick_course and not course_url:
        course_url = "https://echo360.net.au/courses"
    if not pick_course and not course_url:
        parser.error("ECHO360_URL is required unless --pick-course is used.")

    output_path = (
        os.path.expanduser(args["output"])
        if args["output"] is not None
        else DOWNLOADS_DIR
    )
    output_path = output_path if os.path.isdir(output_path) else DOWNLOADS_DIR

    after_date = (
        try_parse_date(args["after_date"], "%Y-%m-%d")
        if args["after_date"]
        else _DEFAULT_AFTER_DATE
    )
    before_date = (
        try_parse_date(args["before_date"], "%Y-%m-%d")
        if args["before_date"]
        else _DEFAULT_BEFORE_DATE
    )

    username = args["unikey"]
    password = args["password"]
    # check if the given uuid is actually a full URL
    course_hostname = re.search(
        "https?:[/]{2}[^/]*", course_url
    )  # would be none if it does not exists
    if course_hostname is not None:
        course_hostname = course_hostname.group()
    else:
        _LOGGER.info(
            "Non-URL value is given, defaults to University of Sydney's echo system"
        )
        _LOGGER.info("Use the full URL if you want to use this in other University")

    args_without_sensitive_info = dict(args)
    args_without_sensitive_info.pop("unikey", None)
    args_without_sensitive_info.pop("password", None)
    _LOGGER.debug("Input args: %s", args_without_sensitive_info)
    _LOGGER.debug("Hostname: %s, UUID: %s", course_hostname, course_url)

    webdriver_to_use = "phantomjs"
    if args["use_stealth"]:
        webdriver_to_use = "stealth"
    elif args["use_chrome"]:
        webdriver_to_use = "chrome"
    elif args["use_firefox"]:
        webdriver_to_use = "firefox"

    is_cloud_url = course_hostname and any(
        token in course_hostname for token in ("echo360.org", "echo360.net")
    )
    if args["manual"]:
        manual = True
    elif args["auto"]:
        manual = False
    elif pick_course or is_cloud_url or args["echo360cloud"]:
        manual = False
    else:
        manual = True

    return (
        course_url,
        course_hostname,
        output_path,
        after_date,
        before_date,
        username,
        password,
        args["setup_credential"],
        args["download_binary"],
        webdriver_to_use,
        args["interactive"],
        args["enable_degbug"],
        manual,
        args["alternative_feeds"],
        args["echo360cloud"],
        args["persistent_session"],
        pick_course,
        args["short_names"],
        args["video_only"],
        args["transcript_only"],
    )


def main():
    (
        course_url,
        course_hostname,
        output_path,
        after_date,
        before_date,
        username,
        password,
        setup_credential,
        download_binary,
        webdriver_to_use,
        interactive_mode,
        enable_degbug,
        manual,
        alternative_feeds,
        usingEcho360Cloud,
        persistent_session,
        pick_course,
        short_names,
        video_only,
        transcript_only,
    ) = handle_args()

    migrate_legacy_local_data()
    setup_logging(enable_degbug)
    from .interrupt import install_sigint_handler

    install_sigint_handler()

    if not usingEcho360Cloud and course_hostname and any(
        token in course_hostname for token in ["echo360.org", "echo360.net"]
    ):
        print("> Echo360 Cloud platform detected")
        print("> This implies setup_credential, and using web_driver")
        print(
            ">> Log in in the browser (SSO if prompted); "
            "the script continues automatically."
        )
        print("-" * 65)
        usingEcho360Cloud = True
        setup_credential = True
    if usingEcho360Cloud:  # for manual --echo360cloud flag
        setup_credential = True

    if (video_only or transcript_only) and not usingEcho360Cloud and not pick_course:
        print(
            "> Note: --video-only / --transcript-only are fully supported on "
            "Echo360 Cloud; legacy portals may still download video normally."
        )

    def cmd_exists(x):
        return any(
            os.access(os.path.join(path, x), os.X_OK)
            for path in os.environ["PATH"].split(os.pathsep)
        )

    # NOTE: local binary will always override system PATH binary
    use_local_binary = True

    if setup_credential and webdriver_to_use == "phantomjs":
        # setup credentials must use web driver
        webdriver_to_use = "chrome"
    if webdriver_to_use == "chrome":
        from .binary_downloader.chromedriver import (
            ChromedriverDownloader as binary_downloader,
        )

        binary_type = "chromedriver"
    elif webdriver_to_use == "firefox":
        from .binary_downloader.firefoxdriver import (
            FirefoxDownloader as binary_downloader,
        )

        binary_type = "geckodriver"
    elif webdriver_to_use == "stealth":
        binary_type = None
    else:
        from .binary_downloader.phantomjs import (
            PhantomjsDownloader as binary_downloader,
        )

        binary_type = "phantomjs"

    if binary_type:
        binary_downloader = binary_downloader()  # initialise class
        _LOGGER.debug(
            "binary_downloader link: %s, bin path: %s",
            binary_downloader.get_download_link(),
            binary_downloader.get_bin(),
        )
        # First test for existance of localbinary file
        if not os.path.isfile(binary_downloader.get_bin()):
            # If failed, then test for existance of global executable in PATH
            if cmd_exists(binary_type):
                use_local_binary = False
                _LOGGER.debug("Using global binary file")
            else:
                # None exists, download binary file
                start_download_binary(binary_downloader, binary_type)
                _LOGGER.debug("Downloading binary file")

    if download_binary:
        start_download_binary(binary_downloader, binary_type, manual=True)
        exit(0)

    if pick_course:
        usingEcho360Cloud = True
        setup_credential = True
        if webdriver_to_use == "phantomjs":
            webdriver_to_use = "chrome"
        if not course_hostname:
            course_hostname = "https://echo360.net.au"
        bootstrap_uuid = "00000000-0000-0000-0000-000000000001"
        uuid_match = re.search(r"([0-9a-f-]{36})", course_url, re.I)
        if uuid_match:
            bootstrap_uuid = uuid_match.group(1)
        course = EchoCloudCourse(
            bootstrap_uuid,
            course_hostname,
            alternative_feeds,
            video_only=video_only,
            transcript_only=transcript_only,
        )
    elif usingEcho360Cloud:
        if "/lesson/" in course_url:
            print("ERROR: This is a single lecture URL, not a course URL.")
            print("Open your course's main page in Echo360 and use that URL instead.")
            print("It should look like:")
            print("  https://echo360.net.au/section/YOUR-SECTION-UUID/home")
            sys.exit(1)
        # echo360 cloud
        course_uuid = re.search(
            "[^/]([0-9a-zA-Z]+[-])+[0-9a-zA-Z]+", course_url
        ).group()  # retrieve the last part of the URL
        course = EchoCloudCourse(
            course_uuid,
            course_hostname,
            alternative_feeds,
            video_only=video_only,
            transcript_only=transcript_only,
        )
    else:
        # import it here for monkey patching gevent, to fix the followings:
        # MonkeyPatchWarning: Monkey-patching ssl after ssl has already been
        # imported may lead to errors, including RecursionError on Python 3.6.
        from . import hls_downloader

        course_uuid = re.search(
            "[^/]+(?=/$|$)", course_url
        ).group()  # retrieve the last part of the URL
        course = EchoCourse(
            course_uuid,
            course_hostname,
            alternative_feeds,
            video_only=video_only,
            transcript_only=transcript_only,
        )
    login_email = None
    if setup_credential and usingEcho360Cloud:
        login_email = resolve_echo360_email()
    downloader = EchoDownloader(
        course,
        output_path,
        date_range=(after_date, before_date),
        username=username,
        password=password,
        setup_credential=setup_credential,
        use_local_binary=use_local_binary,
        webdriver_to_use=webdriver_to_use,
        interactive_mode=interactive_mode,
        persistent_session=persistent_session,
        short_names=short_names,
    )

    _LOGGER.debug(
        '>>> Download will use "{}" webdriver from {} executable <<<'.format(
            binary_type, "LOCAL" if use_local_binary else "GLOBAL"
        )
    )
    if setup_credential:
        login_url = course_hostname
        if pick_course:
            login_url = "{}/courses".format(course_hostname.rstrip("/"))
        run_setup_credential(
            downloader._driver,
            login_url,
            echo360_cloud=True,
            manual=manual,
            email=login_email,
        )
        try:
            downloader._driver.set_window_size(0, 0)
            raise selenium.common.exceptions.InvalidArgumentException()
        except selenium.common.exceptions.InvalidArgumentException:
            # fallback to default size
            # see https://github.com/soraxas/echo360/issues/50
            downloader._driver.set_window_size(800, 600)

    try:
        if pick_course:
            sys.stdout.write(">> Loading enrolled courses... ")
            sys.stdout.flush()
            enrolled = fetch_enrollments(
                course_hostname, session_from_driver(downloader._driver)
            )
            print("Done!")
            picked_courses = pick_enrolled_courses(enrolled)
            for picked in picked_courses:
                print(
                    ">> Downloading all streams for {0} - {1}".format(
                        picked["courseCode"], picked["courseName"]
                    )
                )
                downloader.sections_in_course = len(picked["sections"])
                for section in picked["sections"]:
                    print("-" * 60)
                    print(
                        ">> Stream: {0} ({1} lessons)".format(
                            section["sectionName"], section.get("lessonCount", "?")
                        )
                    )
                    section_course = EchoCloudCourse(
                        section["sectionId"],
                        course_hostname,
                        alternative_feeds,
                        video_only=video_only,
                        transcript_only=transcript_only,
                        section_name=section["sectionName"],
                        course_code=section["courseCode"],
                        course_display_name=section["courseName"],
                    )
                    downloader._course = section_course
                    section_course.set_driver(downloader._driver)
                    downloader.download_all(close_driver=False)
        else:
            downloader.download_all()
    except KeyboardInterrupt:
        print("\n>> Stopped.")
        sys.exit(130)
    else:
        root = os.path.abspath(downloader._output_root)
        print("")
        print(">> All downloads are under:")
        print("   {0}".format(root))
    finally:
        try:
            safe_quit_webdriver(downloader._driver)
        except Exception:
            pass


def start_download_binary(binary_downloader, binary_type, manual=False):
    print("=" * 65)
    if not manual:
        print(
            "Binary file of {0} not found, will initiate a download process now...".format(
                binary_type
            )
        )
    binary_downloader.download()
    print("Done!")
    print("=" * 65)


def _echo360_logged_in(webdriver):
    try:
        if any(c.get("name") == "ECHO_JWT" for c in webdriver.get_cookies()):
            return True
        url = webdriver.current_url or ""
        if "login" in url:
            return False
        if "/courses" in url or "/section/" in url:
            return True
    except Exception:
        pass
    return False


def _try_echo360_email_login(webdriver, email):
    if not email or _echo360_logged_in(webdriver):
        return
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    try:
        wait = WebDriverWait(webdriver, 15)
        email_input = wait.until(
            EC.element_to_be_clickable(
                (
                    By.CSS_SELECTOR,
                    "input[type='email'], input[name='email'], input[id*='email' i]",
                )
            )
        )
        email_input.clear()
        email_input.send_keys(email)
        email_input.send_keys(Keys.RETURN)
        time.sleep(1)
    except Exception as exc:
        _LOGGER.debug("Echo360 email auto-fill skipped: %s", exc)


def run_setup_credential(
    webdriver, url, echo360_cloud=False, manual=False, email=None
):
    webdriver.get(url)
    time.sleep(1)
    # for making it compatiable with Python 2 & 3
    from sys import version_info

    if echo360_cloud and email:
        _try_echo360_email_login(webdriver, email)

    try:
        if echo360_cloud and not manual:
            if _echo360_logged_in(webdriver):
                print(">> Echo360 session ready.")
            else:
                print(
                    ">> Waiting for Echo360 login "
                    "(complete SSO in the browser if prompted)..."
                )
        elif manual:
            print(" >> After you finished logging in, type 'continue' in the terminal.")
        while True:
            if echo360_cloud and not manual:
                if _echo360_logged_in(webdriver):
                    break
                time.sleep(1)
            else:
                if version_info[0] > 2:
                    user_inputs = input("> Type 'continue' and press [enter]\n")
                else:
                    user_inputs = raw_input("> Type 'continue' and press [enter]\n")
                if user_inputs.lower() == "continue":
                    break
    except KeyboardInterrupt:
        pass


def setup_logging(enable_degbug=False):
    # set up logging to file - see previous section for more details
    logging_level = logging.DEBUG if enable_degbug else logging.INFO
    ensure_local_dirs()
    log_path = os.path.join(LOGS_DIR, "echo360Downloader.log")
    logging.basicConfig(
        level=logging_level,
        format="[%(levelname)s: %(asctime)s] %(name)-12s %(message)s",
        datefmt="%m-%d %H:%M",
        filename=log_path,
        filemode="w",
    )
    # define a Handler which writes INFO messages or higher to the sys.stderr
    console = logging.StreamHandler()
    console.setLevel(logging_level)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logging.getLogger("").addHandler(console)  # add handler to the root logger
