"""Настройки и быстрые команды в палитре (F2 / Ctrl+P).

Без запроса показываются только разделы (окна). При вводе текста ищутся и разделы,
и быстрые команды (язык, повтор, видео, пути).
"""
from __future__ import annotations

from functools import partial

from textual.command import DiscoveryHit, Hit, Hits, Provider

from .i18n import available_languages, tr
from .settings import CHOICES


class SettingsCommands(Provider):
    def _sections(self):
        app = self.app
        L = app.lang
        return [
            (tr(L, "cmd_themes"), tr(L, "cmd_themes_help"), app.action_open_themes),
            (tr(L, "cmd_playback"), tr(L, "cmd_playback_help"), app.action_open_playback),
            (tr(L, "cmd_general"), tr(L, "cmd_general_help"), app.action_open_general),
            (tr(L, "cmd_paths"), tr(L, "cmd_paths_help"), app.action_open_paths),
        ]

    def _quick(self):
        app = self.app
        L = app.lang
        e = []
        for mode in ("off", "track", "playlist"):
            e.append((tr(L, "cmd_repeat", name=tr(L, f"repeat_{mode}")), tr(L, "cmd_repeat_help"),
                      partial(app.action_set_repeat, mode)))
        e += [
            (tr(L, "cmd_video_off"), tr(L, "cmd_video_off_help"), partial(app.action_set_option, "video", "off")),
            (tr(L, "cmd_video_window"), tr(L, "cmd_video_window_help"),
             partial(app.action_set_option, "video", "window")),
        ]
        for vo in CHOICES["terminal_vo"]:
            e.append((tr(L, "cmd_terminal_vo", name=vo), tr(L, "cmd_terminal_vo_help"),
                      partial(app.action_set_option, "terminal_vo", vo)))
        for code, name in available_languages():
            e.append((tr(L, "cmd_lang", name=name), tr(L, "cmd_lang_help"),
                      partial(app.action_set_option, "language", code)))
        e += [
            (tr(L, "cmd_set_music"), tr(L, "cmd_set_music_help"), app.action_prompt_music_dir),
            (tr(L, "cmd_set_mpv"), tr(L, "cmd_set_mpv_help"), app.action_prompt_mpv_path),
            (tr(L, "cmd_restart"), tr(L, "cmd_restart_help"), app.action_restart_player),
            (tr(L, "cmd_show_paths"), tr(L, "cmd_show_paths_help"), app.action_show_paths),
        ]
        return e

    async def discover(self) -> Hits:
        for name, help_text, callback in self._sections():
            yield DiscoveryHit(name, callback, help=help_text)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, help_text, callback in self._sections() + self._quick():
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), callback, help=help_text)
