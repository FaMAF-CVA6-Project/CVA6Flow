#!/usr/bin/env python3
"""Serve CVA6Flow over HTTP so it can be used from a container.

A container has no browser and no display. Serving the page and publishing
the port puts it in the host's browser while the VCD stays inside. The
page opens the samples tests/samples.js lists, which make_CVA6Flow_sample.py
writes, and any other JSON only from the host's disk.

    python3 scripts/serve_CVA6Flow.py           # port 8000, the page's folder
    python3 scripts/serve_CVA6Flow.py --port 9000
    python3 scripts/serve_CVA6Flow.py --root ..
    python3 scripts/serve_CVA6Flow.py --bind 0.0.0.0

It listens on 127.0.0.1, so a run on a laptop does not offer the repository
to the network, and on 0.0.0.0 inside a container, which a published port
needs. Inside a container it sits in scripts/ and is run from the root.
Either way the folder served is worked out from where a viewer page is, and
--root overrides it.

Start the container with the port published, or nothing outside it can
connect. The two containers take different host ports, so both viewers can be
served at once:

    docker run -it --name CVA6 -p 8001:8000 manuel313/famaf_cva6 bash

make_containers.py publishes that, and docker_run.py reads the mapping back, so
the URL it prints is the host one.
"""
import argparse
import functools
import http.server
import os
import sys

DEFAULT_PORT = 8000
VIEWER_NAME = "CVA6Flow"

# Only used to find the folder to serve and to print the link, so a layout
# not listed here still serves.
VIEWER_PAGES = (
    "CVA6Flow/CVA6Flow.html",
    "CVA6Flow.html",
    "viewers/CVA6Flow/CVA6Flow.html",
)


# SHARED BEGIN py-serve

# Needs: functools, http.server, os, sys


class Handler(http.server.SimpleHTTPRequestHandler):
    """The stock handler, quieter, and without caching."""

    def log_message(self, fmt, *args):
        # One line per request, without the timestamp.
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    def end_headers(self):
        # A JSON is regenerated in place while the page is open.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def in_container():
    """True inside a Docker container, where the port is published to the
    host and the server has to listen on every interface."""
    return os.path.exists("/.dockerenv")


def default_bind():
    """The --bind default: every interface inside a container, which a
    published port needs, and loopback elsewhere, so a laptop run does not
    offer the repository to the network."""
    return "0.0.0.0" if in_container() else "127.0.0.1"


def default_root(pages):
    """The first of the working directory, this script's folder and the one
    above it that holds one of pages, else the working directory. The script
    is started from a repository root, a container root or scripts/."""
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.getcwd(), here, os.path.dirname(here)):
        if any(os.path.isfile(os.path.join(candidate, page))
               for page in pages):
            return candidate
    return os.getcwd()


def serve(root, bind, port, pages, viewer_name):
    """Serve root on bind and port until Ctrl-C, and return the exit code. A
    container has no browser, so serving is how its pages reach the host's.
    The samples come from tests/samples.js, so no folder listing is needed."""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        print(f"[ERROR] {root} is not a folder", file=sys.stderr)
        return 2

    handler = functools.partial(Handler, directory=root)
    # Threaded, so a large JSON downloading does not hold up the page's other
    # requests.
    try:
        server = http.server.ThreadingHTTPServer((bind, port), handler)
    except OSError as e:
        print(f"[ERROR] Could not listen on {bind}:{port}: {e}",
              file=sys.stderr)
        print("[INFO] Another server may already be running. "
              f"Try --port {port + 1}.")
        return 2

    print(f"[INFO] Serving {root} on http://localhost:{port}/")
    found = [p for p in pages if os.path.isfile(os.path.join(root, p))]
    if found:
        print("[INFO] Open one of these in a browser:")
        for page in found:
            print(f"           http://localhost:{port}/{page}")
    else:
        print("[WARN] No viewer page found under this root. Serve the folder "
              f"holding {viewer_name}.html, or pass --root.")
    if in_container():
        print("[INFO] From the host, use the port that docker run -p "
              f"<host port>:{port} published in place of {port}.")
    print("[INFO] Ctrl-C to stop\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[INFO] Stopped")
    finally:
        server.server_close()
    return 0

# SHARED END py-serve


def main():
    parser = argparse.ArgumentParser(
        description="Serve CVA6Flow and its JSONs over HTTP.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Port to listen on. Defaults to {DEFAULT_PORT}")
    parser.add_argument("--root", default=None,
                        help="Folder to serve. Defaults to the first of the "
                             "working directory, this script's folder and the "
                             "folder above it that holds CVA6Flow.html")
    parser.add_argument("--bind", default=default_bind(),
                        help="Address to listen on. Defaults to 0.0.0.0 "
                             "inside a container, which a published port "
                             "needs, and to 127.0.0.1 elsewhere")
    args = parser.parse_args()
    root = args.root if args.root else default_root(VIEWER_PAGES)
    return serve(root, args.bind, args.port, VIEWER_PAGES, VIEWER_NAME)


if __name__ == "__main__":
    sys.exit(main())
