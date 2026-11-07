# Flexgate Scraper

A Python-based tool for scraping and downloading data files from the Flexgate server.

## Features

- Scrapes multiple sources from Flexgate using configurable serial numbers and logger names.
- Downloads files with specific extensions or explicitly listed filenames.
- Organizes downloads by source in a structured folder hierarchy.
- Optionally decodes downloaded GeoPrecision data files (`.f2b`) to CSV files, with
  GeoPrecision's decoder program (not included; see
  [Decoding GeoPrecision data files](#decoding-geoprecision-data-files)).
- Easily configurable via YAML settings.

## Project Structure

```
.
├── app/
│   ├── decoding.py
│   ├── flexgate_scraper.py
│   ├── requirements.in
│   ├── requirements.txt
│   └── settings.yml
├── config/
│   ├── settings.yml
│   └── cron_jobs/
│       └── cron_scrape_20m
├── docker-compose.yml
├── Dockerfile
├── as-dockeruser.sh       # privilege drop to the scraper user (cron jobs, entrypoint)
├── entrypoint.sh
├── tests/
│   ├── run.sh
│   ├── fake_flexgate.py
│   └── stub_decode3.py
├── LICENSE
├── .gitattributes
└── .gitignore
```

`bin/` is ignored by git: it is the place for local vendor files, such as the decoder program,
which are never committed.

## Configuration

Edit [`config/settings.yml`](config/settings.yml) to specify:

- `flexgate_browse_url`: URL of the Flexgate server.
- `base_download_folder`: Local folder for downloads.
- `downloadable_extensions`: List of file extensions to download.
- `sources`: List of sources with serial numbers, logger names, folders, and specific files.
- `decoding` (optional): decode downloaded data files to CSV, see
  [Decoding GeoPrecision data files](#decoding-geoprecision-data-files).

Settings are checked when the container starts and at every run: unknown keys and invalid
values stop the scraper with a message naming the setting.

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


## Decoding GeoPrecision data files

Decoding is optional. Without a `decoding` block in the settings (or with `enabled: false`)
the scraper only downloads files.

Decoding needs GeoPrecision's decoder program `decode3` (Linux build). It is proprietary
software: it is **not included** in this project and must **never be committed** to this or
any other repository. **Contact GeoPrecision to obtain it.**

### Building an image with the decoder

Build the scraper image as usual, then add the decoder in a thin layer on top. Put the
decoder file in a folder of its own (for example the git-ignored `bin/` of this repository,
or any folder outside the repository) together with this `Dockerfile`:

```dockerfile
FROM flexgate_scraper:local
COPY decode3 /opt/geoprecision/decode3
RUN chmod 0755 /opt/geoprecision/decode3
```

and build with that folder as the build context, so only the decoder is sent to docker:

```sh
docker build -t flexgate_scraper:local .
docker build -t flexgate_scraper:local-decoder bin/
```

`/opt/geoprecision/decode3` is the default `decoder_path`; any other path works if the
settings name it. An image with the decoder contains proprietary software: keep it in
private registries or local image stores only, never push it to a public registry.

### Settings

```yaml
decoding:
  enabled: true                            # false: download only, as without this block
  decoder_path: /opt/geoprecision/decode3  # the decoder program inside the container
  output_folder: decoded                   # subfolder of each logger folder for the CSV files
  timeout_seconds: 60                      # per decoder call; a timeout counts as a failure
  postprocessing:                          # optional; absent or enabled: false = publish
    enabled: true                          #   the decoder's CSV unchanged
    comment_char: "%"
    steps:
      - comment_metadata
```

- `enabled` (default `true`): `false` behaves exactly like an absent `decoding` block.
- `decoder_path` (default `/opt/geoprecision/decode3`): path of the decoder in the container.
- `output_folder` (default `decoded`): a plain folder name, created in each logger folder. It
  must not be `raw`, `par` or one of the source's download folders.
- `timeout_seconds` (default `60`): the longest a single decoder call may take.
- `postprocessing.enabled` (default `true`), `postprocessing.comment_char` (default `%`,
  exactly one non-whitespace character) and `postprocessing.steps` (list of step names, run
  in list order; see below).

Unknown keys and unknown step names are rejected when the settings are loaded.

### Output

After the downloads of each run, every `raw/<stem>.f2b` of a configured source that has no
CSV yet is decoded and published as

```
<base_download_folder>/<serial>_<logger_name>/<output_folder>/<stem>.csv
```

(`<serial>/` without a logger name), with the base name of the raw file:
`raw/D1022D_20261030020139.f2b` becomes `decoded/D1022D_20261030020139.csv`. A file is decoded
once; to decode it again, delete its CSV.

The decoder never runs on the downloaded files. Each run uses a temporary work folder in the
container with its own decoder INI; the inputs are copied there and the folder is removed at
the end of the run. A data file names the parameter set it was recorded with, so all
`par/*.f2p` of the logger are decoded first, then the pending `.f2b` files. Nothing is
written into the download folders except the output folder. A CSV is written as a hidden
temporary file in the output folder first and then renamed (mode 0644), so tools that copy
the folder never see a partial file.

### Post-processing steps

Each step gets the whole decoded file (all lines, with their line endings) and returns the
whole new content, so a step can change, add, remove or reorder lines. Steps run in the order
listed. A step is a function plus a name in the `STEPS` registry in `app/decoding.py`.

- `comment_metadata`: puts `comment_char` in front of every line starting with `<` (the
  decoder's metadata and messages, which also appear between data rows and at the end) and in
  front of every repeated column header line (starting with `NO;TIME` or `NO,TIME`) after the
  first one. All other lines stay byte for byte the same (values, delimiter, decimal comma,
  line endings); no lines are added or removed. The result is a CSV that tools such as
  `pandas.read_csv(..., sep=";", decimal=",", comment="%")` can read directly. Choose a
  `comment_char` that does not occur at the start of a data line or the header.

### Failures and retries

A data file is decoded successfully only if the decoder exits with 0, writes the CSV and the
CSV has no line starting with `<ERROR:` (for example when the parameter set is missing).
Anything else, a timeout or a failing post-processing step is logged as an ERROR naming the
file and the reason; nothing is published for that file and it is tried again in the next
run. A missing or non-executable decoder gives one ERROR per run; the downloads still happen.
A decoding failure never stops the downloads or the decoding of other files and loggers. Each
logger with files to decode gets an INFO line with the number of decoded and failed files.

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
2. Run the scraper (as a normal user; it refuses to run as root):
    ```sh
    python app/flexgate_scraper.py
    ```

### What a run does

- **Run lock**: a run first takes a lock on `/tmp/flexgate_scraper.lock` (without waiting).
  If another run still holds it, the run logs a WARNING "another run is in progress,
  skipping this run" and exits with 0. `--check-settings` does not take the lock. The
  entrypoint removes a lock file left by an earlier start of the container.
- **Timeouts**: every request to the server has a connect timeout of 10 s and a read
  timeout of 120 s (the longest wait for the next data, not for the whole download).
  Test-only/advanced: the environment variable `FLEXGATE_SCRAPER_TIMEOUT_SECONDS` overrides
  them, as `<seconds>` for both or `<connect>,<read>`; an invalid value stops the run with
  exit code 2.
- **One source at a time**: a source (logger) whose listing or download fails (network
  error, timeout, HTTP error, file system error) is logged as an ERROR naming the source and
  the reason, and the run continues with the next source. The decode step still runs for
  all sources afterwards. "All files downloaded successfully." is logged only when no source
  failed; otherwise a WARNING lists the failed sources. The run ends with a summary line
  (`Run finished: <n> source(s) ok, <m> failed, <k> file(s) downloaded.`).
- **Partial downloads**: a file is written as `.<name>.part` in its folder and renamed to
  its final name only when it is complete, so a file with its final name is always whole.
  Files get mode 0644 whatever the umask. Leftover `.part` files from an interrupted run
  are removed (logged at INFO) when the folder is processed next, and the file is
  downloaded again.
- **Exit codes**: 0 when all sources were processed (or the run was skipped because of the
  lock), 1 when any source failed, 2 for invalid settings or a start as root. Decoding
  failures do not change the exit code.

## User and file ownership

- **The scraper user** is `dockeruser`, uid = gid 30000, as in the other images of the
  data server (build arguments `USERNAME`, `USER_UID`, `USER_GID`; the name is also in the
  environment variable `FLEXGATE_SCRAPER_USER`). It has no password and there is no sudo.
- **Root is refused**: the scraper (also `--check-settings`) exits with code 2 and an ERROR
  when started as root, so every file it writes belongs to the scraper user. Folders it
  creates get mode 0755 and files 0644, whatever the umask.
- **Cron stays root**: the container's main process (the entrypoint, then `cron -f`) runs as
  root, because cron must start the jobs.
- **Cron job lines** keep the user field `root` and send the output to the container log
  with `> /proc/1/fd/1 2>&1`, the same cron logging as backup_service (Docker rotates the
  container log). The root shell opens `/proc/1/fd/1`; a job running as `dockeruser` could
  not (PID 1 is cron, owned by root: "Permission denied"). Only the command drops to the
  scraper user, through the image's one privilege drop `/usr/local/bin/as-dockeruser`
  (`setpriv --reuid=dockeruser --regid=dockeruser --init-groups <command...>`, run in `/app`
  with `HOME=/home/dockeruser`, because a root cron job starts in `/root`, which the user
  cannot read), which the entrypoint also uses for `--check-settings`:

  ```
  20 * * * * root /usr/local/bin/as-dockeruser /usr/local/bin/python /app/flexgate_scraper.py > /proc/1/fd/1 2>&1
  ```

  A line without `as-dockeruser` runs the scraper as root, which it refuses: the ERROR
  appears in the container log at every run.
- **Ownership repair at start**: the entrypoint (as root) hands everything in
  `/app/downloads` that does not belong to `dockeruser:dockeruser` to that user and logs one
  line with the number of entries changed, e.g. files written by earlier versions that ran
  as root. When everything already belongs to the user, it logs nothing. A new, empty volume
  mounted at `/app/downloads` belongs to the user from the start (the image's folder does).
- **Settings** must be readable by `dockeruser`; the entrypoint checks them as that user and
  refuses to start otherwise.
- **Manual runs** go through `docker exec` as the scraper user:

  ```sh
  docker exec -u dockeruser flexgate_scraper python /app/flexgate_scraper.py
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
picked up. Run robustness is tested with the fake server's control files (`.fake_status`
answers a listing with an HTTP error, `.fake_delay` stalls it; see `tests/fake_flexgate.py`):
a failing or stalled logger does not stop the next one and gives exit code 1, leftover
`.part` files are removed and downloaded again, downloaded files have mode 0644, and a run
started while the lock is held exits 0 without downloading. The decode step is tested with a stub
decoder (`tests/stub_decode3.py`, which mimics the real decoder's file handling) installed in
a docker volume: decode once, parameter sets first, missing parameter sets, failures and
timeouts, post-processing, file modes, nothing written next to the downloaded files, and
rejected settings. Scraper runs happen as `dockeruser`; the user tests check the user and
its folders in the image, that a run as root is refused, owners and modes under umask 077,
the ownership repair at the entrypoint start (once, with the count), and that a cron job line
in the production form (`root`, `as-dockeruser`, `> /proc/1/fd/1`) runs as uid 30000 with its
output in the container log, while a line without `as-dockeruser` logs the root refusal
(this waits for the next cron minute). All other containers run with `--network none`, and all containers, the
network, the volumes and temporary folders are removed at the end. Each check prints `PASS:` or `FAIL:`, and the
run ends with `ALL <n> TESTS PASSED` (exit 0) or `<m> OF <n> TESTS FAILED` (exit 1).

## Scheduling

To run the scraper periodically, use the cron job template in [`config/cron_jobs/cron_scrape_20m`](config/cron_jobs/cron_scrape_20m).

All files in `config/cron_jobs/` will be automatically installed as cron jobs in the container when 
built using docker compose. A job line has the user field `root`, runs the scraper through
`as-dockeruser` and sends its output to the container log (see
[User and file ownership](#user-and-file-ownership)):

```
20 * * * * root /usr/local/bin/as-dockeruser /usr/local/bin/python /app/flexgate_scraper.py > /proc/1/fd/1 2>&1
```

## License

This project is licensed under the GNU General Public License v3.0. See [LICENSE](LICENSE) for details.
