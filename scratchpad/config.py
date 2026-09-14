"""``config.toml`` loading with defaults (ARCHITECTURE.md section 13).

The file is optional; every key is optional.  Unknown sections/keys and values
of the wrong type are ignored with a warning on stderr, never an exception: a
hand-edited config must not stop the scratchpad from starting.

The TOML file is grouped into sections but :class:`Config` is deliberately flat,
because the rest of the contract refers to ``config.flush_interval_ms``,
``config.heartbeat_seconds``, ``config.large_paste_threshold_lines`` and so on::

    [storage]                      [editor]                      [ui]
    flush_interval_ms = 500        font = "monospace 11"         width = 900
    checkpoint_every_events = 2000 large_paste_threshold_lines   height = 600
    heartbeat_seconds = 30         large_paste_threshold_chars
    keep_checkpoints = 20          continue_bullets = true

Every numeric value is clamped into the range that makes sense for it (see
:data:`_BOUNDS`) with a warning, so that ``checkpoint_every_events = 0`` or a
negative heartbeat interval cannot turn into a busy loop or a division by zero
somewhere in the storage core.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from scratchpad import paths

log = logging.getLogger(__name__)

#: TOML section -> {key: (attribute, type)}.  Also the authority for defaults.
_SCHEMA: dict[str, dict[str, type]] = {
    "storage": {
        "flush_interval_ms": int,
        "checkpoint_every_events": int,
        "heartbeat_seconds": int,
        "keep_checkpoints": int,
    },
    "editor": {
        "font": str,
        "large_paste_threshold_lines": int,
        "large_paste_threshold_chars": int,
        "continue_bullets": bool,
    },
    "ui": {
        "width": int,
        "height": int,
    },
}

#: attribute -> (minimum, maximum).  Values outside the range are clamped with a
#: warning instead of being rejected: a hand-edited config must still start.
_BOUNDS: dict[str, tuple[int, int | None]] = {
    "flush_interval_ms": (0, None),          # 0 means "fsync on every tick"
    "checkpoint_every_events": (1, None),
    "heartbeat_seconds": (1, None),
    "keep_checkpoints": (1, None),
    "large_paste_threshold_lines": (1, None),
    "large_paste_threshold_chars": (1, None),
    "width": (200, None),
    "height": (200, None),
}


@dataclass(frozen=True, slots=True)
class Config:
    """Effective configuration.  Immutable; use :func:`dataclasses.replace`."""

    # [storage]
    flush_interval_ms: int = 500
    checkpoint_every_events: int = 2000
    heartbeat_seconds: int = 30
    keep_checkpoints: int = 20
    # [editor]
    font: str = "monospace 11"
    large_paste_threshold_lines: int = 2000
    large_paste_threshold_chars: int = 200000
    continue_bullets: bool = True
    # [ui]
    width: int = 900
    height: int = 600
    # provenance; None when the defaults were used
    path: Path | None = None

    def __post_init__(self) -> None:
        """Clamp every numeric value into its documented range, loudly."""
        for name, (low, high) in _BOUNDS.items():
            value = getattr(self, name)
            clamped = value
            if clamped < low:
                clamped = low
            elif high is not None and clamped > high:
                clamped = high
            if clamped != value:
                log.warning(
                    "config: %s = %r is out of range; using %r", name, value, clamped
                )
                object.__setattr__(self, name, clamped)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """Load ``path`` (default: :func:`paths.config_file`), falling back to defaults.

        A missing file is not an error.  A malformed file logs a warning and
        yields the defaults.
        """
        target = Path(path) if path is not None else paths.config_file()
        try:
            raw = target.read_bytes()
        except FileNotFoundError:
            return cls()
        except OSError as exc:
            log.warning("config: cannot read %s: %s; using defaults", target, exc)
            return cls()
        try:
            document = tomllib.loads(raw.decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            log.warning("config: %s is not valid TOML (%s); using defaults", target, exc)
            return cls()
        return cls._from_document(document, target)

    @classmethod
    def _from_document(cls, document: dict[str, object], source: Path | None) -> "Config":
        values: dict[str, object] = {}
        for section, content in document.items():
            known = _SCHEMA.get(section)
            if known is None:
                log.warning("config: ignoring unknown section [%s] in %s", section, source)
                continue
            if not isinstance(content, dict):
                log.warning("config: [%s] in %s is not a table; ignored", section, source)
                continue
            for key, value in content.items():
                expected = known.get(key)
                if expected is None:
                    log.warning("config: ignoring unknown key %s.%s in %s", section, key, source)
                    continue
                # bool is a subclass of int; keep them apart.
                if expected is int and isinstance(value, bool):
                    ok = False
                else:
                    ok = isinstance(value, expected)
                if not ok:
                    log.warning(
                        "config: %s.%s should be %s, got %r; using default",
                        section, key, expected.__name__, value,
                    )
                    continue
                values[key] = value
        config = cls(**values)  # type: ignore[arg-type]
        return replace(config, path=source)

    def to_toml(self) -> str:
        """Render the current values as a config.toml document."""
        out: list[str] = []
        current = asdict(self)
        for section, keys in _SCHEMA.items():
            out.append(f"[{section}]")
            for key in keys:
                value = current[key]
                if isinstance(value, bool):
                    rendered = "true" if value else "false"
                elif isinstance(value, str):
                    rendered = '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
                else:
                    rendered = str(value)
                out.append(f"{key} = {rendered}")
            out.append("")
        return "\n".join(out)

    @property
    def flush_interval_ns(self) -> int:
        """``flush_interval_ms`` in nanoseconds (for monotonic deadlines)."""
        return self.flush_interval_ms * 1_000_000

    @property
    def heartbeat_ns(self) -> int:
        """``heartbeat_seconds`` in nanoseconds."""
        return self.heartbeat_seconds * 1_000_000_000


#: The effective defaults, as a value.
DEFAULTS = Config()
