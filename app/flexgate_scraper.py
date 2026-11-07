import requests
from bs4 import BeautifulSoup
from pathlib import Path
import logging
import sys
import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import BaseModel, ValidationError, model_validator
from typing import List, Dict, Optional, Union
from urllib.parse import urlparse, urlunparse, urljoin, parse_qs, urlencode, quote

from decoding import DecodingSettings, LoggerSource, logger_folder_name, run_decoding

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

# Function to download a file
def download_file(file_url: str, output_path: Path):
    logging.info(f"Downloading {file_url}...")
    response = requests.get(file_url)
    response.raise_for_status()

    # Save the file
    with output_path.open("wb") as f:
        f.write(response.content)
    
    logging.info(f"Saved to {output_path}")

# Function to get page content and parse links
def get_links_from_page(url: str):
    response = requests.get(url)
    response.raise_for_status()  # Raise an error if the request failed
    soup = BeautifulSoup(response.text, "html.parser")
    return soup.find_all("a")


parsed_url = urlparse(settings.flexgate_browse_url)
if not parsed_url.scheme or not parsed_url.netloc:
    raise ValueError("Invalid base URL provided in settings.")

base_path = '/'.join(parsed_url.path.split('/')[:-1])
if not base_path:
    base_path = "/"
elif not base_path.endswith("/"):
    base_path += "/"

base_url = urlunparse((parsed_url.scheme, parsed_url.netloc, base_path, '', '', ''))


# Iterate over each source in the settings
for source in settings.sources:
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

        # Get links from the serial page
        links = get_links_from_page(logger_url)

        # Download each specific file if found
        for link in links:
            href = link.get("href")
            if href:
                file_name = Path(urlparse(href).path).name  # Extract only the filename
                if file_name in specific_files:
                    file_path = serial_output_folder / file_name
                    if file_path.exists():
                        logging.debug(f"Skipping {file_name}, already downloaded.")
                        continue
                    
                    # combine baseurl wiht href
                    file_url = urljoin(base_url, href)
                    download_file(str(file_url), file_path)

    # Download files from specified folders
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

        # Get a listing of the local directory
        existing_files = set(output_folder.iterdir())

        # Get links from the folder page
        links = get_links_from_page(folder_url)

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
                download_file(str(file_url), file_path)           

logging.info("All files downloaded successfully.")

# Decode step (only with an enabled "decoding" block in the settings)
run_decoding(settings.decoding, [
    LoggerSource(serial=s.serial, logger_name=s.logger_name or "",
                 folder=Path(settings.base_download_folder) / logger_folder_name(s.serial, s.logger_name))
    for s in settings.sources])

