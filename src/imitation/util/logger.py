"""Logging for quantitative metrics and free-form text."""

import contextlib
import datetime
import os
import tempfile
from typing import Any, Dict, Generator, List, Optional, Sequence, Tuple, Union

import stable_baselines3.common.logger as sb_logger

from imitation.data import types


def _build_output_formats(
    folder: str,
    format_strs: Sequence[str],
) -> Sequence[sb_logger.KVWriter]:
    """Build output formats for initializing a Stable Baselines Logger.

    Args:
        folder: Path to directory that logs are written to.
        format_strs: An list of output format strings. For details on available
            output formats see `stable_baselines3.logger.make_output_format`.

    Returns:
        A list of output formats, one corresponding to each `format_strs`.
    """
    os.makedirs(folder, exist_ok=True)
    output_formats = []
    for f in format_strs:
        if f == "wandb":
            output_formats.append(WandbOutputFormat())
        else:
            output_formats.append(sb_logger.make_output_format(f, folder))
    return output_formats


class HierarchicalLogger(sb_logger.Logger):
    """A logger supporting contexts for accumulating mean values.

    `self.accumulate_means` creates a context manager. While in this context,
    values are loggged to a sub-logger, with only mean values recorded in the
    top-level (root) logger.

    >>> import tempfile
    >>> with tempfile.TemporaryDirectory() as dir:
    ...     logger: HierarchicalLogger = configure(dir, ('log',))
    ...     # record the key value pair (loss, 1.0) to path `dir`
    ...     # at step 1.
    ...     logger.record("loss", 1.0)
    ...     logger.dump(step=1)
    ...     with logger.accumulate_means("dataset"):
    ...         # record the key value pair `("raw/dataset/entropy", 5.0)` to path
    ...         # `dir/raw/dataset` at step 100
    ...         logger.record("entropy", 5.0)
    ...         logger.dump(step=100)
    ...         # record the key value pair `("raw/dataset/entropy", 6.0)` to path
    ...         # `dir/raw/dataset` at step 200
    ...         logger.record("entropy", 6.0)
    ...         logger.dump(step=200)
    ...     # record the key value pair `("mean/dataset/entropy", 5.5)` to path
    ...     # `dir` at step 1.
    ...     logger.dump(step=1)
    ...     with logger.add_prefix("foo"), logger.accumulate_means("bar"):
    ...         # record the key value pair ("raw/foo/bar/entropy", 42.0) to path
    ...         # `dir/raw/foo/bar` at step 2000
    ...         logger.record("biz", 42.0)
    ...         logger.dump(step=2000)
    ...     # record the key value pair `("mean/foo/bar/entropy", 42.0)` to path
    ...     # `dir` at step 1.
    ...     logger.dump(step=1)
    ...     with open(os.path.join(dir, 'log.txt')) as f:
    ...         print(f.read())
    -------------------
    | loss | 1        |
    -------------------
    ---------------------------------
    | mean/              |          |
    |    dataset/entropy | 5.5      |
    ---------------------------------
    -----------------------------
    | mean/          |          |
    |    foo/bar/biz | 42       |
    -----------------------------
    <BLANKLINE>
    """

    default_logger: sb_logger.Logger
    current_logger: Optional[sb_logger.Logger]
    _cached_loggers: Dict[str, sb_logger.Logger]
    _prefixes: List[str]
    _subdir: Optional[str]
    _name: Optional[str]

    def __init__(
        self,
        default_logger: sb_logger.Logger,
        format_strs: Sequence[str] = ("stdout", "log", "csv"),
    ):
        """Builds HierarchicalLogger.

        Args:
            default_logger: The default logger when not in an `accumulate_means`
                context. Also the logger to which mean values are written to after
                exiting from a context.
            format_strs: A list of output format strings that should be used by
                every Logger initialized by this class during an `AccumulatingMeans`
                context. For details on available output formats see
                `stable_baselines3.logger.make_output_format`.
        """
        self.default_logger = default_logger
        self.current_logger = None
        self._cached_loggers = {}
        self._prefixes = []
        self._subdir = None
        self._name = None
        self.format_strs = format_strs
        super().__init__(folder=self.default_logger.dir, output_formats=[])

    def _update_name_to_maps(self) -> None:
        self.name_to_value = self._logger.name_to_value
        self.name_to_count = self._logger.name_to_count
        self.name_to_excluded = self._logger.name_to_excluded

    @contextlib.contextmanager
    def add_prefix(self, prefix: str) -> Generator[None, None, None]:
        """Add a prefix to the subdirectory used to accumulate means.

        This prefix only applies when a `accumulate_means` context is active. If there
        are multiple active prefixes, then they are concatenated.

        Args:
            prefix: The prefix to add to the named sub.

        Yields:
            None when the context manager is entered

        Raises:
            RuntimeError: if accumulate means context is already active.
        """
        if self.current_logger is not None:
            raise RuntimeError(
                "Cannot add prefix when accumulate_means context is already active.",
            )

        try:
            self._prefixes.append(prefix)
            yield
        finally:
            self._prefixes.pop()

    @contextlib.contextmanager
    def accumulate_means(self, name: str) -> Generator[None, None, None]:
        """Temporarily modifies this HierarchicalLogger to accumulate means values.

        Within this context manager, `self.record(key, value)` writes the "raw" values
        in `f"{self.default_logger.log_dir}/{prefix}/{name}"` under the key
        `"raw/{prefix}/{name}/{key}"`. At the same time, any call to `self.record` will
        also accumulate mean values on the default logger by calling
        `self.default_logger.record_mean(f"mean/{prefix}/{name}/{key}", value)`.

        Multiple prefixes may be active at once. In this case the `prefix` is simply the
        concatenation of each of the active prefixes in the order they
        where created e.g. if the active `prefixes` are ['foo', 'bar'] then
        the `prefix` is 'foo/bar'.

        After the context exits, calling `self.dump()` will write the means
        of all the "raw" values accumulated during this context to
        `self.default_logger` under keys of the form  `mean/{prefix}/{name}/{key}`

        Note that the behavior of other logging methods, `log` and `record_mean`
        are unmodified and will go straight to the default logger.

        Args:
            name: A string key which determines the `folder` where raw data is
                written and temporary logging prefixes for raw and mean data. Entering
                an `accumulate_means` context in the future with the same `subdir`
                will safely append to logs written in this folder rather than
                overwrite.

        Yields:
            None when the context is entered.

        Raises:
            RuntimeError: If this context is entered into while already in
                an `accumulate_means` context.
        """
        if self.current_logger is not None:
            raise RuntimeError("Nested `accumulate_means` context")

        subdir = os.path.join(*self._prefixes, name)

        if subdir in self._cached_loggers:
            logger = self._cached_loggers[subdir]
        else:
            folder = os.path.join(self.default_logger.dir, "raw", subdir)
            os.makedirs(folder, exist_ok=True)
            output_formats = _build_output_formats(folder, self.format_strs)
            logger = sb_logger.Logger(folder, list(output_formats))
            self._cached_loggers[subdir] = logger

        try:
            self.current_logger = logger
            self._subdir = subdir
            self._name = name
            self._update_name_to_maps()
            yield
        finally:
            self.current_logger = None
            self._subdir = None
            self._name = None
            self._update_name_to_maps()

    def record(self, key, val, exclude=None):
        if self.current_logger is not None:  # In accumulate_means context.
            assert self._subdir is not None
            raw_key = "/".join(["raw", *self._prefixes, self._name, key])
            self.current_logger.record(raw_key, val, exclude)

            mean_key = "/".join(["mean", *self._prefixes, self._name, key])
            self.default_logger.record_mean(mean_key, val, exclude)
        else:  # Not in accumulate_means context.
            self.default_logger.record(key, val, exclude)

    @property
    def _logger(self):
        if self.current_logger is not None:
            return self.current_logger
        else:
            return self.default_logger

    def dump(self, step=0):
        self._logger.dump(step)

    def get_dir(self) -> str:
        return self._logger.get_dir()

    def log(self, *args, **kwargs):
        self.default_logger.log(*args, **kwargs)

    def set_level(self, level: int) -> None:
        self.default_logger.set_level(level)

    def record_mean(self, key, val, exclude=None):
        self.default_logger.record_mean(key, val, exclude)

    def close(self):
        self.default_logger.close()
        for logger in self._cached_loggers.values():
            logger.close()


class WandbOutputFormat(sb_logger.KVWriter):
    """A stable-baseline logger that writes to wandb.

    Users need to call `wandb.init()` before initializing `WandbOutputFormat`.
    """

    def __init__(self):
        """Initializes an instance of WandbOutputFormat.

        Raises:
            ModuleNotFoundError: wandb is not installed.
        """
        try:
            import wandb
        except ModuleNotFoundError as e:
            raise ModuleNotFoundError(
                "Trying to log data with `WandbOutputFormat` "
                "but `wandb` not installed: try `pip install wandb`.",
            ) from e
        self.wandb_module = wandb

    def write(
        self,
        key_values: Dict[str, Any],
        key_excluded: Dict[str, Union[str, Tuple[str, ...]]],
        step: int = 0,
    ) -> None:
        for (key, value), (key_ex, excluded) in zip(
            sorted(key_values.items()),
            sorted(key_excluded.items()),
        ):
            assert key == key_ex, f"key mismatch between {key} and {key_ex}."
            if excluded is not None and "wandb" in excluded:
                continue

            self.wandb_module.log({key: value}, step=step)
        self.wandb_module.log({}, commit=True)

    def close(self) -> None:
        self.wandb_module.finish()


def configure(
    folder: Optional[types.AnyPath] = None,
    format_strs: Optional[Sequence[str]] = None,
) -> HierarchicalLogger:
    """Configure Stable Baselines logger to be `accumulate_means()`-compatible.

    After this function is called, `stable_baselines3.logger.{configure,reset}()`
    are replaced with stubs that raise RuntimeError.

    Args:
        folder: Argument from `stable_baselines3.logger.configure`.
        format_strs: An list of output format strings. For details on available
            output formats see `stable_baselines3.logger.make_output_format`.

    Returns:
        The configured HierarchicalLogger instance.
    """
    if folder is None:
        now = datetime.datetime.now()
        timestamp = now.strftime("imitation-%Y-%m-%d-%H-%M-%S-%f")
        folder = os.path.join(tempfile.gettempdir(), timestamp)
    else:
        folder = types.path_to_str(folder)
    if format_strs is None:
        format_strs = ["stdout", "log", "csv"]
    output_formats = _build_output_formats(folder, format_strs)
    default_logger = sb_logger.Logger(folder, list(output_formats))
    hier_format_strs = [f for f in format_strs if f != "wandb"]
    hier_logger = HierarchicalLogger(default_logger, hier_format_strs)
    return hier_logger
