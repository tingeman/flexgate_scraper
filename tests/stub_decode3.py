#!/usr/bin/env python3
"""Stand-in for GeoPrecision's decode3, used by tests/run.sh. It mimics the decoder's file
handling, not its file formats.

    stub_decode3.py INFILE -k<inifile> [-v]

INI: when <inifile> does not exist it is created with data_path = <dir of inifile>/data;
otherwise data_path is read from it. The serial is the part of the file name before the
first "_". All output files get mode 0666.

Input formats (text files):
  .f2p  parameter set; its first line is the column header of the CSV files decoded with it,
        e.g. "NO;TIME;#1(oC);#2(oC);HK-BAT(V)". The input is copied to
        <data_path>/<serial>/par/, <stem>.ptx is written there and next to the input,
        <stem>.log next to the input.
  .f2b  data file; first line "PARSET <name>.f2p" names the parameter set, the remaining
        lines are copied into the CSV unchanged (data rows, "<...>" lines, repeated headers).
        The input is copied to <data_path>/<serial>/raw/, <stem>.csv and <stem>.log are
        written there and next to the input. The CSV is a few "<...>" metadata lines, the
        header from the parameter set in <data_path>/<serial>/par/, the remaining lines and a
        final "<INFO: Decode OK, n Lines>". When the parameter set is not there, the header
        is a "#n((unk.))" header after an "<ERROR: PARAMETER SET NOT FOUND(1): ...>" line,
        and the exit code is still 0. Lines end with CRLF unless the input line had none.

Lines at the top of any input starting with "STUB:" control the stub (and are not data):
  STUB:exit255     exit 255 without output
  STUB:sleep <s>   sleep <s> seconds first
  STUB:noout       exit 0 without writing a CSV
An unreadable or missing input exits 255.

Environment (test hooks):
  STUB_DECODE3_CALLS  file to which the input's base name is appended for every call
  STUB_DECODE3_KEEP   folder in which a copy of every CSV written is kept as <stem>.csv
"""
import os
import shutil
import sys
import time
from pathlib import Path


def out(path: Path, data: bytes, append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab" if append else "wb") as f:
        f.write(data)
    os.chmod(path, 0o666)


def main(argv):
    args = [a for a in argv if a != "-v"]
    ini_args = [a for a in args if a.startswith("-k")]
    files = [a for a in args if not a.startswith("-")]
    if len(files) != 1 or len(ini_args) != 1:
        print("usage: decode3 INFILE -k<inifile> [-v]")
        return 255
    infile, ini = Path(files[0]), Path(ini_args[0][2:])

    calls = os.environ.get("STUB_DECODE3_CALLS")
    if calls:
        with open(calls, "a") as f:
            f.write(infile.name + "\n")

    try:
        lines = infile.read_bytes().splitlines(keepends=True)
    except OSError as e:
        print(f"<ERROR: cannot read {infile}: {e}>")
        return 255

    while lines and lines[0].startswith(b"STUB:"):
        cmd = lines.pop(0).decode().strip()[5:].split()
        if cmd[0] == "exit255":
            print("<ERROR: stub exit 255>")
            return 255
        if cmd[0] == "sleep":
            time.sleep(float(cmd[1]))
        if cmd[0] == "noout":
            return 0

    if ini.exists():
        data_path = next(ln.split("=", 1)[1].strip() for ln in ini.read_text().splitlines()
                         if ln.startswith("data_path"))
    else:
        data_path = str(ini.parent / "data")
        out(ini, f"[decode3]\ndata_path={data_path}\n".encode())
    data = Path(data_path)

    serial = infile.name.split("_", 1)[0]
    stem = infile.stem
    log_line = f"stub decode3: decoded {infile.name}\n".encode()
    nl = b"\r\n"

    if infile.suffix == ".f2p":
        par_dir = data / serial / "par"
        par_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(infile, par_dir / infile.name)
        os.chmod(par_dir / infile.name, 0o666)
        ptx = b"<PARAMETER SET " + infile.name.encode() + b">" + nl + b"".join(lines)
        out(par_dir / f"{stem}.ptx", ptx)
        out(infile.with_suffix(".ptx"), ptx)
        out(infile.with_suffix(".log"), log_line, append=True)
        return 0

    raw_dir = data / serial / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_copy = raw_dir / infile.name
    shutil.copyfile(infile, raw_copy)
    os.chmod(raw_copy, 0o666)

    parset = lines.pop(0).decode().split(None, 1)[1].strip() if lines and lines[0].startswith(b"PARSET") else ""
    par_file = data / serial / "par" / parset
    csv = [b"<INFO: Raw-Data to CSV: '" + str(infile).encode() + b"' ...>" + nl,
           b"<FG2 'Decode3 stub'>" + nl,
           b"<FILE: " + str(raw_copy).encode() + b">" + nl,
           b"<LOGGER: $" + serial.encode() + b">" + nl]
    if parset and par_file.is_file():
        header = par_file.read_bytes().splitlines()[0]
        csv.append(b"<PARAMETER_SET: " + str(par_file).encode() + b">" + nl)
    else:
        csv.append(b"<ERROR: PARAMETER SET NOT FOUND(1): '" + str(par_file).encode() + b"'>" + nl)
        header = b"NO;TIME;#1((unk.));#2((unk.))"
    csv.append(header + nl)
    n = 0
    for ln in lines:
        csv.append(ln[:-1] + nl if ln.endswith(b"\n") and not ln.endswith(b"\r\n") else ln)
        n += 0 if ln.startswith((b"<", b"NO;")) else 1
    csv.append(f"<INFO: Decode OK, {n} Lines>".encode() + nl)
    content = b"".join(csv)

    out(raw_dir / f"{stem}.csv", content)
    out(raw_dir / f"{stem}.log", log_line, append=True)
    out(infile.with_suffix(".csv"), content)
    out(infile.with_suffix(".log"), log_line, append=True)
    keep = os.environ.get("STUB_DECODE3_KEEP")
    if keep:
        out(Path(keep) / f"{stem}.csv", content)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
