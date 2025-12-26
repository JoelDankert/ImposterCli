"""
Simple fullscreen CLI UI with a centered message box and scroll listener.

Usage: python main.py
"""

import curses
import random
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Optional


@dataclass
class MessageBox:
    title: str = "Nachrichten"
    text: str = "Zum Starten scrollen."


@dataclass
class BoxStyle:
    fill_pair: int = 3
    text_pair: int = 6
    border_pair: int = 4


@dataclass
class GameMode:
    name: str
    description: str
    style: BoxStyle
    source: str  # all, nouns, verbs, player, back


@dataclass
class ScrollState:
    active: bool = False
    last_time: float = 0.0
    last_direction: Optional[str] = None
    last_trigger_time: float = 0.0


@dataclass
class UIState:
    screen: str = "idle"  # idle, player_input, word_count, imposter_percent, confirm, mode_select, word_entry, random_pick, wait_scroll, reveal, handoff, done
    input_buffer: str = ""
    word_count_buffer: str = ""
    imposter_percent_buffer: str = ""
    player_count: int = 0
    word_options_count: int = 0
    word_buffer: str = ""
    chosen_word: str = ""
    mode_index: int = 0
    selected_mode: Optional[GameMode] = None
    imposter_index: Optional[int] = None
    current_player: int = 0
    countdown_until: float = 0.0
    reveal_until: float = 0.0
    handoff_until: float = 0.0
    handoff_advance: bool = True
    handoff_pending_action: str = ""
    imposter_all_chance: int = 0
    all_imposter_except_first: bool = False
    random_candidates: list[str] = None  # type: ignore
    random_filter: str = ""
    scroll_block_until: float = 0.0
    simple_candidates: list[str] = None  # type: ignore
    simple_index: int = 0
    simple_hold_started: bool = False
    simple_hold_until: float = 0.0


message_box = MessageBox()
box_style = BoxStyle()

GO_DIRECTION = "down"
BACK_DIRECTION = "up"

scroll_state = ScrollState()
ui_state = UIState()

# Treat events within this window as one continuous scroll.
# Debounce scrolls for eight-tenths of a second to avoid double triggers (wheel and arrows alike).
SCROLL_BUFFER_SECONDS = 0.8
SCROLL_GAP_SECONDS = SCROLL_BUFFER_SECONDS
MIN_SCROLL_INTERVAL = SCROLL_BUFFER_SECONDS

WORDS_PATH = Path(__file__).with_name("words.txt")

def load_words(path: Path = WORDS_PATH) -> list[str]:
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    words: list[str] = []
    seen: set[str] = set()
    for line in raw_lines:
        word = line.strip()
        if not word or word in seen:
            continue
        seen.add(word)
        words.append(word)
    return words


WORDS = load_words()

DEFAULT_STYLE = BoxStyle()
IMPOSTER_STYLE = BoxStyle(fill_pair=3, text_pair=11, border_pair=11)

GAME_MODES = [
    GameMode("BACK", "Return to start.", IMPOSTER_STYLE, "back"),
    GameMode("Player", "Spieler gibt Geheimwort ein.", DEFAULT_STYLE, "player"),
    GameMode("Random", "Zufällige Wörter mit Suche.", DEFAULT_STYLE, "random"),
    GameMode("Random Simple", "Wörter durchscrollen, unten wählen.", DEFAULT_STYLE, "random_simple"),
]


def set_message_box_title(title: str) -> None:
    message_box.title = title


def set_message_box_text(text: str) -> None:
    message_box.text = text


def set_box_style(style: BoxStyle) -> None:
    box_style.fill_pair = style.fill_pair
    box_style.text_pair = style.text_pair
    box_style.border_pair = style.border_pair


def is_current_imposter() -> bool:
    if ui_state.all_imposter_except_first:
        if ui_state.player_count > 1:
            return ui_state.current_player != 0
        return True
    return ui_state.imposter_index is not None and ui_state.current_player == ui_state.imposter_index


def wrap_text(text: str, width: int) -> list[str]:
    if width <= 0:
        return [text]
    lines: list[str] = []
    for raw_line in text.splitlines() or [""]:
        words = raw_line.split(" ")
        current = ""
        for word in words:
            if not current:
                if len(word) <= width:
                    current = word
                else:
                    for i in range(0, len(word), width):
                        lines.append(word[i : i + width])
                    current = ""
            elif len(current) + 1 + len(word) <= width:
                current += " " + word
            else:
                lines.append(current)
                if len(word) <= width:
                    current = word
                else:
                    for i in range(0, len(word), width):
                        chunk = word[i : i + width]
                        if len(chunk) == width:
                            lines.append(chunk)
                        else:
                            current = chunk
                            break
                    else:
                        current = ""
        if current:
            lines.append(current)
    return lines or [""]


def safe_addstr(stdscr: curses.window, y: int, x: int, s: str, attr: int = 0) -> None:
    try:
        stdscr.addstr(y, x, s, attr)
    except curses.error:
        pass


def safe_addch(stdscr: curses.window, y: int, x: int, ch: str, attr: int = 0) -> None:
    try:
        stdscr.addch(y, x, ch, attr)
    except curses.error:
        pass


def init_colors() -> None:
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLACK)   # base text on black
    curses.init_pair(2, curses.COLOR_CYAN, curses.COLOR_BLACK)    # accents
    curses.init_pair(3, curses.COLOR_WHITE, curses.COLOR_BLACK)   # box fill on black
    curses.init_pair(4, curses.COLOR_WHITE, curses.COLOR_BLACK)   # border
    curses.init_pair(5, curses.COLOR_YELLOW, curses.COLOR_BLACK)  # flash text
    curses.init_pair(6, curses.COLOR_WHITE, curses.COLOR_BLACK)   # box text
    curses.init_pair(7, curses.COLOR_CYAN, curses.COLOR_BLACK)    # subtle blue tint
    curses.init_pair(8, curses.COLOR_RED, curses.COLOR_BLACK)     # imposter red outline/text (bold red)
    curses.init_pair(11, curses.COLOR_RED, curses.COLOR_BLACK)    # strong red for text/borders


def draw_message_box(stdscr: curses.window, now: float, highlight_word: Optional[str] = None) -> None:
    h, w = stdscr.getmaxyx()
    padding_x = 4
    wrap_width = max(10, w - 10)
    lines = wrap_text(message_box.text, wrap_width)
    content_width = max(len(message_box.title), max((len(line) for line in lines), default=0))
    max_width = max(10, w - 4)
    box_width = min(max_width, max(20, content_width + padding_x * 2))
    box_height = max(7, 6 + len(lines))
    start_y = max(1, (h - box_height) // 2)
    start_x = max(1, (w - box_width) // 2)

    # Arrow indicators during mode selection or simple random browse (single arrows only)
    if ui_state.screen in ("mode_select", "random_simple"):
        indicator_attr = curses.color_pair(2) | curses.A_BOLD
        arrow_up = "↑"
        arrow_down = "↓"
        arrow_x = start_x + (box_width - 1) // 2
        arrow_up_y = start_y - 2
        if arrow_up_y >= 0:
            stdscr.attrset(indicator_attr)
            safe_addstr(stdscr, arrow_up_y, arrow_x, arrow_up)
        if ui_state.screen == "random_simple":
            arrow_down_y = start_y + box_height + 1
            if arrow_down_y < h:
                stdscr.attrset(indicator_attr)
                safe_addstr(stdscr, arrow_down_y, arrow_x, arrow_down)

    # Reveal countdown indicator
    if ui_state.screen == "reveal":
        remaining = max(0, int((ui_state.reveal_until - now) + 0.999))
        label = f"{remaining}"
        label_y = start_y - 3
        if label_y >= 0:
            label_x = start_x + (box_width - len(label)) // 2
            stdscr.attrset(curses.color_pair(5) | curses.A_BOLD)
            safe_addstr(stdscr, label_y, label_x, label)

    fill_attr = curses.color_pair(box_style.fill_pair)
    if box_style.border_pair != 11:
        fill_attr |= curses.A_DIM
    fill_x_start = start_x + (0 if ui_state.screen == "handoff" else 1)
    fill_x_end = start_x + box_width - (0 if ui_state.screen == "handoff" else 1)
    fill_y_start = start_y + (0 if ui_state.screen == "handoff" else 1)
    fill_y_end = start_y + box_height - (0 if ui_state.screen == "handoff" else 1)
    for y in range(fill_y_start, fill_y_end):
        for x in range(fill_x_start, fill_x_end):
            safe_addch(stdscr, y, x, " ", fill_attr)

    # Border with box-drawing characters (skip for handoff screen)
    use_imposter_style = ui_state.screen == "reveal" and is_current_imposter()
    if use_imposter_style:
        border_attr = curses.color_pair(IMPOSTER_STYLE.border_pair) | curses.A_BOLD
        title_attr = curses.color_pair(IMPOSTER_STYLE.text_pair) | curses.A_BOLD
        body_attr = curses.color_pair(IMPOSTER_STYLE.text_pair)
    else:
        border_attr = curses.color_pair(box_style.border_pair) | curses.A_BOLD
        title_attr = curses.color_pair(box_style.text_pair) | curses.A_BOLD
        body_attr = curses.color_pair(box_style.text_pair)

    if ui_state.screen != "handoff":
        border_top = "┌" + "─" * (box_width - 2) + "┐"
        border_bottom = "└" + "─" * (box_width - 2) + "┘"
        stdscr.attrset(border_attr)
        safe_addstr(stdscr, start_y, start_x, border_top, border_attr)
        for y in range(start_y + 1, start_y + box_height - 1):
            safe_addstr(stdscr, y, start_x, "│", border_attr)
            safe_addstr(stdscr, y, start_x + box_width - 1, "│", border_attr)
        safe_addstr(stdscr, start_y + box_height - 1, start_x, border_bottom, border_attr)

    title_x = start_x + (box_width - len(message_box.title)) // 2
    stdscr.attrset(title_attr)
    safe_addstr(stdscr, start_y + 2, title_x, message_box.title[: box_width - 2], title_attr)
    stdscr.attrset(body_attr)
    text_start_y = start_y + 4
    for i, line in enumerate(lines):
        line_x = start_x + (box_width - len(line)) // 2
        if highlight_word:
            words = line.split(" ")
            cursor_x = line_x
            for idx, word in enumerate(words):
                if idx > 0:
                    safe_addstr(stdscr, text_start_y + i, cursor_x, " ", body_attr)
                    cursor_x += 1
                attr = body_attr
                if word.lower() == highlight_word.lower():
                    attr |= curses.A_BOLD
                safe_addstr(stdscr, text_start_y + i, cursor_x, word[: box_width - 2], attr)
                cursor_x += len(word)
        else:
            safe_addstr(stdscr, text_start_y + i, line_x, line[: box_width - 2], body_attr)


def render(stdscr: curses.window) -> None:
    stdscr.erase()
    stdscr.bkgd(" ", curses.color_pair(1) | curses.A_DIM)
    now = time.monotonic()
    highlight_word = None
    if (
        ui_state.screen == "random_simple"
        and ui_state.simple_hold_started
        and ui_state.simple_hold_until > 0
    ):
        remaining = max(0, int(ui_state.simple_hold_until - now + 0.999))
        set_message_box_text(f"Auswahl in {remaining} sek")
    if ui_state.screen == "random_pick" and ui_state.random_filter:
        highlight_word = best_random_choice()
    if ui_state.screen == "reveal" and is_current_imposter():
        set_box_style(IMPOSTER_STYLE)
    draw_message_box(stdscr, now, highlight_word)

    # Footer
    h, w = stdscr.getmaxyx()
    footer = "Imposter-CLI"
    footer_x = max(0, (w - len(footer)) // 2)
    stdscr.attrset(curses.color_pair(4) | curses.A_BOLD)
    stdscr.addstr(h - 2, footer_x, footer[: max(0, w - 1)])

    stdscr.refresh()


def handle_scroll(direction: str, now: float) -> None:
    if now - scroll_state.last_trigger_time < MIN_SCROLL_INTERVAL:
        return
    if now < ui_state.scroll_block_until:
        return
    if not scroll_state.active:
        scroll_state.active = True
        scroll_state.last_direction = direction
        on_direction(direction)
    else:
        if (
            direction != scroll_state.last_direction
            or now - scroll_state.last_time > SCROLL_GAP_SECONDS
        ):
            on_direction(direction)
            scroll_state.last_direction = direction
    scroll_state.last_time = now
    scroll_state.last_trigger_time = now


def update_scroll_state(now: float) -> None:
    if scroll_state.active and now - scroll_state.last_time > SCROLL_GAP_SECONDS:
        scroll_state.active = False
        scroll_state.last_direction = None


def reset_idle() -> None:
    ui_state.screen = "idle"
    ui_state.input_buffer = ""
    ui_state.word_count_buffer = ""
    ui_state.imposter_percent_buffer = ""
    ui_state.word_options_count = 0
    ui_state.word_buffer = ""
    ui_state.player_count = 0
    ui_state.chosen_word = ""
    ui_state.selected_mode = None
    ui_state.imposter_index = None
    ui_state.current_player = 0
    ui_state.countdown_until = 0.0
    ui_state.reveal_until = 0.0
    ui_state.handoff_until = 0.0
    ui_state.handoff_advance = True
    ui_state.handoff_pending_action = ""
    ui_state.imposter_all_chance = 0
    ui_state.all_imposter_except_first = False
    ui_state.simple_hold_started = False
    ui_state.simple_hold_until = 0.0
    ui_state.random_candidates = []
    ui_state.random_filter = ""
    set_box_style(DEFAULT_STYLE)
    set_message_box_title("Imposter-CLI")
    set_message_box_text("Zum Starten scrollen")
    ui_state.simple_candidates = []
    ui_state.simple_index = 0


def enter_player_input() -> None:
    ui_state.screen = "player_input"
    set_box_style(DEFAULT_STYLE)
    title = "Spieleranzahl eingeben"
    body = ui_state.input_buffer or "Zahl tippen..."
    set_message_box_title(title)
    set_message_box_text(body)


def enter_word_count_input() -> None:
    ui_state.screen = "word_count"
    set_box_style(DEFAULT_STYLE)
    title = "Anzahl Wortoptionen"
    body = ui_state.word_count_buffer or "Zahl tippen..."
    set_message_box_title(title)
    set_message_box_text(body)
    ui_state.imposter_percent_buffer = ui_state.imposter_percent_buffer or ""


def enter_imposter_percent_input() -> None:
    ui_state.screen = "imposter_percent"
    set_box_style(DEFAULT_STYLE)
    title = "Prozent: alle sind Imposter"
    body = ui_state.imposter_percent_buffer or "0-100 eingeben"
    set_message_box_title(title)
    set_message_box_text(body)


def enter_confirm() -> None:
    ui_state.screen = "confirm"
    try:
        ui_state.player_count = max(1, int(ui_state.input_buffer))
    except ValueError:
        ui_state.player_count = 1
    try:
        ui_state.word_options_count = max(0, int(ui_state.word_count_buffer or "0"))
    except ValueError:
        ui_state.word_options_count = 0
    try:
        ui_state.imposter_all_chance = max(0, min(100, int(ui_state.imposter_percent_buffer or "0")))
    except ValueError:
        ui_state.imposter_all_chance = 0
    set_box_style(DEFAULT_STYLE)
    set_message_box_title("Passt")
    set_message_box_text(
        f"{ui_state.player_count} Spieler, {ui_state.word_options_count} Optionen, {ui_state.imposter_all_chance}% alle Imposter. Scrollen zum Start..."
    )


def enter_word_entry() -> None:
    ui_state.screen = "word_entry"
    set_box_style(DEFAULT_STYLE)
    set_message_box_title("Geheimes Wort eingeben")
    display = ui_state.word_buffer if ui_state.word_buffer else "Wort tippen..."
    set_message_box_text(display)


def show_mode(index: int) -> None:
    ui_state.mode_index = index % len(GAME_MODES)
    current = GAME_MODES[ui_state.mode_index]
    set_box_style(current.style)
    set_message_box_title(current.name)
    set_message_box_text(current.description)


def enter_modes() -> None:
    ui_state.screen = "mode_select"
    if ui_state.mode_index >= len(GAME_MODES):
        ui_state.mode_index = 0
    show_mode(ui_state.mode_index)


def select_mode() -> None:
    current = GAME_MODES[ui_state.mode_index]
    if current.source == "back":
        reset_idle()
        return
    ui_state.selected_mode = current
    set_box_style(current.style)
    pending_action = ""
    if current.source == "player":
        ui_state.word_buffer = ""
        pending_action = "enter_word_entry"
    elif current.source == "random":
        pending_action = "start_random_flow"
    elif current.source == "random_simple":
        pending_action = "start_random_simple"
    else:
        pending_action = "prepare_word_and_start"
    start_handoff(time.monotonic(), advance_after=False, pending_action=pending_action)


def choose_word_for_source(source: str) -> str:
    pool = WORDS
    if not pool:
        return "mystery"
    return random.choice(pool)


def assign_imposter() -> None:
    if ui_state.player_count <= 1:
        ui_state.imposter_index = None
        ui_state.all_imposter_except_first = False
        return
    ui_state.all_imposter_except_first = False
    roll = random.randint(1, 100)
    if roll <= ui_state.imposter_all_chance:
        ui_state.imposter_index = None
        ui_state.all_imposter_except_first = True
        return
    if ui_state.selected_mode and ui_state.selected_mode.source == "player":
        ui_state.imposter_index = random.randint(1, ui_state.player_count - 1)
    elif (
        ui_state.selected_mode
        and ui_state.selected_mode.source == "random"
        and ui_state.word_options_count > 1
    ):
        ui_state.imposter_index = random.randint(1, ui_state.player_count - 1)
    elif (
        ui_state.selected_mode
        and ui_state.selected_mode.source == "random_simple"
        and ui_state.word_options_count > 1
    ):
        ui_state.imposter_index = random.randint(1, ui_state.player_count - 1)
    else:
        ui_state.imposter_index = random.randint(0, ui_state.player_count - 1)


def start_waiting() -> None:
    ui_state.screen = "wait_scroll"
    player_num = ui_state.current_player + 1
    set_box_style(DEFAULT_STYLE)
    set_message_box_title("Wischen, um Wort zu erhalten")
    set_message_box_text(f"Spieler {player_num} von {ui_state.player_count}")


def start_reveal(now: float) -> None:
    ui_state.screen = "reveal"
    ui_state.reveal_until = now + 3
    if is_current_imposter():
        set_box_style(IMPOSTER_STYLE)
        set_message_box_title("Du bist der Imposter")
        set_message_box_text("Lass dir nichts anmerken.")
    else:
        set_box_style(ui_state.selected_mode.style if ui_state.selected_mode else DEFAULT_STYLE)
        set_message_box_title("Dein Wort")
        set_message_box_text(ui_state.chosen_word or "mystery")


def start_handoff(now: float, advance_after: bool = True, pending_action: str = "") -> None:
    ui_state.screen = "handoff"
    ui_state.handoff_until = now + 3
    ui_state.handoff_advance = advance_after
    ui_state.handoff_pending_action = pending_action
    set_box_style(DEFAULT_STYLE)
    set_message_box_title("Weitergeben")
    set_message_box_text("Gebe das Gerät dem nächsten Spieler")


def advance_player() -> None:
    ui_state.current_player += 1
    if ui_state.current_player >= ui_state.player_count:
        ui_state.screen = "done"
        set_box_style(DEFAULT_STYLE)
        set_message_box_title("Fertig")
        set_message_box_text("Alle bereit – startet das Spiel!")
    else:
        start_waiting()


def prepare_word_and_start() -> None:
    if not ui_state.selected_mode:
        return
    if ui_state.selected_mode.source == "player":
        ui_state.chosen_word = ui_state.word_buffer or "mystery"
    elif ui_state.selected_mode.source == "random" and ui_state.chosen_word:
        pass
    elif ui_state.selected_mode.source == "random_simple" and ui_state.chosen_word:
        pass
    else:
        ui_state.chosen_word = choose_word_for_source(ui_state.selected_mode.source)
    ui_state.current_player = 0
    assign_imposter()
    if (
        ui_state.word_options_count > 1
        and ui_state.player_count > 1
        and ui_state.current_player == 0
    ):
        start_handoff(time.monotonic(), advance_after=True)
    elif ui_state.word_options_count > 1:
        start_reveal(time.monotonic())
    else:
        start_waiting()


def start_random_flow() -> None:
    if ui_state.word_options_count <= 1:
        ui_state.chosen_word = choose_word_for_source("random")
        ui_state.random_candidates = []
        ui_state.random_filter = ""
        prepare_word_and_start()
        return

    ui_state.random_candidates = random.sample(WORDS, min(ui_state.word_options_count, len(WORDS)))
    ui_state.random_filter = ""
    enter_random_pick()


def start_random_simple() -> None:
    if ui_state.word_options_count <= 1:
        ui_state.chosen_word = choose_word_for_source("random")
        ui_state.simple_candidates = []
        ui_state.simple_index = 0
        prepare_word_and_start()
        return

    pool_size = len(WORDS)
    count = ui_state.word_options_count if ui_state.word_options_count > 0 else pool_size
    count = max(1, min(count, pool_size))
    ui_state.simple_candidates = random.sample(WORDS, count)
    ui_state.simple_index = 0
    ui_state.simple_hold_started = False
    ui_state.simple_hold_until = 0.0
    enter_random_simple()


def enter_random_simple(now: float | None = None) -> None:
    if now is None:
        now = time.monotonic()
    ui_state.screen = "random_simple"
    set_box_style(IMPOSTER_STYLE)
    word = ui_state.simple_candidates[ui_state.simple_index] if ui_state.simple_candidates else "..."
    set_message_box_title(word.upper())
    if ui_state.simple_hold_started and ui_state.simple_hold_until > 0:
        remaining = max(0, int(ui_state.simple_hold_until - now + 0.999))
        set_message_box_text(f"Auswahl in {remaining} sek")
    else:
        set_message_box_text("Scrollen zum Durchblättern")


def best_random_choice() -> str:
    candidates = ui_state.random_candidates or []
    if not candidates:
        return choose_word_for_source("random")
    filt = ui_state.random_filter.lower()
    if not filt:
        return candidates[0]

    def tail_prefix_len(f: str, w: str) -> int:
        limit = min(len(f), len(w))
        for k in range(limit, 0, -1):
            if w.startswith(f[-k:]):
                return k
        return 0

    def tail_any_len(f: str, w: str) -> int:
        limit = min(len(f), len(w))
        for k in range(limit, 0, -1):
            if f[-k:] in w:
                return k
        return 0

    def score(word: str) -> tuple[int, int, int, int, int]:
        w = word.lower()
        tail_pref = tail_prefix_len(filt, w)
        tail_any = tail_any_len(filt, w)
        # subsequence score
        idx = 0
        sub = 0
        for ch in filt:
            found = w.find(ch, idx)
            if found == -1:
                continue
            sub += 1
            idx = found + 1
        # prefix score
        prefix = 0
        for a, b in zip(filt, w):
            if a == b:
                prefix += 1
            else:
                break
        return tail_pref, tail_any, prefix, sub, -len(word)

    best = max(candidates, key=lambda w: score(w))
    return best


def enter_random_pick() -> None:
    ui_state.screen = "random_pick"
    set_box_style(IMPOSTER_STYLE)
    top = best_random_choice()
    others = " ".join(ui_state.random_candidates) if ui_state.random_candidates else "keine"
    title = top.upper() if ui_state.random_filter else "Start Typing"
    set_message_box_title(title)
    set_message_box_text(f"{others}")


def lock_random_choice() -> None:
    ui_state.chosen_word = best_random_choice()
    ui_state.random_filter = ""
    ui_state.random_candidates = []
    prepare_word_and_start()


def lock_random_simple_choice() -> None:
    if not ui_state.simple_candidates:
        ui_state.chosen_word = choose_word_for_source("random")
    else:
        ui_state.chosen_word = ui_state.simple_candidates[ui_state.simple_index]
    ui_state.simple_hold_started = False
    ui_state.simple_hold_until = 0.0
    prepare_word_and_start()


def update_timers(now: float) -> None:
    if (
        ui_state.screen == "random_simple"
        and ui_state.simple_hold_started
        and ui_state.simple_hold_until > 0
        and now >= ui_state.simple_hold_until
    ):
        lock_random_simple_choice()
        return

    if ui_state.screen == "reveal" and now >= ui_state.reveal_until:
        if ui_state.current_player + 1 >= ui_state.player_count:
            advance_player()
            ui_state.scroll_block_until = now + 1.0
        else:
            start_handoff(now, advance_after=True)
            ui_state.scroll_block_until = now + 1.0
    elif ui_state.screen == "handoff" and now >= ui_state.handoff_until:
        if ui_state.handoff_pending_action:
            action = ui_state.handoff_pending_action
            ui_state.handoff_pending_action = ""
            if action == "enter_word_entry":
                enter_word_entry()
            elif action == "start_random_flow":
                start_random_flow()
            elif action == "start_random_simple":
                start_random_simple()
            elif action == "prepare_word_and_start":
                prepare_word_and_start()
        elif ui_state.handoff_advance:
            advance_player()
        else:
            start_waiting()
        ui_state.scroll_block_until = now + 1.0


def on_direction(direction: str) -> None:
    if ui_state.screen == "handoff":
        return
    if ui_state.screen == "wait_scroll":
        start_reveal(time.monotonic())
        return
    if ui_state.screen == "reveal":
        if ui_state.current_player + 1 >= ui_state.player_count:
            advance_player()
        else:
            now = time.monotonic()
            start_handoff(now, advance_after=True)
            ui_state.scroll_block_until = now + 1.0
        return
    if ui_state.screen == "done":
        enter_modes()
        return
    if ui_state.screen == "random_simple":
        if direction in (GO_DIRECTION, BACK_DIRECTION):
            length = len(ui_state.simple_candidates) if ui_state.simple_candidates else len(WORDS)
            length = max(1, length)
            delta = 1 if direction == GO_DIRECTION else -1
            ui_state.simple_index = (ui_state.simple_index + delta) % length
            ui_state.simple_hold_started = True
            ui_state.simple_hold_until = time.monotonic() + 5.0
            enter_random_simple(time.monotonic())
        return
    if direction == GO_DIRECTION:
        if ui_state.screen == "idle":
            enter_player_input()
        elif ui_state.screen == "player_input":
            if ui_state.input_buffer:
                enter_word_count_input()
        elif ui_state.screen == "word_count":
            enter_imposter_percent_input()
        elif ui_state.screen == "imposter_percent":
            enter_confirm()
        elif ui_state.screen == "confirm":
            enter_modes()
        elif ui_state.screen == "mode_select":
            select_mode()
        elif ui_state.screen == "word_entry":
            if ui_state.word_buffer:
                prepare_word_and_start()
        elif ui_state.screen == "random_pick":
            lock_random_choice()
    elif direction == BACK_DIRECTION:
        if ui_state.screen == "player_input":
            reset_idle()
        elif ui_state.screen == "confirm":
            enter_imposter_percent_input()
        elif ui_state.screen == "mode_select":
            show_mode(ui_state.mode_index - 1)
        elif ui_state.screen == "word_entry":
            enter_modes()
        elif ui_state.screen == "word_count":
            enter_player_input()
        elif ui_state.screen == "imposter_percent":
            enter_word_count_input()
        elif ui_state.screen == "random_pick":
            enter_modes()


def run(stdscr: curses.window) -> None:
    curses.curs_set(0)
    init_colors()
    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
    curses.mouseinterval(0)
    stdscr.nodelay(True)
    stdscr.timeout(50)
    reset_idle()

    while True:
        now = time.monotonic()
        update_scroll_state(now)
        update_timers(now)
        render(stdscr)

        ch = stdscr.getch()
        if ch == -1:
            continue
        if ch == curses.KEY_RESIZE:
            continue
        if ch in (curses.KEY_DOWN, curses.KEY_UP):
            direction = GO_DIRECTION if ch == curses.KEY_DOWN else BACK_DIRECTION
            handle_scroll(direction, now)
            continue
        if ch == curses.KEY_MOUSE:
            try:
                _, _, _, _, bstate = curses.getmouse()
            except curses.error:
                continue
            direction = None
            if bstate & curses.BUTTON4_PRESSED:
                direction = "up"
            elif bstate & curses.BUTTON5_PRESSED:
                direction = "down"
            if direction:
                handle_scroll(direction, now)
            continue
        if ch in (10, 13):  # Enter
            if ui_state.screen in ("wait_scroll", "reveal", "done"):
                handle_scroll(GO_DIRECTION, now)
                continue
            if ui_state.screen == "player_input":
                handle_scroll(GO_DIRECTION, now)
                continue
            if ui_state.screen == "word_count":
                handle_scroll(GO_DIRECTION, now)
                continue
            if ui_state.screen == "imposter_percent":
                handle_scroll(GO_DIRECTION, now)
                continue
            if ui_state.screen == "confirm":
                handle_scroll(GO_DIRECTION, now)
                continue
            if ui_state.screen == "mode_select":
                handle_scroll(GO_DIRECTION, now)
                continue
            if ui_state.screen == "word_entry":
                handle_scroll(GO_DIRECTION, now)
                continue
            if ui_state.screen == "random_pick":
                lock_random_choice()
                continue
        if ui_state.screen == "player_input":
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                ui_state.input_buffer = ui_state.input_buffer[:-1]
                enter_player_input()
                continue
            if 48 <= ch <= 57:  # digits
                ui_state.input_buffer += chr(ch)
                enter_player_input()
                continue
        if ui_state.screen == "word_count":
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                ui_state.word_count_buffer = ui_state.word_count_buffer[:-1]
                enter_word_count_input()
                continue
            if 48 <= ch <= 57:
                ui_state.word_count_buffer += chr(ch)
                enter_word_count_input()
                continue
        if ui_state.screen == "imposter_percent":
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                ui_state.imposter_percent_buffer = ui_state.imposter_percent_buffer[:-1]
                enter_imposter_percent_input()
                continue
            if 48 <= ch <= 57:
                ui_state.imposter_percent_buffer += chr(ch)
                enter_imposter_percent_input()
                continue
        if ui_state.screen == "word_entry":
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                ui_state.word_buffer = ui_state.word_buffer[:-1]
                enter_word_entry()
                continue
            if 32 <= ch <= 126:
                ui_state.word_buffer += chr(ch)
                enter_word_entry()
                continue
        if ui_state.screen == "random_pick":
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                ui_state.random_filter = ui_state.random_filter[:-1]
                enter_random_pick()
                continue
            if ch in (10, 13):
                lock_random_choice()
                continue
            if 32 <= ch <= 126:
                ui_state.random_filter += chr(ch)
                enter_random_pick()
                continue


def main() -> None:
    curses.wrapper(run)


if __name__ == "__main__":
    main()
