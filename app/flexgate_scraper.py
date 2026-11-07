import fcntl
import json
import os
import requests
from bs4 import BeautifulSoup
from pathlib import Path
import logging
import sys
import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import BaseModel, ValidationError, model_validator
from datetime import datetime, timezone
from typing import List, Dict, Optional, Union
from urllib.parse import urlparse, urlunparse, urljoin, parse_qs, urlencode, quote

from decoding import DecodingSettings, LoggerSource, logger_folder_name, publish, run_decoding
from healthcheck import STATUS_VERSION, format_time, status_file_path

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Define a Source model to handle each source
class Source(BaseModel):
    serial: str
    logger_name: Union[str, None] = None
    folders: Union[str, List[str]]
    files: Union[str, List[str], None] = None

    def get_folders_list(self) -> List[str]:
        if isinstance(self.folders, str):
            return [folder.strip() for folder in self.folders.split(",")]
        return self.folders

    def get_files_list(self) -> List[str]:
        if isinstance(self.files, str):
            return [file.strip() for file in self.files.split(",")]
        return self.files or []

# Settings class to read from YAML file
class Settings(BaseSettings):
    flexgate_browse_url: str  # Renamed from base_url
    base_download_folder: str
    sources: List[Source]
    downloadable_extensions: List[str]
    # Optional decode step after the downloads (see decoding.py); absent = download only
    decoding: Optional[DecodingSettings] = None

    model_config = SettingsConfigDict(env_file=".env")

    @model_validator(mode="after")
    def _output_folder_not_a_source_folder(self):
        if self.decoding is not None:
            for source in self.sources:
                if self.decoding.output_folder in source.get_folders_list():
                    raise ValueError(f"decoding.output_folder '{self.decoding.output_folder}' is also "
                                     f"a download folder of source {source.serial}")
        return self


# Never run as root: everything the scraper writes must belong to the scraper user (the
# entrypoint runs as root only to start cron and repair ownership). Also for --check-settings.
SCRAPER_USER = os.environ.get("FLEXGATE_SCRAPER_USER") or "dockeruser"
if os.geteuid() == 0:
    logging.error(f"Refusing to run as root: run the scraper as the scraper user {SCRAPER_USER}, e.g. "
                  f"docker exec -u {SCRAPER_USER} flexgate_scraper python /app/flexgate_scraper.py")
    sys.exit(2)

# Folders and files the scraper creates get mode 0755/0644 whatever umask it was started
# with (also intermediate folders from mkdir(parents=True), which an explicit chmod after
# each mkdir would miss); downloads and published CSVs also set 0644 explicitly.
os.umask(0o022)

# Get the directory of the current script
script_dir = Path(__file__).parent

# Construct the path to the settings.yml file
settings_path = script_dir / "settings.yml"

# Load settings from YAML file and merge them with Pydantic Settings; refuse to run with
# invalid settings. "flexgate_scraper.py --check-settings" only checks them (the entrypoint
# does this before starting cron).
try:
    with open(settings_path, "r") as file:
        yaml_settings = yaml.safe_load(file)
    if not isinstance(yaml_settings, dict):
        raise ValueError("the file does not contain a YAML mapping")
    settings = Settings(**yaml_settings)
except (OSError, yaml.YAMLError, ValueError, ValidationError) as e:
    logging.error(f"Invalid settings in {settings_path}, not running: {e}")
    sys.exit(2)
if "--check-settings" in sys.argv[1:]:
    print(f"Settings in {settings_path} are valid.")
    sys.exit(0)

# Network timeouts of every request in seconds, (connect, read). The read timeout is the
# longest wait for the next data from the server, not for the whole download.
DEFAULT_TIMEOUT = (10.0, 120.0)
# Test-only/advanced override: "<seconds>" for both, or "<connect>,<read>"
TIMEOUT_ENV = "FLEXGATE_SCRAPER_TIMEOUT_SECONDS"
# Only one run at a time (cron starts a run every hour; a slow run may still be going)
LOCK_PATH = "/tmp/flexgate_scraper.lock"
PART_SUFFIX = ".part"
FILE_MODE = 0o644


def request_timeout() -> tuple:
    value = os.environ.get(TIMEOUT_ENV, "").strip()
    if not value:
        return DEFAULT_TIMEOUT
    try:
        parts = [float(v) for v in value.split(",")]
        if len(parts) == 1:
            parts *= 2
        if len(parts) != 2 or min(parts) <= 0:
            raise ValueError
    except ValueError:
        logging.error(f"Invalid {TIMEOUT_ENV}={value!r}, expected <seconds> or <connect>,<read>; not running")
        sys.exit(2)
    return tuple(parts)


def acquire_run_lock():
    """Take the run lock without waiting. Returns its file descriptor, which must stay open
    for the whole run, or None if another run holds the lock."""
    # read-only is enough for flock, and also works on a lock file another user created
    fd = os.open(LOCK_PATH, os.O_RDONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def part_path(output_path: Path) -> Path:
    return output_path.with_name(f".{output_path.name}{PART_SUFFIX}")


def is_part_file(name: str) -> bool:
    return name.startswith(".") and name.endswith(PART_SUFFIX)


def remove_partial_downloads(folder: Path):
    """Remove .<name>.part files left by an interrupted run."""
    for p in folder.iterdir():
        if is_part_file(p.name) and p.is_file():
            logging.info(f"Removing partial download {p}")
            p.unlink()


def fetch(file_url: str, timeout: tuple) -> bytes:
    """Content of file_url; network and HTTP errors are raised."""
    response = requests.get(file_url, timeout=timeout)
    response.raise_for_status()
    return response.content


def write_file(content: bytes, output_path: Path):
    """Write content to .<name>.part in the same folder (mode 0644), then rename it to
    output_path. On any error the .part file is removed and an existing output_path is left
    as it was."""
    tmp = part_path(output_path)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), FILE_MODE)  # independent of the umask
            f.write(content)
        os.replace(tmp, output_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# Download a file that is not there yet (written through .<name>.part, see write_file)
def download_file(file_url: str, output_path: Path, timeout: tuple):
    logging.info(f"Downloading {file_url}...")
    write_file(fetch(file_url, timeout), output_path)
    logging.info(f"Saved to {output_path}")


def refresh_file(file_url: str, output_path: Path, timeout: tuple) -> bool:
    """Fetch a file that FlexGate updates (the top-level files of a source) and replace the
    local copy only when the content differs; an unchanged file is not written at all (its
    mtime stays). Returns True when the file was written. On errors the old file is kept and
    the error is raised."""
    if not output_path.exists():
        download_file(file_url, output_path, timeout)
        return True
    logging.debug(f"Refreshing {file_url}")
    content = fetch(file_url, timeout)
    if output_path.read_bytes() == content:
        logging.debug(f"{output_path} unchanged")
        return False
    write_file(content, output_path)
    logging.info(f"Updated {output_path}")
    return True


# Function to get page content and parse links
def get_links_from_page(url: str, timeout: tuple):
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()  # Raise an error if the request failed
    soup = BeautifulSoup(response.text, "html.parser")
    return soup.find_all("a")


def get_base_url(browse_url: str) -> str:
    parsed_url = urlparse(browse_url)
    if not parsed_url.scheme or not parsed_url.netloc:
        raise ValueError("Invalid base URL provided in settings.")

    base_path = '/'.join(parsed_url.path.split('/')[:-1])
    if not base_path:
        base_path = "/"
    elif not base_path.endswith("/"):
        base_path += "/"

    return urlunparse((parsed_url.scheme, parsed_url.netloc, base_path, '', '', ''))


def scrape_source(source: Source, base_url: str, timeout: tuple) -> int:
    """Download what is new for one source (new folder files, new or changed top-level
    files) and return the number of files written. Network, HTTP and file system errors are raised to the caller."""
    downloaded = 0
    serial = source.serial
    logger_name = source.logger_name or ""
    folders = source.get_folders_list()
    specific_files = source.get_files_list()

    logger_url = f"{settings.flexgate_browse_url}?dir=../data/{serial}"
    output_folder_name = logger_folder_name(serial, logger_name)
    base_output_folder = Path(f"{settings.base_download_folder}/{output_folder_name}")
    base_output_folder.mkdir(parents=True, exist_ok=True)

    # Download specific files from the main serial page if specified
    if specific_files:
        serial_output_folder = base_output_folder / ""
        serial_output_folder.mkdir(parents=True, exist_ok=True)
        remove_partial_downloads(serial_output_folder)

        # Get links from the serial page
        links = get_links_from_page(logger_url, timeout)

        # Fetch each specific file found on every run (FlexGate updates them); written only
        # when new or changed
        for link in links:
            href = link.get("href")
            if href:
                file_name = Path(urlparse(href).path).name  # Extract only the filename
                if file_name in specific_files:
                    file_path = serial_output_folder / file_name
                    # combine baseurl wiht href
                    file_url = urljoin(base_url, href)
                    if refresh_file(str(file_url), file_path, timeout):
                        downloaded += 1

    # Download files from specified folders (once: a file already here is not fetched again)
    for folder in folders:
        # Create URL to get the contentlisting of the folder
        parsed_url = urlparse(settings.flexgate_browse_url)
        query_params = parse_qs(parsed_url.query)
        query_params["dir"] = [quote(f"../data/{serial}/{folder}", safe="/")]
        new_query = urlencode(query_params, doseq=True)
        folder_url = urlunparse(parsed_url._replace(query=new_query))

        # Create the destination folder
        output_folder = base_output_folder / folder
        output_folder.mkdir(parents=True, exist_ok=True)
        remove_partial_downloads(output_folder)

        # Get a listing of the local directory (a partial download never counts)
        existing_files = {p for p in output_folder.iterdir() if not is_part_file(p.name)}

        # Get links from the folder page
        links = get_links_from_page(folder_url, timeout)

        # Download each file
        for link in links:
            href = link.get("href")
            if href and any(href.endswith(ext) for ext in settings.downloadable_extensions):  # Check for file extensions
                file_name = Path(urlparse(href).path).name  # Extract only the filename

                file_path = output_folder / file_name
                if file_path in existing_files:
                    logging.debug(f"Skipping {file_name}, already downloaded.")
                    continue

                # combine baseurl wiht href
                file_url = urljoin(base_url, href)
                download_file(str(file_url), file_path, timeout)
                downloaded += 1

    return downloaded


def write_status(status: dict):
    """Write the status file of this run atomically (mode 0644); healthcheck.py reads it.
    A failure is logged and does not change the exit code."""
    path = Path(status_file_path())
    try:
        publish(json.dumps(status, indent=2) + "\n", path)
    except OSError as e:
        logging.error(f"Could not write the status file {path}: {e}")


def main() -> int:
    """One run: downloads for every source, then the decode step. Returns the exit code:
    1 if any source failed, else 0 (decoding problems do not change it). Every run that takes
    the run lock writes the status file at its end, also when it fails unexpectedly."""
    timeout = request_timeout()
    base_url = get_base_url(settings.flexgate_browse_url)

    try:
        lock_fd = acquire_run_lock()
    except OSError as e:
        logging.error(f"Cannot open the run lock {LOCK_PATH}, not running: {e}")
        return 1
    if lock_fd is None:
        logging.warning(f"Lock {LOCK_PATH} is held: another run is in progress, skipping this run.")
        return 0

    started = datetime.now(timezone.utc)
    ok, failed, downloaded = 0, [], 0
    totals = None
    error = None
    try:
        # Iterate over each source in the settings; a failing source does not stop the others
        for source in settings.sources:
            name = logger_folder_name(source.serial, source.logger_name)
            try:
                downloaded += scrape_source(source, base_url, timeout)
                ok += 1
            except (requests.RequestException, OSError) as e:
                logging.error(f"Source {name} failed, continuing with the next source: {type(e).__name__}: {e}")
                failed.append(name)

        if failed:
            logging.warning(f"Downloads incomplete: {len(failed)} source(s) failed: {', '.join(failed)}")
        else:
            logging.info("All files downloaded successfully.")

        # Decode step (only with an enabled "decoding" block in the settings), for all sources
        totals = run_decoding(settings.decoding, [
            LoggerSource(serial=s.serial, logger_name=s.logger_name or "",
                         folder=Path(settings.base_download_folder) / logger_folder_name(s.serial, s.logger_name))
            for s in settings.sources])

        logging.info(f"Run finished: {ok} source(s) ok, {len(failed)} failed, {downloaded} file(s) downloaded.")
        return 1 if failed else 0
    except BaseException as e:
        error = f"{type(e).__name__}: {e}"
        raise
    finally:
        decoded = totals.decoded if totals else 0
        decode_failed = totals.failed if totals else 0
        decode_error = totals.error if totals else None
        write_status({
            "version": STATUS_VERSION,
            "started": format_time(started),
            "finished": format_time(datetime.now(timezone.utc)),
            "ok": not failed and not decode_failed and decode_error is None and error is None,
            "sources_ok": ok,
            "sources_failed": failed,
            "files_downloaded": downloaded,
            "files_decoded": decoded,
            "decode_failed": decode_failed,
            "decode_error": decode_error,
            "error": error,
        })


if __name__ == "__main__":
    sys.exit(main())
