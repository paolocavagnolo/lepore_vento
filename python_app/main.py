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
SERIAL_PORT = "/dev/ttyACM0"     # porta seriale della pala
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
BG_ROTATE_SECONDS = 6            # cambia sfondo ogni N secondi

# Colori (coerenti con la palette MAGIS delle immagini)
PINK = (236, 30, 121)            # rosa MAGIS
WHITE = (255, 255, 255)

# Font
FONT_NAME = "dejavusans"         # font di sistema, sempre presente su Raspbian

# Mappatura rotazione -> vento -> potenza
# rotazione: 0..500 rpm (valore letto in seriale)
# vento: 0..15 m/s (scala lineare, modificabile)
MAX_ROTATION = 500.0
MAX_WIND_MS = 15.0

# Potenza: modello semplificato P = 0.5 * rho * A * v^3 * Cp
AIR_DENSITY = 1.225              # kg/m^3
BLADE_RADIUS = 0.5               # m  (raggio della pala didattica)
SWEPT_AREA = math.pi * BLADE_RADIUS ** 2
POWER_COEFF = 0.35               # Cp tipico

# Filtro sulla lettura (media mobile) per evitare sfarfallio.
# Dati a 20 Hz -> 10 campioni = 0.5 s di finestra.
SMOOTHING = 10

# Frame rate del display (Hz). Il loop drena comunque tutti i campioni
# arrivati nel frattempo, quindi non si perde nessuna lettura.
DISPLAY_FPS = 30


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------
def rotation_to_wind(rot: float) -> float:
    rot = max(0.0, min(MAX_ROTATION, rot))
    return rot / MAX_ROTATION * MAX_WIND_MS


def wind_to_power(v_ms: float) -> float:
    """Ritorna la potenza in watt."""
    return 0.5 * AIR_DENSITY * SWEPT_AREA * (v_ms ** 3) * POWER_COEFF


def open_serial():
    try:
        return serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=SERIAL_TIMEOUT)
    except Exception as exc:
        print(f"[WARN] Impossibile aprire {SERIAL_PORT}: {exc}", file=sys.stderr)
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # su alcune installazioni RPi serve per il framebuffer
    os.environ.setdefault("SDL_VIDEO_CENTERED", "1")

    pygame.init()
    flags = pygame.FULLSCREEN if FULLSCREEN else 0
    screen = pygame.display.set_mode(SCREEN_SIZE, flags)
    pygame.display.set_caption("Parco Eolico MAGIS")
    pygame.mouse.set_visible(False)

    clock = pygame.time.Clock()

    # Precarica immagini
    start_img = load_image_scaled(START_IMAGE, SCREEN_SIZE)
    bgs = [load_image_scaled(p, SCREEN_SIZE) for p in BG_IMAGES if p.exists()]
    if not bgs:
        # fallback: sfondo rosa pieno
        surf = pygame.Surface(SCREEN_SIZE)
        surf.fill(PINK)
        bgs = [surf]

    # Font grandi
    font_label = pygame.font.SysFont(FONT_NAME, 90, bold=True)
    font_value = pygame.font.SysFont(FONT_NAME, 260, bold=True)
    font_unit = pygame.font.SysFont(FONT_NAME, 90, bold=True)
    font_footer = pygame.font.SysFont(FONT_NAME, 60, bold=True)

    ser = open_serial()
    samples: list[float] = []
    last_rot = 0.0

    bg_index = 0
    bg_last_switch = time.time()

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
        # A 20 Hz arrivano ~0.67 campioni per frame (a 30 fps): svuotiamo
        # tutto il buffer disponibile e accumuliamo ogni valore nella media
        # mobile, così nessuna lettura viene scartata.
        if ser is not None:
            while ser.in_waiting > 0:
                v = read_rotation(ser)
                if v is None:
                    break
                samples.append(v)
                if len(samples) > SMOOTHING:
                    samples.pop(0)
            if samples:
                last_rot = sum(samples) / len(samples)

        # --- rendering ---
        if last_rot <= 0.5:
            screen.blit(start_img, (0, 0))
        else:
            # ruota sfondo
            if time.time() - bg_last_switch > BG_ROTATE_SECONDS:
                bg_index = (bg_index + 1) % len(bgs)
                bg_last_switch = time.time()

            # sfondo rosa pieno per garantire leggibilità
            screen.fill(PINK)

            wind = rotation_to_wind(last_rot)
            power = wind_to_power(wind)

            draw_metric(
                screen,
                label="VELOCITÀ DEL VENTO",
                value=f"{wind:.1f}",
                unit="m/s",
                center=(SCREEN_SIZE[0] // 2, 340),
                font_label=font_label,
                font_value=font_value,
                font_unit=font_unit,
            )
            draw_metric(
                screen,
                label="POTENZA GENERATA",
                value=format_power(power),
                unit=power_unit(power),
                center=(SCREEN_SIZE[0] // 2, 780),
                font_label=font_label,
                font_value=font_value,
                font_unit=font_unit,
            )

            footer = font_footer.render("PARCO EOLICO MAGIS", True, WHITE)
            screen.blit(
                footer,
                footer.get_rect(center=(SCREEN_SIZE[0] // 2, SCREEN_SIZE[1] - 60)),
            )

        pygame.display.flip()
        clock.tick(DISPLAY_FPS)

    pygame.quit()
    if ser is not None:
        ser.close()


def draw_metric(screen, label, value, unit, center, font_label, font_value, font_unit):
    cx, cy = center
    lbl = font_label.render(label, True, WHITE)
    val = font_value.render(value, True, WHITE)
    unt = font_unit.render(unit, True, WHITE)

    screen.blit(lbl, lbl.get_rect(center=(cx, cy - 130)))

    # valore + unità affiancati
    total_w = val.get_width() + 20 + unt.get_width()
    x0 = cx - total_w // 2
    screen.blit(val, val.get_rect(midleft=(x0, cy + 40)))
    screen.blit(unt, unt.get_rect(midleft=(x0 + val.get_width() + 20, cy + 80)))


def format_power(watt: float) -> str:
    if watt >= 1000:
        return f"{watt / 1000:.2f}"
    return f"{watt:.1f}"


def power_unit(watt: float) -> str:
    return "kW" if watt >= 1000 else "W"


if __name__ == "__main__":
    main()