#!/usr/bin/env python3
"""
Parco Eolico MAGIS - Display fullscreen per Raspberry Pi.

Legge la velocità di rotazione (0-500) dalla pala eolica via seriale USB
e mostra a schermo intero:
  - immagine iniziale quando la pala è ferma (rotazione == 0)
  - velocità del vento e potenza generata quando la pala gira

Avvio automatico: aggiungere in /etc/xdg/lxsession/LXDE-pi/autostart:
    @/usr/bin/python3 /home/pi/rpi-app/wind_display.py
"""

import os
import sys
import math
import time
from pathlib import Path

import pygame
import serial

# ---------------------------------------------------------------------------
# CONFIGURAZIONE
# ---------------------------------------------------------------------------
# Porte seriali possibili: viene provata la prima disponibile
SERIAL_PORTS = [
    "/dev/ttyACM0",
    "/dev/ttyUSB0",
    "/dev/ttyACM1",
    "/dev/ttyUSB1",
]
SERIAL_BAUD = 9600
SERIAL_TIMEOUT = 0.01            # s - lettura non bloccante (dati a 20 Hz)

SCREEN_SIZE = (1920, 1080)
FULLSCREEN = True

ASSETS_DIR = Path(__file__).parent / "imgs"
START_IMAGE = ASSETS_DIR / "inizio.png"
BG_IMAGES = [
    ASSETS_DIR / "sfondo_1.jpg",
    ASSETS_DIR / "sfondo_2.jpg",
    ASSETS_DIR / "sfondo_3.jpg",
]
# Soglie di potenza (W): quando la potenza generata supera la soglia
# viene mostrato lo sfondo corrispondente (indice = numero di soglie superate - 1).
# Sotto la prima soglia si vede solo il rosa pieno.
POWER_THRESHOLDS_W = [20.0, 31.0, 42.0]
# Isteresi: soglia più bassa per scendere di livello, così quando il vento
# oscilla intorno alla soglia le immagini non cambiano continuamente.
POWER_HYSTERESIS_W = 5.0

# Opacità (0-255) del pannello rosa dietro le scritte quando c'è uno sfondo immagine.
TEXT_PANEL_ALPHA = 180
# Raggio angoli arrotondati dei pannelli (px @1920x1080, viene scalato).
PANEL_RADIUS = 24
# Padding interno pannelli (px @1920x1080).
PANEL_PADDING_X = 28
PANEL_PADDING_Y = 12

# Durata (s) del fade quando cambia lo sfondo per soglia di potenza.
BG_FADE_SECONDS = 0.4

# Il messaggio "MODALITÀ TEST" scompare dopo questi secondi.
TEST_HINT_VISIBLE_SECONDS = 4.0

# Colori (coerenti con la palette MAGIS delle immagini)
PINK = (236, 30, 121)            # rosa MAGIS
PINK_DARK = (150, 15, 75)        # per ombra testo
WHITE = (255, 255, 255)

# Font
FONT_NAME = "dejavusans"         # font di sistema, sempre presente su Raspbian

# Mappatura rotazione -> vento -> potenza
# rotazione: 0..500 rpm (valore letto in seriale)
# vento: 0..15 m/s (scala lineare, modificabile)
MAX_ROTATION = 200.0
MAX_WIND_MS = 15.0

# Potenza: modello semplificato P = 0.5 * rho * A * v^3 * Cp
AIR_DENSITY = 1.225              # kg/m^3
BLADE_RADIUS = 0.25               # m  (raggio della pala didattica)
SWEPT_AREA = math.pi * BLADE_RADIUS ** 2
POWER_COEFF = 0.80               # Cp tipico

# Filtro sulla lettura (media mobile) per evitare sfarfallio.
# Dati a 20 Hz -> 10 campioni = 0.5 s di finestra.
SMOOTHING = 7

# Frame rate del display (Hz). Il loop drena comunque tutti i campioni
# arrivati nel frattempo, quindi non si perde nessuna lettura.
DISPLAY_FPS = 30

# Ogni quanto aggiornare i numeri mostrati a schermo (s).
# La lettura seriale continua a 20 Hz, ma il valore visualizzato
# viene "congelato" per questo intervallo così da non essere illeggibile.
VALUE_REFRESH_SECONDS = 0.1

# Se la rotazione resta a 0 per questo tempo torna alla schermata iniziale.
IDLE_TIMEOUT_SECONDS = 10.0

# --- Modalità test (senza pala eolica) ---------------------------------------
# Se nessuna porta seriale è disponibile, si entra in modalità test:
# la freccia SU aumenta la rotazione, la freccia GIÙ la diminuisce.
TEST_STEP_PER_SEC = 200.0        # incremento (unità rotazione) al secondo tenendo premuto
TEST_DECAY_PER_SEC = 80.0        # decadimento automatico quando non si preme nulla


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------
def rotation_to_wind(rot: float) -> float:
    rot = max(0.0, min(MAX_ROTATION, rot))
    return rot / MAX_ROTATION * MAX_WIND_MS


def wind_to_power(v_ms: float) -> float:
    """Ritorna la potenza in watt."""
    return 0.5 * AIR_DENSITY * SWEPT_AREA * (v_ms ** 3) * POWER_COEFF


def power_level(power: float, current: int) -> int:
    """
    Calcola il livello con isteresi.
    Per salire serve superare la soglia; per scendere serve scendere sotto
    la soglia meno l'isteresi.
    """
    level = current
    # tenta di salire
    while level < len(POWER_THRESHOLDS_W) and power >= POWER_THRESHOLDS_W[level]:
        level += 1
    # tenta di scendere
    while level > 0 and power < (POWER_THRESHOLDS_W[level - 1] - POWER_HYSTERESIS_W):
        level -= 1
    return level


def open_serial():
    for port in SERIAL_PORTS:
        try:
            ser = serial.Serial(port, SERIAL_BAUD, timeout=SERIAL_TIMEOUT)
            print(f"[INFO] Seriale aperta su {port}")
            return ser
        except Exception as exc:
            print(f"[WARN] {port} non disponibile: {exc}", file=sys.stderr)
    print("[WARN] Nessuna porta seriale disponibile", file=sys.stderr)
    return None


def read_rotation(ser) -> float | None:
    """Legge una riga dalla seriale e prova a interpretarla come numero."""
    if ser is None:
        return None
    try:
        line = ser.readline().decode("ascii", errors="ignore").strip()
    except Exception:
        return None
    if not line:
        return None
    # accetta "123", "123.4", "rpm=123", ecc.
    digits = "".join(c for c in line if c.isdigit() or c in ".-")
    if not digits:
        return None
    try:
        return float(digits)
    except ValueError:
        return None


def load_image_scaled(path: Path, size):
    img = pygame.image.load(str(path)).convert()
    return pygame.transform.smoothscale(img, size)


def load_image_cover(path: Path, size):
    """Carica l'immagine e la scala per coprire `size` mantenendo le proporzioni."""
    img = pygame.image.load(str(path)).convert()
    img_w, img_h = img.get_size()
    target_w, target_h = size

    # Fattore di scala per coprire tutto lo schermo
    scale = max(target_w / img_w, target_h / img_h)
    new_w = int(img_w * scale)
    new_h = int(img_h * scale)

    scaled = pygame.transform.smoothscale(img, (new_w, new_h))
    # Ritaglia i bordi in eccesso per centrare
    crop_x = (new_w - target_w) // 2
    crop_y = (new_h - target_h) // 2
    return scaled.subsurface((crop_x, crop_y, target_w, target_h)).copy()



# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # su alcune installazioni RPi serve per il framebuffer
    os.environ.setdefault("SDL_VIDEO_CENTERED", "1")

    pygame.init()
    flags = pygame.FULLSCREEN if FULLSCREEN else 0
    # In fullscreen usiamo (0,0) per prendere la risoluzione reale del display,
    # altrimenti pygame potrebbe scalare e lasciare bande fuori scala.
    mode_size = (0, 0) if FULLSCREEN else SCREEN_SIZE
    screen = pygame.display.set_mode(mode_size, flags)
    actual_size = screen.get_size()
    print(f"[INFO] Risoluzione display: {actual_size[0]}x{actual_size[1]}")
    pygame.display.set_caption("Parco Eolico MAGIS")
    pygame.mouse.set_visible(False)

    clock = pygame.time.Clock()

    # Precarica immagini
    start_img = load_image_cover(START_IMAGE, actual_size)
    bgs = [load_image_cover(p, actual_size) for p in BG_IMAGES if p.exists()]

    if not bgs:
        # fallback: sfondo rosa pieno
        surf = pygame.Surface(actual_size)
        surf.fill(PINK)
        bgs = [surf]

    # Font grandi — scalati sulla risoluzione reale (riferimento 1920x1080).
    scale = min(actual_size[0] / SCREEN_SIZE[0], actual_size[1] / SCREEN_SIZE[1])
    def sz(px: int) -> int:
        return max(10, int(px * scale))
    # Font per la schermata iniziale (rosa pieno): numeri grandi e centrati.
    font_label_idle = pygame.font.SysFont(FONT_NAME, sz(90), bold=True)
    font_value_idle = pygame.font.SysFont(FONT_NAME, sz(260), bold=True)
    font_unit_idle = pygame.font.SysFont(FONT_NAME, sz(90), bold=True)
    # Font più piccoli quando appare un'immagine di sfondo, così il soggetto resta visibile.
    font_label_bg = pygame.font.SysFont(FONT_NAME, sz(60), bold=True)
    font_value_bg = pygame.font.SysFont(FONT_NAME, sz(170), bold=True)
    font_unit_bg = pygame.font.SysFont(FONT_NAME, sz(60), bold=True)
    font_footer = pygame.font.SysFont(FONT_NAME, sz(60), bold=True)

    ser = open_serial()
    test_mode = ser is None
    if test_mode:
        print("[INFO] Nessuna seriale trovata: modalità TEST attiva "
              "(freccia SU/GIÙ per regolare la velocità)")
    samples: list[float] = []
    last_rot = 0.0
    displayed_rot = 0.0
    last_value_refresh = 0.0
    # Inizializziamo "nel passato" così all'avvio siamo già in stato idle
    # e mostriamo la schermata iniziale finché non arriva una rotazione > 0.
    last_nonzero_time = time.time() - IDLE_TIMEOUT_SECONDS - 1.0
    test_rot = 0.0
    last_frame_time = time.time()
    app_start_time = time.time()
    current_level = 0
    prev_level = 0
    level_change_time = -10.0  # nel passato -> nessun fade iniziale

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key in (
                pygame.K_ESCAPE,
                pygame.K_q,
            ):
                running = False

        # --- lettura seriale ---
        now = time.time()
        dt = max(0.0, now - last_frame_time)
        last_frame_time = now

        if ser is not None:
            # A 20 Hz arrivano ~0.67 campioni per frame (a 30 fps): svuotiamo
            # tutto il buffer disponibile e accumuliamo ogni valore nella media
            # mobile, così nessuna lettura viene scartata.
            while ser.in_waiting > 0:
                v = read_rotation(ser)
                if v is None:
                    break
                samples.append(v)
                if len(samples) > SMOOTHING:
                    samples.pop(0)
            if samples:
                last_rot = sum(samples) / len(samples)
        else:
            # Modalità test: freccia SU aumenta, GIÙ diminuisce, altrimenti decade.
            keys = pygame.key.get_pressed()
            if keys[pygame.K_UP]:
                test_rot += TEST_STEP_PER_SEC * dt
            elif keys[pygame.K_DOWN]:
                test_rot -= TEST_STEP_PER_SEC * dt
            else:
                # decadimento morbido verso 0
                if test_rot > 0:
                    test_rot = max(0.0, test_rot - TEST_DECAY_PER_SEC * dt)
            test_rot = max(0.0, min(MAX_ROTATION, test_rot))
            last_rot = test_rot

        if last_rot > 0.5:
            last_nonzero_time = now
        # Aggiorna il valore mostrato solo ogni VALUE_REFRESH_SECONDS
        if now - last_value_refresh >= VALUE_REFRESH_SECONDS:
            displayed_rot = last_rot
            last_value_refresh = now

        idle = (now - last_nonzero_time) > IDLE_TIMEOUT_SECONDS

        # --- rendering ---
        # Torna alla schermata iniziale SOLO dopo IDLE_TIMEOUT_SECONDS
        # di rotazione a zero: se la pala rallenta un attimo continuiamo
        # a mostrare velocità/potenza (congelate a 0) finché non scade.
        if idle:
            screen.blit(start_img, (0, 0))
        else:
            wind = rotation_to_wind(displayed_rot)
            power = wind_to_power(wind)

            # Scegli lo sfondo in base al numero di soglie di potenza superate,
            # con isteresi per evitare cambi continui vicino alle soglie.
            level = power_level(power, current_level)
            if level != current_level:
                prev_level = current_level
                current_level = level
                level_change_time = now

            def draw_bg_for(lv: int):
                if lv == 0 or not bgs:
                    screen.fill(PINK)
                else:
                    idx = min(lv - 1, len(bgs) - 1)
                    screen.blit(bgs[idx], (0, 0))

            # fade tra vecchio e nuovo sfondo
            fade_t = (now - level_change_time) / BG_FADE_SECONDS
            if fade_t < 1.0 and prev_level != current_level:
                # disegna il precedente, poi il nuovo con alpha crescente
                draw_bg_for(prev_level)
                overlay = pygame.Surface(actual_size).convert()
                if current_level == 0 or not bgs:
                    overlay.fill(PINK)
                else:
                    idx = min(current_level - 1, len(bgs) - 1)
                    overlay.blit(bgs[idx], (0, 0))
                overlay.set_alpha(int(255 * max(0.0, min(1.0, fade_t))))
                screen.blit(overlay, (0, 0))
            else:
                draw_bg_for(current_level)

            # Layout: centrato quando siamo su rosa pieno, disposto negli angoli
            # quando c'è un'immagine di sfondo così il soggetto resta visibile.
            has_bg = current_level > 0 and bool(bgs)
            if has_bg:
                vel_center = (
                    int(actual_size[0] * 0.20),
                    int(actual_size[1] * 0.20),
                )
                pow_center = (
                    int(actual_size[0] * 0.80),
                    int(actual_size[1] * 0.80),
                )
                font_label = font_label_bg
                font_value = font_value_bg
                font_unit = font_unit_bg
            else:
                vel_center = (actual_size[0] // 2, int(actual_size[1] * 0.32))
                pow_center = (actual_size[0] // 2, int(actual_size[1] * 0.72))
                font_label = font_label_idle
                font_value = font_value_idle
                font_unit = font_unit_idle

            draw_metric(
                screen,
                label="VELOCITÀ DEL VENTO",
                value=f"{wind:.1f}",
                unit="m/s",
                center=vel_center,
                font_label=font_label,
                font_value=font_value,
                font_unit=font_unit,
                scale=scale,
                with_panel=has_bg,
            )
            draw_metric(
                screen,
                label="POTENZA GENERATA",
                value=format_power(power),
                unit=power_unit(power),
                center=pow_center,
                font_label=font_label,
                font_value=font_value,
                font_unit=font_unit,
                scale=scale,
                with_panel=has_bg,
            )

            # Footer con ombra morbida: visibile solo sul rosa pieno,
            # negli sfondi con immagine lo nascondiamo per non coprire il soggetto.
            if not has_bg:
                draw_text_shadowed(
                    screen, font_footer, "PARCO EOLICO MAGIS",
                    center=(actual_size[0] // 2, actual_size[1] - sz(50)),
                    scale=scale,
                )

            # Hint modalità test: visibile solo per i primi secondi, poi sparisce.
            if test_mode:
                hint_age = now - app_start_time
                if hint_age < TEST_HINT_VISIBLE_SECONDS:
                    alpha = 255
                    if hint_age > TEST_HINT_VISIBLE_SECONDS - 1.0:
                        alpha = int(255 * (TEST_HINT_VISIBLE_SECONDS - hint_age))
                    hint_font = pygame.font.SysFont(FONT_NAME, sz(22))
                    hint = hint_font.render(
                        "MODALITÀ TEST — ↑/↓ per regolare la velocità", True, WHITE
                    )
                    hint.set_alpha(max(0, min(255, alpha)))
                    screen.blit(hint, (sz(20), sz(20)))

        pygame.display.flip()
        clock.tick(DISPLAY_FPS)

    pygame.quit()
    if ser is not None:
        ser.close()


def _blit_text_with_shadow(screen, surf, rect, scale=1.0, shadow=True):
    if shadow:
        # ombra soft: 2 offset per un effetto morbido
        off = max(2, int(4 * scale))
        shadow_surf = surf.copy()
        # tinge di scuro convertendo i pixel bianchi
        dark = pygame.Surface(surf.get_size(), pygame.SRCALPHA)
        dark.fill((PINK_DARK[0], PINK_DARK[1], PINK_DARK[2], 200))
        shadow_surf.blit(dark, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        srect = rect.copy()
        srect.x += off
        srect.y += off
        screen.blit(shadow_surf, srect)
    screen.blit(surf, rect)


def draw_text_shadowed(screen, font, text, center, scale=1.0):
    surf = font.render(text, True, WHITE)
    rect = surf.get_rect(center=center)
    _blit_text_with_shadow(screen, surf, rect, scale=scale)


def draw_metric(screen, label, value, unit, center, font_label, font_value,
                font_unit, scale=1.0, with_panel=False):
    cx, cy = center
    lbl = font_label.render(label, True, WHITE)
    val = font_value.render(value, True, WHITE)
    unt = font_unit.render(unit, True, WHITE)

    gap = int(20 * scale)
    lbl_rect = lbl.get_rect(center=(cx, cy - int(130 * scale)))

    total_w = val.get_width() + gap + unt.get_width()
    x0 = cx - total_w // 2
    # unità allineata alla baseline del numero (non a mezz'altezza)
    val_rect = val.get_rect(midleft=(x0, cy + int(40 * scale)))
    unt_rect = unt.get_rect(bottomleft=(val_rect.right + gap, val_rect.bottom - int(20 * scale)))

    # Pannello rosa arrotondato dietro le scritte per leggibilità sopra all'immagine.
    if with_panel:
        pad_x = int(PANEL_PADDING_X * scale)
        pad_y = int(PANEL_PADDING_Y * scale)
        union = lbl_rect.union(val_rect).union(unt_rect)
        panel_rect = union.inflate(pad_x * 2, pad_y * 2)
        panel = pygame.Surface(panel_rect.size, pygame.SRCALPHA)
        radius = int(PANEL_RADIUS * scale)
        pygame.draw.rect(
            panel,
            (PINK[0], PINK[1], PINK[2], TEXT_PANEL_ALPHA),
            panel.get_rect(),
            border_radius=radius,
        )
        screen.blit(panel, panel_rect)

    _blit_text_with_shadow(screen, lbl, lbl_rect, scale=scale, shadow=not with_panel)
    _blit_text_with_shadow(screen, val, val_rect, scale=scale, shadow=not with_panel)
    _blit_text_with_shadow(screen, unt, unt_rect, scale=scale, shadow=not with_panel)


def format_power(watt: float) -> str:
    if watt >= 1000:
        return f"{watt / 1000:.2f}"
    return f"{watt:.1f}"


def power_unit(watt: float) -> str:
    return "kW" if watt >= 1000 else "W"


if __name__ == "__main__":
    main()