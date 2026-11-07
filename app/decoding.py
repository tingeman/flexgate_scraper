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
  has no line starting with ``<ERROR:``; anything else is logged as an ERROR and retried next run;
* optional post-processing steps (registry ``STEPS``) get the whole file and return the whole
  new content;
* the result is published atomically as ``<logger folder>/<output_folder>/<stem>.csv``
  (mode 0644). Nothing else is written in the download folders.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


def decode_logger(dec: Decoder, src: LoggerSource) -> Tuple[int, int]:
    """Decode the pending data files of one logger. Returns (decoded, failed)."""
    cfg = dec.cfg
    out_dir = src.folder / cfg.output_folder
    raw_dir = src.folder / "raw"
    pending = sorted(p for p in raw_dir.glob("*.f2b") if p.is_file()
                     and not (out_dir / f"{p.stem}.csv").exists()) if raw_dir.is_dir() else []
    if not pending:
        log.debug("Decoding %s: nothing to decode", src.folder.name)
        return 0, 0

    par_dir = src.folder / "par"
    for par in sorted(par_dir.glob("*.f2p")) if par_dir.is_dir() else []:
        if not par.is_file():
            continue
        rc, out = dec.run(par)
        if rc != 0:
            log.warning("Decoding %s: parameter set %s: decoder %s: %s", src.folder.name, par.name,
                        "timed out" if rc is None else f"exit code {rc}", out.strip())

    pp = cfg.postprocessing if cfg.postprocessing and cfg.postprocessing.enabled else None
    decoded = failed = 0
    for raw in pending:
        reason = None
        try:
            rc, out = dec.run(raw)
            csv = dec.data / src.serial / "raw" / f"{raw.stem}.csv"
            if rc is None:
                reason = f"decoder timed out after {cfg.timeout_seconds:g} s"
            elif rc != 0:
                reason = f"decoder exit code {rc}: {out.strip()}"
            elif not csv.is_file():
                reason = f"decoder wrote no {csv.relative_to(dec.work)}"
            else:
                lines = split_lines(csv.read_bytes())
                err = next((ln.rstrip("\r\n") for ln in lines if ln.startswith("<ERROR:")), None)
                if err is not None:
                    reason = f"decoder reported {err}"
                else:
                    if pp is not None:
                        ctx = StepContext(raw_file=raw.name, serial=src.serial,
                                          logger_name=src.logger_name, settings=pp)
                        try:
                            lines = postprocess(lines, ctx)
                        except Exception as e:
                            raise RuntimeError(f"post-processing failed: {type(e).__name__}: {e}") from e
                    publish("".join(lines), out_dir / f"{raw.stem}.csv")
        except Exception as e:
            reason = str(e)
        if reason is None:
            decoded += 1
            log.debug("Decoded %s/raw/%s -> %s/%s.csv", src.folder.name, raw.name, cfg.output_folder, raw.stem)
        else:
            failed += 1
            log.error("Decoding %s/raw/%s failed (retried next run): %s", src.folder.name, raw.name, reason)
    log.info("Decoding %s: %d decoded, %d failed", src.folder.name, decoded, failed)
    return decoded, failed


def run_decoding(cfg: Optional[DecodingSettings], sources: Iterable[LoggerSource]) -> None:
    """The decode step of one scraper run. Never raises for decoding problems."""
    if cfg is None or not cfg.enabled:
        return
    path = cfg.decoder_path
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        log.error("Decoder %s is missing or not executable; nothing decoded in this run", path)
        return
    try:
        dec = Decoder(cfg)
    except OSError as e:
        log.error("Could not create the decoder work folder; nothing decoded in this run: %s", e)
        return
    try:
        for src in sources:
            try:
                decode_logger(dec, src)
            except Exception:
                log.exception("Decoding %s failed", src.folder.name)
    finally:
        dec.close()
