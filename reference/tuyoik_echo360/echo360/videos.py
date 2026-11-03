import os
import re

import dateutil.parser
import operator
import sys

import ffmpy
import requests
import selenium
import logging

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException
from selenium.common.exceptions import InvalidSessionIdException

from .hls_downloader import Downloader, urljoin, stream_response_to_file
from .naive_m3u8_parser import NaiveM3U8Parser
from .utils import (
    ensure_section_media_dirs,
    safe_move,
    truncate_filename_for_dir,
    truncate_filename_for_section,
    win_extended_path,
    write_text_file,
)

_LOGGER = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
_SEGMENT_M3U8_RE = re.compile(r"/s\d+q\d+\.m3u8", re.I)


def _ms_to_vtt_timestamp(ms):
    """Convert milliseconds to WebVTT timestamp format: HH:MM:SS.mmm"""
    h = ms // 3600000
    ms %= 3600000
    m = ms // 60000
    ms %= 60000
    s = ms // 1000
    ms %= 1000
    return "{:02d}:{:02d}:{:02d}.{:03d}".format(h, m, s, ms)


def _cues_to_vtt(cues):
    lines = ["WEBVTT", ""]
    for cue in cues:
        text = cue.get("content", "").strip()
        if not text:
            continue
        title = cue.get("title")
        if title:
            lines.append(str(title))
        start = _ms_to_vtt_timestamp(cue["startMs"])
        end = _ms_to_vtt_timestamp(cue["endMs"])
        speaker = cue.get("speaker", "").strip()
        lines.append("{} --> {}".format(start, end))
        if speaker:
            lines.append("<v {}>{}".format(speaker, text))
        else:
            lines.append(text)
        lines.append("")
    return "\n".join(lines)


_VOICE_TAG_RE = re.compile(r"^<v [^>]+>(.*)$", re.I)


def _normalize_vtt_for_playback(text):
    """Rebuild Echo360 WebVTT into a compact file players can render."""
    if isinstance(text, bytes):
        text = text.decode("utf-8")

    lines = text.replace("\r\n", "\n").split("\n")
    out = ["WEBVTT", ""]
    i = 0
    while i < len(lines) and "-->" not in lines[i]:
        i += 1

    while i < len(lines):
        timing = lines[i].strip()
        if "-->" not in timing:
            i += 1
            continue
        i += 1
        payload = []
        while i < len(lines):
            stripped = lines[i].strip()
            if stripped.startswith("NOTE"):
                i += 1
                while i < len(lines) and lines[i].strip() != "":
                    i += 1
                continue
            if stripped == "":
                if payload:
                    break
                i += 1
                continue
            if "-->" in stripped:
                break
            voice_match = _VOICE_TAG_RE.match(stripped)
            payload.append(voice_match.group(1) if voice_match else stripped)
            i += 1
        if payload:
            out.append(timing)
            out.extend(payload)
            out.append("")

    return "\n".join(out).rstrip() + "\n"


def _extract_uuids_from_url(video_url):
    try:
        from urllib.parse import urlparse
    except ImportError:
        from urlparse import urlparse
    return [p for p in urlparse(video_url).path.split("/") if _UUID_RE.match(p)]


def update_course_retrieval_progress(current, total):
    prefix = ">> Retrieving echo360 Course Info... "
    status = "{}/{} videos".format(current, total)
    text = "\r{0} {1} ".format(prefix, status)
    sys.stdout.write(text)
    sys.stdout.flush()


class EchoVideos(object):
    def __init__(self, videos_json, driver, video_only=False, transcript_only=False):
        assert videos_json is not None
        self._driver = driver
        self._videos = []
        total_videos_num = len(videos_json)
        update_course_retrieval_progress(0, total_videos_num)

        for i, video_json in enumerate(videos_json):
            self._videos.append(
                EchoVideo(
                    video_json,
                    self._driver,
                    video_only=video_only,
                    transcript_only=transcript_only,
                )
            )
            update_course_retrieval_progress(i + 1, total_videos_num)

        self._videos.sort(key=operator.attrgetter("date"))

    @property
    def videos(self):
        return self._videos

    def _blow_up(self, str, e):
        print(str)
        print("Exception: {}".format(str(e)))
        sys.exit(1)


class EchoVideo(object):
    def __init__(self, video_json, driver, video_only=False, transcript_only=False):
        self._driver = driver
        self.video_only = video_only
        self.transcript_only = transcript_only

        try:
            video_url = "{0}".format(video_json["richMedia"])
            video_url = str(video_url)  # cast back to string

            self._driver.get(video_url)
            _LOGGER.debug(
                "Dumping video page at %s: %s", video_url, self._driver.page_source
            )

            m3u8_url = self._loop_find_m3u8_url(video_url, waitsecond=30)
            _LOGGER.debug("Found the following urls %s", m3u8_url)
            self._url = m3u8_url

            self._date = self.get_date(video_json["startTime"])
            self._title = video_json["title"]

        except KeyError as e:
            self._blow_up("Unable to parse video data from JSON (course_data)", e)

    def _loop_find_m3u8_url(self, video_url, waitsecond=15, max_attempts=5):
        stale_attempt = 1
        refresh_attempt = 1
        while True:
            self._driver.get(video_url)
            try:
                # wait for maximum second before timeout
                WebDriverWait(self._driver, waitsecond).until(
                    EC.presence_of_element_located((By.ID, "content-player"))
                )
                return (
                    self._driver.find_element(By.ID, "content-player")
                    .find_element(By.TAG_NAME, "video")
                    .get_attribute("src")
                )
            except selenium.common.exceptions.TimeoutException:
                if refresh_attempt >= max_attempts:
                    print(
                        "\r\nERROR: Connection timeouted after {} second for {} attempts... \
                          Possibly internet problem?".format(
                            waitsecond, max_attempts
                        )
                    )
                    raise
                refresh_attempt += 1
            except StaleElementReferenceException:
                if stale_attempt >= max_attempts:
                    print(
                        "\r\nERROR: Elements are not stable to retrieve after {} attempts... \
                        Possibly internet problem?".format(
                            max_attempts
                        )
                    )
                    raise
                stale_attempt += 1

    @property
    def date(self):
        return self._date

    @property
    def url(self):
        return self._url

    @property
    def title(self):
        if type(self._title) != str:
            # it's type unicode for python2
            return self._title.encode("utf-8")
        return self._title

    def get_date(self, video_json):
        try:
            # date is not important so we will just ignore it if something went wrong
            # Also, some echoCloud videos returns None for video start time... :(
            date = dateutil.parser.parse(self._extract_date(video_json)).date()
            return date.strftime("%Y-%m-%d")
        except Exception:
            return "1970-01-01"

    def _extract_date(self, video_json):
        return video_json["startTime"]

    def _blow_up(self, str, e):
        print(str)
        print("Exception: {}".format(str(e)))
        sys.exit(1)

    def download(self, output_dir, filename, pool_size=50):
        print("")
        print("-" * 60)
        print('Downloading "{}"'.format(filename))
        self._download_url_to_dir(self.url, output_dir, filename, pool_size)
        print("-" * 60)
        return True

    def _safe_driver_cookies(self):
        try:
            return self._driver.get_cookies()
        except Exception:
            return []

    def _download_url_to_dir(
        self,
        url,
        output_dir,
        filename,
        pool_size,
        convert_to_mp4=True,
        selenium_cookies=None,
    ):
        if selenium_cookies is None:
            selenium_cookies = self._safe_driver_cookies()
        echo360_downloader = Downloader(pool_size, selenium_cookies=selenium_cookies)
        try:
            echo360_downloader.run(url, output_dir, convert_to_mp4=convert_to_mp4)
        except KeyboardInterrupt:
            raise

        # rename file
        src = echo360_downloader.result_file_name
        ext = src.rsplit(".", 1)[-1]
        result_full_path = os.path.join(output_dir, "{0}.{1}".format(filename, ext))
        if os.path.abspath(src) != os.path.abspath(result_full_path):
            safe_move(src, result_full_path)
        return result_full_path

    def _download_url_to_dir_request(self, session, url, output_dir, filename):
        ext = url.split(".")[-1]

        r = session.get(url, stream=True)
        result_full_path = os.path.join(output_dir, filename + ext)
        stream_response_to_file(r, result_full_path, title="  > Downloading")
        return result_full_path

    def get_all_parts(self):
        return [self]


class EchoCloudVideos(EchoVideos):
    def __init__(
        self,
        videos_json,
        driver,
        hostname,
        alternative_feeds,
        skip_video_on_error=True,
        video_only=False,
        transcript_only=False,
    ):
        assert videos_json is not None
        self._driver = driver
        self._videos = []
        total_videos_num = len(videos_json)
        update_course_retrieval_progress(0, total_videos_num)

        for i, video_json in enumerate(videos_json):
            try:
                self._videos.append(
                    EchoCloudVideo(
                        video_json,
                        self._driver,
                        hostname,
                        alternative_feeds,
                        video_only=video_only,
                        transcript_only=transcript_only,
                    )
                )
            except Exception:
                if not skip_video_on_error:
                    raise
            update_course_retrieval_progress(i + 1, total_videos_num)

        self._videos.sort(key=operator.attrgetter("date"))

    @property
    def videos(self):
        return self._videos


class EchoCloudVideo(EchoVideo):
    @property
    def video_url(self):
        return "{}/lesson/{}/classroom".format(self.hostname, self.video_id)

    def __init__(
        self, video_json, driver, hostname, alternative_feeds, video_only=False, transcript_only=False
    ):
        self.hostname = hostname
        self._driver = driver
        self.video_json = video_json
        self.is_multipart_video = False
        self.sub_videos = [self]
        self.download_alternative_feeds = alternative_feeds
        self.video_only = video_only
        self.transcript_only = transcript_only
        if "lessons" in video_json:
            # IS a multi-part lesson.
            self.sub_videos = [
                EchoCloudSubVideo(
                    sub_video_json,
                    driver,
                    hostname,
                    group_name=video_json["groupInfo"]["name"],
                    alternative_feeds=alternative_feeds,
                    video_only=video_only,
                    transcript_only=transcript_only,
                )
                for sub_video_json in video_json["lessons"]
            ]
            self.is_multipart_video = True
            # THIS OBJECT SHOULD NOT BE USED ANYMORE as no further
            # processing will be proceeded.
            self._date = self.get_date(video_json)
            return

        video_id = "{0}".format(video_json["lesson"]["lesson"]["id"])
        self.video_id = str(video_id)  # cast back to string

        self._date = self.get_date(video_json)
        self._title = video_json["lesson"]["lesson"]["name"]
        # Resolve from syllabus JSON only during scan; browser lookup is deferred to download.
        self._url = self._resolve_url_from_json()

    def _url_from_json_mp4(self):
        lesson = self.video_json.get("lesson", {})
        mp4_files = lesson["video"]["media"]["media"]["current"]["primaryFiles"]
        urls = [obj["s3Url"] for obj in mp4_files if obj.get("s3Url")]
        if not urls:
            raise ValueError("Cannot find mp4 urls")
        return next(reversed(urls))

    def _url_from_json_m3u8(self):
        lesson = self.video_json.get("lesson", {})
        if not lesson.get("hasVideo"):
            return False
        try:
            manifests = lesson["video"]["media"]["media"]["versions"][0]["manifests"]
            m3u8urls = [m["uri"] for m in manifests if m.get("uri")]
        except (KeyError, IndexError, TypeError):
            m3u8urls = []
        if not m3u8urls:
            # Only skip when Echo360 explicitly marks the recording unavailable.
            if lesson.get("hasAvailableVideo") is False:
                return False
            return None
        try:
            from urllib.parse import urlparse
        except ImportError:
            from urlparse import urlparse
        new_m3u8urls = []
        new_hostname = urlparse(self.hostname).netloc
        for url in m3u8urls:
            parse_result = urlparse(url)
            new_m3u8urls.append(
                "{}://content.{}{}".format(
                    parse_result.scheme, new_hostname, parse_result.path
                )
            )
        return new_m3u8urls

    def _json_has_media_refs(self):
        lesson = self.video_json.get("lesson", {})
        try:
            manifests = lesson["video"]["media"]["media"]["versions"][0]["manifests"]
            if any(m.get("uri") for m in manifests):
                return True
        except (KeyError, IndexError, TypeError):
            pass
        try:
            files = lesson["video"]["media"]["media"]["current"]["primaryFiles"]
            if any(f.get("s3Url") for f in files):
                return True
        except (KeyError, IndexError, TypeError):
            pass
        return False

    def _lesson_is_in_future(self):
        from datetime import date

        try:
            return dateutil.parser.parse(self._date).date() > date.today()
        except Exception:
            return False

    def _lesson_media_unavailable(self):
        lesson = self.video_json.get("lesson", {})
        if not lesson.get("hasVideo"):
            return True
        if self._json_has_media_refs():
            return False
        if lesson.get("hasAvailableVideo") is False:
            return True
        # Scheduled slot with no media yet (common for future lectures on UNSW).
        if self._lesson_is_in_future():
            return True
        return False

    def _resolve_url_from_json(self):
        try:
            return self._url_from_json_mp4()
        except Exception as e:
            _LOGGER.debug("JSON mp4 lookup failed: %s", e)
        m3u8urls = self._url_from_json_m3u8()
        if m3u8urls is False:
            return False
        if m3u8urls:
            return self._finalize_m3u8_urls(m3u8urls, quiet=True)
        if self._lesson_media_unavailable():
            return False
        return None

    def _ensure_media_url(self, waitsecond=10):
        if self._url is False:
            return False
        if self._url is not None:
            return True
        if self._lesson_media_unavailable():
            self._url = False
            return False
        try:
            self._driver.get(self.video_url)
        except InvalidSessionIdException:
            print(
                ">> Chrome session ended (window closed?). "
                "Cannot look up media in browser."
            )
            return False
        _LOGGER.debug(
            "Dumping video page at %s: %s", self.video_url, self._driver.page_source
        )
        self._url = self._loop_find_m3u8_url(self.video_url, waitsecond=waitsecond)
        return self._url is not False and self._url is not None

    def download(self, output_dir, filename, pool_size=50):
        mp4_dir, transcript_dir = ensure_section_media_dirs(output_dir)
        filename = truncate_filename_for_section(output_dir, filename)
        mp4_path = os.path.join(mp4_dir, filename + ".mp4")
        vtt_path = os.path.join(transcript_dir, filename + ".vtt")

        if self.transcript_only:
            if os.path.exists(win_extended_path(vtt_path)):
                print("")
                print("-" * 60)
                print('Skipping "{}" (transcript already downloaded)'.format(filename))
                print("-" * 60)
                return True
            print("")
            print("-" * 60)
            print('Downloading transcript for "{}"'.format(filename))
            session = requests.Session()
            try:
                for cookie in self._safe_driver_cookies():
                    session.cookies.set(cookie["name"], cookie["value"])
            except InvalidSessionIdException:
                print("  [WARN] Chrome session ended; cannot fetch subtitles.")
                print("-" * 60)
                return False
            if not self._ensure_media_url(waitsecond=5):
                print(">> No media URL found for this lecture.")
                print("-" * 60)
                return False
            urls = self.url if isinstance(self.url, list) else [self.url]
            self._fetch_echo360_transcript(
                session, transcript_dir, filename, urls[0]
            )
            print("Done!")
            print("-" * 60)
            return os.path.exists(vtt_path)

        if os.path.exists(win_extended_path(mp4_path)):
            print("")
            print("-" * 60)
            print('Skipping "{}" (already downloaded)'.format(filename))
            session = requests.Session()
            try:
                for cookie in self._safe_driver_cookies():
                    session.cookies.set(cookie["name"], cookie["value"])
            except InvalidSessionIdException:
                print("  [WARN] Chrome session ended; cannot fetch missing subtitles.")
                print("-" * 60)
                return True
            if not self.video_only and self._url:
                urls = self.url if isinstance(self.url, list) else [self.url]
                self._ensure_transcript(
                    session, mp4_dir, transcript_dir, filename, urls[0]
                )
            print("-" * 60)
            return True

        if self._url is False or (
            self._url is None and self._lesson_media_unavailable()
        ):
            print("")
            print("-" * 60)
            print('Skipping "{}" (no published media yet)'.format(filename))
            print("-" * 60)
            return False

        print("")
        print("-" * 60)
        print('Downloading "{}"'.format(filename))
        if not self._ensure_media_url():
            print(">> No media URL found for this lecture.")
            print("-" * 60)
            return False
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        session = requests.Session()
        driver_cookies = self._safe_driver_cookies()
        for cookie in driver_cookies:
            session.cookies.set(cookie["name"], cookie["value"])

        urls = self.url
        if not isinstance(urls, list):
            urls = [urls]

        if not self.download_alternative_feeds:
            # download_alternative_feeds defaults to False, slice to include only the first one
            urls = urls[:1]

        final_result = True
        for counter, single_url in enumerate(urls):
            if self.download_alternative_feeds:
                print("- Downloading video feed {}...".format(counter + 1))
            new_filename = (
                (filename + str(counter + 1))
                if self.download_alternative_feeds
                else filename
            )
            result = self.download_single(
                session, single_url, output_dir, new_filename, pool_size, driver_cookies
            )
            final_result = final_result and result

        return final_result

    def download_single(
        self, session, single_url, output_dir, filename, pool_size, driver_cookies=None
    ):
        try:
            return self._download_single_impl(
                session, single_url, output_dir, filename, pool_size, driver_cookies
            )
        except KeyboardInterrupt:
            print("\n  > Download interrupted.")
            raise

    def _download_single_impl(
        self, session, single_url, output_dir, filename, pool_size, driver_cookies=None
    ):
        if driver_cookies is None:
            driver_cookies = self._safe_driver_cookies()
        mp4_dir, transcript_dir = ensure_section_media_dirs(output_dir)
        filename = truncate_filename_for_section(output_dir, filename)
        mp4_path = os.path.join(mp4_dir, filename + ".mp4")
        if os.path.exists(mp4_path):
            if not self.video_only:
                self._ensure_transcript(
                    session, mp4_dir, transcript_dir, filename, single_url
                )
            print(" > Skipping downloaded video")
            print("-" * 60)
            return True
        if self.transcript_only:
            self._fetch_echo360_transcript(
                session, transcript_dir, filename, single_url
            )
            print("Done!")
            print("-" * 60)
            return True
        if single_url.endswith(".m3u8"):
            r = session.get(single_url)
            if not r.ok:
                print("Error: Failed to get m3u8 info. Skipping this video")
                return False

            lines = [n for n in r.content.decode().split("\n")]
            m3u8_video = None
            m3u8_audio = None

            _LOGGER.debug("Searching for m3u8 with content {}".format(lines))

            m3u8_parser = NaiveM3U8Parser(lines)
            try:
                m3u8_parser.parse()
            except Exception as e:
                _LOGGER.debug("Exception occurred while parsing m3u8: {}".format(e))
                print("Failed to parse m3u8. Skipping...")
                return False

            m3u8_video, m3u8_audio = m3u8_parser.get_video_and_audio()

            if (
                m3u8_video is None
            ):  # even if audio is None it's okay, maybe audio is include with video
                print("ERROR: Failed to find video m3u8... skipping this one")
                return False
            # NOW we can finally start downloading!
            m3u8_subtitle = m3u8_parser.get_subtitle()

            audio_file = None
            if m3u8_audio is not None:
                print("  > Downloading audio:")
                audio_file = self._download_url_to_dir(
                    urljoin(single_url, m3u8_audio),
                    mp4_dir,
                    filename + "_audio",
                    pool_size,
                    convert_to_mp4=False,
                    selenium_cookies=driver_cookies,
                )
            print("  > Downloading video:")
            video_file = self._download_url_to_dir(
                urljoin(single_url, m3u8_video),
                mp4_dir,
                filename + "_video",
                pool_size,
                convert_to_mp4=False,
                selenium_cookies=driver_cookies,
            )

            vtt_path = None
            if not self.video_only:
                if m3u8_subtitle is not None:
                    print("  > Downloading subtitles (HLS track):")
                    vtt_path = self._download_vtt_segments(
                        session,
                        urljoin(single_url, m3u8_subtitle),
                        transcript_dir,
                        filename,
                    )
                else:
                    vtt_path = self._fetch_echo360_transcript(
                        session, transcript_dir, filename, single_url
                    )

            sys.stdout.write("  > Converting to mp4... ")
            sys.stdout.flush()

            # combine audio file with video (separate audio might not exists.)
            if self.combine_audio_video(
                audio_file=audio_file,
                video_file=video_file,
                final_file=mp4_path,
            ):
                # remove left-over plain audio/video files. (if mixing was successful)
                if audio_file is not None:
                    os.remove(audio_file)
                os.remove(video_file)

        else:  # ends with mp4
            r = session.get(single_url, stream=True)
            stream_response_to_file(r, mp4_path, title="  > Downloading video")
            if not self.video_only:
                vtt_path = self._fetch_echo360_transcript(
                    session, transcript_dir, filename, single_url
                )

        vtt_file = os.path.join(transcript_dir, filename + ".vtt")
        if (
            not self.video_only
            and os.path.exists(mp4_path)
            and os.path.exists(vtt_file)
        ):
            self.mux_subtitles_into_mp4(mp4_path, vtt_file)

        print("Done!")
        print("-" * 60)
        return True

    def _ensure_transcript(self, session, mp4_dir, transcript_dir, filename, video_url):
        """Fetch missing VTT for an existing download and mux into MP4."""
        if self.video_only:
            return
        vtt_path = os.path.join(transcript_dir, filename + ".vtt")
        mp4_path = os.path.join(mp4_dir, filename + ".mp4")
        if not os.path.exists(vtt_path):
            self._fetch_echo360_transcript(
                session, transcript_dir, filename, video_url
            )
        if os.path.exists(vtt_path) and os.path.exists(mp4_path):
            self.mux_subtitles_into_mp4(mp4_path, vtt_path)

    def _transcript_headers(self):
        headers = {"Accept": "application/json, text/vtt;q=0.9, */*;q=0.8"}
        for cookie in self._safe_driver_cookies():
            if cookie["name"] == "ECHO_JWT":
                headers["Authorization"] = "Bearer " + cookie["value"]
                break
        return headers

    def _resolve_media_id(self, video_url=None):
        institution_id = None
        try:
            institution_id = self.video_json["lesson"]["video"]["media"]["media"].get(
                "institutionId"
            )
        except (KeyError, TypeError):
            pass

        try:
            medias = self.video_json["lesson"]["medias"]
            if video_url:
                for media in medias:
                    media_id = media.get("id")
                    if media_id and media_id in video_url:
                        return media_id
            if len(medias) == 1:
                return medias[0].get("id")
        except (KeyError, TypeError):
            pass

        for key_path in (
            ("lesson", "video", "media", "media", "id"),
            ("lesson", "video", "media", "mediaId"),
        ):
            try:
                node = self.video_json
                for key in key_path:
                    node = node[key]
                if node:
                    return str(node)
            except (KeyError, TypeError):
                pass

        if video_url:
            uuids = _extract_uuids_from_url(video_url)
            if not uuids:
                return None
            if institution_id and uuids[0].lower() == str(institution_id).lower():
                if len(uuids) > 1:
                    return uuids[1]
                return None
            if len(uuids) >= 2:
                return uuids[1]
            return uuids[0]

        try:
            for media in self.video_json["lesson"]["medias"]:
                media_id = media.get("id")
                if media_id:
                    return media_id
        except (KeyError, TypeError):
            pass
        return None

    def _save_vtt(self, output_dir, filename, content):
        filename = truncate_filename_for_dir(output_dir, filename, ".vtt")
        vtt_path = os.path.join(output_dir, filename + ".vtt")
        write_text_file(vtt_path, _normalize_vtt_for_playback(content))
        print("  Subtitles saved: {}.vtt".format(filename))
        return vtt_path

    def _fetch_echo360_transcript(self, session, output_dir, filename, video_url=None):
        """
        Download transcript for Echo360 Cloud and save as WebVTT.
        Tries transcript-file?format=vtt first, then JSON /transcript.
        """
        media_id = self._resolve_media_id(video_url)
        if not media_id:
            print("  [WARN] Could not resolve media id for transcript.")
            _LOGGER.debug(
                "media_id not found for lesson %s (video_url=%s)",
                getattr(self, "video_id", None),
                video_url,
            )
            return None

        if not getattr(self, "video_id", None):
            print("  [WARN] Could not resolve lesson id for transcript.")
            return None

        headers = self._transcript_headers()
        vtt_url = "{}/api/ui/echoplayer/lessons/{}/medias/{}/transcript-file?format=vtt".format(
            self.hostname, self.video_id, media_id
        )
        try:
            print("  > Downloading subtitles:")
            r = session.get(vtt_url, headers=headers, timeout=20)
            if r.ok:
                body = r.content.lstrip()
                if body.upper().startswith(b"WEBVTT"):
                    return self._save_vtt(output_dir, filename, body)
                _LOGGER.debug(
                    "VTT endpoint returned non-VTT body: %s", body[:120]
                )
            else:
                _LOGGER.debug("VTT endpoint returned %s", r.status_code)

            json_url = "{}/api/ui/echoplayer/lessons/{}/medias/{}/transcript".format(
                self.hostname, self.video_id, media_id
            )
            r = session.get(json_url, headers=headers, timeout=20)
            if not r.ok:
                print(
                    "  [WARN] No transcript available (HTTP {}).".format(r.status_code)
                )
                return None

            data = r.json()
            if data.get("status") != "ok":
                print("  [WARN] No transcript available for this lecture.")
                _LOGGER.debug("Transcript API status: %s", data.get("status"))
                return None

            cues = data.get("data", {}).get("contentJSON", {}).get("cues", [])
            if not cues:
                print("  [WARN] Transcript exists but has no caption cues.")
                return None

            return self._save_vtt(output_dir, filename, _cues_to_vtt(cues))
        except Exception as e:
            print("  [WARN] Failed to download subtitles: {}".format(e))
            _LOGGER.debug("Echo360 transcript API failed: %s", e)
            return None

    def _download_vtt_segments(self, session, subtitle_m3u8_url, output_dir, filename):
        """Download and combine VTT subtitle segments from an HLS subtitle playlist."""
        try:
            filename = truncate_filename_for_dir(output_dir, filename, ".vtt")
            r = session.get(subtitle_m3u8_url, timeout=10)
            if not r.ok:
                print(
                    "  [WARN] Failed to fetch subtitle playlist ({}), skipping subtitles.".format(
                        r.status_code
                    )
                )
                return None

            lines = r.content.decode("utf-8", errors="replace").split("\n")
            segment_urls = [
                urljoin(subtitle_m3u8_url, line.strip())
                for line in lines
                if line.strip() and not line.startswith("#")
            ]
            if not segment_urls:
                print("  [WARN] No subtitle segments found in playlist.")
                return None

            combined_vtt_path = os.path.join(output_dir, filename + ".vtt")
            first_segment = True
            write_text_file(combined_vtt_path, "")
            with open(win_extended_path(combined_vtt_path), "a", encoding="utf-8") as outfile:
                for seg_url in segment_urls:
                    seg_r = session.get(seg_url, timeout=10)
                    if not seg_r.ok:
                        continue
                    content = seg_r.content.decode("utf-8", errors="replace")
                    if first_segment:
                        outfile.write(content)
                        first_segment = False
                    else:
                        # Skip the WEBVTT header (and any X-TIMESTAMP-MAP) for
                        # subsequent segments so the file stays valid WebVTT.
                        seg_lines = content.split("\n")
                        start_idx = 0
                        for li, seg_line in enumerate(seg_lines):
                            if seg_line.strip().upper().startswith("WEBVTT"):
                                start_idx = li + 1
                                break
                        # Skip X-TIMESTAMP-MAP and blank lines after the header
                        while start_idx < len(seg_lines) and (
                            seg_lines[start_idx].strip() == ""
                            or seg_lines[start_idx].strip().startswith("X-TIMESTAMP-MAP")
                        ):
                            start_idx += 1
                        outfile.write("\n")
                        outfile.write("\n".join(seg_lines[start_idx:]))

            with open(win_extended_path(combined_vtt_path), "r", encoding="utf-8") as infile:
                normalized = _normalize_vtt_for_playback(infile.read())
            write_text_file(combined_vtt_path, normalized)

            print("  Subtitles saved: {}.vtt".format(filename))
            return combined_vtt_path
        except Exception as e:
            print("  [WARN] Failed to download subtitles: {}".format(e))
            return None

    @staticmethod
    def mux_subtitles_into_mp4(mp4_path, vtt_path):
        """Mux soft subtitles into MP4; keep the separate VTT file."""
        if not os.path.exists(mp4_path) or not os.path.exists(vtt_path):
            return False
        tmp_path = mp4_path + ".tmp.mp4"
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        try:
            ff = ffmpy.FFmpeg(
                global_options="-loglevel panic -y",
                inputs={mp4_path: None, vtt_path: None},
                outputs={
                    tmp_path: [
                        "-map",
                        "0:v:0",
                        "-map",
                        "0:a?",
                        "-map",
                        "1:0",
                        "-c:v",
                        "copy",
                        "-c:a",
                        "copy",
                        "-c:s",
                        "mov_text",
                        "-metadata:s:s:0",
                        "language=eng",
                    ]
                },
            )
            ff.run()
            os.replace(tmp_path, mp4_path)
            print("  Subtitles muxed into {}".format(os.path.basename(mp4_path)))
            return True
        except ffmpy.FFExecutableNotFoundError:
            print(
                '  [WARN] Skipping subtitle mux because "ffmpeg" not installed.'
            )
            return False
        except ffmpy.FFRuntimeError:
            print("  [WARN] Could not mux subtitles into MP4 (ffmpeg failed).")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            return False

    @staticmethod
    def combine_audio_video(audio_file, video_file, final_file):
        if os.path.exists(final_file):
            os.remove(final_file)
        _inputs = {}
        _inputs[video_file] = None
        if audio_file is not None:
            _inputs[audio_file] = None
        try:
            ff = ffmpy.FFmpeg(
                global_options="-loglevel panic",
                inputs=_inputs,
                outputs={final_file: ["-c:v", "copy", "-c:a", "ac3"]},
            )
            ff.run()
        except ffmpy.FFExecutableNotFoundError:
            print(
                '[WARN] Skipping mixing of audio/video because "ffmpeg" not installed.'
            )
            return False
        except ffmpy.FFRuntimeError:
            print(
                "[Error] Skipping mixing of audio/video because ffmpeg exited with non-zero status code."
            )
            return False
        return True

    def _finalize_m3u8_urls(self, m3u8urls, quiet=False):
        """Pick master playlist URL(s); prefer combined av.m3u8 when present."""
        if not m3u8urls:
            if not quiet:
                print(
                    "No m3u8 manifests found! Skipping...\n"
                    "This lecture may not be published yet, or only has audio."
                )
            return False

        av_urls = [url for url in m3u8urls if url.endswith("av.m3u8")]
        if av_urls:
            candidates = av_urls
        else:
            # UNSW / newer Echo360 use master playlists (s1q0.m3u8), not *av.m3u8.
            candidates = [
                url for url in m3u8urls if not _SEGMENT_M3U8_RE.search(url)
            ]
            if not candidates:
                candidates = list(m3u8urls)

        ordered = list(reversed(sorted(candidates)))
        return ordered[:2]

    def _loop_find_m3u8_url(self, video_url, waitsecond=15, max_attempts=5):
        def brute_force_get_url(suffix):
            # this is the first method I tried, which sort of works
            stale_attempt = 1
            refresh_attempt = 1
            while True:
                try:
                    self._driver.get(video_url)
                except InvalidSessionIdException:
                    print(
                        ">> Chrome session ended (window closed?). "
                        "Cannot look up media in browser."
                    )
                    return set()
                try:
                    WebDriverWait(self._driver, waitsecond).until(
                        EC.presence_of_element_located((By.ID, "content-player"))
                    )
                except selenium.common.exceptions.TimeoutException:
                    pass
                try:
                    # the replace is for reversing the escape by the escapped js in the page source
                    urls = set(
                        re.findall(
                            'https://[^,"]*?[.]{}'.format(suffix),
                            self._driver.page_source.replace("\\/", "/"),
                        )
                    )
                    return urls

                except selenium.common.exceptions.TimeoutException:
                    if refresh_attempt >= max_attempts:
                        print(
                            "\r\nERROR: Connection timeouted after {} second for {} attempts... \
                              Possibly internet problem?".format(
                                waitsecond, max_attempts
                            )
                        )
                        raise
                    refresh_attempt += 1
                except StaleElementReferenceException:
                    if stale_attempt >= max_attempts:
                        print(
                            "\r\nERROR: Elements are not stable to retrieve after {} attempts... \
                            Possibly internet problem?".format(
                                max_attempts
                            )
                        )
                        raise
                    stale_attempt += 1

        def brute_force_get_mp4_url():
            """Forcefully try to find all .mp4 url in the page source"""
            urls = brute_force_get_url(suffix="mp4")
            if len(urls) == 0:
                raise Exception("None were found.")
            # in many cases, there would be urls in the format of http://xxx.{hd1,hd2,sd1,sd2}
            # I'm not sure what does the 1 and 2 in hd1,hd2 stands for, but hd and sd should means
            # high or low definition.
            # Some university uses hd1 and hd2 for their alternative feeds, use flag `-a`
            # to download both feeds.
            # Let's prioritise hd over sd, and 1 over 2 (the latter is arbitary)
            # which happens to be the natual order of letter anyway, so we can simply use sorted.
            return sorted(urls)[:2]

        def from_json_m3u8():
            m3u8urls = self._url_from_json_m3u8()
            if m3u8urls is False:
                return False
            return m3u8urls

        def from_json_mp4():
            return self._url_from_json_mp4()

        # try different methods in series, first the preferred ones, then the more
        # obscure ones.
        try:
            _LOGGER.debug("Trying from_json_mp4 method")
            return from_json_mp4()
        except Exception as e:
            _LOGGER.debug("Encountered exception: {}".format(e))
        try:
            _LOGGER.debug("Trying from_json_m3u8 method")
            m3u8urls = from_json_m3u8()
            if m3u8urls:
                return self._finalize_m3u8_urls(m3u8urls)
            if m3u8urls is False:
                return False
        except Exception as e:
            _LOGGER.debug("Encountered exception: {}".format(e))
        try:
            _LOGGER.debug("Trying brute_force_all_mp4 method")
            return brute_force_get_mp4_url()
        except Exception as e:
            _LOGGER.debug("Encountered exception: {}".format(e))
        try:
            _LOGGER.debug("Trying brute_force_all_m3u8 method")
            m3u8urls = brute_force_get_url(suffix="m3u8")
        except Exception as e:
            _LOGGER.debug("Encountered exception: {}".format(e))
            _LOGGER.debug("All methods had been exhausted.")
            print("Tried all methods to retrieve videos but all had failed!")
            if isinstance(e, InvalidSessionIdException):
                print(
                    ">> Chrome session ended. Keep the browser window open during downloads."
                )
            return False

        return self._finalize_m3u8_urls(m3u8urls)

    def _extract_date(self, video_json):
        if self.is_multipart_video:
            if video_json["groupInfo"]["createdAt"] is not None:
                return video_json["groupInfo"]["createdAt"]
            if video_json["groupInfo"]["u'updatedAt'"] is not None:
                return video_json["groupInfo"]["u'updatedAt'"]

        if "startTimeUTC" in video_json["lesson"]:
            if video_json["lesson"]["startTimeUTC"] is not None:
                return video_json["lesson"]["startTimeUTC"]
        if "createdAt" in video_json["lesson"]["lesson"]:
            return video_json["lesson"]["lesson"]["createdAt"]

    def get_all_parts(self):
        return self.sub_videos


class EchoCloudSubVideo(EchoCloudVideo):
    """Some video in echo360 cloud is multi-part and this represents it."""

    def __init__(
        self,
        video_json,
        driver,
        hostname,
        group_name,
        alternative_feeds,
        video_only=False,
        transcript_only=False,
    ):
        super(EchoCloudSubVideo, self).__init__(
            video_json,
            driver,
            hostname,
            alternative_feeds,
            video_only=video_only,
            transcript_only=transcript_only,
        )
        self.group_name = group_name

    @property
    def title(self):
        if type(self._title) != str:
            # it's type unicode for python2
            self._title = self._title.encode("utf-8")
        return "{} - {}".format(self.group_name, self._title)
