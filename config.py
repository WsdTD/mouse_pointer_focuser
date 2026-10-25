"""All user settings: config.toml, layered over config.template.toml.

The template is the single source of defaults — there are no default values in code. A key
missing from config.toml falls back to the template's, so the user's file can hold just what
they changed, and a fresh install (no config.toml at all) behaves exactly like the template.

Everything here is re-read on every call: a TOML file this size parses in well under a
millisecond, and re-reading is what lets an edit take effect on the next hotkey press without a
restart. The hotkey itself is grabbed at startup, so it is only read then.
"""
import logging
import shutil
import tomllib
from pathlib import Path

logger = logging.getLogger(__name__)

_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = _DIR / "config.template.toml"
# Next to app.py, not in ~/.config: the app is run straight out of its source directory (the
# autostart entry points at it), so the file is where the user already looks. Untracked.
CONFIG_PATH = _DIR / "config.toml"


def _template() -> dict:
	with open(TEMPLATE_PATH, "rb") as f:
		return tomllib.load(f)


def _user() -> dict:
	"""config.toml, or {} if it is missing or broken. Broken/unreadable → the whole template
	plus a warning: a typo should not silently half-apply."""
	try:
		with open(CONFIG_PATH, "rb") as f:
			return tomllib.load(f)
	except FileNotFoundError:
		return {}
	except (OSError, tomllib.TOMLDecodeError) as e:
		logger.warning("Cannot use %s (%s) — using the template's settings", CONFIG_PATH, e)
		return {}


def load() -> dict:
	"""The effective settings. A value whose type differs from the template's is dropped with a
	warning — the template's type is the schema."""
	merged = _template()
	for key, value in _user().items():
		merged[key] = _checked(key, value, merged.get(key))
	return merged


def _checked(name: str, value, default):
	if default is None or type(value) is type(default):
		return value
	logger.warning("config.toml: %s should be %s, got %r — using %r",
	               name, type(default).__name__, value, default)
	return default


def ensure_file() -> Path:
	"""Create config.toml as a copy of the template if it isn't there yet, so the menu has
	something to open — with every setting and its explanation already in it."""
	if not CONFIG_PATH.exists():
		shutil.copyfile(TEMPLATE_PATH, CONFIG_PATH)
	return CONFIG_PATH


def hotkey() -> str:
	return load()["hotkey"]


def default_hotkey() -> str:
	return _template()["hotkey"]


def monitor() -> int:
	"""1-based, left to right."""
	return max(1, load()["monitor"])


def monitor_name() -> str:
	return load()["monitor_name"].strip()


def focus_window() -> bool:
	return load()["focus_window"]
