import ffmpy
import gevent
from gevent.pool import Pool
import requests
import os, sys
import time

from .echo_exceptions import HlsDownloaderError
from .interrupt import register_downloader, unregister_downloader
from .utils import win_extended_path


def urljoin(a, b):
    # get url relative root path
    a = a[: a.rfind("/") + 1]
    # remove slashes at beginning if needed
    while b[0] == "/":
        b = b[1:]
    return a + b


# update_progress() : Displays or updates a console progress bar
## Accepts a float between 0 and 1. Any int will be converted to a float.
## A value under 0 represents a 'halt'.
## A value at 1 or bigger represents 100%
def update_progress(current, total, title=None, complete=False):
    if title is None:
        title = "Progress"
    barLength = 20  # Modify this to change the length of the progress bar
    status = " {}/{}".format(current, total)
    progress = float(current) / float(total)
    if progress < 0:
        progress = 0
        status = "Halt...\r\n"
    if progress >= 1:
        progress = 1
    if complete:
        status += " Done!\r\n"
    block = "=" * int(round(barLength * progress))
    if len(block) < barLength:
        block += ">"
    text = "\r{0}: [{1}] {2:.2f}% {3}".format(
        title, block + " " * (barLength - len(block)), progress * 100, status
    )
    sys.stdout.write(text)
    sys.stdout.flush()


def stream_response_to_file(response, dest_path, title="  > Downloading", abort_check=None):
    """Stream a requests response to disk with a single-line progress indicator."""
    total_size = int(response.headers.get("content-length", 0))
    block_size = 1024 * 64
    downloaded = 0
    dest_path = win_extended_path(dest_path)
    with open(dest_path, "wb") as f:
        for chunk in response.iter_content(block_size):
            if abort_check and abort_check():
                raise KeyboardInterrupt()
            if not chunk:
                continue
            f.write(chunk)
            downloaded += len(chunk)
            if total_size and downloaded < total_size:
                update_progress(downloaded, total_size, title=title)
    if total_size:
        update_progress(total_size, total_size, title=title, complete=True)
    print("")
    return dest_path


class Downloader:
    def __init__(self, pool_size, retry=3, selenium_cookies=None):
        self.pool = Pool(pool_size)
        self.session = self._get_http_session(
            pool_size, pool_size, retry, selenium_cookies
        )
        self.retry = retry
        self.dir = ""
        self.succed = {}
        self.failed = []
        self.ts_total = 0
        self._result_file_name = None
        self._abort = False

    def _abort_download(self):
        self._abort = True
        try:
            self.pool.kill()
        except Exception:
            pass

    def _get_http_session(
        self, pool_connections, pool_maxsize, max_retries, selenium_cookies=None
    ):
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=pool_connections,
            pool_maxsize=pool_maxsize,
            max_retries=max_retries,
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        if selenium_cookies is not None:
            # load cookies
            for cookie in selenium_cookies:
                session.cookies.set(cookie["name"], cookie["value"])
        return session

    def run(self, m3u8_url, dir="", convert_to_mp4=True):
        register_downloader(self)
        try:
            self._run(m3u8_url, dir=dir, convert_to_mp4=convert_to_mp4)
        except KeyboardInterrupt:
            self._abort_download()
            raise
        finally:
            unregister_downloader(self)

    def _run(self, m3u8_url, dir="", convert_to_mp4=True):
        self.dir = dir
        if self.dir and not os.path.isdir(win_extended_path(self.dir)):
            os.makedirs(win_extended_path(self.dir), exist_ok=True)
        r = self.session.get(m3u8_url, timeout=10)
        if r.ok:
            body = r.content
            if body:
                # use set to prevent duplicates
                ts_list = {
                    urljoin(m3u8_url, n.strip())
                    for n in body.decode().split("\n")
                    if n and not n.startswith("#")
                }
                ts_list = list(ts_list)
                # this is very hacky as well.. But idk how to overcome some m3u8 has nested
                # m3u8 and some don't.
                if len(ts_list) == 1 and ts_list[0].split(".")[-1] not in (
                    "ts",
                    "mp4",
                    "m4s",
                ):
                    file_name = ts_list[0].split("/")[-1].split("?")[0]
                    chunk_list_url = "{0}/{1}".format(
                        m3u8_url[: m3u8_url.rfind("/")], file_name
                    )
                    r = self.session.get(chunk_list_url, timeout=20)
                    if r.ok:
                        body = r.content
                        ts_list = [
                            urljoin(m3u8_url, n.strip())
                            for n in body.decode().split("\n")
                            if n and not n.startswith("#")
                        ]
                    # re-retrieve to get all ts file list

                ts_list = zip(ts_list, [n for n in range(len(ts_list))])
                ts_list = list(ts_list)

                if ts_list:
                    self.ts_total = len(ts_list)
                    self.ts_current = 0
                    g1 = gevent.spawn(self._join_file)
                    try:
                        self._download(ts_list)
                        g1.join()
                    except KeyboardInterrupt:
                        g1.kill()
                        self._abort_download()
                        raise
        else:
            print("Failed status code: {}".format(r.status_code))
        infile_name = win_extended_path(
            os.path.join(
                self.dir,
                self._result_file_name.split(".")[0]
                + "_all."
                + self.result_file_name.split(".")[-1],
            )
        )
        self._result_file_name = infile_name
        if convert_to_mp4:
            outfile_name = infile_name.split(".")[0] + ".mp4"
            sys.stdout.write("  > Converting to mp4... ")
            sys.stdout.flush()
            try:
                ff = ffmpy.FFmpeg(
                    global_options="-loglevel panic",
                    inputs={infile_name: None},
                    outputs={outfile_name: ["-c", "copy"]},
                )
                ff.run()
                # delete source file after done
                os.remove(infile_name)
                self._result_file_name = outfile_name
                print("Done!")
            except ffmpy.FFExecutableNotFoundError:
                print('Skipping! Because "ffmpeg" not installed.')
                self._result_file_name = infile_name
            except ffmpy.FFRuntimeError:
                print("Error! ffmpeg exited with non-zero status code.")
                self._result_file_name = infile_name

    def _download(self, ts_list):
        try:
            if len(ts_list) == 1:
                self._worker_single(ts_list[0])
            else:
                self.pool.map(self._worker, ts_list)
            if self.failed and not self._abort:
                ts_list = self.failed
                self.failed = []
                self._download(ts_list)
        except KeyboardInterrupt:
            self._abort_download()
            raise

    def _worker_single(self, ts_tuple):
        url = ts_tuple[0]
        index = ts_tuple[1]
        retry = self.retry
        while retry:
            if self._abort:
                return
            try:
                r = self.session.get(url, stream=True, timeout=20)
                total_size = int(r.headers.get("content-length", 0))
                block_size = 1024  # 1 kilobyte
                file_name = url.split("/")[-1].split("?")[0]
                result_full_path = win_extended_path(os.path.join(self.dir, file_name))
                downloaded = 0
                with open(result_full_path, "wb") as f:
                    for data in r.iter_content(block_size):
                        if self._abort:
                            return
                        if not data:
                            continue
                        f.write(data)
                        downloaded += len(data)
                        if total_size and downloaded < total_size:
                            update_progress(
                                downloaded,
                                total_size,
                                title="  > Downloading",
                            )
                if total_size:
                    update_progress(
                        total_size,
                        total_size,
                        title="  > Downloading",
                        complete=True,
                    )
                self.succed[index] = file_name
                self.ts_current += 1
                print("")
                return
            except KeyboardInterrupt:
                self._abort_download()
                raise
            except EnvironmentError as e:
                print("\r\nError in writing file: {}".format(e))
                raise HlsDownloaderError
            except Exception:
                retry -= 1
        sys.stdout.write("[FAIL]")
        self.failed.append((url, index))

    def _worker(self, ts_tuple):
        url = ts_tuple[0]
        index = ts_tuple[1]
        retry = self.retry
        while retry:
            if self._abort:
                return
            try:
                r = self.session.get(url, timeout=20)
                if r.ok:
                    file_name = url.split("/")[-1].split("?")[0]
                    chunk_path = win_extended_path(os.path.join(self.dir, file_name))
                    with open(chunk_path, "wb") as f:
                        f.write(r.content)
                    self.succed[index] = file_name
                    self.ts_current += 1
                    update_progress(
                        self.ts_current,
                        self.ts_total,
                        title="  > {}".format("Progress"),
                    )
                    return
            except KeyboardInterrupt:
                self._abort_download()
                raise
            except EnvironmentError as e:
                print("\r\nError in writing file: {}".format(e))
                raise HlsDownloaderError
            except Exception:
                retry -= 1
        sys.stdout.write("[FAIL]")
        self.failed.append((url, index))

    def _join_file(self):
        index = 0
        outfile = ""
        while index < self.ts_total and not self._abort:
            file_name = self.succed.get(index, "")
            if file_name:
                if self._result_file_name is None:
                    self._result_file_name = file_name
                chunk_path = win_extended_path(os.path.join(self.dir, file_name))
                infile = open(chunk_path, "rb")
                if not outfile:
                    outfile_path = win_extended_path(
                        os.path.join(
                            self.dir,
                            file_name.split(".")[0]
                            + "_all."
                            + file_name.split(".")[-1],
                        )
                    )
                    outfile = open(outfile_path, "wb")
                outfile.write(infile.read())
                infile.close()
                os.remove(chunk_path)
                index += 1
            else:
                time.sleep(1)
        if outfile:
            outfile.close()

    @property
    def result_file_name(self):
        return self._result_file_name
