"""Minimal stand-in for a FlexGate server, used by tests/run.sh.

    python fake_flexgate.py <root> [port]

<root> is laid out like the server: <root>/fg2lite/sw/ is where browse.php lives and
<root>/fg2lite/data/<serial>/<folder>/<file> holds the logger files. Two kinds of request
are answered:

  /fg2lite/sw/browse.php?dir=../data/<serial>[/<folder>]
      an HTML page with one <a> per entry of that directory: files link to
      ../data/<serial>/.../<file> (relative to browse.php), directories link back to
      browse.php?dir=...
  any other path under <root>
      the file itself

Only the standard library is used, so it runs in the scraper image. Each request is
logged to stdout as "REQ <path>".
"""
import html
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

ROOT = Path(sys.argv[1]).resolve()
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
SW_DIR = ROOT / "fg2lite" / "sw"


def inside_root(path: Path) -> bool:
    try:
        path.resolve().relative_to(ROOT)
        return True
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep stdout to one line per request
        pass

    def send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        print(f"REQ {self.path}", flush=True)
        url = urlparse(self.path)
        if url.path.endswith("/browse.php"):
            self.browse(parse_qs(url.query).get("dir", [""])[0])
            return
        target = ROOT / unquote(url.path).lstrip("/")
        if inside_root(target) and target.is_file():
            self.send(200, target.read_bytes(), "application/octet-stream")
        else:
            self.send(404, b"not found")

    def browse(self, rel_dir: str):
        directory = SW_DIR / rel_dir
        if not rel_dir or not inside_root(directory) or not directory.is_dir():
            self.send(404, b"no such directory")
            return
        rel_dir = rel_dir.rstrip("/")
        rows = ['<a href="index.php">Home</a>']
        for entry in sorted(directory.iterdir()):
            name = html.escape(entry.name)
            if entry.is_dir():
                href = "browse.php?dir=" + quote(f"{rel_dir}/{entry.name}", safe="")
                rows.append(f'<a href="{href}">{name}/</a>')
            else:
                href = quote(f"{rel_dir}/{entry.name}", safe="/.")
                rows.append(f'<a href="{href}">{name}</a>')
        body = "<html><body>\n" + "<br>\n".join(rows) + "\n</body></html>\n"
        self.send(200, body.encode())


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"fake flexgate listening on {PORT}", flush=True)
    server.serve_forever()
