import fcntl
import functools
import logging
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

# Same as voice_input: pystray's first choice here is AppIndicator; Gtk.StatusIcon sits in the
# old XEmbed tray, which Cinnamon still renders. Must precede the import.
os.environ.setdefault("PYSTRAY_BACKEND", "gtk")

import pystray  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
from Xlib import X, XK, display as xdisplay, error as xerror  # noqa: E402
from Xlib.ext import randr  # noqa: E402
from Xlib.protocol import event as xevent  # noqa: E402

import config  # noqa: E402

logging.basicConfig(level=logging.INFO)

_HOTKEY_MOD_MASKS = {"ctrl": X.ControlMask, "alt": X.Mod1Mask, "shift": X.ShiftMask,
                     "super": X.Mod4Mask}


def parse_hotkey(spec: str) -> tuple[int, int]:
	"""'ctrl+alt+shift+m' → (modifier mask, keysym). ValueError on anything it can't resolve."""
	*mods, key = [p.strip() for p in spec.split("+")]
	try:
		mask = functools.reduce(lambda m, name: m | _HOTKEY_MOD_MASKS[name.lower()], mods, 0)
	except KeyError as e:
		raise ValueError(f"unknown modifier {e} in {spec!r}") from None
	# "M" is a different keysym from "m" (shifted), but it's the same key to the user.
	keysym = XK.string_to_keysym(key.lower() if len(key) == 1 else key)
	if not keysym:
		raise ValueError(f"unknown key {key!r} in {spec!r}")
	return mask, keysym


def _configured_hotkey() -> tuple[int, int]:
	"""The hotkey from config.toml; the template's if that one doesn't parse. Read once at
	startup — that is when it is grabbed."""
	try:
		return parse_hotkey(config.hotkey())
	except ValueError as e:
		logging.error("Bad hotkey in config.toml (%s) — using %s", e, config.default_hotkey())
		return parse_hotkey(config.default_hotkey())


LOCK_FILE = Path.home() / ".cache" / "mouse_pointer_focuser" / "instance.lock"


def acquire_single_instance():
	"""The open lock file, or None if another copy already holds it. flock, not a pid file: the
	kernel drops the lock when its holder dies, crash included, so nothing is ever left stale.
	The fd is close-on-exec, so «Перезапустить» (execv) releases it for the new process."""
	LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
	f = open(LOCK_FILE, "w")
	try:
		fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
	except BlockingIOError:
		f.close()
		return None
	return f


AUTOSTART_DIR = Path.home() / ".config" / "autostart"
AUTOSTART_FILE = AUTOSTART_DIR / "mouse-pointer-focuser.desktop"


class Monitor:
	def __init__(self, name: str, x: int, y: int, width: int, height: int, primary: bool):
		self.name, self.x, self.y, self.width, self.height = name, x, y, width, height
		self.primary = primary

	@property
	def center(self) -> tuple[int, int]:
		return self.x + self.width // 2, self.y + self.height // 2

	def __str__(self):
		return f"{self.name} {self.width}×{self.height}{' (основной)' if self.primary else ''}"


def list_monitors(d: xdisplay.Display) -> list[Monitor]:
	"""Active monitors, left to right, then top to bottom — the order `monitor = N` counts in.
	Asked afresh every time, so a monitor plugged in or rearranged since startup just works."""
	root = d.screen().root
	try:
		reply = root.xrandr_get_monitors(is_active=True)
		mons = [Monitor(d.get_atom_name(m.name), m.x, m.y, m.width_in_pixels,
		                m.height_in_pixels, bool(m.primary))
		        for m in reply.monitors]
	except Exception:
		# No RandR 1.5 — treat the whole screen as one monitor rather than doing nothing.
		logging.exception("Cannot list monitors via RandR")
		mons = []
	if not mons:
		s = d.screen()
		mons = [Monitor("screen", 0, 0, s.width_in_pixels, s.height_in_pixels, True)]
	return sorted(mons, key=lambda m: (m.x, m.y))


def pick_monitor(mons: list[Monitor]) -> Monitor:
	"""monitor_name wins when such a monitor exists; otherwise the 1-based number, clamped."""
	name = config.monitor_name()
	if name:
		for m in mons:
			if m.name == name:
				return m
		logging.warning("No monitor named %r right now — using number %d", name, config.monitor())
	return mons[min(config.monitor(), len(mons)) - 1]


class Pointer:
	"""Moves the pointer and focuses what it lands on. A fresh X connection per jump: the
	hotkey thread and pystray's menu thread both call in, and the lock keeps jumps whole."""

	def __init__(self):
		self._lock = threading.Lock()

	def jump(self, target=None, timestamp: int = X.CurrentTime):
		"""target: a monitor name, or None for the one config.toml points at."""
		with self._lock:
			d = xdisplay.Display()
			try:
				mons = list_monitors(d)
				mon = next((m for m in mons if m.name == target), None) or pick_monitor(mons)
				x, y = mon.center
				d.screen().root.warp_pointer(x, y)
				d.sync()
				logging.info("Pointer → %s at (%d, %d)", mon, x, y)
				if config.focus_window():
					self._focus_under_pointer(d, timestamp)
			except Exception:
				logging.exception("Jump failed")
			finally:
				d.close()

	def _focus_under_pointer(self, d: xdisplay.Display, timestamp: int):
		"""Ask the window manager to activate the window under the pointer.

		Through _NET_ACTIVE_WINDOW, not XSetInputFocus: setting focus behind the WM's back leaves
		the window unraised and the WM's idea of the active window stale. Source 2 ("pager")
		tells it the request comes from the user, so focus-stealing prevention doesn't veto it.
		The timestamp is the hotkey press's own, for the same reason."""
		root = d.screen().root
		frame = root.query_pointer().child
		if not frame:
			return  # bare root: no window there
		client = self._find_client(d, frame)
		if client is None:
			logging.info("No managed window under the pointer")
			return
		ev = xevent.ClientMessage(
			window=client,
			client_type=d.intern_atom("_NET_ACTIVE_WINDOW"),
			data=(32, [2, timestamp, 0, 0, 0]),
		)
		root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
		d.sync()

	def _find_client(self, d: xdisplay.Display, w, depth: int = 0):
		"""The WM reparents every application window into a frame of its own, and query_pointer
		returns that frame. The application window is the descendant carrying WM_STATE."""
		wm_state = d.intern_atom("WM_STATE")
		try:
			if w.get_full_property(wm_state, X.AnyPropertyType) is not None:
				return w
			if depth >= 3:
				return None
			for child in reversed(w.query_tree().children):  # topmost first
				found = self._find_client(d, child, depth + 1)
				if found is not None:
					return found
		except xerror.XError:
			pass  # the window went away mid-walk
		return None


class HotkeyListener:
	def __init__(self, mods: int, keysym: int, callback):
		self._d = xdisplay.Display()
		self._root = self._d.screen().root
		self._mods = mods
		self._keycode = self._d.keysym_to_keycode(keysym)
		self._callback = callback
		self._running = False
		self._thread = threading.Thread(target=self._loop, daemon=True)

	# Grab with all ignored-modifier combos so NumLock/CapsLock don't break matching
	_IGNORED_MODS = [0, X.Mod2Mask, X.LockMask, X.Mod2Mask | X.LockMask]

	def start(self):
		for attempt in range(10):
			grab_failed = []
			old_handler = self._d.set_error_handler(
				lambda e, _: grab_failed.append(e)
			)
			for extra in self._IGNORED_MODS:
				self._root.grab_key(self._keycode, self._mods | extra, 0, X.GrabModeAsync, X.GrabModeAsync)
			self._d.sync()
			self._d.set_error_handler(old_handler)
			if not grab_failed:
				break
			logging.warning("grab_key BadAccess (attempt %d/10), retrying...", attempt + 1)
			time.sleep(0.3)
		else:
			raise RuntimeError("Failed to grab hotkey after 10 attempts")
		self._running = True
		self._thread.start()

	def stop(self):
		self._running = False
		for extra in self._IGNORED_MODS:
			self._root.ungrab_key(self._keycode, self._mods | extra)
		self._d.flush()

	def _loop(self):
		fd = self._d.fileno()
		while self._running:
			rlist, _, _ = select.select([fd], [], [], 0.1)
			if not rlist:
				continue
			while self._d.pending_events():
				ev = self._d.next_event()
				if ev.type == X.KeyPress and ev.detail == self._keycode:
					threading.Thread(target=self._callback, args=(ev.time,), daemon=True).start()


class MonitorWatcher:
	"""Calls back whenever a monitor is plugged, unplugged or rearranged. The hotkey reads the
	monitors afresh anyway; this is for the «Мониторы» submenu, which pystray only re-evaluates
	when told to rebuild the menu."""

	def __init__(self, callback):
		self._d = xdisplay.Display()
		self._callback = callback
		self._d.screen().root.xrandr_select_input(
			randr.RRScreenChangeNotifyMask | randr.RRCrtcChangeNotifyMask
			| randr.RROutputChangeNotifyMask)
		self._d.flush()
		threading.Thread(target=self._loop, daemon=True).start()

	# X delivers MappingNotify to every client unasked — each keyboard layout switch sends a pair.
	# Only these mean the monitors changed.
	_RANDR_EVENTS = (randr.ScreenChangeNotify, randr.CrtcChangeNotify, randr.OutputChangeNotify)

	def _loop(self):
		while True:
			if not isinstance(self._d.next_event(), self._RANDR_EVENTS):
				continue
			# One hotplug arrives as a burst of events: wait for it to settle, swallow the rest.
			time.sleep(0.5)
			while self._d.pending_events():
				self._d.next_event()
			try:
				self._callback()
			except Exception:
				logging.exception("Monitor change handling failed")


def make_icon(size: int = 64) -> Image.Image:
	"""A crosshair in a ring — drawn here rather than shipped, there's only the one state."""
	img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
	draw = ImageDraw.Draw(img)
	c, w = size // 2, max(2, size // 12)
	color = (80, 170, 255, 255)
	draw.ellipse((w, w, size - w - 1, size - w - 1), outline=color, width=w)
	draw.line((c, 0, c, size * 3 // 8), fill=color, width=w)
	draw.line((c, size * 5 // 8, c, size), fill=color, width=w)
	draw.line((0, c, size * 3 // 8, c), fill=color, width=w)
	draw.line((size * 5 // 8, c, size, c), fill=color, width=w)
	draw.ellipse((c - w, c - w, c + w, c + w), fill=color)
	return img


class TrayApp:
	def __init__(self):
		self._hotkey_spec = config.hotkey()
		self._pointer = Pointer()
		self._listener = HotkeyListener(*_configured_hotkey(), self._on_hotkey)
		self.icon = None

	def _on_hotkey(self, timestamp: int):
		self._jump(timestamp=timestamp)

	def _jump(self, name: str | None = None, timestamp: int = X.CurrentTime):
		self._pointer.jump(name, timestamp)
		# pystray evaluates the submenu only when the menu is rebuilt; a config edit since the
		# last one would leave the «Мониторы» checkmark on the old monitor.
		if self.icon is not None:
			self.icon.update_menu()

	def _on_monitors_changed(self):
		logging.info("Monitors changed")
		if self.icon is not None:
			self.icon.update_menu()

	def _jump_to_configured(self, icon, item):
		# Off pystray's callback thread: the jump talks to X and would hold the menu up.
		threading.Thread(target=self._jump, daemon=True).start()

	def _jump_to_monitor(self, icon, item, name: str):
		threading.Thread(target=self._jump, args=(name,), daemon=True).start()

	def _monitor_items(self):
		"""Called whenever pystray rebuilds the menu, so it shows the monitors there are then."""
		d = xdisplay.Display()
		try:
			mons = list_monitors(d)
		finally:
			d.close()
		try:
			current = pick_monitor(mons).name
		except Exception:
			current = None
		return [
			pystray.MenuItem(
				f"{i}. {m}",
				functools.partial(self._jump_to_monitor, name=m.name),
				checked=lambda item, name=m.name: name == current,
				radio=True,
			)
			for i, m in enumerate(mons, 1)
		]

	def _notify(self, title: str, body: str):
		try:
			subprocess.Popen(["notify-send", "-a", "mouse_pointer_focuser", title, body],
			                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
		except OSError as e:
			logging.error("Cannot show a notification: %s", e)

	def _edit_settings(self, icon, item):
		"""Open config.toml in whatever the desktop uses for text files. Every setting but the
		hotkey is re-read on the next press, so there is nothing to save or apply here."""
		try:
			path = config.ensure_file()
		except OSError as e:
			logging.error("Cannot create %s: %s", config.CONFIG_PATH, e)
			return
		# Popen, not run(): blocking here would freeze the tray menu while the editor is open.
		try:
			subprocess.Popen(["xdg-open", str(path)],
			                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
		except OSError as e:
			logging.error("Cannot open %s in an editor: %s", path, e)

	def _autostart_enabled(self, item=None) -> bool:
		return AUTOSTART_FILE.exists()

	def _toggle_autostart(self, icon, item):
		if AUTOSTART_FILE.exists():
			AUTOSTART_FILE.unlink(missing_ok=True)
		else:
			app_path = Path(__file__).resolve()
			content = (
				"[Desktop Entry]\n"
				"Type=Application\n"
				"Name=Mouse Pointer Focuser\n"
				f"Exec={sys.executable} {app_path}\n"
				f"Path={app_path.parent}\n"
				"X-GNOME-Autostart-enabled=true\n"
			)
			AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
			AUTOSTART_FILE.write_text(content)
		icon.update_menu()

	def _bind_left_click_to_menu(self):
		"""Left click opens the menu too. pystray runs the default menu item on it, but the jump
		belongs to the keyboard — there is no default item, so `activate` would do nothing.
		pystray exposes no click callback; we hook the Gtk.StatusIcon it built (as voice_input
		does) and hand the click to its own right-click handler."""
		status_icon = getattr(self.icon, "_status_icon", None)
		if status_icon is None:
			logging.warning("Tray backend exposes no status icon — menu on right click only")
			return
		status_icon.connect("activate", lambda si: self.icon._on_status_icon_popup_menu(si, 1, 0))

	def _restart(self, icon, item):
		"""Pick up a changed hotkey: it is grabbed once at startup. The grab must be released
		first, or the new process would find its own old grab in the way."""
		self._listener.stop()
		icon.stop()
		os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve())])

	def _quit(self, icon, item):
		self._listener.stop()
		icon.stop()

	def run(self):
		try:
			self._listener.start()
		except RuntimeError:
			# Another program (or a second copy of this one) holds the combination. Stay in the
			# tray anyway: the menu still jumps, and «Настройки» is how the user fixes it.
			logging.error("Hotkey %s is taken by another program", self._hotkey_spec)
			self._notify("Сочетание занято",
			             f"{self._hotkey_spec} уже захвачено другой программой. Поменяйте hotkey "
			             "в настройках и перезапустите.")
		menu = pystray.Menu(
			pystray.MenuItem(f"Курсор на монитор ({self._hotkey_spec})", self._jump_to_configured),
			pystray.MenuItem("Мониторы", pystray.Menu(self._monitor_items)),
			pystray.MenuItem("Настройки", self._edit_settings),
			pystray.MenuItem(
				"Запускать при входе",
				self._toggle_autostart,
				checked=self._autostart_enabled,
			),
			pystray.MenuItem("Перезапустить", self._restart),
			pystray.MenuItem("Выход", self._quit),
		)
		self.icon = pystray.Icon("mouse_pointer_focuser", make_icon(),
		                         f"Mouse Pointer Focuser — {self._hotkey_spec}", menu)
		self._bind_left_click_to_menu()
		try:
			MonitorWatcher(self._on_monitors_changed)
		except Exception:
			# Not fatal: the hotkey still finds the current monitors, only the submenu may lag.
			logging.exception("Cannot watch for monitor changes")
		self.icon.run()


if __name__ == "__main__":
	_lock = acquire_single_instance()  # held for the life of the process
	if _lock is None:
		logging.info("Already running — exiting")
		subprocess.run(["notify-send", "-a", "mouse_pointer_focuser", "Уже запущено",
		                "Mouse Pointer Focuser уже работает — значок в трее."], check=False)
		sys.exit(0)
	TrayApp().run()
