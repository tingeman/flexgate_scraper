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
│   ├── requirements.in
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
├── tests/
│   ├── run.sh
│   └── fake_flexgate.py
├── LICENSE
├── .gitattributes
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

## Build

The Dockerfile pins the base image by digest and installs the Python packages from a hash
lock (`pip install --require-hashes`). The image contains only `bash`, `cron` and Python
with the packages from the lock.

```sh
docker build -t flexgate_scraper:local .
```

To change the Python packages, edit `app/requirements.in` and regenerate the lock
`app/requirements.txt` inside the pinned base image, so that the hashes match the image's
Python and platform. From the repository root:

```sh
docker run --rm -v "$PWD/app":/w -w /w python:3.13-slim@sha256:<digest from Dockerfile> \
  sh -c 'pip install pip-tools && pip-compile --generate-hashes -o requirements.txt requirements.in'
```

To update the base image, get the current digest with
`docker buildx imagetools inspect python:3.13-slim`, put it in the `FROM` line and
regenerate the lock as above.

## Tests

`tests/run.sh <image>` runs behaviour tests against a built image with the docker CLI only
(bash on Linux or WSL, or Git Bash on Windows with Docker Desktop):

```sh
docker build -t flexgate_scraper:local .
bash tests/run.sh flexgate_scraper:local
```

It covers the image contents (tools present and absent, `pip check`, Python imports, script
syntax), the entrypoint (cron jobs installed with mode 0644, refusal to start without
settings) and the scraper itself: it is run against a local fake FlexGate server
(`tests/fake_flexgate.py`) on a private `--internal` docker network, and the tests check
where files land, that a second run downloads nothing and that a new file on the server is
picked up. All other containers run with `--network none`, and all containers, the network
and temporary folders are removed at the end. Each check prints `PASS:` or `FAIL:`, and the
run ends with `ALL <n> TESTS PASSED` (exit 0) or `<m> OF <n> TESTS FAILED` (exit 1).

## Scheduling

To run the scraper periodically, use the cron job template in [`config/cron_jobs/cron_scrape_20m`](config/cron_jobs/cron_scrape_20m).

All files in `config/cron_jobs/` will be automatically installed as cron jobs in the container when 
built using docker compose.

## License

This project is licensed under the GNU General Public License v3.0. See [LICENSE](LICENSE) for details.
