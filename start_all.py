"""Start the servers the game needs: http_config.py on 443 (https://APSEODESK172, plus the
admin page on :8088) and messenger.py on :28002.

Streams both outputs prefixed, and on Ctrl-C (or --selftest) terminates both so
nothing is left listening. (It used to also start server.py and a second http_config.py
on 8080; the client uses neither, and the two http_config.py fought over the admin port.)

Usage:
    python start_all.py
    python start_all.py --selftest   # start, verify alive ~3s, stop, exit
"""
import os, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
selftest = "--selftest" in sys.argv

procs = []


def emit(text):
    """Print a child line even when the console's own codec cannot encode it."""
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except UnicodeEncodeError:
        buf = getattr(sys.stdout, "buffer", None)
        if buf is not None:
            buf.write(text.encode("utf-8", "replace"))
            buf.flush()


def pump(name, proc):
    for line in proc.stdout:
        emit(f"[{name}] {line}")


def start(name, extra):
    # text=True alone decodes with the locale codec (cp936 here), while play.bat /
    # the launcher set PYTHONIOENCODING=utf-8 for the children. Pin both sides to
    # UTF-8 so a Chinese log line (or a traceback containing one) cannot kill pump.
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.Popen([sys.executable, "-u", os.path.join(HERE, name), *extra],
                         cwd=HERE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace", bufsize=1)
    threading.Thread(target=pump, args=(name, p), daemon=True).start()
    procs.append((name, p))


def shutdown():
    for name, p in procs:
        if p.poll() is None:
            p.terminate()
    for name, p in procs:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


def main():
    start("http_config.py", ["443", "--tls"])  # https://APSEODESK172 (hardcoded in fifazf.exe)
    start("messenger.py", [])  # :28002, address handed out by /info/my/services/messenger/2
    print("[start_all] http_config.py (:443 https, admin :8088) + messenger.py (:28002) up. Ctrl-C to stop.")

    if selftest:
        time.sleep(3)
        alive = all(p.poll() is None for _, p in procs)
        shutdown()
        print("[start_all] selftest", "OK" if alive else "FAILED")
        sys.exit(0 if alive else 1)

    try:
        while all(p.poll() is None for _, p in procs):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()
        print("[start_all] stopped.")


if __name__ == "__main__":
    main()
