# Flexgate Scraper

A Python-based tool for scraping and downloading data files from the Flexgate server.

## Features

- Scrapes multiple sources from Flexgate using configurable serial numbers and logger names.
- Downloads files with specific extensions or explicitly listed filenames.
- Organizes downloads by source in a structured folder hierarchy.
- Easily configurable via YAML settings.

## Project Structure

```
.
├── app/
│   ├── flexgate_scraper.py
│   ├── requirements.txt
│   └── settings.yml
├── bin/
│   └── decode3
├── config/
│   ├── settings.yml
│   └── cron_jobs/
│       └── cron_scrape_20m
├── docker-compose.yml
├── Dockerfile
├── entrypoint.sh
├── LICENSE
└── .gitignore
```

## Configuration

Edit [`config/settings.yml`](config/settings.yml) to specify:

- `flexgate_browse_url`: URL of the Flexgate server.
- `base_download_folder`: Local folder for downloads.
- `downloadable_extensions`: List of file extensions to download.
- `sources`: List of sources with serial numbers, logger names, folders, and specific files.

Example:
```yaml
# Template for settings.yml file
# Should be overridden with actual values

# URL for the Flexgate server
flexgate_browse_url: "https://flexgate.org/fg2lite/sw/browse.php"

# Downloaded files will be placed here
base_download_folder: "downloads"

# List of file extensions that are considered downloadable
# Only files in the specified source folders that match these extensions will be downloaded
# Specific source files listed in the sources::files section will always be downloaded regardless of their extension
downloadable_extensions:
  - ".f2p"
  - ".f2b"
  - ".txt"
  - ".dat"
  - ".par"

# List of sources to scrape
# Each source must have a unique serial number used to identify it in the flexgate system
# Downloaded files will be placed in the folder ./{base_download_folder}/{serial}_{logger_name}/
sources:
  - serial: "D1022C"
    logger_name: "GIOS2023-02"
    folders: 
      - raw
      - par
    files: 
      - "con_log.txt"
      - "log.txt"
      - "loggerinfo.dat"
      - "service.dat"
      - "state.dat"
```

Docker compose mounts the config file `config/settings.yml` to the `/app` folder in the container,
thus overwriting any `settings.yml` file that may already exist there.

When the scraper is run locally, it reads the config file `app/settings.yml`.


## Usage

### With Docker Compose

Build and run the scraper using Docker Compose:

```sh
docker compose up --build
```

### Directly with Python

1. Install dependencies:
    ```sh
    pip install -r app/requirements.txt
    ```
2. Run the scraper:
    ```sh
    python app/flexgate_scraper.py
    ```

## Scheduling

To run the scraper periodically, use the cron job template in [`config/cron_jobs/cron_scrape_20m`](config/cron_jobs/cron_scrape_20m).

All files in `config/cron_jobs/` will be automatically installed as cron jobs in the container when 
built using docker compose.

## License

This project is licensed under the GNU General Public License v3.0. See [LICENSE](LICENSE) for details.
