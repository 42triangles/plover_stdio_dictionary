# vim: set fileencoding=utf-8 :
import subprocess
import json
import threading
import queue

from plover import log
from plover.steno_dictionary import StenoDictionary


none_t = type(None)


_no_default = object()
def _get(obj, name, ty, *, default=_no_default):
    if name not in obj:
        if default is _no_default:
            raise ValueError(f"Expected {name!r} in {obj!r}")
        return default
    out = obj[name]
    if not isinstance(out, ty):
        raise ValueError(
            f"Expected {name} to be of type {out!r}"
        )
    return out


no_fallback = object()
class StdioDictionary(StenoDictionary):
    readonly = True

    def __init__(self):
        super().__init__()
        self._process = None
        self._timeout = None
        self._longest_key = 0
        self._untranslate = False
        self._stdout = None
        self._stderr = None
        self._threads = []
        self._seq = -1
        self._undos = []
        self._failed = False
        self.readonly = True

    def _load_inner(self, filename):
        self._process = subprocess.Popen(
            [filename],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        def handle_file(file, output):
            for i in file:
                output.put(i)
            output.put(None)

        self._stdout = queue.SimpleQueue()
        self._stderr = queue.SimpleQueue()
        self._threads = [
            threading.Thread(
                target=handle_file,
                args=(f, q),
                daemon=True
            ).start()
            for f, q in [
                (self._process.stdout, self._stdout),
                (self._process.stderr, self._stderr)
            ]
        ]

        config_s = self._stdout.get()
        if config_s is None:
            raise ValueError("Dictionary exited")
        config = json.loads(config_s)

        self._longest_key = _get(config, "longest-key", int)
        if self._longest_key <= 0:
            raise ValueError(
                f"'longest-key' is not a valid value: "
                f"{self._longest_key}"
            )

        latency_ms = _get(
            config,
            "max-latency-ms",
            (int, float, none_t),
            default=None
        )
        if latency_ms is not None:
            if latency_ms <= 0:
                raise ValueError(
                    f"The maximum latency is not a valid "
                    f"value: {latency_ms}"
                )
            self._timeout = latency_ms / 1000.

        self._untranslate = (
            _get(config, "untranslate", bool, default=False)
        )

    def _load(self, filename):
        try:
            self._load_inner(filename)
        except Exception as e:
            self._failed = True
            raise e

    def _collect_errs(self):
        try:
            error = self._stderr.get_nowait()
            if error is None:
                return
        except queue.Empty:
            return
        raise ValueError(error)

    def _communicate(self, request):
        self._collect_errs()

        self._seq += 1
        request["seq"] = self._seq

        self._process.stdin.write(json.dumps(request) + "\n")
        self._process.stdin.flush()

        seq = -1
        while seq < self._seq:
            response_s = self._stdout.get(timeout=self._timeout)
            if response_s is None:
                self._failed = True
                raise ValueError("Dictionary exited")
            response = json.loads(response_s)
            seq = _get(response, "seq", int, default=-1)

        if seq > self._seq:
            raise ValueError("The dictionary is in the future")

        return response

    def _lookup(
        self, key,
        *, allow_undos=False, fallback=no_fallback
    ):
        if self._failed:
            if fallback is no_fallback:
                raise KeyError
            else:
                return fallback

        if len(key) > self._longest_key:
            if fallback is no_fallback:
                raise KeyError
            else:
                return fallback

        response = self._communicate({"translate": key})

        out = _get(response, "translation", str, default=None)
        if out is None:
            if fallback is no_fallback:
                raise KeyError
            else:
                return fallback
        else:
            return out

    def __contains__(self, key):
        return self._lookup(key, fallback=None) is not None

    def __getitem__(self, key):
        return self._lookup(key)

    def get(self, key, fallback=None):
        return self._lookup(key, fallback=fallback)

    def reverse_lookup(self, value):
        if self._failed:
            return {}

        response = self._communicate({"untranslate": value})
        out = _get(response, "strokes", list, default=None)
        return {} if out is None else set(str(i) for i in out)
