"""Engine 'edge': modern Microsoft Edge (Chromium) via Playwright. Full-page screenshots, headless optional."""
from __future__ import annotations

from pathlib import Path

from uiagent.browser import OBSERVE_JS, Browser, split_element_id


class EdgeBrowser(Browser):
    engine = "edge"

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.dialogs: list[dict] = []
        self._pw = self._browser = self._context = None
        self.page = None
        self.main_page = None

    def start(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(channel="msedge", headless=bool(self.cfg.get("headless")))
        w, h = self.cfg.get("window", [1600, 1000])
        self._context = self._browser.new_context(viewport={"width": w, "height": h})
        self._context.set_default_timeout(int(self.cfg.get("page_timeout_s", 60)) * 1000)
        self.page = self.main_page = self._context.new_page()
        self._context.on("page", self._on_page)
        self.page.on("dialog", self._on_dialog)
        self._popups: list = []

    def _on_page(self, page) -> None:
        page.on("dialog", self._on_dialog)
        self._popups.append(page)

    def _on_dialog(self, dialog) -> None:
        accept = not self.cfg.get("_dismiss_confirm", True) or dialog.type == "alert"
        self.dialogs.append({"text": dialog.message, "accepted": accept, "url": self.current_url()})
        dialog.accept() if accept else dialog.dismiss()

    def stop(self) -> None:
        for obj in (self._context, self._browser):
            try:
                obj and obj.close()
            except Exception:  # noqa: BLE001
                pass
        if self._pw:
            self._pw.stop()

    def goto(self, url: str) -> None:
        self.page.goto(url, wait_until="load")

    def current_url(self) -> str:
        return self.page.url if self.page else ""

    def back(self) -> None:
        if self.close_popup():
            return
        self.page.go_back(wait_until="load")

    def _frame(self, path: list[int]):
        frame = self.page.main_frame
        for idx in path:
            children = frame.child_frames
            if idx >= len(children):
                return None
            frame = children[idx]
        return frame

    def frame_count(self, path: list[int]) -> int:
        frame = self._frame(path)
        return len(frame.child_frames) if frame else 0

    def observe_frame(self, path: list[int]) -> dict | None:
        return self.eval_frame(path, OBSERVE_JS, self.frame_prefix(path))

    def eval_frame(self, path: list[int], source: str, arg) -> dict | None:
        frame = self._frame(path)
        if frame is None:
            return None
        try:
            return frame.evaluate(source, arg)
        except Exception:  # noqa: BLE001 - detached or cross-origin frame
            return None

    def _locator(self, element_id: str):
        path, _ = split_element_id(element_id)
        frame = self._frame(path)
        if frame is None:
            raise LookupError(f"frame for {element_id} is gone")
        return frame.locator(f"[data-uia='{element_id}']").first

    def click(self, element_id: str) -> None:
        loc = self._locator(element_id)
        try:
            loc.click(timeout=15000)
        except Exception:  # noqa: BLE001
            loc.evaluate("e => e.click()")
        try:
            self.page.wait_for_load_state("load", timeout=int(self.cfg.get("page_timeout_s", 60)) * 1000)
        except Exception:  # noqa: BLE001
            pass

    def fill(self, element_id: str, value: str) -> None:
        self._locator(element_id).fill(value)

    def select(self, element_id: str, value: str) -> None:
        loc = self._locator(element_id)
        try:
            loc.select_option(value=value)
        except Exception:  # noqa: BLE001
            loc.select_option(label=value)

    def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), full_page=True)

    def session_ids(self) -> list[str]:
        return [c["value"] for c in self._context.cookies() if "session" in c["name"].lower() or c["name"].upper() == "JSESSIONID"]

    def handle_popups(self) -> str | None:
        if not self._popups:
            return None
        self.page = self._popups.pop()
        self.page.wait_for_load_state("load")
        return self.page.url

    def close_popup(self) -> bool:
        if self.page is self.main_page:
            return False
        self.page.close()
        self.page = self.main_page
        return True
