# vim: set fileencoding=utf-8 :
from typing import Generic, IO, Literal, ParamSpec, TypeVar
from typing import cast, overload, TYPE_CHECKING
from collections.abc import Callable
import subprocess
import json
import threading
import queue
import inspect
from enum import Enum
from dataclasses import dataclass

from plover import log  # type: ignore
from plover.steno_dictionary import StenoDictionary  # type: ignore


T = TypeVar('T')

P = ParamSpec('P')
R = TypeVar('R')


# type hack for sentinel values from
# https://stackoverflow.com/questions/57959664/handling-conditional-logic-sentinel-value-with-mypy
class Sentinels(Enum):
    GIVEN_DEFAULT = 0  # used for `ReturnArg`
    NO_DEFAULT = 1  # used in `StdioDictionary._extract`


GivenDefault = Literal[Sentinels.GIVEN_DEFAULT]
GIVEN_DEFAULT: GivenDefault = Sentinels.GIVEN_DEFAULT

NoDefault = Literal[Sentinels.NO_DEFAULT]
NO_DEFAULT: NoDefault = Sentinels.NO_DEFAULT


class NoDefaultNeeded(Generic[T]):
    pass


def is_exception_type(obj: object) -> bool:
    return isinstance(obj, type) and issubclass(obj, Exception)


@dataclass
class ReturnArg(Generic[T]):
    name: str
    or_default: object | GivenDefault = GIVEN_DEFAULT


@overload
def handle_error(
    return_ty: type[R], method: Literal["error"],
    default: NoDefaultNeeded[R] = NoDefaultNeeded(),
    *, ignore_inactive: bool = False
) -> Callable[[Callable[P, R]], Callable[P, R]]:  ...


@overload
def handle_error(
    return_ty: type[R], method: Literal["log"],
    default: ReturnArg[R] | Callable[[], R] | R | type[Exception],
    *, ignore_inactive: bool = False
) -> Callable[[Callable[P, R]], Callable[P, R]]:  ...


def handle_error(
    return_ty: type[R], method: Literal["error", "log"],
    default: ReturnArg[R] | Callable[[], R] | R | type[Exception]
    | NoDefaultNeeded[R] = NoDefaultNeeded(),
    *, ignore_inactive: bool = False
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """
    Error handling when the question of when and how to throw
    errors is complicated is going to be a mess either way.

    This decorator is supposed to simplify it a bit on the other
    methods to aid in their readability, though in doing so all
    the ugly details are swept under a very inelegant rug - this
    function.

    `method` is either:
    * "error" - the error is passed on as is
    * "log"   - the error is logged

    `default` is either
    * `ReturnArg(...)`  - return a given argument. Note that
                          this uses `inspect` to try and get the
                          correct position and default if
                          `GIVEN_DEFAULT` is used (used for
                          `StenoDictionary.get`)
    * a callable object - return the result of a call with empty
                          arguments (used for mutable defaults,
                          so `lambda: []` instead of `[]`)
    * an exception type - raise that exception. This also causes
                          exceptions of that type to not be
                          logged and be rethrown without
                          modification (used for
                          `StenoDictionary.__getitem__`)
    * any other object  - returned as is

    If `ignore_inactive` is `True` this function will not
    immediately try and return the default and instead run the
    function anyway (used for `StenoDictionary._load`).
    """
    def accept_function(func: Callable[P, R]) -> Callable[P, R]:
        if isinstance(default, ReturnArg):
            parameters = inspect.signature(func).parameters
            arg_default = (
                cast(R, parameters[default.name].default)
                if default.or_default is GIVEN_DEFAULT
                else cast(R, default.or_default)
            )
            arg_pos = next(
                idx
                for idx, name in enumerate(parameters.keys())
                if name == default.name
            )

        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            self: StdioDictionary = \
                cast(StdioDictionary, args[0])

            def return_value() -> R:
                if isinstance(default, ReturnArg):
                    if default.name in kwargs:
                        return cast(R, kwargs["fallback"])
                    elif arg_pos < len(args):
                        return cast(R, args[arg_pos])
                    else:
                        return arg_default
                elif is_exception_type(default):
                    raise cast(type[Exception], default)
                elif hasattr(default, "__call__"):
                    return cast(Callable[[], R], default)()
                else:
                    return cast(R, default)

            if not self._active and not ignore_inactive:
                return return_value()

            try:
                return func(*args, **kwargs)
            except Exception as ex:
                if is_exception_type(default):
                    if isinstance(ex, cast(type, default)):
                        raise ex  # rethrow unchanged

                self._active = False

                if method == "log":
                    log.error(str(ex))
                    return return_value()
                elif method == "error":
                    raise ex
                else:
                    raise NotImplementedError from ex

        return wrapper

    return accept_function


class StdioDictionary(StenoDictionary):  # type: ignore
    readonly = True

    def __init__(self) -> None:
        super().__init__()

        self._filename: str
        self._process: subprocess.Popen[str] | None = None
        self._stdout: queue.SimpleQueue[str | None]

        self._longest_key: int
        self._timeout: float | None = None
        self._untranslate: bool

        self._seq: int

        self._active: bool = False

        self.readonly = True

    def _expect_stdout(self) -> object:
        out_s = self._stdout.get(timeout=self._timeout)
        if out_s is None:
            raise ValueError(
                f"Dictionary {self._filename} exited early"
            )

        try:
            out = json.loads(out_s)
        except json.decoder.JSONDecodeError as ex:
            raise ValueError(
                f"Dictionary {self._filename} pushed invalid "
                f"JSON: {out_s}"
            ) from ex

        return out

    def _extract(
        self, obj: object, name: str, ty: type[T],
        *, default: T | NoDefault = NO_DEFAULT
    ) -> T:
        if not isinstance(obj, dict) or name not in obj:
            if default is NO_DEFAULT:
                raise ValueError(
                    f"Dictionary {self._filename} pushed an "
                    f"invalid object, expected {name} to be "
                    f"present"
                )
            return default

        out = obj[name]

        if TYPE_CHECKING:
            if isinstance(ty, tuple):
                for i in ty:
                    if isinstance(out, i):
                        return out
                raise Exception
            else:
                if isinstance(out, ty):
                    return out

        if not isinstance(out, ty):
            raise ValueError(
                f"Expected {name} to be of type {ty!r}"
            )

        return out

    def _setup_process(self) -> None:
        # terminate earlier instances if they exist:
        if self._process is not None:
            self._process.terminate()

        self._process = subprocess.Popen(
            [self._filename],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        def handle_errors(file: IO[str]) -> None:
            for i in file:
                log.error(i)

        threading.Thread(
            target=handle_errors, args=(self._process.stderr,),
            daemon=True
        ).start()

        def handle_file(
            file: IO[str],
            output: queue.SimpleQueue[str | None]
        ) -> None:
            for i in file:
                output.put(i)
            output.put(None)

        self._stdout = queue.SimpleQueue()
        threading.Thread(
            target=handle_file,
            args=(self._process.stdout, self._stdout),
            daemon=True
        ).start()

    def _communicate(
        self, request: dict[str, object]
    ) -> object:
        self._seq += 1
        request["seq"] = self._seq

        assert self._process is not None
        assert self._process.stdin is not None
        self._process.stdin.write(json.dumps(request) + "\n")
        self._process.stdin.flush()

        seq = -1
        while seq < self._seq:
            response = self._expect_stdout()
            seq = self._extract(response, "seq", int, default=-1)

        if seq > self._seq:
            raise ValueError("The dictionary is in the future")

        return response

    @handle_error(type(None), "error", ignore_inactive=True)
    def _load(self, filename: str) -> None:
        self._filename = filename
        self._active = True

        self._setup_process()

        # Get the global configuration for the dictionary
        self._timeout = None
        config = self._expect_stdout()

        # Extract the longest key
        self._longest_key = \
            self._extract(config, "longest-key", int)
        if self._longest_key <= 0:
            raise ValueError(
                f"'longest-key' is not a valid value: "
                f"{self._longest_key}"
            )

        # Extract the maximum accepted latency
        latency_ms = self._extract(
            config, "max-latency-ms",
            cast(
                type[int | float | None],
                int | float | type(None)
            ),
            default=None
        )
        if latency_ms is not None:
            if latency_ms <= 0:
                raise ValueError(
                    f"The maximum latency is not a valid "
                    f"value: {latency_ms}"
                )
        self._timeout = (
            latency_ms / 1000.
            if latency_ms is not None
            else None
        )

        # Extract if it is untranslate-capable
        self._untranslate = self._extract(
            config, "untranslate", bool, default=False
        )

        # Restart the sequence numbers
        self._seq = -1

    def _lookup(self, key: tuple[str, ...]) -> str:
        if len(key) > self._longest_key:
            raise KeyError

        response = self._communicate({"translate": key})

        out = self._extract(
            response,
            "translation",
            cast(type[str | None], str | type(None)),
            default=None
        )

        if out is None:
            raise KeyError

        return out

    @handle_error(bool, "log", False)
    def __contains__(self, key: tuple[str, ...]) -> bool:
        try:
            self._lookup(key)
            return True
        except KeyError:
            return False

    @handle_error(str, "log", KeyError)
    def __getitem__(self, key: tuple[str, ...]) -> str:
        return self._lookup(key)

    @overload
    def get(self, key: tuple[str, ...]) -> str | None: ...

    @overload
    def get(self, key: tuple[str, ...], fallback: T) \
        -> str | T: ...

    @handle_error(  # type: ignore
        str | T | None, "log", ReturnArg("fallback")
    )
    def get(
        self, key: tuple[str, ...], fallback: T | None = None
    ) -> str | T | None:
        try:
            return self._lookup(key)
        except KeyError:
            return fallback

    @handle_error(set[tuple[str, ...]], "log", lambda: set())
    def reverse_lookup(self, value: str) \
            -> set[tuple[str, ...]]:
        response = self._communicate({"untranslate": value})
        outl: list[object] = self._extract(
            response,
            "reverse-translation",
            list[object] if TYPE_CHECKING else list,
            default=[]
        )
        assert all(isinstance(i, list) for i in outl)
        outll = cast(list[list[object]], outl)
        assert all(isinstance(j, str) for i in outll for j in i)
        return {tuple(i) for i in cast(list[list[str]], outll)}
