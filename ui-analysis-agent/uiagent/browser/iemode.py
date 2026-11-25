"""Engine 'ie': Microsoft Edge in Internet Explorer mode, driven by Selenium's IEDriverServer (Windows only)."""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path

from uiagent.browser import OBSERVE_JS, Browser, split_element_id

log = logging.getLogger("uiagent.ie")


def _quiet(fn) -> None:
    try:
        fn()
    except Exception:  # noqa: BLE001 - best-effort shutdown
        pass


def _try(fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - reported by the caller
        return exc


# Edge trees started by IEDriverServer use a temporary IEDriver-<guid> profile; the user's own Edge never matches.
_SWEEP = ("$live = @(Get-Process IEDriverServer -ErrorAction SilentlyContinue | ForEach-Object Id); "
          "$all = @(Get-Process | ForEach-Object Id); "
          "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | Where-Object { "
          "$_.CommandLine -match 'IEDriver-[0-9a-fA-F-]{36}' -and $_.CommandLine -notmatch '--type=' -and "
          "$live -notcontains $_.ParentProcessId } | ForEach-Object { taskkill /PID $_.ProcessId /T /F | Out-Null; $_.ProcessId }; "
          "Get-CimInstance Win32_Process -Filter \"Name='iexplore.exe'\" | Where-Object { "
          "$_.CommandLine -match 'dualengineadapter' -and $all -notcontains $_.ParentProcessId } | "
          "ForEach-Object { taskkill /PID $_.ProcessId /F | Out-Null; $_.ProcessId }")


def _sweep_orphans() -> int:
    """Stop IE-mode Edge trees whose IEDriverServer has exited; they make the next session start stall."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _SWEEP],
                             capture_output=True, text=True, timeout=60, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return 0
    return sum(1 for line in out.splitlines() if line.strip().isdigit())


class IEModeBrowser(Browser):
    engine = "ie"

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.driver = None
        self.main_handle: str | None = None
        self.dialogs: list[dict] = []

    def start(self) -> None:
        from selenium import webdriver
        from selenium.webdriver.common.proxy import Proxy, ProxyType
        from selenium.webdriver.ie.options import Options
        from selenium.webdriver.ie.service import Service

        opts = Options()
        opts.attach_to_edge_chrome = True
        opts.edge_executable_path = self.cfg["edge_path"]
        opts.ignore_zoom_level = True
        opts.native_events = False
        opts.page_load_strategy = "normal"
        opts.set_capability("unhandledPromptBehavior", "ignore")
        if self.cfg.get("initial_url"):
            opts.initial_browser_url = self.cfg["initial_url"]
        # With system settings, WinINet proxy auto-detection stalled session start for ~2 minutes.
        proxy = self.cfg.get("proxy") or "direct"
        if proxy != "system":
            opts.use_per_process_proxy = True
            opts.proxy = Proxy({"proxyType": ProxyType.DIRECT} if proxy == "direct" else
                               {"proxyType": ProxyType.MANUAL, "httpProxy": proxy, "sslProxy": proxy})
        if self.cfg.get("ignore_protected_mode_settings"):
            opts.ignore_protected_mode_settings = True
        args = ["--log-level=DEBUG", f"--log-file={self.cfg['iedriver_log']}"] if self.cfg.get("iedriver_log") else None
        timeout = int(self.cfg.get("start_timeout_s", 60))
        for attempt in (1, 2):
            swept = _sweep_orphans()
            if swept:
                log.info("stopped %d orphaned IE-mode Edge windows from earlier sessions", swept)
            service = Service(executable_path=self.cfg["iedriver_path"], service_args=args) if self.cfg.get("iedriver_path") else Service(service_args=args)
            box: dict = {}
            starter = threading.Thread(target=lambda: box.update(d=_try(lambda: webdriver.Ie(service=service, options=opts))), daemon=True)
            starter.start()
            starter.join(timeout)
            driver = box.get("d")
            if driver is not None and not isinstance(driver, Exception):
                self.driver = driver
                break
            reason = str(driver)[:200] if driver is not None else f"no IE-mode session after {timeout}s"
            proc = getattr(service, "process", None)  # Selenium 4.4x+ does not set it when the driver failed to start
            if proc:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
            _sweep_orphans()
            if attempt == 2:
                raise RuntimeError(f"Edge IE mode did not start: {reason}")
            log.warning("IE-mode start failed (%s); retrying once", reason)
        self.driver.set_page_load_timeout(int(self.cfg.get("page_timeout_s", 60)))
        w, h = self.cfg.get("window", [1600, 1000])
        try:
            self.driver.set_window_size(w, h)
        except Exception:  # noqa: BLE001 - some IE-mode windows refuse resizing
            pass
        self.main_handle = self.driver.current_window_handle

    def stop(self) -> None:
        if not self.driver:
            return
        driver, self.driver = self.driver, None
        pid = getattr(getattr(driver, "service", None), "process", None)
        pid = pid.pid if pid else None
        # quit() can block for minutes on a stuck IE-mode tab; leftover IE-mode processes then stall the next session start.
        closer = threading.Thread(target=lambda: _quiet(driver.quit), daemon=True)
        closer.start()
        closer.join(20)
        if pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)

    # ------------------------------------------------------------ navigation & dialogs
    def goto(self, url: str) -> None:
        self.driver.switch_to.default_content()
        self.driver.get(url)
        self._after_action()

    def current_url(self) -> str:
        try:
            return self.driver.current_url
        except Exception:  # noqa: BLE001
            return ""

    def back(self) -> None:
        if self.close_popup():
            return
        self.driver.switch_to.default_content()
        self.driver.back()
        self._after_action()

    def _after_action(self) -> None:
        self._drain_dialogs()
        deadline = time.monotonic() + int(self.cfg.get("page_timeout_s", 60))
        while time.monotonic() < deadline:
            try:
                if self.driver.execute_script("return document.readyState") == "complete":
                    break
            except Exception:  # noqa: BLE001 - dialog or navigation in progress
                self._drain_dialogs()
            time.sleep(0.3)
        self._drain_dialogs()

    def _drain_dialogs(self) -> None:
        from selenium.common.exceptions import NoAlertPresentException

        for _ in range(5):
            try:
                alert = self.driver.switch_to.alert
                text = alert.text
            except NoAlertPresentException:
                return
            except Exception:  # noqa: BLE001
                return
            accept = not self.cfg.get("_dismiss_confirm", True)
            self.dialogs.append({"text": text, "accepted": accept, "url": self.current_url()})
            try:
                alert.accept() if accept else alert.dismiss()
            except Exception:  # noqa: BLE001 - plain alerts may only allow accept
                alert.accept()

    # ------------------------------------------------------------ frames & observation
    def _switch(self, path: list[int]) -> bool:
        self.driver.switch_to.default_content()
        try:
            for idx in path:
                self.driver.switch_to.frame(idx)
            return True
        except Exception:  # noqa: BLE001
            self.driver.switch_to.default_content()
            return False

    def frame_count(self, path: list[int]) -> int:
        if not self._switch(path):
            return 0
        try:
            return int(self.driver.execute_script("return window.frames ? window.frames.length : 0;") or 0)
        except Exception:  # noqa: BLE001
            return 0
        finally:
            self.driver.switch_to.default_content()

    def observe_frame(self, path: list[int]) -> dict | None:
        return self.eval_frame(path, OBSERVE_JS, self.frame_prefix(path))

    def eval_frame(self, path: list[int], source: str, arg) -> dict | None:
        if not self._switch(path):
            return None
        try:
            return self.driver.execute_script("return (" + source + ")(arguments[0]);", arg)
        except Exception as exc:  # noqa: BLE001 - cross-domain or unloaded frames
            log.debug("script in frame %s failed: %s", path, exc)
            return None
        finally:
            self.driver.switch_to.default_content()

    # ------------------------------------------------------------ actions
    def _element(self, element_id: str):
        from selenium.webdriver.common.by import By

        path, _ = split_element_id(element_id)
        if not self._switch(path):
            raise LookupError(f"frame for {element_id} is gone")
        return self.driver.find_element(By.XPATH, f"//*[@data-uia='{element_id}']")

    def click(self, element_id: str) -> None:
        el = self._element(element_id)
        try:
            el.click()
        except Exception:  # noqa: BLE001 - fall back to a script click for elements IE reports as not interactable
            self.driver.execute_script("arguments[0].click();", el)
        self.driver.switch_to.default_content()
        self._after_action()

    def fill(self, element_id: str, value: str) -> None:
        el = self._element(element_id)
        el.clear()
        el.send_keys(value)
        self.driver.switch_to.default_content()

    def select(self, element_id: str, value: str) -> None:
        from selenium.webdriver.support.ui import Select

        sel = Select(self._element(element_id))
        try:
            sel.select_by_value(value)
        except Exception:  # noqa: BLE001
            sel.select_by_visible_text(value)
        self.driver.switch_to.default_content()

    def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.driver.switch_to.default_content()
        self.driver.save_screenshot(str(path))

    def session_ids(self) -> list[str]:
        try:
            return [c["value"] for c in self.driver.get_cookies() if "session" in c["name"].lower() or c["name"].upper() == "JSESSIONID"]
        except Exception:  # noqa: BLE001
            return []

    # ------------------------------------------------------------ popups
    def handle_popups(self) -> str | None:
        handles = self.driver.window_handles
        new = [h for h in handles if h != self.main_handle and h != self.driver.current_window_handle]
        if not new:
            return None
        self.driver.switch_to.window(new[-1])
        self._after_action()
        return self.current_url()

    def close_popup(self) -> bool:
        if self.driver.current_window_handle == self.main_handle:
            return False
        self.driver.close()
        self.driver.switch_to.window(self.main_handle)
        return True
