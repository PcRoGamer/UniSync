import dateutil.parser
import os
import sys
import logging
import re
import time

from .course import EchoCloudCourse
from .echo_exceptions import EchoLoginError
from .utils import naive_versiontuple, PERSISTENT_SESSION_FOLDER, DOWNLOADS_DIR, LOGS_DIR, ensure_local_dirs, sanitize_path_component, truncate_filename_for_section, BY_LECTURE_SUBDIR, clear_stale_browser_session_locks, terminate_chrome_processes_for_profile, format_downloads_location

import pip_ensure_version
from pick import pick
import selenium
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
import selenium.common.exceptions as seleniumException
import warnings  # hide the warnings of phantomjs being deprecated

warnings.filterwarnings("ignore", category=UserWarning, module="selenium")

_LOGGER = logging.getLogger(__name__)


def build_stealth_driver(
    use_local_binary,
    selenium_version_ge_4100,
    setup_credential,
    user_agent,
    log_path,
    persistent_session,
):
    pip_ensure_version.require_package("undetected-chromedriver")
    import undetected_chromedriver as uc

    kwargs = dict()
    if persistent_session:
        kwargs["user_data_dir"] = PERSISTENT_SESSION_FOLDER
        print(
            ">> Warning: persistent session *might* not supported by stealth. If it has issue, try to delete '{}' folder.".format(
                PERSISTENT_SESSION_FOLDER
            )
        )

    from selenium.webdriver.chrome.options import Options

    opts = Options()
    opts.add_argument("--window-size=1920x1080")
    opts.add_argument("user-agent={}".format(user_agent))

    if not selenium_version_ge_4100:
        print(">> This version of selenium might not be supported by stealth")

    driver = uc.Chrome(**kwargs)

    return driver


def _launch_chrome(kwargs, persistent_session):
    max_attempts = 3 if persistent_session else 1
    last_exc = None
    for attempt in range(max_attempts):
        if persistent_session:
            if attempt:
                time.sleep(1.5 * attempt)
                terminate_chrome_processes_for_profile(PERSISTENT_SESSION_FOLDER)
            clear_stale_browser_session_locks(PERSISTENT_SESSION_FOLDER)
        try:
            return webdriver.Chrome(**kwargs)
        except seleniumException.SessionNotCreatedException as exc:
            last_exc = exc
            message = str(exc).lower()
            recoverable = persistent_session and (
                "devtoolsactiveport" in message or "chrome failed to start" in message
            )
            if not recoverable or attempt >= max_attempts - 1:
                if recoverable:
                    print(
                        ">> Chrome still failed after clearing locks "
                        "({0} attempts).".format(max_attempts)
                    )
                    print(
                        ">> Delete '{0}' and rerun to log in again.".format(
                            PERSISTENT_SESSION_FOLDER
                        )
                    )
                raise
            print(
                ">> Chrome profile was locked (common after Ctrl+C or taskkill). "
                "Clearing stale locks and retrying ({0}/{1})...".format(
                    attempt + 2, max_attempts
                )
            )
    raise last_exc


def build_chrome_driver(
    use_local_binary,
    selenium_version_ge_4100,
    setup_credential,
    user_agent,
    log_path,
    persistent_session,
):
    from selenium.webdriver.chrome.options import Options

    opts = Options()
    if not setup_credential:
        opts.add_argument("--headless")
    if persistent_session:
        folder_path = PERSISTENT_SESSION_FOLDER  # default current dir
        opts.add_argument("--user-data-dir={}".format(folder_path))
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920x1080")
    opts.add_argument("user-agent={}".format(user_agent))

    kwargs = dict()
    if selenium_version_ge_4100:
        kwargs["options"] = opts
    else:
        kwargs["chrome_options"] = opts

    if selenium_version_ge_4100:
        from selenium.webdriver.chrome.service import Service

        service = Service(log_output=log_path)
        kwargs = dict(
            service=service,
            options=opts,
        )
    else:
        if use_local_binary:
            # newer selenium helps us to auto-download executable
            from .binary_downloader.chromedriver import ChromedriverDownloader

            kwargs["executable_path"] = ChromedriverDownloader().get_bin()
        kwargs.update(
            dict(
                service_log_path=log_path,
                chrome_options=opts,
            )
        )
    return _launch_chrome(kwargs, persistent_session)


def build_firefox_driver(
    use_local_binary,
    selenium_version_ge_4100,
    setup_credential,
    user_agent,
    log_path,
    persistent_session,
):
    if persistent_session:
        raise NotImplementedError(
            "Save-login not implemented for Firefox! Feel free to make a PR for it..."
        )

    profile = webdriver.FirefoxProfile()
    profile.set_preference("general.useragent.override", user_agent)
    kwargs = dict()

    if selenium_version_ge_4100:
        from selenium.webdriver.firefox.service import Service
        from selenium.webdriver.firefox.options import Options

        option = Options()
        option.profile = profile

        service = Service(log_output=log_path)
        kwargs = dict(
            service=service,
            options=option,
        )
    else:
        if use_local_binary:
            from .binary_downloader.firefoxdriver import FirefoxDownloader

            kwargs["executable_path"] = FirefoxDownloader().get_bin()
        kwargs.update(
            dict(
                service_log_path=log_path,
                firefox_profile=profile,
            )
        )
    return webdriver.Firefox(**kwargs)


def build_phantomjs_driver(
    use_local_binary,
    selenium_version_ge_4100,
    setup_credential,
    user_agent,
    log_path,
    persistent_session,
):
    if persistent_session:
        raise NotImplementedError(
            "Save-login not implemented for Firefox! Feel free to make a PR for it..."
        )

    dcap = dict()
    dcap.update(DesiredCapabilities.PHANTOMJS)
    dcap["phantomjs.page.settings.userAgent"] = (
        "Mozilla/5.0 (iPad; CPU OS 6_0 like Mac OS X) AppleWebKit/536.26 "
        "(KHTML, like Gecko) Version/6.0 Mobile/10A5376e Safari/8536.25"
    )
    kwargs = {
        "desired_capabilities": dcap,
        "service_log_path": log_path,
    }

    if use_local_binary:
        from .binary_downloader.phantomjs import PhantomjsDownloader

        kwargs["executable_path"] = PhantomjsDownloader().get_bin()

    return webdriver.PhantomJS(**kwargs)


class EchoDownloader(object):
    def __init__(
        self,
        course,
        output_dir,
        date_range,
        username,
        password,
        setup_credential,
        use_local_binary=False,
        webdriver_to_use="phantomjs",
        interactive_mode=False,
        persistent_session=False,
        short_names=False,
    ):
        self._course = course
        ensure_local_dirs()
        if output_dir == "" or output_dir == "default_out_path":
            output_dir = DOWNLOADS_DIR
        self._output_root = output_dir
        self._output_dir = output_dir
        self._date_range = date_range
        self._username = username
        self._password = password
        self.interactive_mode = interactive_mode
        self.short_names = short_names
        self.sections_in_course = 1

        self.regex_replace_invalid = re.compile(r"[\\\\/:*?\"<>|]")

        # define a log path for phantomjs to output, to prevent hanging due to PIPE being full
        log_path = os.path.join(LOGS_DIR, "webdriver_service.log")

        self._useragent = "Mozilla/5.0 (iPad; CPU OS 6_0 like Mac OS X) AppleWebKit/536.26 (KHTML, like Gecko) Version/6.0 Mobile/10A5376e Safari/8536.25"
        # self._driver = webdriver.PhantomJS()

        if webdriver_to_use == "phantomjs":
            selenium_major_version = int(selenium.__version__.split(".")[0])
            if selenium_major_version >= 4:
                print("============================================================")
                print("WARNING: PhantomJS support had been removed in in selenium")
                print("         version 4. If this app errors out later on, consider")
                print("         installing earlier version of selenium.")
                print("         e.g. pip3 install selenium==3.14")
                print(
                    "         (see https://github.com/SeleniumHQ/selenium/blob/58122b261a5f5406da8e5252c9ab54c464da7aa8/py/CHANGES#L324)"
                )
                print("============================================================")

        if webdriver_to_use == "chrome":
            driver_builder = build_chrome_driver
        elif webdriver_to_use == "firefox":
            driver_builder = build_firefox_driver
        elif webdriver_to_use == "stealth":
            driver_builder = build_stealth_driver
        else:
            driver_builder = build_phantomjs_driver

        self._driver = driver_builder(
            use_local_binary=use_local_binary,
            selenium_version_ge_4100=(
                naive_versiontuple(selenium.__version__) >= naive_versiontuple("4.10.0")
            ),
            setup_credential=setup_credential,
            user_agent=self._useragent,
            log_path=log_path,
            persistent_session=persistent_session,
        )

        self.setup_credential = setup_credential
        # Monkey Patch, set the course's driver to the one from .downloader
        self._course.set_driver(self._driver)
        self._videos = []

    def login(self):
        # Initialize to establish the 'anon' cookie that Echo360 sends.
        self._driver.get(self._course.url)
        # First see if we have successfully access course page without the need to login
        # for example: https://view.streaming.sydney.edu.au:8443/ess/portal/section/ed9b26eb-a785-4f4e-bd51-69f3faab388a
        if self.find_element_by_partial_id("username") is not None:
            self.loginWithCredentials()
        else:
            # check if it is network error
            if "<html><head></head><body></body></html>" in self._driver.page_source:
                print("Failed!")
                print("  > Failed to connect to server, is your internet working...?")
                _LOGGER.debug("Network seems to be down")
                _LOGGER.debug(
                    "Dumping page at %s: %s", self._course.url, self._driver.page_source
                )
                raise EchoLoginError(self._driver)
            elif "check your URL" in self._driver.page_source:
                print("Failed!")
                print("  > Failed to connet to course page, is the uuid correct...?")
                _LOGGER.debug("Failed to find a valid course page")
                _LOGGER.debug(
                    "Dumping page at %s: %s", self._course.url, self._driver.page_source
                )
                raise EchoLoginError(self._driver)
            else:
                # Should be only for the case where login details is not required left
                print("INFO: No need to login :)")
                _LOGGER.debug("No username found (no need to login?)")
                _LOGGER.debug(
                    "Dumping login page at %s: %s",
                    self._course.url,
                    self._driver.page_source,
                )
        if not isinstance(self._course, EchoCloudCourse):
            # for canvas echo360
            self.retrieve_real_uuid()
        print("Done!")

    def loginWithCredentials(self):
        _LOGGER.debug("Logging in with credentials")
        # retrieve username / password if not given before
        if self._username is None or self._password is None:
            print("Credentials needed...")
            if self._username is None:
                if sys.version_info < (3, 0):  # special handling for python2
                    input = raw_input
                else:
                    from builtins import input
                self._username = input("Unikey: ")
            if self._password is None:
                import getpass

                self._password = getpass.getpass(
                    "Passowrd for {0}: ".format(self._username)
                )
        # Input username and password:
        # user_name = self._driver.find_element_by_id('j_username')
        user_name = self.find_element_by_partial_id("username")
        user_name.clear()
        user_name.send_keys(self._username)

        # user_passwd = self._driver.find_element_by_id('j_password')
        user_passwd = self.find_element_by_partial_id("password")
        user_passwd.clear()
        user_passwd.send_keys(self._password)

        try:
            login_btn = self._driver.find_element(By.ID, "login-btn")
            login_btn.submit()
        except seleniumException.NoSuchElementException:
            # try submit via enter key
            from selenium.webdriver.common.keys import Keys

            user_passwd.send_keys(Keys.RETURN)

        # test if the login is success
        if self.find_element_by_partial_id("username") is not None:
            print("Failed!")
            print("  > Failed to login, is your username/password correct...?")
            raise EchoLoginError(self._driver)

    def download_all(self, close_driver=True):
        if self.setup_credential:
            sys.stdout.write(
                ">> I'm gonna assume you are responsible enough to had "
                "finished logged in by now ;)\n"
            )
        else:
            sys.stdout.write('>> Logging into "{0}"... '.format(self._course.url))
            sys.stdout.flush()
            self.login()
        sys.stdout.write(">> Retrieving echo360 Course Info... ")
        sys.stdout.flush()
        videos = self._course.get_videos().videos
        print("Done!")
        self._output_dir = self._build_section_output_dir()

        filtered_videos = [video for video in videos if self._in_date_range(video.date)]
        videos_to_be_download = []
        for video in reversed(filtered_videos):  # reverse so we download newest first
            lecture_number = self._find_pos(videos, video)
            # Sometimes a video could have multiple part. This special method returns a
            # generator where: (i) if it's a multi-part video it will contains multiple
            # videos and (ii) if it is NOT a multi-part video, it will just
            # returns itself
            sub_videos = video.get_all_parts()
            for sub_i, sub_video in reversed(list(enumerate(sub_videos))):
                sub_lecture_num = lecture_number + 1
                # use a friendly way to name sub-part lectures
                if len(sub_videos) > 1:
                    sub_lecture_num = "{}.{}".format(sub_lecture_num, sub_i + 1)
                if self.short_names:
                    title = self._short_lecture_title(sub_lecture_num)
                else:
                    title = "Lecture {} [{}]".format(sub_lecture_num, sub_video.title)
                filename = self._get_filename(
                    self._course.course_id, sub_video.date, title
                )
                videos_to_be_download.append((filename, sub_video))
        if self.interactive_mode:
            title = (
                "Select video(s) to be downloaded (SPACE to mark, ENTER to continue):"
            )
            selected = pick(
                [v[0] for v in videos_to_be_download],
                title,
                multiselect=True,
                min_selection_count=1,
            )
            videos_to_be_download = [videos_to_be_download[s[1]] for s in selected]

        print("=" * 60)
        print("    Course: {0}".format(self._course.nice_name))
        if isinstance(self._course, EchoCloudCourse):
            print("    Stream: {0}".format(self._course.section_folder_name))
        n_items = len(videos_to_be_download)
        n_lessons = len(videos)
        if n_items != n_lessons:
            count_msg = "{0} download items from {1} lessons".format(
                n_items, n_lessons
            )
        else:
            count_msg = "{0} videos".format(n_items)
        print("      Total to download: {0}".format(count_msg))
        print("=" * 60)

        downloaded_videos = []
        try:
            for filename, video in videos_to_be_download:
                if video.url is False:
                    print(
                        ">> Skipping Lecture '{0}' (no published media yet).".format(
                            filename
                        )
                    )
                else:
                    if video.download(self._output_dir, filename):
                        downloaded_videos.insert(0, filename)
        except KeyboardInterrupt:
            print("\n>> Download cancelled (Ctrl+C).")
            raise
        print(self.success_msg(self._course.course_name, downloaded_videos))
        if downloaded_videos:
            print(format_downloads_location(self._output_dir))
        if close_driver:
            self._driver.close()

    def _build_section_output_dir(self):
        single_stream = self.sections_in_course <= 1
        if isinstance(self._course, EchoCloudCourse):
            if self.short_names:
                course_folder = self._short_course_folder_name()
                if single_stream:
                    path = os.path.join(
                        self._output_root, BY_LECTURE_SUBDIR, course_folder
                    )
                else:
                    section_slug = sanitize_path_component(
                        self._course.section_folder_name.replace("/", "_")
                    )[:40]
                    path = os.path.join(
                        self._output_root, BY_LECTURE_SUBDIR, course_folder, section_slug
                    )
            else:
                course_folder = sanitize_path_component(self._course.output_folder_name)
                if single_stream:
                    path = os.path.join(self._output_root, course_folder)
                else:
                    section_folder = sanitize_path_component(
                        self._course.section_folder_name
                    )
                    path = os.path.join(self._output_root, course_folder, section_folder)
        else:
            if self.short_names:
                path = os.path.join(
                    self._output_root,
                    BY_LECTURE_SUBDIR,
                    sanitize_path_component("{0}".format(self._course.nice_name).strip())[
                        :40
                    ],
                )
            else:
                path = os.path.join(
                    self._output_root,
                    sanitize_path_component("{0}".format(self._course.nice_name).strip()),
                )
        # Sanitize folder name components only; do not regex the full path (breaks C:\ on Windows).
        return path

    def _short_course_folder_name(self):
        code = getattr(self._course, "_course_code", None) or ""
        if code:
            return sanitize_path_component(code.replace("/", "_"))
        return sanitize_path_component(self._course.output_folder_name)[:40]

    @staticmethod
    def _short_lecture_title(lecture_num):
        """e.g. 7.2 -> Lecture 07.2, 12 -> Lecture 12"""
        text = str(lecture_num)
        if "." in text:
            main, sub = text.split(".", 1)
            return "Lecture {:02d}.{}".format(int(main), sub)
        return "Lecture {:02d}".format(int(text))

    @property
    def useragent(self):
        return self._useragent

    @useragent.setter
    def useragent(self, useragent):
        self._useragent = useragent

    def _initialize(self, echo_course):
        self._driver.get(self._course.url)

    def _get_filename(self, course, date, title):
        if self.short_names:
            filename = title
        elif course:
            filename = "{} - {} - {}".format(course, date, title[:150])
        else:
            filename = "{} - {}".format(date, title[:150])
        filename = self.regex_replace_invalid.sub("_", filename)
        if self.short_names:
            return filename
        return truncate_filename_for_section(self._build_section_output_dir(), filename)

    def _in_date_range(self, date_string):
        the_date = dateutil.parser.parse(date_string).date()
        return self._date_range[0] <= the_date and the_date <= self._date_range[1]

    def _find_pos(self, videos, the_video):
        # compare by object id, because date could possibly be the same in some case.
        return videos.index(the_video)

    def success_msg(self, course_name, videos):
        bar = "=" * 65
        msg = "\n{0}\n".format(bar)
        msg += "    Course: {0}".format(self._course.nice_name)
        msg += "\n{0}\n".format(bar)
        msg += "    Successfully downloaded:\n"
        for i in videos:
            msg += "        {}\n".format(i)
        msg += "{0}\n".format(bar)
        return msg

    def find_element_by_partial_id(self, id):
        try:
            return self._driver.find_element(
                By.XPATH, "//*[contains(@id,'{0}')]".format(id)
            )
        except seleniumException.NoSuchElementException:
            return None

    def retrieve_real_uuid(self):
        # patch for cavas (canvas.sydney.edu.au) where uuid is hidden in page source
        # we detect it by trying to retrieve the real uuid
        uuid = re.search(
            "/ess/client/section/([0-9a-zA-Z]{8}-[0-9a-zA-Z]{4}-[0-9a-zA-Z]{4}-[0-9a-zA-Z]{4}-[0-9a-zA-Z]{12})",
            self._driver.page_source,
        )
        if uuid is not None:
            uuid = uuid.groups()[0]
            self._course._uuid = uuid
