"""Native Windows dashboard for the J.A.R.V.I.S. voice assistant.

The dashboard deliberately uses only Tkinter plus the project's existing
dependencies. It controls the background listener, launches only discovered
local applications, runs safe automations, and presents bounded runtime logs.
"""

from __future__ import annotations

import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import font as tkfont
from typing import Any, Optional

import psutil


class Palette:
    VOID = "#05090D"
    SURFACE = "#091218"
    SURFACE_2 = "#0D1A21"
    SURFACE_3 = "#10252D"
    LINE = "#1B3A43"
    CYAN = "#53E6FF"
    CYAN_SOFT = "#1C8192"
    MINT = "#73F7C8"
    AMBER = "#FFCE6A"
    RED = "#FF7B7B"
    TEXT = "#EAFBFF"
    MUTED = "#86A5AE"
    DIM = "#4C6971"


class JarvisDesktopApp:
    """Thread-safe native control surface around the existing backend module."""

    def __init__(self, root: tk.Tk, backend: Any) -> None:
        self.root = root
        self.backend = backend
        self.workspace = Path(backend.__file__).resolve().parent
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.apps: dict[str, str] = {}
        self.filtered_apps: list[str] = []
        self.process_items: list[dict[str, Any]] = []
        self.jarvis = backend.JarvisAutomation()
        self.process_cleaner = self.jarvis.cleaner
        self.active_view = "overview"
        self.engine_state = "offline"
        self.voice_state = "idle"
        self.animation_phase = 0.0
        self._last_log_signature = ""
        self._busy = False
        self._process_busy = False

        self.colors = Palette()
        self._configure_window()
        self._configure_fonts()
        self._build_shell()
        self._show_view("overview")
        self._bind_shortcuts()

        self.root.after(80, self._drain_events)
        self.root.after(120, self._animate_core)
        self.root.after(250, self._load_initial_state)
        self.root.after(900, self._refresh_metrics)
        self.root.after(1500, self._poll_engine)
        self.root.after(1800, self._refresh_activity)

    def _configure_window(self) -> None:
        self.root.title("J.A.R.V.I.S. — Command Interface")
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = min(1240, max(1000, screen_width - 96))
        height = min(760, max(640, screen_height - 96))
        left = max(0, (screen_width - width) // 2)
        top = max(0, (screen_height - height) // 2 - 8)
        self.root.geometry(f"{width}x{height}+{left}+{top}")
        self.root.minsize(1000, 640)
        self.root.configure(bg=self.colors.VOID)
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        try:
            self.root.tk.call("tk", "scaling", 1.15)
        except tk.TclError:
            pass

    def _configure_fonts(self) -> None:
        families = set(tkfont.families(self.root))
        self.ui_family = (
            "Segoe UI Variable Display"
            if "Segoe UI Variable Display" in families
            else "Segoe UI"
        )
        self.mono_family = "Cascadia Mono" if "Cascadia Mono" in families else "Consolas"
        self.font_display = (self.ui_family, 27, "bold")
        self.font_title = (self.ui_family, 17, "bold")
        self.font_body = (self.ui_family, 11)
        self.font_small = (self.ui_family, 9)
        self.font_label = (self.mono_family, 9, "bold")
        self.font_mono = (self.mono_family, 10)

    def _build_shell(self) -> None:
        self.shell = tk.Frame(self.root, bg=self.colors.VOID)
        self.shell.pack(fill="both", expand=True)
        self.shell.grid_columnconfigure(1, weight=1)
        self.shell.grid_rowconfigure(0, weight=1)

        self._build_navigation()
        self.stage = tk.Frame(self.shell, bg=self.colors.VOID)
        self.stage.grid(row=0, column=1, sticky="nsew", padx=(8, 20), pady=18)
        self.stage.grid_rowconfigure(1, weight=1)
        self.stage.grid_columnconfigure(0, weight=1)
        self._build_header()

        self.view_host = tk.Frame(self.stage, bg=self.colors.VOID)
        self.view_host.grid(row=1, column=0, sticky="nsew", pady=(16, 0))
        self.view_host.grid_rowconfigure(0, weight=1)
        self.view_host.grid_columnconfigure(0, weight=1)

        self.views: dict[str, tk.Frame] = {}
        self._build_overview()
        self._build_applications()
        self._build_processes()
        self._build_activity()
        self._build_settings()

    def _build_navigation(self) -> None:
        nav = tk.Frame(self.shell, bg=self.colors.SURFACE, width=210)
        nav.grid(row=0, column=0, sticky="nsw", padx=(18, 0), pady=18)
        nav.grid_propagate(False)

        mark = tk.Canvas(nav, width=72, height=72, bg=self.colors.SURFACE, highlightthickness=0)
        mark.pack(pady=(24, 8))
        mark.create_oval(8, 8, 64, 64, outline=self.colors.CYAN_SOFT, width=2)
        mark.create_arc(2, 2, 70, 70, start=18, extent=118, style="arc", outline=self.colors.CYAN, width=2)
        mark.create_text(36, 36, text="J", fill=self.colors.TEXT, font=(self.ui_family, 25, "bold"))
        tk.Label(nav, text="J.A.R.V.I.S.", bg=self.colors.SURFACE, fg=self.colors.TEXT, font=self.font_title).pack()
        tk.Label(nav, text="LOCAL COMMAND SYSTEM", bg=self.colors.SURFACE, fg=self.colors.CYAN, font=self.font_label).pack(pady=(2, 24))

        self.nav_buttons: dict[str, tk.Button] = {}
        items = [
            ("overview", "⌂  Overview"),
            ("applications", "▦  Applications"),
            ("processes", "◎  Processes"),
            ("activity", "≋  Activity"),
            ("settings", "⚙  Settings"),
        ]
        for key, label in items:
            button = tk.Button(
                nav,
                text=label,
                command=lambda item=key: self._show_view(item),
                anchor="w",
                padx=20,
                pady=11,
                bd=0,
                relief="flat",
                bg=self.colors.SURFACE,
                fg=self.colors.MUTED,
                activebackground=self.colors.SURFACE_3,
                activeforeground=self.colors.TEXT,
                font=self.font_body,
                cursor="hand2",
            )
            button.pack(fill="x", padx=12, pady=3)
            self.nav_buttons[key] = button

        footer = tk.Frame(nav, bg=self.colors.SURFACE)
        footer.pack(side="bottom", fill="x", padx=18, pady=18)
        self.nav_status_dot = tk.Label(footer, text="●", bg=self.colors.SURFACE, fg=self.colors.DIM, font=(self.ui_family, 13))
        self.nav_status_dot.pack(side="left")
        self.nav_status_text = tk.Label(footer, text="ENGINE OFFLINE", bg=self.colors.SURFACE, fg=self.colors.MUTED, font=self.font_label)
        self.nav_status_text.pack(side="left", padx=(7, 0))

    def _build_header(self) -> None:
        header = tk.Frame(self.stage, bg=self.colors.VOID)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        heading = tk.Frame(header, bg=self.colors.VOID)
        heading.grid(row=0, column=0, sticky="w")
        self.page_kicker = tk.Label(heading, text="SYSTEM / OVERVIEW", bg=self.colors.VOID, fg=self.colors.CYAN, font=self.font_label)
        self.page_kicker.pack(anchor="w")
        self.page_title = tk.Label(heading, text="Good morning, Archit.", bg=self.colors.VOID, fg=self.colors.TEXT, font=self.font_display)
        self.page_title.pack(anchor="w", pady=(3, 0))

        controls = tk.Frame(header, bg=self.colors.VOID)
        controls.grid(row=0, column=1, sticky="e")
        self.engine_badge = tk.Label(controls, text="●  OFFLINE", bg=self.colors.SURFACE_2, fg=self.colors.MUTED, font=self.font_label, padx=15, pady=10)
        self.engine_badge.pack(side="left", padx=(0, 10))
        self.engine_button = self._button(controls, "START JARVIS", self._toggle_engine, accent=True)
        self.engine_button.pack(side="left")

    def _button(self, parent: tk.Misc, text: str, command: Any, *, accent: bool = False, compact: bool = False) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bd=0,
            relief="flat",
            bg=self.colors.CYAN if accent else self.colors.SURFACE_3,
            fg=self.colors.VOID if accent else self.colors.TEXT,
            activebackground=self.colors.MINT if accent else self.colors.LINE,
            activeforeground=self.colors.VOID if accent else self.colors.TEXT,
            font=self.font_label,
            padx=14 if compact else 18,
            pady=8 if compact else 11,
            cursor="hand2",
            disabledforeground=self.colors.DIM,
        )

    def _panel(self, parent: tk.Misc, *, bg: Optional[str] = None) -> tk.Frame:
        return tk.Frame(
            parent,
            bg=bg or self.colors.SURFACE,
            highlightbackground=self.colors.LINE,
            highlightthickness=1,
            bd=0,
        )

    def _build_overview(self) -> None:
        view = tk.Frame(self.view_host, bg=self.colors.VOID)
        self.views["overview"] = view
        view.grid_columnconfigure(0, weight=5)
        view.grid_columnconfigure(1, weight=3)
        view.grid_rowconfigure(0, weight=1)

        core_panel = self._panel(view)
        core_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        core_panel.grid_rowconfigure(1, weight=1)
        core_panel.grid_columnconfigure(0, weight=1)
        cap = tk.Frame(core_panel, bg=self.colors.SURFACE)
        cap.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 0))
        tk.Label(cap, text="VOICE CORE", bg=self.colors.SURFACE, fg=self.colors.CYAN, font=self.font_label).pack(side="left")
        self.core_hint = tk.Label(cap, text="Say “Jarvis, open…”", bg=self.colors.SURFACE, fg=self.colors.MUTED, font=self.font_small)
        self.core_hint.pack(side="right")

        self.core_canvas = tk.Canvas(core_panel, bg=self.colors.SURFACE, highlightthickness=0, height=280)
        self.core_canvas.grid(row=1, column=0, sticky="nsew", padx=12)

        command = tk.Frame(core_panel, bg=self.colors.SURFACE_2, highlightbackground=self.colors.LINE, highlightthickness=1)
        command.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 20))
        tk.Label(command, text="⌘", bg=self.colors.SURFACE_2, fg=self.colors.CYAN, font=(self.ui_family, 17)).pack(side="left", padx=(14, 8))
        self.command_var = tk.StringVar()
        self.command_entry = tk.Entry(
            command,
            textvariable=self.command_var,
            bg=self.colors.SURFACE_2,
            fg=self.colors.TEXT,
            insertbackground=self.colors.CYAN,
            selectbackground=self.colors.CYAN_SOFT,
            relief="flat",
            bd=0,
            font=self.font_body,
        )
        self.command_entry.pack(side="left", fill="x", expand=True, ipady=14)
        self.command_entry.insert(0, "")
        self.command_entry.bind("<Return>", lambda _event: self._submit_command())
        self.command_entry.bind("<Escape>", lambda _event: self.command_var.set(""))
        self.send_button = self._button(command, "EXECUTE", self._submit_command, accent=True, compact=True)
        self.send_button.pack(side="right", padx=8, pady=8)

        side = tk.Frame(view, bg=self.colors.VOID)
        side.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        side.grid_columnconfigure(0, weight=1)
        side.grid_rowconfigure(2, weight=1)

        status = self._panel(side)
        status.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        tk.Label(status, text="SYSTEM VITALS", bg=self.colors.SURFACE, fg=self.colors.CYAN, font=self.font_label).pack(anchor="w", padx=18, pady=(16, 10))
        metric_row = tk.Frame(status, bg=self.colors.SURFACE)
        metric_row.pack(fill="x", padx=12, pady=(0, 16))
        self.metric_labels: dict[str, tk.Label] = {}
        for key in ("CPU", "MEM", "DISK"):
            item = tk.Frame(metric_row, bg=self.colors.SURFACE_2)
            item.pack(side="left", fill="x", expand=True, padx=4)
            tk.Label(item, text=key, bg=self.colors.SURFACE_2, fg=self.colors.MUTED, font=self.font_label).pack(pady=(10, 0))
            value = tk.Label(item, text="--%", bg=self.colors.SURFACE_2, fg=self.colors.TEXT, font=(self.mono_family, 15, "bold"))
            value.pack(pady=(2, 10))
            self.metric_labels[key] = value

        audit = self._panel(side)
        audit.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        audit_top = tk.Frame(audit, bg=self.colors.SURFACE)
        audit_top.pack(fill="x", padx=18, pady=(15, 8))
        tk.Label(audit_top, text="LAST RESPONSE", bg=self.colors.SURFACE, fg=self.colors.CYAN, font=self.font_label).pack(side="left")
        self.audit_button = self._button(audit_top, "RUN AUDIT", self._run_audit, compact=True)
        self.audit_button.pack(side="right")
        self.clean_button = self._button(
            audit_top, "CLEAN", self._clean_approved, compact=True
        )
        self.clean_button.pack(side="right", padx=(0, 7))
        self.response_label = tk.Label(
            audit,
            text="Standing by. Your commands stay on this device unless intelligence is requested.",
            bg=self.colors.SURFACE,
            fg=self.colors.MUTED,
            font=self.font_body,
            justify="left",
            anchor="nw",
            wraplength=330,
        )
        self.response_label.pack(fill="x", padx=18, pady=(4, 17))

        apps_panel = self._panel(side)
        apps_panel.grid(row=2, column=0, sticky="nsew")
        apps_panel.grid_rowconfigure(1, weight=1)
        apps_top = tk.Frame(apps_panel, bg=self.colors.SURFACE)
        apps_top.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 8))
        tk.Label(apps_top, text="QUICK LAUNCH", bg=self.colors.SURFACE, fg=self.colors.CYAN, font=self.font_label).pack(side="left")
        self.app_count_label = tk.Label(apps_top, text="SCANNING", bg=self.colors.SURFACE, fg=self.colors.MUTED, font=self.font_small)
        self.app_count_label.pack(side="right")
        self.quick_apps = tk.Frame(apps_panel, bg=self.colors.SURFACE)
        self.quick_apps.grid(row=1, column=0, sticky="nsew", padx=14, pady=(2, 14))

    def _build_applications(self) -> None:
        view = tk.Frame(self.view_host, bg=self.colors.VOID)
        self.views["applications"] = view
        view.grid_rowconfigure(1, weight=1)
        view.grid_columnconfigure(0, weight=1)

        tools = self._panel(view)
        tools.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        tk.Label(tools, text="⌕", bg=self.colors.SURFACE, fg=self.colors.CYAN, font=(self.ui_family, 18)).pack(side="left", padx=(16, 8), pady=10)
        self.search_var = tk.StringVar()
        search = tk.Entry(tools, textvariable=self.search_var, bg=self.colors.SURFACE, fg=self.colors.TEXT, insertbackground=self.colors.CYAN, relief="flat", font=self.font_body)
        search.pack(side="left", fill="x", expand=True, ipady=12)
        search.bind("<KeyRelease>", lambda _event: self._filter_apps())
        self.search_entry = search
        self.refresh_apps_button = self._button(tools, "REFRESH CATALOG", self._refresh_apps, compact=True)
        self.refresh_apps_button.pack(side="right", padx=10, pady=8)

        container = self._panel(view)
        container.grid(row=1, column=0, sticky="nsew")
        self.apps_canvas = tk.Canvas(container, bg=self.colors.SURFACE, highlightthickness=0)
        scrollbar = tk.Scrollbar(container, orient="vertical", command=self.apps_canvas.yview, troughcolor=self.colors.SURFACE, bg=self.colors.LINE)
        self.apps_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.apps_canvas.pack(side="left", fill="both", expand=True)
        self.apps_grid = tk.Frame(self.apps_canvas, bg=self.colors.SURFACE)
        self.apps_window = self.apps_canvas.create_window((0, 0), window=self.apps_grid, anchor="nw")
        self.apps_grid.bind("<Configure>", lambda _event: self.apps_canvas.configure(scrollregion=self.apps_canvas.bbox("all")))
        self.apps_canvas.bind("<Configure>", lambda event: self.apps_canvas.itemconfigure(self.apps_window, width=event.width))
        self.apps_canvas.bind_all("<MouseWheel>", self._scroll_apps)

    def _build_processes(self) -> None:
        view = tk.Frame(self.view_host, bg=self.colors.VOID)
        self.views["processes"] = view
        view.grid_rowconfigure(1, weight=1)
        view.grid_columnconfigure(0, weight=1)

        tools = self._panel(view)
        tools.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        copy = tk.Frame(tools, bg=self.colors.SURFACE)
        copy.pack(side="left", fill="x", expand=True, padx=18, pady=12)
        tk.Label(
            copy,
            text="APPROVED PROCESS CLEANUP",
            bg=self.colors.SURFACE,
            fg=self.colors.CYAN,
            font=self.font_label,
        ).pack(anchor="w")
        tk.Label(
            copy,
            text="Approve user apps once. Then say “Jarvis, clean unnecessary processes.”",
            bg=self.colors.SURFACE,
            fg=self.colors.MUTED,
            font=self.font_body,
        ).pack(anchor="w", pady=(3, 0))
        self.process_count_label = tk.Label(
            tools,
            text="SCANNING",
            bg=self.colors.SURFACE,
            fg=self.colors.MUTED,
            font=self.font_label,
        )
        self.process_count_label.pack(side="right", padx=12)
        self.process_clean_button = self._button(
            tools, "CLEAN APPROVED", self._clean_approved, accent=True, compact=True
        )
        self.process_clean_button.pack(side="right", padx=(0, 8), pady=10)
        self.process_refresh_button = self._button(
            tools, "REFRESH", self._refresh_processes, compact=True
        )
        self.process_refresh_button.pack(side="right", padx=(0, 8), pady=10)

        container = self._panel(view)
        container.grid(row=1, column=0, sticky="nsew")
        self.process_canvas = tk.Canvas(
            container, bg=self.colors.SURFACE, highlightthickness=0
        )
        scrollbar = tk.Scrollbar(
            container,
            orient="vertical",
            command=self.process_canvas.yview,
            troughcolor=self.colors.SURFACE,
            bg=self.colors.LINE,
        )
        self.process_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.process_canvas.pack(side="left", fill="both", expand=True)
        self.process_grid = tk.Frame(self.process_canvas, bg=self.colors.SURFACE)
        self.process_window = self.process_canvas.create_window(
            (0, 0), window=self.process_grid, anchor="nw"
        )
        self.process_grid.bind(
            "<Configure>",
            lambda _event: self.process_canvas.configure(
                scrollregion=self.process_canvas.bbox("all")
            ),
        )
        self.process_canvas.bind(
            "<Configure>",
            lambda event: self.process_canvas.itemconfigure(
                self.process_window, width=event.width
            ),
        )
        self.process_canvas.bind(
            "<MouseWheel>",
            lambda event: self.process_canvas.yview_scroll(
                int(-event.delta / 120), "units"
            ),
        )

    def _build_activity(self) -> None:
        view = tk.Frame(self.view_host, bg=self.colors.VOID)
        self.views["activity"] = view
        view.grid_rowconfigure(0, weight=1)
        view.grid_columnconfigure(0, weight=1)
        panel = self._panel(view)
        panel.grid(row=0, column=0, sticky="nsew")
        panel.grid_rowconfigure(1, weight=1)
        panel.grid_columnconfigure(0, weight=1)
        top = tk.Frame(panel, bg=self.colors.SURFACE)
        top.grid(row=0, column=0, sticky="ew", padx=18, pady=14)
        tk.Label(top, text="LIVE ACTIVITY LEDGER", bg=self.colors.SURFACE, fg=self.colors.CYAN, font=self.font_label).pack(side="left")
        self._button(top, "REFRESH", self._refresh_activity, compact=True).pack(side="right")
        self.log_text = tk.Text(
            panel,
            bg=self.colors.VOID,
            fg=self.colors.MUTED,
            insertbackground=self.colors.CYAN,
            selectbackground=self.colors.CYAN_SOFT,
            relief="flat",
            bd=0,
            padx=18,
            pady=16,
            font=self.font_mono,
            wrap="word",
            state="disabled",
        )
        self.log_text.grid(row=1, column=0, sticky="nsew", padx=1, pady=(0, 1))

    def _build_settings(self) -> None:
        view = tk.Frame(self.view_host, bg=self.colors.VOID)
        self.views["settings"] = view
        view.grid_columnconfigure(0, weight=1)
        view.grid_columnconfigure(1, weight=1)

        startup = self._panel(view)
        startup.grid(row=0, column=0, sticky="nsew", padx=(0, 7), pady=(0, 14))
        tk.Label(startup, text="STARTUP", bg=self.colors.SURFACE, fg=self.colors.CYAN, font=self.font_label).pack(anchor="w", padx=20, pady=(18, 8))
        tk.Label(startup, text="Ready when Windows signs you in", bg=self.colors.SURFACE, fg=self.colors.TEXT, font=self.font_title).pack(anchor="w", padx=20)
        tk.Label(startup, text="Runs the voice engine silently in the background. The dashboard remains optional.", bg=self.colors.SURFACE, fg=self.colors.MUTED, font=self.font_body, justify="left", wraplength=430).pack(anchor="w", padx=20, pady=(8, 16))
        actions = tk.Frame(startup, bg=self.colors.SURFACE)
        actions.pack(fill="x", padx=20, pady=(0, 20))
        self._button(actions, "ENABLE", lambda: self._set_startup(True), accent=True, compact=True).pack(side="left")
        self._button(actions, "DISABLE", lambda: self._set_startup(False), compact=True).pack(side="left", padx=8)

        audio = self._panel(view)
        audio.grid(row=0, column=1, sticky="nsew", padx=(7, 0), pady=(0, 14))
        tk.Label(audio, text="AUDIO INPUT", bg=self.colors.SURFACE, fg=self.colors.CYAN, font=self.font_label).pack(anchor="w", padx=20, pady=(18, 8))
        tk.Label(audio, text="WASAPI voice enhancement", bg=self.colors.SURFACE, fg=self.colors.TEXT, font=self.font_title).pack(anchor="w", padx=20)
        self.microphone_label = tk.Label(audio, text="Detecting active microphone…", bg=self.colors.SURFACE, fg=self.colors.MUTED, font=self.font_body, justify="left", wraplength=430)
        self.microphone_label.pack(anchor="w", padx=20, pady=(8, 16))
        tk.Label(audio, text="80 Hz rumble filter  •  automatic normalization  •  dual-language app recognition", bg=self.colors.SURFACE, fg=self.colors.CYAN, font=self.font_small, justify="left", wraplength=430).pack(anchor="w", padx=20, pady=(0, 20))

        privacy = self._panel(view)
        privacy.grid(row=1, column=0, columnspan=2, sticky="ew")
        tk.Label(privacy, text="PRIVACY BOUNDARY", bg=self.colors.SURFACE, fg=self.colors.CYAN, font=self.font_label).pack(anchor="w", padx=20, pady=(18, 8))
        tk.Label(privacy, text="Applications launch only from the local Windows catalog. No browser fallback. Voice unlocking remains disabled. System cleanup follows your explicit allowlist.", bg=self.colors.SURFACE, fg=self.colors.MUTED, font=self.font_body, justify="left", wraplength=900).pack(anchor="w", padx=20, pady=(0, 20))

    def _show_view(self, name: str) -> None:
        self.active_view = name
        for key, frame in self.views.items():
            if key == name:
                frame.grid(row=0, column=0, sticky="nsew")
            else:
                frame.grid_remove()
        titles = {
            "overview": ("SYSTEM / OVERVIEW", self._greeting()),
            "applications": ("LOCAL / APPLICATIONS", "Installed applications"),
            "processes": ("SYSTEM / PROCESSES", "Cleanup approvals"),
            "activity": ("SYSTEM / ACTIVITY", "Operational timeline"),
            "settings": ("SYSTEM / SETTINGS", "Assistant configuration"),
        }
        kicker, title = titles[name]
        self.page_kicker.configure(text=kicker)
        self.page_title.configure(text=title)
        for key, button in self.nav_buttons.items():
            active = key == name
            button.configure(
                bg=self.colors.SURFACE_3 if active else self.colors.SURFACE,
                fg=self.colors.TEXT if active else self.colors.MUTED,
            )
        if name == "activity":
            self._refresh_activity()
        if name == "applications":
            self.root.after(60, self.search_entry.focus_set)
        if name == "processes":
            self._refresh_processes()

    def _greeting(self) -> str:
        hour = datetime.now().hour
        period = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
        return f"Good {period}, Archit."

    def _bind_shortcuts(self) -> None:
        self.root.bind("<Control-k>", self._focus_search)
        self.root.bind("<Control-l>", self._focus_command)
        self.root.bind("<Control-p>", self._focus_processes)
        self.root.bind("<F5>", lambda _event: self._refresh_apps())

    def _focus_search(self, _event: tk.Event[Any]) -> str:
        self._show_view("applications")
        self.search_entry.focus_set()
        return "break"

    def _focus_command(self, _event: tk.Event[Any]) -> str:
        self._show_view("overview")
        self.command_entry.focus_set()
        return "break"

    def _focus_processes(self, _event: tk.Event[Any]) -> str:
        self._show_view("processes")
        return "break"

    def _run_worker(self, function: Any, *args: Any) -> None:
        threading.Thread(target=self._worker_guard, args=(function, args), daemon=True).start()

    def _worker_guard(self, function: Any, args: tuple[Any, ...]) -> None:
        try:
            function(*args)
        except Exception as exc:  # UI boundary: surface failures without crashing Tk.
            self.events.put(("error", f"{type(exc).__name__}: {exc}"))

    def _load_initial_state(self) -> None:
        self._run_worker(self._load_apps, False)
        self._detect_microphone()
        self._update_engine_state()

    def _load_apps(self, force: bool) -> None:
        self.events.put(("busy", "Refreshing application catalog…"))
        apps = self.backend.scan_installed_apps(force_refresh=force)
        self.events.put(("apps", apps))

    def _refresh_apps(self) -> None:
        if not self._busy:
            self._run_worker(self._load_apps, True)

    def _filter_apps(self) -> None:
        query = self.backend._normalise(self.search_var.get())
        names = sorted(self.apps, key=str.casefold)
        if query:
            terms = query.split()
            names = [name for name in names if all(term in self.backend._normalise(name) for term in terms)]
        self.filtered_apps = names[:120]
        self._render_apps()

    def _render_apps(self) -> None:
        for child in self.apps_grid.winfo_children():
            child.destroy()
        for column in range(3):
            self.apps_grid.grid_columnconfigure(column, weight=1, uniform="apps")
        if not self.filtered_apps:
            tk.Label(self.apps_grid, text="No installed application matches that name.", bg=self.colors.SURFACE, fg=self.colors.MUTED, font=self.font_body).grid(row=0, column=0, padx=28, pady=40, sticky="w")
            return
        for index, name in enumerate(self.filtered_apps):
            row, column = divmod(index, 3)
            card = tk.Frame(self.apps_grid, bg=self.colors.SURFACE_2, highlightbackground=self.colors.LINE, highlightthickness=1)
            card.grid(row=row, column=column, sticky="ew", padx=8, pady=7, ipady=5)
            glyph = (name[:1] or "?").upper()
            tk.Label(card, text=glyph, width=3, bg=self.colors.SURFACE_3, fg=self.colors.CYAN, font=(self.ui_family, 14, "bold")).pack(side="left", padx=10, pady=8, ipady=5)
            label = tk.Label(card, text=name, bg=self.colors.SURFACE_2, fg=self.colors.TEXT, font=self.font_body, anchor="w")
            label.pack(side="left", fill="x", expand=True, padx=(0, 6))
            self._button(card, "OPEN", lambda app=name: self._launch_named_app(app), compact=True).pack(side="right", padx=8, pady=8)

    def _render_quick_apps(self) -> None:
        for child in self.quick_apps.winfo_children():
            child.destroy()
        preferred = ["File Explorer", "Settings", "Riot Client", "VALORANT", "Steam", "Spotify"]
        selected = [name for name in preferred if name in self.apps]
        if len(selected) < 6:
            selected.extend(name for name in sorted(self.apps, key=str.casefold) if name not in selected)
        for index, name in enumerate(selected[:6]):
            row, column = divmod(index, 2)
            button = tk.Button(
                self.quick_apps,
                text=f"{(name[:1] or '?').upper()}   {name}",
                command=lambda app=name: self._launch_named_app(app),
                anchor="w",
                bg=self.colors.SURFACE_2,
                fg=self.colors.TEXT,
                activebackground=self.colors.SURFACE_3,
                activeforeground=self.colors.CYAN,
                bd=0,
                relief="flat",
                font=self.font_body,
                padx=12,
                pady=10,
                cursor="hand2",
            )
            button.grid(row=row, column=column, sticky="ew", padx=4, pady=4)
            self.quick_apps.grid_columnconfigure(column, weight=1, uniform="quick")

    def _scroll_apps(self, event: tk.Event[Any]) -> None:
        if self.active_view == "applications":
            self.apps_canvas.yview_scroll(int(-event.delta / 120), "units")

    def _refresh_processes(self) -> None:
        if self._process_busy:
            return
        self._process_busy = True
        self.process_refresh_button.configure(state="disabled", text="SCANNING")
        self._run_worker(self._process_inventory_worker)

    def _process_inventory_worker(self) -> None:
        self.events.put(("processes", self.process_cleaner.inventory()))

    def _render_processes(self) -> None:
        for child in self.process_grid.winfo_children():
            child.destroy()
        approved_count = sum(
            1 for item in self.process_items if bool(item.get("approved"))
        )
        self.process_count_label.configure(
            text=f"{approved_count} APPROVED • {len(self.process_items)} RUNNING"
        )
        for row, item in enumerate(self.process_items[:120]):
            protected = bool(item.get("protected"))
            approved = bool(item.get("approved"))
            card = tk.Frame(
                self.process_grid,
                bg=self.colors.SURFACE_2,
                highlightbackground=(
                    self.colors.CYAN_SOFT if approved else self.colors.LINE
                ),
                highlightthickness=1,
            )
            card.grid(row=row, column=0, sticky="ew", padx=10, pady=5)
            self.process_grid.grid_columnconfigure(0, weight=1)

            mark = "✓" if approved else "◆" if protected else "○"
            mark_color = (
                self.colors.MINT
                if approved
                else self.colors.DIM
                if protected
                else self.colors.CYAN
            )
            tk.Label(
                card,
                text=mark,
                width=3,
                bg=self.colors.SURFACE_2,
                fg=mark_color,
                font=(self.ui_family, 14, "bold"),
            ).pack(side="left", padx=(10, 4), pady=11)
            identity = tk.Frame(card, bg=self.colors.SURFACE_2)
            identity.pack(side="left", fill="x", expand=True, pady=9)
            tk.Label(
                identity,
                text=str(item.get("name") or "Unknown process"),
                bg=self.colors.SURFACE_2,
                fg=self.colors.TEXT,
                font=self.font_body,
                anchor="w",
            ).pack(anchor="w")
            instances = int(item.get("instances") or 0)
            tk.Label(
                identity,
                text=f"{instances} instance{'s' if instances != 1 else ''}",
                bg=self.colors.SURFACE_2,
                fg=self.colors.MUTED,
                font=self.font_small,
                anchor="w",
            ).pack(anchor="w")
            tk.Label(
                card,
                text=f"{float(item.get('memory_mb') or 0.0):.1f} MB",
                width=12,
                bg=self.colors.SURFACE_2,
                fg=self.colors.AMBER if approved else self.colors.MUTED,
                font=self.font_mono,
            ).pack(side="left", padx=8)
            if protected:
                tk.Label(
                    card,
                    text="PROTECTED",
                    width=13,
                    bg=self.colors.SURFACE_3,
                    fg=self.colors.DIM,
                    font=self.font_label,
                    padx=8,
                    pady=9,
                ).pack(side="right", padx=10, pady=9)
            else:
                action = "REMOVE" if approved else "APPROVE"
                self._button(
                    card,
                    action,
                    lambda name=str(item.get("name") or ""), value=not approved: self._toggle_process_approval(
                        name, value
                    ),
                    accent=not approved,
                    compact=True,
                ).pack(side="right", padx=10, pady=9)

    def _toggle_process_approval(self, process_name: str, approved: bool) -> None:
        if self._process_busy:
            return
        self._process_busy = True
        self._run_worker(self._process_approval_worker, process_name, approved)

    def _process_approval_worker(self, process_name: str, approved: bool) -> None:
        result = self.process_cleaner.set_approved(process_name, approved)
        self.events.put(("response", (result.message, False)))
        self.events.put(("processes", self.process_cleaner.inventory()))

    def _clean_approved(self) -> None:
        if self._process_busy:
            return
        self._process_busy = True
        self.clean_button.configure(state="disabled", text="CLEANING")
        self.process_clean_button.configure(state="disabled", text="CLEANING")
        self._run_worker(self._cleanup_worker)

    def _cleanup_worker(self) -> None:
        self.events.put(("voice", "thinking"))
        result = self.process_cleaner.clean()
        self.events.put(("response", (result.message, False)))
        self.backend.speak(result.message)
        self.events.put(("cleanup_done", self.process_cleaner.inventory()))

    def _launch_named_app(self, name: str) -> None:
        if name not in self.apps:
            self._set_response("That application is no longer in the local catalog.", error=True)
            return
        self._run_worker(self._launch_worker, name)

    def _launch_worker(self, name: str) -> None:
        self.events.put(("voice", "thinking"))
        success, detail = self.backend.launch_app(self.apps[name])
        message = f"Opening {name}." if success else f"Could not open {name}: {detail}"
        self.events.put(("response", (message, not success)))
        if success:
            self.backend.speak(message)
        self.events.put(("voice", "listening" if self._engine_processes() else "idle"))

    def _submit_command(self) -> None:
        text = self.command_var.get().strip()
        if not text or self._busy:
            return
        if len(text) > 160:
            self._set_response("Keep commands under 160 characters.", error=True)
            return
        if re.search(r"https?://|www\.", text, flags=re.IGNORECASE):
            self._set_response("Web addresses are blocked. I only launch installed applications.", error=True)
            return
        self.command_var.set("")
        self._busy = True
        self.send_button.configure(state="disabled", text="WORKING")
        self._run_worker(self._command_worker, text)

    def _command_worker(self, text: str) -> None:
        self.events.put(("voice", "thinking"))
        automation = self.jarvis.handle(text)
        if automation.handled:
            message = automation.message
            self.events.put(("response", (message, False)))
            self.backend.speak(message)
            self.events.put(("command_done", None))
            return

        normalised = self.backend._normalise(text)
        explicit = bool(re.search(r"\b(?:open|launch|start|run|close|quit|switch)\b", normalised))
        if explicit:
            action, target = self.backend.parse_intent(text, self.apps)
            result = self.backend.match_app(target, self.apps)
            if result.alternatives:
                message = "Choose one: " + ", ".join(result.alternatives[:4])
                self.events.put(("response", (message, True)))
            elif result.selected:
                name = result.selected
                if action == "close":
                    success, detail = self.backend.close_app(name, self.apps[name])
                    message = f"Closing {name}." if success else f"Could not close {name}: {detail}"
                else:
                    success, detail = self.backend.launch_app(self.apps[name])
                    message = f"Opening {name}." if success else f"Could not open {name}: {detail}"
                self.events.put(("response", (message, not success)))
                self.backend.speak(message)
            else:
                self.events.put(("response", ("That application is not installed on this device.", True)))
        else:
            answer = self.jarvis.answer(text)
            self.events.put(("response", (answer.message, False)))
            self.backend.speak(answer.message)
        self.events.put(("command_done", None))

    def _run_audit(self) -> None:
        if not self._busy:
            self._busy = True
            self.audit_button.configure(state="disabled", text="AUDITING")
            self._run_worker(self._audit_worker)

    def _audit_worker(self) -> None:
        result = self.jarvis.auditor.run()
        self.events.put(("response", (result.message, not result.handled)))
        self.events.put(("audit_done", None))

    def _engine_processes(self) -> list[psutil.Process]:
        main_path = str((self.workspace / "main.py").resolve()).casefold()
        matches: list[psutil.Process] = []
        for process in psutil.process_iter(["pid", "cmdline", "exe"]):
            try:
                if process.pid == os.getpid():
                    continue
                command = [str(part) for part in (process.info.get("cmdline") or [])]
                folded = [part.casefold() for part in command]
                if "--startup" in folded and any(main_path == str(Path(part).resolve()).casefold() for part in command if part.casefold().endswith("main.py")):
                    matches.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        return matches

    def _toggle_engine(self) -> None:
        if self.engine_state in {"starting", "stopping"}:
            return
        if self._engine_processes():
            self.engine_state = "stopping"
            self._render_engine_state()
            self._run_worker(self._stop_engine)
        else:
            self.engine_state = "starting"
            self._render_engine_state()
            self._run_worker(self._start_engine)

    def _start_engine(self) -> None:
        pythonw = Path(sys.executable).with_name("pythonw.exe") if os.name == "nt" else Path(sys.executable)
        executable = pythonw if pythonw.exists() else Path(sys.executable)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        subprocess.Popen(
            [str(executable), str(self.workspace / "main.py"), "--startup"],
            cwd=str(self.workspace),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        time.sleep(1.4)
        self.events.put(("engine", "online" if self._engine_processes() else "error"))

    def _stop_engine(self) -> None:
        processes = self._engine_processes()
        for process in processes:
            try:
                process.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        psutil.wait_procs(processes, timeout=3)
        self.events.put(("engine", "offline" if not self._engine_processes() else "error"))

    def _update_engine_state(self) -> None:
        self.engine_state = "online" if self._engine_processes() else "offline"
        self.voice_state = "listening" if self.engine_state == "online" else "idle"
        self._render_engine_state()

    def _poll_engine(self) -> None:
        if self.engine_state not in {"starting", "stopping"}:
            self._update_engine_state()
        self.root.after(2500, self._poll_engine)

    def _render_engine_state(self) -> None:
        online = self.engine_state == "online"
        transitioning = self.engine_state in {"starting", "stopping"}
        color = self.colors.MINT if online else self.colors.AMBER if transitioning else self.colors.RED if self.engine_state == "error" else self.colors.DIM
        label = self.engine_state.upper()
        self.engine_badge.configure(text=f"●  {label}", fg=color)
        self.nav_status_dot.configure(fg=color)
        self.nav_status_text.configure(text=f"ENGINE {label}")
        self.engine_button.configure(
            text="STOP JARVIS" if online else "PLEASE WAIT" if transitioning else "START JARVIS",
            state="disabled" if transitioning else "normal",
        )

    def _refresh_metrics(self) -> None:
        try:
            cpu = int(psutil.cpu_percent(interval=None))
            memory = int(psutil.virtual_memory().percent)
            drive = os.environ.get("SystemDrive", "C:") + "\\"
            disk = int(psutil.disk_usage(drive).percent)
            for key, value in (("CPU", cpu), ("MEM", memory), ("DISK", disk)):
                color = self.colors.RED if value >= 90 else self.colors.AMBER if value >= 75 else self.colors.TEXT
                self.metric_labels[key].configure(text=f"{value}%", fg=color)
        except (OSError, psutil.Error):
            pass
        self.root.after(2200, self._refresh_metrics)

    def _animate_core(self) -> None:
        canvas = self.core_canvas
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        canvas.delete("core")
        cx, cy = width / 2, height / 2 - 4
        radius = min(width, height) * 0.23
        active = self.engine_state == "online"
        thinking = self.voice_state == "thinking"
        self.animation_phase += 0.10 if active else 0.025

        for ring in range(4):
            spread = radius + ring * 15 + math.sin(self.animation_phase + ring) * (4 if active else 1)
            color = self.colors.CYAN_SOFT if ring < 2 else self.colors.LINE
            canvas.create_oval(cx - spread, cy - spread, cx + spread, cy + spread, outline=color, width=2 if ring == 0 else 1, tags="core")
        arc_color = self.colors.AMBER if thinking else self.colors.CYAN if active else self.colors.DIM
        for offset in (0, 120, 240):
            canvas.create_arc(cx - radius - 28, cy - radius - 28, cx + radius + 28, cy + radius + 28, start=(self.animation_phase * 55 + offset) % 360, extent=58, style="arc", outline=arc_color, width=3, tags="core")

        for index in range(22):
            x = cx - 112 + index * 10.5
            amplitude = (16 + 13 * math.sin(index * 0.77 + self.animation_phase * 3)) if active else 5
            canvas.create_line(x, cy - amplitude / 2, x, cy + amplitude / 2, fill=arc_color, width=2, tags="core")
        state_text = "THINKING" if thinking else "LISTENING" if active else "STANDBY"
        canvas.create_text(cx, cy + radius + 58, text=state_text, fill=arc_color, font=self.font_label, tags="core")
        canvas.create_text(cx, cy + radius + 78, text="VOICE CHANNEL SECURE", fill=self.colors.MUTED, font=self.font_small, tags="core")
        self.root.after(50 if active else 100, self._animate_core)

    def _detect_microphone(self) -> None:
        preference = self.backend.MICROPHONE_PREFERENCE
        label = preference if preference else "Windows default • Realtek / WASAPI auto-selection"
        self.microphone_label.configure(text=label)

    def _set_startup(self, enabled: bool) -> None:
        function = self.backend.install_startup if enabled else self.backend.remove_startup
        self._run_worker(self._startup_worker, function)

    def _startup_worker(self, function: Any) -> None:
        success, message = function()
        self.events.put(("response", (message, not success)))

    def _refresh_activity(self) -> None:
        path = self.workspace / "jarvis.log"
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 48_000))
                content = handle.read().decode("utf-8", errors="replace")
            lines = content.splitlines()[-220:]
            display = "\n".join(lines)
        except OSError as exc:
            display = f"Activity log unavailable: {exc}"
        signature = display[-500:]
        if signature != self._last_log_signature:
            self._last_log_signature = signature
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.insert("end", display or "No activity recorded yet.")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(3500, self._refresh_activity)

    def _set_response(self, message: str, *, error: bool = False) -> None:
        compact = " ".join(str(message).split())
        if len(compact) > 330:
            compact = compact[:327].rstrip() + "…"
        self.response_label.configure(text=compact, fg=self.colors.RED if error else self.colors.MUTED)

    def _drain_events(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "apps":
                self.apps = dict(payload)
                self.app_count_label.configure(text=f"{len(self.apps)} LOCAL APPS")
                self._filter_apps()
                self._render_quick_apps()
                self._busy = False
                self.refresh_apps_button.configure(state="normal", text="REFRESH CATALOG")
                self._set_response(f"Application catalog ready. {len(self.apps)} local apps available.")
            elif kind == "processes":
                self.process_items = list(payload)
                self._process_busy = False
                self.process_refresh_button.configure(state="normal", text="REFRESH")
                self.process_clean_button.configure(
                    state="normal", text="CLEAN APPROVED"
                )
                self.clean_button.configure(state="normal", text="CLEAN")
                self._render_processes()
            elif kind == "busy":
                self._busy = True
                self.refresh_apps_button.configure(state="disabled", text="SCANNING")
                self._set_response(str(payload))
            elif kind == "voice":
                self.voice_state = str(payload)
            elif kind == "response":
                message, error = payload
                self._set_response(str(message), error=bool(error))
            elif kind == "command_done":
                self._busy = False
                self.send_button.configure(state="normal", text="EXECUTE")
                self.voice_state = "listening" if self.engine_state == "online" else "idle"
            elif kind == "audit_done":
                self._busy = False
                self.audit_button.configure(state="normal", text="RUN AUDIT")
            elif kind == "cleanup_done":
                self.process_items = list(payload)
                self._process_busy = False
                self.voice_state = (
                    "listening" if self.engine_state == "online" else "idle"
                )
                self.clean_button.configure(state="normal", text="CLEAN")
                self.process_clean_button.configure(
                    state="normal", text="CLEAN APPROVED"
                )
                self.process_refresh_button.configure(state="normal", text="REFRESH")
                self._render_processes()
            elif kind == "engine":
                self.engine_state = str(payload)
                self.voice_state = "listening" if payload == "online" else "idle"
                self._render_engine_state()
            elif kind == "error":
                self._busy = False
                self.send_button.configure(state="normal", text="EXECUTE")
                self.audit_button.configure(state="normal", text="RUN AUDIT")
                self.refresh_apps_button.configure(state="normal", text="REFRESH CATALOG")
                self._process_busy = False
                self.clean_button.configure(state="normal", text="CLEAN")
                self.process_clean_button.configure(
                    state="normal", text="CLEAN APPROVED"
                )
                self.process_refresh_button.configure(state="normal", text="REFRESH")
                self._set_response(str(payload), error=True)
        self.root.after(80, self._drain_events)


def run_ui(backend: Any) -> None:
    """Open the native dashboard using an already imported backend module."""

    root = tk.Tk()
    JarvisDesktopApp(root, backend)
    root.mainloop()
