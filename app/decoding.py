"""Optional decode step: turn downloaded GeoPrecision data files (.f2b) into CSV files.

The decoder program (GeoPrecision's ``decode3``, Linux build) is not part of this project; see
README.md, "Decoding GeoPrecision data files". When the ``decoding`` block in settings.yml is
absent or disabled, nothing in this module runs.

Per run and per configured source (logger folder ``<base>/<serial>_<logger_name>/`` or
``<base>/<serial>/``):

* pending = every ``raw/*.f2b`` without ``<output_folder>/<stem>.csv``; with none pending the
  decoder is not called for that logger;
* the decoder never runs on files in the download folders: inputs are copied into a temporary
  work folder (own INI, ``-k<work>/decode3.ini``) that is removed at the end of the run;
* all ``par/*.f2p`` of the logger are decoded first (the decoder looks for the parameter set of
  a data file in its own data folder), then each pending ``.f2b``;
* a result is good when the exit code is 0, the CSV exists in the decoder's data folder and it
  has no line starting with ``<ERROR:``; anything else is logged as an ERROR and retried next run,
  except a missing parameter set (see below);
* a data file whose only problem is the decoder's ``<ERROR: PARAMETER SET NOT FOUND`` waits:
  while it is younger than ``missing_parameter_set_days`` (mtime of ``raw/<stem>.f2b``) it is
  retried every run with a WARNING; after that it is listed in
  ``<output_folder>/skipped.txt`` and no longer retried, until a ``par/*.f2p`` newer than its
  skip time arrives or its line is deleted. Neither counts as a failure;
* optional post-processing steps (registry ``STEPS``) get the whole file and return the whole
  new content;
* the result is published atomically as ``<logger folder>/<output_folder>/<stem>.csv``
  (mode 0644). Nothing else is written in the download folders (``skipped.txt`` is in the
  output folder too).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

from healthcheck import format_time, parse_time

log = logging.getLogger("flexgate_scraper.decoding")

# Lines are handled as latin-1 text: every byte maps to one character, so a file is written
# back byte for byte wherever a step leaves a line unchanged.
ENCODING = "latin-1"


# ---- post-processing steps ------------------------------------------------------------

@dataclass(frozen=True)
class StepContext:
    """What a post-processing step knows about the file it works on."""
    raw_file: str        # name of the raw data file, e.g. "D1022D_20261030020139.f2b"
    serial: str
    logger_name: str     # "" when the source has no logger_name
    settings: "PostprocessingSettings"


# A step gets the whole file as a list of lines (each with its line ending) and returns the
# whole new content. It may change, add, remove or reorder lines.
Step = Callable[[List[str], StepContext], List[str]]

HEADER_PREFIXES = ("NO;TIME", "NO,TIME")


def comment_metadata(lines: List[str], ctx: StepContext) -> List[str]:
    """Prefix comment_char to every line starting with "<" and to every repeated column header
    line (starting with "NO;TIME" or "NO,TIME") after the first one. All other lines are
    unchanged; nothing is added or removed."""
    c = ctx.settings.comment_char
    out = []
    header_seen = False
    for line in lines:
        if line.startswith("<"):
            out.append(c + line)
        elif line.startswith(HEADER_PREFIXES):
            out.append(c + line if header_seen else line)
            header_seen = True
        else:
            out.append(line)
    return out


# Registry of post-processing steps: name in settings.yml -> function
STEPS: Dict[str, Step] = {
    "comment_metadata": comment_metadata,
}


# ---- settings -------------------------------------------------------------------------

class PostprocessingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    comment_char: str = "%"
    steps: List[str] = Field(default_factory=list)

    @field_validator("comment_char")
    @classmethod
    def _one_char(cls, v: str) -> str:
        if len(v) != 1 or v.isspace():
            raise ValueError(f"comment_char must be exactly one non-whitespace character, got {v!r}")
        return v

    @field_validator("steps")
    @classmethod
    def _known_steps(cls, v: List[str]) -> List[str]:
        unknown = [s for s in v if s not in STEPS]
        if unknown:
            raise ValueError(f"unknown post-processing step(s) {unknown}; known steps: {sorted(STEPS)}")
        return v


class DecodingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    decoder_path: str = "/opt/geoprecision/decode3"
    output_folder: str = "decoded"
    timeout_seconds: float = Field(default=60, gt=0)
    # Days a data file whose parameter set is missing is retried before it is skipped
    # (0: skipped on the first failure). Strict: a whole number, not a string or a bool.
    missing_parameter_set_days: int = Field(default=7, ge=0, strict=True)
    postprocessing: Optional[PostprocessingSettings] = None

    @field_validator("output_folder")
    @classmethod
    def _plain_folder_name(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", v) or v in ("raw", "par"):
            raise ValueError(
                f"output_folder must be a plain folder name ([A-Za-z0-9._-], not 'raw' or 'par'), got {v!r}")
        return v


# ---- decoding -------------------------------------------------------------------------

def logger_folder_name(serial: str, logger_name: Optional[str]) -> str:
    """Name of a source's download folder: <serial>_<logger_name>, or <serial>."""
    return f"{serial}_{logger_name}" if logger_name else serial


@dataclass(frozen=True)
class LoggerSource:
    serial: str
    logger_name: str
    folder: Path         # the logger's download folder


class Decoder:
    """Runs the decoder in one work folder (removed by close())."""

    def __init__(self, cfg: DecodingSettings):
        self.cfg = cfg
        self.work = Path(tempfile.mkdtemp(prefix="flexgate_decode_"))
        self.inbox = self.work / "in"
        self.inbox.mkdir()
        self.ini = self.work / "decode3.ini"
        self.data = self.work / "data"   # data_path the decoder writes into its new INI

    def close(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)

    def run(self, src: Path) -> Tuple[Optional[int], str]:
        """Copy src into the work folder and decode it. Returns (exit code or None on timeout,
        decoder output)."""
        infile = self.inbox / src.name
        shutil.copyfile(src, infile)
        cmd = [self.cfg.decoder_path, str(infile), f"-k{self.ini}"]
        try:
            res = subprocess.run(cmd, cwd=self.work, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, timeout=self.cfg.timeout_seconds)
        except subprocess.TimeoutExpired as e:
            return None, (e.output or b"").decode(ENCODING)
        out = res.stdout.decode(ENCODING)
        log.debug("decoder %s: exit %s: %s", src.name, res.returncode, out.strip())
        return res.returncode, out


def split_lines(data: bytes) -> List[str]:
    """Lines with their line endings (only \\n, \\r\\n and \\r end a line)."""
    return [line.decode(ENCODING) for line in data.splitlines(keepends=True)]


def postprocess(lines: List[str], ctx: StepContext) -> List[str]:
    for name in ctx.settings.steps:
        lines = STEPS[name](lines, ctx)
        if not isinstance(lines, list):
            raise TypeError(f"post-processing step {name} returned {type(lines).__name__}, not a list of lines")
    return lines


def publish(content: str, target: Path) -> None:
    """Write content to target atomically (temporary file in the same folder, mode 0644)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.tmp"
    try:
        with tmp.open("wb") as f:
            f.write(content.encode(ENCODING))
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ---- missing parameter sets: waiting and skipped data files ----------------------------

# Lists the data files of a logger that are no longer retried because their parameter set is
# missing; in the output folder, so it is synced with the CSV files.
SKIPPED_FILE = "skipped.txt"
SKIPPED_HEADER = (
    "# Data files (raw/*.f2b) of this logger that are not decoded because their parameter set\n"
    "# (par/*.f2p) is missing: the decoder reported PARAMETER SET NOT FOUND for longer than\n"
    "# decoding.missing_parameter_set_days after the download. The flexgate scraper writes this\n"
    "# file and no longer retries the files listed here, except:\n"
    "# - put the missing .f2p (named in the error) into the logger's par/ folder on the host:\n"
    "#   the file is retried in the next run (any file modification time);\n"
    "# - a file is also retried when any parameter set in par/ is newer than its skip time;\n"
    "# - deleting a file's line makes the scraper retry it in the next run.\n"
    "# An error ending in \"[present in par/ but not accepted by the decoder]\" means the named\n"
    "# .f2p is in par/ but the decoder still reports it missing; such a file is not retried for\n"
    "# that .f2p again (delete the line to retry it).\n"
    "# One line per file, tab-separated: data file, skipped at (UTC), decoder error.\n"
)
# Appended to the error of a skipped file whose named parameter set is in par/ when it is
# skipped: the decoder did not accept it, so its presence no longer triggers a retry
NOT_ACCEPTED = " [present in par/ but not accepted by the decoder]"
# The decoder's error for a data file whose parameter set it cannot find, e.g.
# "<ERROR: PARAMETER SET NOT FOUND(1): /x/par/D1022D_20250928120758.f2p (Line 1)>"
MISSING_PARSET = re.compile(r"\s*<ERROR:\s*PARAMETER SET NOT FOUND", re.IGNORECASE)
PARSET_NAME = re.compile(r"PARAMETER SET NOT FOUND[^:]*:\s*'?([^'<>]*?\.f2p)", re.IGNORECASE)
DAY_SECONDS = 86400


@dataclass(frozen=True)
class SkipEntry:
    skipped_at: datetime   # UTC, whole seconds
    error: str             # the decoder's error line


def read_skipped(path: Path) -> Dict[str, SkipEntry]:
    """Entries of a skipped.txt by data file name ({} without the file). Blank lines, comment
    lines and malformed lines are ignored."""
    try:
        text = path.read_bytes().decode(ENCODING)
    except FileNotFoundError:
        return {}
    entries = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = [f.strip() for f in line.split("\t", 2)]
        if len(fields) < 2 or not fields[0].endswith(".f2b") or "/" in fields[0]:
            continue
        try:
            skipped_at = parse_time(fields[1])
        except ValueError:
            continue
        entries[fields[0]] = SkipEntry(skipped_at, fields[2] if len(fields) > 2 else "")
    return entries


def write_skipped(path: Path, entries: Dict[str, SkipEntry]) -> None:
    """Write skipped.txt atomically (mode 0644, see publish); without entries it is removed."""
    if not entries:
        path.unlink(missing_ok=True)
        return
    lines = [f"{name}\t{format_time(e.skipped_at)}\t{' '.join(e.error.split())}\n"
             for name, e in sorted(entries.items())]
    publish(SKIPPED_HEADER + "".join(lines), path)


def named_parameter_set(error: str) -> Optional[str]:
    """Base name of the missing .f2p in the decoder's error line, or None."""
    m = PARSET_NAME.search(error)
    return re.split(r"[/\\]", m.group(1).strip())[-1] if m else None


def parameter_set_name(error: str) -> str:
    """Name of the missing .f2p in the decoder's error line, or a description."""
    return named_parameter_set(error) or "(name not in the decoder's error)"


def decode_file(dec: Decoder, src: LoggerSource, raw: Path, out_dir: Path,
                pp: Optional[PostprocessingSettings]) -> Tuple[Optional[str], Optional[str]]:
    """Decode one data file and publish its CSV in out_dir. Returns (reason, missing):
    (None, None) when the CSV was published; otherwise reason says why not, and missing is the
    decoder's PARAMETER SET NOT FOUND line when that is the only error the decoder reported
    (the parameter set is missing), else None. Raises for unexpected problems."""
    cfg = dec.cfg
    rc, out = dec.run(raw)
    if rc is None:
        return f"decoder timed out after {cfg.timeout_seconds:g} s", None
    csv = dec.data / src.serial / "raw" / f"{raw.stem}.csv"
    lines = split_lines(csv.read_bytes()) if csv.is_file() else None
    # The decoder's errors: the CSV's "<ERROR:" lines, plus those in its output when the CSV
    # alone does not tell (no CSV, or a non-zero exit code)
    errors = [ln.strip() for ln in lines if ln.startswith("<ERROR:")] if lines is not None else []
    if rc != 0 or lines is None:
        errors += [ln.strip() for ln in out.splitlines() if ln.lstrip().startswith("<ERROR:")]
    missing = errors[0] if errors and all(MISSING_PARSET.match(e) for e in errors) else None
    if rc != 0:
        return f"decoder exit code {rc}: {out.strip()}", missing
    if lines is None:
        return f"decoder wrote no {csv.relative_to(dec.work)}", missing
    if errors:
        return f"decoder reported {errors[0]}", missing
    if pp is not None:
        ctx = StepContext(raw_file=raw.name, serial=src.serial, logger_name=src.logger_name, settings=pp)
        try:
            lines = postprocess(lines, ctx)
        except Exception as e:
            raise RuntimeError(f"post-processing failed: {type(e).__name__}: {e}") from e
    publish("".join(lines), out_dir / f"{raw.stem}.csv")
    return None, None


@dataclass
class LoggerResult:
    """What the decode step did for one logger."""
    decoded: int = 0
    failed: int = 0
    waiting: int = 0   # data files waiting for their parameter set (retried next run)
    skipped: int = 0   # data files listed in skipped.txt after this run


def files_text(n: int) -> str:
    return f"{n} file" if n == 1 else f"{n} files"


def decode_logger(dec: Decoder, src: LoggerSource) -> LoggerResult:
    """Decode the pending data files of one logger: those without a CSV that are not listed
    in skipped.txt (or are listed, but a parameter set newer than their skip time is in par/)."""
    cfg = dec.cfg
    out_dir = src.folder / cfg.output_folder
    raw_dir = src.folder / "raw"
    par_dir = src.folder / "par"
    skipped_path = out_dir / SKIPPED_FILE
    skipped_rel = f"{cfg.output_folder}/{SKIPPED_FILE}"
    res = LoggerResult()

    undecoded = sorted(p for p in raw_dir.glob("*.f2b") if p.is_file()
                       and not (out_dir / f"{p.stem}.csv").exists()) if raw_dir.is_dir() else []
    pars = sorted(p for p in par_dir.glob("*.f2p") if p.is_file()) if par_dir.is_dir() else []
    # in whole seconds, like the skip times
    newest_par = max((int(p.stat().st_mtime) for p in pars), default=None)
    # names in par/, case-insensitive (files may be copied there from Windows)
    par_names = {p.name.lower() for p in pars}

    def in_par(error: str) -> Optional[str]:
        """The parameter set named in error, when a file of that name is in par/."""
        named = named_parameter_set(error)
        return named if named and named.lower() in par_names else None

    old = read_skipped(skipped_path)
    skipped: Dict[str, SkipEntry] = {}   # skipped.txt after this run
    pending = []
    for raw in undecoded:
        entry = old.get(raw.name)
        if entry is None:
            pending.append(raw)
        elif newest_par is not None and newest_par > entry.skipped_at.timestamp():
            log.info("Decoding %s/raw/%s: retrying, a parameter set in par/ is newer than its skip time %s",
                     src.folder.name, raw.name, format_time(entry.skipped_at))
            pending.append(raw)
        elif not entry.error.endswith(NOT_ACCEPTED.strip()) and in_par(entry.error):
            # its parameter set was put into par/ (possibly with an old modification time)
            log.info("Decoding %s/raw/%s: retrying, its parameter set %s is now in par/",
                     src.folder.name, raw.name, in_par(entry.error))
            pending.append(raw)
        else:
            skipped[raw.name] = entry
    if skipped:
        log.info("%s: %s skipped (parameter set missing), see %s", src.folder.name, files_text(len(skipped)),
                 skipped_rel)

    if not pending:
        log.debug("Decoding %s: nothing to decode", src.folder.name)
    else:
        for par in pars:
            rc, out = dec.run(par)
            if rc != 0:
                log.warning("Decoding %s: parameter set %s: decoder %s: %s", src.folder.name, par.name,
                            "timed out" if rc is None else f"exit code {rc}", out.strip())

        pp = cfg.postprocessing if cfg.postprocessing and cfg.postprocessing.enabled else None
        days = cfg.missing_parameter_set_days
        newly_skipped = 0
        for raw in pending:
            name = f"{src.folder.name}/raw/{raw.name}"
            try:
                reason, missing = decode_file(dec, src, raw, out_dir, pp)
            except Exception as e:
                reason, missing = str(e), None
            if reason is None:
                res.decoded += 1
                log.debug("Decoded %s -> %s/%s.csv", name, cfg.output_folder, raw.stem)
                continue
            if missing is None:
                res.failed += 1
                log.error("Decoding %s failed (retried next run): %s", name, reason)
                continue
            # Only the parameter set is missing: wait for it, then skip the file
            par_name = parameter_set_name(missing)
            now = datetime.now(timezone.utc)
            try:
                downloaded = raw.stat().st_mtime
            except OSError:
                downloaded = now.timestamp()
            give_up = downloaded + days * DAY_SECONDS
            if raw.name not in old and now.timestamp() < give_up:
                res.waiting += 1
                log.warning("Decoding %s: waiting for its parameter set %s (retried every run; skipped from %s, "
                            "%d days after the download)", name, par_name,
                            format_time(datetime.fromtimestamp(give_up, timezone.utc)), days)
                continue
            # Skip time in whole seconds, never before the newest parameter set, so that the
            # next run does not retry the file for a parameter set it has already seen
            skipped_at = datetime.fromtimestamp(max(int(now.timestamp()), newest_par or 0), timezone.utc)
            # The named parameter set is in par/ and still reported missing: mark the entry,
            # so that the file is not retried for it in every run
            present = in_par(missing)
            skipped[raw.name] = SkipEntry(skipped_at, missing + (NOT_ACCEPTED if present else ""))
            newly_skipped += 1
            if present:
                log.warning("Decoding %s: parameter set %s is in par/ but the decoder still reports it missing; "
                            "skipped, listed in %s (not retried for it again; delete its line to retry)",
                            name, present, skipped_rel)
            elif raw.name in old:
                log.warning("Decoding %s: parameter set %s still missing after a newer parameter set arrived "
                            "in par/; skipped again, listed in %s", name, par_name, skipped_rel)
            else:
                log.warning("Decoding %s: parameter set %s still missing, given up after %d days, listed in %s",
                            name, par_name, days, skipped_rel)
        extra = (f", {res.waiting} waiting for a parameter set, {newly_skipped} skipped"
                 if res.waiting or newly_skipped else "")
        log.info("Decoding %s: %d decoded, %d failed%s", src.folder.name, res.decoded, res.failed, extra)

    # Lines of decoded and retried files, and of files no longer pending, go away
    if skipped != old:
        try:
            write_skipped(skipped_path, skipped)
        except OSError as e:
            log.error("Decoding %s: could not write %s: %s", src.folder.name, skipped_rel, e)
            res.failed += 1
    res.skipped = len(skipped)
    return res


@dataclass(frozen=True)
class DecodeTotals:
    """What the decode step of one run did, summed over the loggers (for the status file)."""
    decoded: int = 0
    failed: int = 0              # data files that failed, plus 1 per logger whose decoding raised
    waiting: int = 0             # data files waiting for their parameter set (not failures)
    skipped: int = 0             # data files listed in skipped.txt (not failures)
    error: Optional[str] = None  # set when the decode step could not run at all


def run_decoding(cfg: Optional[DecodingSettings], sources: Iterable[LoggerSource]) -> DecodeTotals:
    """The decode step of one scraper run. Never raises for decoding problems. Returns the
    totals; all 0 when decoding is off."""
    if cfg is None or not cfg.enabled:
        return DecodeTotals()
    path = cfg.decoder_path
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        log.error("Decoder %s is missing or not executable; nothing decoded in this run", path)
        return DecodeTotals(error=f"decoder {path} is missing or not executable")
    try:
        dec = Decoder(cfg)
    except OSError as e:
        log.error("Could not create the decoder work folder; nothing decoded in this run: %s", e)
        return DecodeTotals(error=f"could not create the decoder work folder: {e}")
    total = LoggerResult()
    try:
        for src in sources:
            try:
                r = decode_logger(dec, src)
            except Exception:
                log.exception("Decoding %s failed", src.folder.name)
                total.failed += 1
                continue
            total.decoded += r.decoded
            total.failed += r.failed
            total.waiting += r.waiting
            total.skipped += r.skipped
    finally:
        dec.close()
    return DecodeTotals(decoded=total.decoded, failed=total.failed, waiting=total.waiting, skipped=total.skipped)
