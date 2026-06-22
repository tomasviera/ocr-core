#!/usr/bin/env python
"""
scripts/transcribir_aistudio.py

Wrapper de producción que transcribe UNA imagen vía Google AI Studio web,
conectándose a Chrome por CDP (con la sesión del usuario ya logueada).

Producto del POC `scripts/poc_aistudio.py`. Diferencias:
  - Sin pausas debug ni Playwright Inspector.
  - Escribe `salida.json` con el shape que espera `web/includes/lib_aistudio.php`.
  - Cierra el tab tras éxito (salvo `--no-cerrar-tab-error` en falla).
  - Maneja modo split (System Instructions + chat input) buscando el marcador
    `<<<CHAT_INPUT>>>` en el archivo del prompt.

Lo invoca PHP desde un sandbox efímero como subprocess:

  python scripts/transcribir_aistudio.py \\
      --imagen <sandbox>/image.jpg \\
      --prompt <sandbox>/prompt.md \\
      --salida-json <sandbox>/salida.json \\
      --cdp http://localhost:9222 \\
      --modelo gemini-3.1-pro-preview \\
      --timeout-respuesta 300

Exit codes:
  0  = pudo escribir `salida.json` (ok puede ser true o false adentro)
  2  = no pudo conectar a CDP (Chrome no responde)
  3  = falla pre-flight (imagen/prompt no existe)
  4  = excepción inesperada antes de escribir el JSON

PRECONDICIÓN: Chrome lanzado con --remote-debugging-port=9222 y --user-data-dir
dedicado (E:\\chrome-cdp-profile). El usuario debe estar logueado en
aistudio.google.com en ese Chrome.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import traceback
import unicodedata
from datetime import datetime
from pathlib import Path

# Windows: forzar stdout/stderr a utf-8 para prints con tildes/flechas.
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.stderr.write("ERROR: falta playwright. Instalá con:  pip install playwright\n")
    sys.exit(3)


# ============================================================
# Selectores AI Studio (snapshot 2026-05-13; variantes ES añadidas 2026-05-31)
# ============================================================
# La UI de AI Studio se localiza según el idioma de la cuenta Google (no por
# ?hl=, que se ignora). Las cuentas en español rinden los aria-label/placeholder
# traducidos, así que todo selector basado en texto enumera en/es (misma premisa
# bilingüe que POPUP_ACCEPT_LABELS y THINKING_ALIASES). Sin esto, una cuenta en
# español falla al no encontrar el textarea (#system_instructions_no_aparecio).
SEL_FILE_INPUT = 'input[type="file"]'
SEL_PROMPT_TEXTAREA = ('textarea[aria-label="Enter a prompt"], '
                       'textarea[aria-label="Introduzca una solicitud"], '
                       'textarea[placeholder*="Start typing" i], '
                       'textarea[placeholder*="Empieza a escribir" i]')
SEL_SYSTEM_INSTRUCTIONS_CARD = ('button[aria-label="System instructions"], '
                                'button[aria-label="Instrucciones del sistema"], '
                                '.system-instructions-card')
SEL_SYSTEM_INSTRUCTIONS_TEXTAREA = ('textarea[aria-label="System instructions"], '
                                    'textarea[aria-label="Instrucciones del sistema"]')
# Match parcial por dos substrings (robusto a puntuación: el ES termina en punto).
SEL_INSERT_BUTTON = ('button[aria-label*="Insert" i][aria-label*="file" i], '
                     'button[aria-label*="Inserta" i][aria-label*="archivo" i]')
MENU_ITEM_UPLOAD_HINTS = ['upload', 'computer', 'device', 'from your', 'from this', 'archivo', 'mi computadora']
SEL_STOP_BUTTON = ('button[aria-label*="Stop" i], button[aria-label*="Detener" i], '
                   'button:has-text("Stop"), button:has-text("Detener")')
SEL_MODEL_SELECTOR_CARD = '.model-selector-card'
SEL_ADV_SETTINGS_EXPAND = ('button[aria-label="Expand or collapse advanced settings"], '
                           'button[aria-label="Expandir o contraer la configuración avanzada"]')
SEL_MEDIA_RES_SELECT = ('mat-select[aria-label="Media resolution"], '
                        'mat-select[aria-label="Resolución de medios"]')
SEL_ATTACHMENT_CHIP = '.prompt-media-item-container, img[alt$=".jpg" i], img[alt$=".jpeg" i], img[alt$=".png" i]'
SEL_ATTACHMENT_READY = '.prompt-media-item-container:has-text("tokens")'
SEL_TOS_DISMISS = 'button:has-text("Dismiss")'

CHAT_INPUT_MARKER = '<<<CHAT_INPUT>>>'

# Palabras de cuota / rate-limit. NO exigimos fraseo ni orden exacto: AI Studio
# devolvió "Failed to generate content: user has exceeded quota..." (visto
# 2026-06-01, puertos 9224/9227) — dice "exceeded quota", NO "quota exceeded" —, y
# Chrome puede auto-traducir el UI al español (ver memoria del proyecto), con lo
# que el snackbar puede llegar como "...superó la cuota..." / "límite de
# frecuencia". En vez de enumerar cada fraseo, detectamos cuota con DOS señales
# combinadas (criterio de Tomás): (1) el texto es MUY corto (UMBRAL_CUOTA_CHARS)
# y (2) contiene alguna de estas palabras. El gate de longitud es la salvaguarda
# contra falsos positivos: una transcripción real que mencione "cuota"/"límite"
# (común en avisos de prensa del s.XIX) es larga y no entra.
# Bilingüe en/es. Sin marcadores demasiado genéricos sueltos ('excedido',
# 'superó') que aparecen en prosa de diario; 'cuota'/'límite' ya son distintivos
# bajo el techo de chars.
CUOTA_MARKERS = [
    # inglés
    'quota', 'rate limit', 'too many requests', 'resource exhausted',
    'usage limit',
    # español (traducción automática de Chrome)
    'cuota', 'límite de frecuencia', 'límite de uso', 'límite de velocidad',
    'demasiadas solicitudes', 'recurso agotado',
]

# Techo de caracteres por debajo del cual un texto con una palabra de CUOTA_MARKERS
# se considera error de cuota. Un snackbar real ronda los ~90 chars; lo dejamos
# holgado pero MUY por debajo del piso general de longitud sospechosa (1000) a
# propósito: así una transcripción corta que mencione "cuota"/"límite" en su
# contenido no se confunde con un error de cuota.
UMBRAL_CUOTA_CHARS = 300

# Marcadores de bloqueo de seguridad de Gemini en el UI: el turno no trae
# transcripción usable, sólo el chip de warning + chrome de la UI. "Recitation
# block" es el más común con prensa antigua que el modelo cree estar recitando.
# Sólo se evalúan en el fallback de turno (cuando no hubo chunk limpio), así que
# 'blocked'/'safety' no producen falsos positivos sobre transcripciones reales.
BLOQUEO_MARKERS = [
    'recitation', 'blocked', 'safety block', 'prohibited content',
    'content not permitted',
]

# Error de generación de AI Studio que aparece como SNACKBAR/toast (no popup),
# usualmente arriba: "Failed to generate content: permission denied. Please try
# again." (visto en cuenta F, 2026-06-01). Suele preceder o acompañar al
# "An internal error has occurred." de la caja de transcripción. Lo detectamos
# por su texto para: (a) cortar el job tratándolo como error sin reintentos
# inútiles en la misma sesión, (b) capturar el mensaje exacto —da más señal del
# tipo de fallo que el genérico internal_error—. Bilingüe best-effort: Chrome
# puede auto-traducir la UI de AI Studio al español (ver familia de selectores
# en/es de arriba).
PERMISO_MARKERS = [
    'permission denied', 'failed to generate',
    'permiso denegado', 'no se pudo generar', 'error al generar',
]

# Labels de botones (material symbols) de la toolbar de una respuesta de AI Studio.
# Cuando el turno NO produjo transcripción (la respuesta colapsó / no se renderizó),
# innerText del chunk/turno arrastra SÓLO estos labels pegados sin espacios, p.ej.
# 'downloadfullscreen' (job #6502: se guardó como DUDOSA, una "transcripción" basura).
# Sirven para reconocer una respuesta que es 100% chrome de UI y tratarla como error
# (veredicto RESPUESTA_CHROME) en vez de aceptarla. Lista best-effort; si aparece un
# label nuevo no listado, el texto deja resto y NO se marca chrome (falla conservadora:
# vuelve al camino DUDOSA de antes, nunca un falso positivo sobre transcripción real).
CHROME_UI_TOKENS = [
    'fullscreen_exit', 'open_in_full', 'close_fullscreen', 'restart_alt',
    'content_copy', 'more_vert', 'thumb_up', 'thumb_down', 'expand_content',
    'expand_more', 'expand_less', 'fullscreen', 'download', 'refresh', 'edit',
    'tune', 'close', 'code', 'share', 'delete', 'sync', 'stop', 'send', 'check',
]


def _es_chrome_ui(texto: str) -> bool:
    """True si `texto` es ÚNICAMENTE chrome de UI (labels de botones concatenados,
    sin transcripción). Una transcripción real SIEMPRE trae whitespace (espacios /
    saltos entre bloques #T#/#/B#/#/C#); el chrome viene pegado sin espacios. Se
    quitan los tokens conocidos (más largos primero, por solapamientos como
    'fullscreen' ⊂ 'fullscreen_exit') y lo que sobra debe quedar vacío."""
    if not texto:
        return False
    low = texto.strip().lower()
    if not low or any(c.isspace() for c in low):
        return False
    resto = low
    for tok in sorted(CHROME_UI_TOKENS, key=len, reverse=True):
        resto = resto.replace(tok, '')
    resto = ''.join(ch for ch in resto if ch.isalnum())
    return resto == ''

# Piso de caracteres para aceptar un chunk como transcripción real cuando ADEMÁS
# hay un error co-presente (camino REVISAR). Una transcripción de página de diario
# del s.XIX siempre supera holgadamente este piso (miles de chars con marcadores
# #T#/#/B#/#/C#); por debajo, un "chunk" suele ser chrome de la UI que innerText arrastra
# cuando el turno NO produjo texto —botones 'download'/'fullscreen', etc.— (visto en
# #5172: response='downloadfullscreen', 18 chars, junto a un snackbar "permission
# denied"). En ese caso no es REVISAR (transcripción usable + warning) sino un error
# puro: se trata como error_snackbar (→ INTERNAL_ERROR → reintenta/re-transcribe).
# Sólo aplica cuando hay error co-presente; una respuesta corta SIN error sigue su
# camino normal (DUDOSA/OK), así que no degrada transcripciones legítimas.
MIN_CHARS_TRANSCRIPCION_REVISAR = 100

# Piso FIJO de longitud para marcar una transcripción ACEPTADA (OK_SANA/DUDOSA/
# REVISAR) como sospechosa por longitud baja ("OK dudoso"). Calibrado contra el
# detector QA `longitud_baja` del WEB: 20% de la mediana de El Lucero (mediana
# 8178 chars sobre 4626 jobs OK al 2026-06-01) → 1636. Por debajo de este piso la
# respuesta casi siempre quedó TRUNCADA a mitad del stream (visto en api_call
# #5188: 279 chars de fragmentos `#T# FRANCIA / una cu / impe...`). Cuando se
# cruza, NO se descarta la transcripción (ya se guarda como hasta ahora) pero se
# setea out['longitud_sospechosa'] para dejar el tab abierto y poder inspeccionar
# en vivo por qué se cortó. Fijo a propósito (pedido de Tomás): no se pondera por
# periódico — el de Lucero es el de referencia.
# Bajado de 1636 a 1000 por pedido de Tomás (2026-06-01).
UMBRAL_LONGITUD_SOSPECHOSA = 1000

def parse_args():
    p = argparse.ArgumentParser(description="Wrapper AI Studio via Playwright + CDP")
    p.add_argument("--imagen", required=True, help="Ruta absoluta a la imagen a transcribir.")
    p.add_argument("--prompt", required=True, help="Ruta absoluta al archivo de prompt completo.")
    p.add_argument("--salida-json", required=True, dest="salida_json",
                   help="Ruta absoluta donde escribir el JSON con el resultado.")
    p.add_argument("--cdp", default="http://localhost:9222", help="Endpoint CDP.")
    p.add_argument("--modelo", default="gemini-3.1-pro-preview",
                   help="Modelo a forzar via ?model= en URL.")
    p.add_argument("--timeout-respuesta", type=int, default=300, dest="timeout_respuesta",
                   help="Segundos máximo de espera por la respuesta del modelo.")
    p.add_argument("--media-resolution", default="High", dest="media_resolution",
                   help="Opción de Media Resolution a setear (default High).")
    p.add_argument("--thinking-level", default=None, dest="thinking_level",
                   help="Nivel de razonamiento del dropdown 'Thinking level' "
                        "(minimal|low|medium|high). Vacío/omitido = no tocar la UI.")
    p.add_argument("--no-cerrar-tab-error", action="store_true", dest="no_cerrar_tab_error",
                   help="No cerrar el tab si la transcripción falla (debug).")
    p.add_argument("--screenshot-error", default=None, dest="screenshot_error",
                   help="[IGNORADO desde 2026-06-01] Se mantiene por compat con "
                        "la invocación de lib_aistudio.php. Ya no se sacan "
                        "screenshots; en debug la pestaña queda abierta.")
    p.add_argument("--hold-tras-run", action="store_true", dest="hold_tras_run",
                   help="DEBUG: tras disparar Run, NO leer/clickear/cerrar nada. "
                        "Dejar el tab abierto (hold-seg) para inspección manual del "
                        "popup de fin de turno. El tab queda abierto al desconectar.")
    p.add_argument("--hold-seg", type=int, default=120, dest="hold_seg",
                   help="Segundos a esperar en modo --hold-tras-run antes de "
                        "desconectar (el tab queda abierto igual). Default 120.")
    return p.parse_args()


# ============================================================
# Heurísticas de la respuesta (idénticas al POC)
# ============================================================

def detectar_token_repetition(texto: str) -> str | None:
    if not texto:
        return None
    m = re.search(r"(\w{2,10})\1{4,}", texto)
    return m.group(0) if m else None


def detectar_prompt_echo(response: str, prompt: str) -> bool:
    if not response or not prompt:
        return False
    if len(response) < len(prompt) * 0.5:
        return False

    def clean(s: str) -> str:
        return re.sub(r'\W+', '', s).lower()[:200]

    r, p = clean(response), clean(prompt)
    if not p:
        return False
    matches = sum(1 for a, b in zip(p, r) if a == b)
    return matches / max(len(p), 1) > 0.7


def _normalizar(s: str) -> str:
    return re.sub(r'\W+', '', s or '').lower()


def _norm_opcion(s: str) -> str:
    """Minúsculas, sin acentos, sólo alfanumérico. 'Mínimo' -> 'minimo'."""
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', '', s.lower())


# Botones para CERRAR/ACEPTAR popups de onboarding/promo/acuerdo de AI Studio.
# SÓLO variantes de descartar/aceptar (no destructivas, no navegación, NO 'Cancel').
# en/es, normalizadas. El caso central es el modal de copyright "Start creating
# with media in Google AI Studio" (botón "Acknowledge"), que se dispara al subir
# la imagen y es modal (Escape/backdrop NO lo cierran: hay que clickear el botón).
POPUP_ACCEPT_LABELS = {
    _norm_opcion(s) for s in (
        'Acknowledge', 'Got it', 'Got it!', 'Dismiss', 'OK', 'Okay',
        'No thanks', 'No, thanks', 'Close', 'Done', 'Continue', 'Maybe later',
        'Entendido', 'Aceptar', 'Reconocer', 'Cerrar', 'Omitir', 'No, gracias',
        'Listo', 'Continuar', 'De acuerdo', 'Más tarde',
    )
}

# Botones de diálogo/overlay (incluye tag custom mat-dialog-container y su clase
# mat-mdc-dialog-container, según el DOM real de AI Studio).
_SEL_DIALOG_BUTTONS = (
    'mat-dialog-container button, .mat-mdc-dialog-container button, '
    '.mat-mdc-dialog-actions button, [role="dialog"] button, '
    '[role="alertdialog"] button, .cdk-overlay-pane button'
)


async def _cerrar_un_popup(page) -> bool:
    """Una pasada: clickea el primer botón de aceptar/descartar (no 'Cancel')
    dentro de un diálogo/overlay. Devuelve True si clickeó algo."""
    try:
        botones = page.locator(_SEL_DIALOG_BUTTONS)
        n = await botones.count()
    except Exception:
        return False
    for i in range(n):
        b = botones.nth(i)
        try:
            if not await b.is_visible():
                continue
            txt = _norm_opcion(await b.inner_text() or '')
            aria = _norm_opcion(await b.get_attribute('aria-label') or '')
        except Exception:
            continue
        if txt in POPUP_ACCEPT_LABELS or aria in POPUP_ACCEPT_LABELS:
            try:
                await b.click(timeout=2000)
                await page.wait_for_timeout(300)
                return True
            except Exception:
                pass
    return False


# Diálogo de auth de Drive de AI Studio: <ms-auth-request-dialog> "Save your
# conversations in Google Drive (Recommended)". Aparece AL TERMINAR el turno en
# cuentas sin Drive habilitado (reportado en cuenta F). Si se deja o se clickea
# su acción primaria "Enable Google Drive", AI Studio intenta guardar, falla con
# "Failed to generate content, permission denied" y COLAPSA la respuesta buena a
# "An internal error has occurred". El botón SEGURO es el secundario "Cancel and
# use Temporary chat" (aria-label="Cancel", clase ms-button-borderless, jslog
# 273917): queda en chat temporal y no dispara el guardado. Por eso el closer
# genérico _cerrar_un_popup —que EXCLUYE 'Cancel'— no lo toca: acá 'Cancel' ES la
# salida correcta. Selectores resistentes a la traducción automática de Chrome:
# la clase y el jslog no se traducen, y el aria-label no es texto visible.
_SEL_DRIVE_DIALOG = 'ms-auth-request-dialog'
_SEL_DRIVE_CANCEL_BTN = (
    'button[aria-label="Cancel"], '
    'button[jslog*="273917"], '
    'button.ms-button-borderless'
)


async def _descartar_dialogo_drive(page) -> bool:
    """Si está visible el diálogo de guardado en Drive, clickea 'Cancel and use
    Temporary chat' y devuelve True. No fatal: ante cualquier error devuelve
    False y el caller sigue."""
    try:
        dlg = page.locator(_SEL_DRIVE_DIALOG).first
        if not await dlg.is_visible():
            return False
    except Exception:
        return False
    try:
        btn = dlg.locator(_SEL_DRIVE_CANCEL_BTN).first
        if not await btn.is_visible():
            return False
        await btn.click(timeout=2000)
        await page.wait_for_timeout(300)
        sys.stderr.write("\n[ais] diálogo de Drive descartado (Cancel → Temporary chat)\n")
        sys.stderr.flush()
        return True
    except Exception:
        return False


async def _describir_overlays(page) -> str:
    """Read-only: describe TODO overlay VISIBLE (diálogos, snackbars/toasts y
    cualquier .cdk-overlay-pane) con su texto y sus botones (inner_text +
    aria-label). NO clickea nada. Instrumenta el popup que aparece al terminar la
    transcripción y colapsa la respuesta a 'internal error' (reportado en cuenta
    F): no sabemos su forma exacta (mat-dialog, snackbar, overlay custom…), así
    que enumeramos todas. El dump va a stderr (queda en stderr_raw / stderr.log)
    para diseñar después el descarte correcto. Devuelve '' si no hay nada."""
    partes = []

    # 1. Snackbars / toasts (ej. "Failed to generate content, permission denied").
    try:
        snacks = page.locator('mat-snack-bar-container, .mat-mdc-snack-bar-container, [matsnackbarlabel]')
        for i in range(min(await snacks.count(), 4)):
            s = snacks.nth(i)
            try:
                if not await s.is_visible():
                    continue
                t = (await s.inner_text() or '').strip().replace('\n', ' ')[:200]
            except Exception:
                continue
            if t:
                partes.append(f"snackbar={t!r}")
    except Exception:
        pass

    # 2. Diálogos / overlays panes con su texto + botones.
    try:
        panes = page.locator('mat-dialog-container, .mat-mdc-dialog-container, '
                             '[role="dialog"], [role="alertdialog"], .cdk-overlay-pane')
        npanes = await panes.count()
    except Exception:
        npanes = 0
    for i in range(min(npanes, 6)):
        pane = panes.nth(i)
        try:
            if not await pane.is_visible():
                continue
            ptxt = (await pane.inner_text() or '').strip().replace('\n', ' ')[:240]
        except Exception:
            continue
        botones = []
        try:
            bs = pane.locator('button')
            for j in range(min(await bs.count(), 10)):
                b = bs.nth(j)
                try:
                    if not await b.is_visible():
                        continue
                    bt = (await b.inner_text() or '').strip().replace('\n', ' ')
                    ba = (await b.get_attribute('aria-label') or '').strip()
                except Exception:
                    continue
                if bt or ba:
                    botones.append(f"[txt={bt!r} aria={ba!r}]")
        except Exception:
            pass
        if ptxt or botones:
            partes.append(f"pane(text={ptxt!r} buttons={botones})")

    return " || ".join(partes)


async def _aceptar_popups(page, espera_seg: float = 2.0, cerrar_max: int = 3) -> int:
    """Best-effort: durante hasta `espera_seg` s pollea y cierra popups de
    onboarding/promo/acuerdo (el modal tarda ~3s en renderizar). No fatal.
    Devuelve cuántos cerró. Robusto a idioma."""
    cerrados = 0
    deadline = time.time() + max(0.0, espera_seg)
    vacios = 0
    while cerrados < cerrar_max:
        if await _cerrar_un_popup(page):
            cerrados += 1
            vacios = 0
            await page.wait_for_timeout(400)
            continue
        if time.time() >= deadline:
            break
        if cerrados > 0:
            vacios += 1
            if vacios >= 2:  # ya cerré algo y no aparece más → no seguir esperando
                break
        await page.wait_for_timeout(500)
    return cerrados


# Etiquetas del dropdown "Thinking level" de AI Studio por valor canónico, en/es
# (normalizadas sin acentos). El usuario sólo tiene estos dos idiomas.
THINKING_ALIASES = {
    'minimal': ('minimal', 'minimo'),
    'low':     ('low', 'bajo'),
    'medium':  ('medium', 'medio'),
    'high':    ('high', 'alto'),
}


async def _esperar_opciones(page, timeout_ms: int = 3000) -> bool:
    """Pollea hasta que el overlay de mat-option tenga ≥1 opción (o timeout).
    Reemplaza el `wait_for_timeout(350)` fijo que, en un Chrome lento/cargado
    (p.ej. una cuenta con varias pestañas + jobs en vuelo), podía leer 0 opciones
    y abortar el seteo dejando el DEFAULT de la cuenta —que para gemini-3-flash es
    'High'— (causa raíz del 'Alto' que aparecía pese a pedir 'medium'; verificado
    en vivo 2026-06-01: el overlay rinde a ~57ms en un Chrome libre, pero el wait
    fijo no tolera la cola). Devuelve True si aparecieron opciones."""
    deadline = time.time() + max(0.0, timeout_ms / 1000.0)
    while time.time() < deadline:
        try:
            if await page.locator('mat-option').count() > 0:
                return True
        except Exception:
            pass
        await page.wait_for_timeout(60)
    return False


async def _valor_select(sel) -> str:
    """Texto visible (valor actualmente seleccionado) de un mat-select. '' si falla."""
    try:
        return (await sel.inner_text() or '').strip()
    except Exception:
        return ''


async def _seleccionar_opcion(page, sel, wanted: tuple, intentos: int = 2) -> str | None:
    """Abre `sel`, ESPERA las opciones (poll, no wait fijo), clickea la que matchea
    `wanted` y RELEE el valor del select para confirmar que quedó aplicado. Si el
    readback no coincide (click que no aterrizó en un Chrome lento), reintenta hasta
    `intentos` veces. Devuelve la etiqueta efectivamente aplicada (leída del select),
    o None si no se pudo. El readback es la pieza clave: antes el seteo era
    fire-and-forget y un click perdido quedaba como 'éxito' silencioso dejando el
    default High."""
    for _ in range(max(1, intentos)):
        try:
            await sel.click(timeout=2000)
        except Exception:
            return None
        if not await _esperar_opciones(page, 3000):
            try:
                await page.keyboard.press('Escape')
            except Exception:
                pass
            continue  # overlay no rindió a tiempo: reintentar
        opts = page.locator('mat-option')
        clickeado = False
        for j in range(await opts.count()):
            try:
                txt = (await opts.nth(j).inner_text()).strip()
            except Exception:
                continue
            if _norm_opcion(txt) in wanted:
                try:
                    await opts.nth(j).click()
                    clickeado = True
                except Exception:
                    clickeado = False
                break
        if not clickeado:
            # La opción pedida no existe en este select (no es problema de timing)
            # → cerrar y no reintentar.
            try:
                await page.keyboard.press('Escape')
            except Exception:
                pass
            return None
        # Readback: confirmar que el valor del select quedó en lo pedido.
        await page.wait_for_timeout(200)
        valor = await _valor_select(sel)
        if _norm_opcion(valor) in wanted:
            return valor
        # No aplicó: reintentar (siguiente vuelta del for).
    return None


async def _set_thinking_level(page, target: str) -> str | None:
    """Setea el dropdown 'Thinking level' al nivel pedido y VERIFICA releyendo el
    valor (ver _seleccionar_opcion). Robusto a idioma: ubica el mat-select por
    aria-label (en/es) y, si falla, por sus opciones (es el único con
    'Minimal'/'Mínimo'; Media resolution no la tiene). Devuelve la etiqueta aplicada
    y VERIFICADA, o None si no se pudo (no fatal: el caller lo registra). None ⇒
    quedó el default de la cuenta (para flash, 'High') — señal accionable de que el
    seteo falló, no un 'quizás'."""
    wanted = THINKING_ALIASES.get(target)
    if not wanted:
        return None

    selects = page.locator('mat-select')
    n = await selects.count()

    # Paso 1: candidato por aria-label. 'Thinking Level' (en),
    # 'Nivel de razonamiento'/'pensamiento' (es).
    for i in range(n):
        s = selects.nth(i)
        try:
            al = _norm_opcion(await s.get_attribute('aria-label') or '')
        except Exception:
            al = ''
        if 'thinking' in al or 'razona' in al or 'pensa' in al:
            return await _seleccionar_opcion(page, s, wanted)

    # Paso 2 (fallback): abrir cada select y reconocer el de thinking por la opción
    # 'minimal'/'minimo'.
    for i in range(n):
        s = selects.nth(i)
        try:
            await s.click(timeout=2000)
        except Exception:
            continue
        if not await _esperar_opciones(page, 2000):
            try:
                await page.keyboard.press('Escape')
            except Exception:
                pass
            continue
        opts = page.locator('mat-option')
        textos = []
        for j in range(await opts.count()):
            try:
                textos.append(_norm_opcion(await opts.nth(j).inner_text()))
            except Exception:
                pass
        try:
            await page.keyboard.press('Escape')
        except Exception:
            pass
        await page.wait_for_timeout(150)
        if any(t in ('minimal', 'minimo') for t in textos):
            return await _seleccionar_opcion(page, s, wanted)
    return None


def detectar_cuota_agotada(texto: str) -> bool:
    if not texto:
        return False
    t = texto.lower()
    return any(m in t for m in CUOTA_MARKERS)


def calcular_veredicto(texto: str, prompt_enviado: str, fuente: str) -> tuple[str, dict]:
    """Devuelve (veredicto, heuristicas dict)."""
    chars = len(texto or '')
    # DESACTIVADO (pedido de Tomás 2026-06-01): el detector de repetición de tokens
    # daba falso positivo sobre los filetes de tabla contable hechos con guiones bajos
    # (job #6535: '________________' → DEGRADADA sobre una transcripción impecable) y,
    # más en general, descartaba transcripción real por una heurística de contenido,
    # contra la filosofía save-and-flag del WEB (lib_qa_detectores). La función se deja
    # definida por si se revive como flag QA NO destructivo.
    # rep = detectar_token_repetition(texto)
    rep = None
    # "Tiene estructura" según la convención de ESTE proyecto: marcadores #T# /
    # #ST# (subtítulos) / #/B# / #/C# (mismo esqueleto que qaDetectarSinEstructura en
    # lib_qa_detectores.php). Antes buscaba `[tipo:]`, convención heredada del
    # sistema del que se importó AI Studio y que el parser de Prensa del Plata no
    # usa: volvía marcador=False SIEMPRE → OK_SANA inalcanzable y todo caía a
    # DUDOSA. Basta con que cualquiera de los tres marcadores aparezca ≥2 veces.
    _t = texto or ''
    marcador = (_t.count('#T#') >= 2) or (_t.count('#ST#') >= 2) or (_t.count('#/B#') >= 2) or (_t.count('#/C#') >= 2)
    es_echo = detectar_prompt_echo(texto, prompt_enviado)
    es_internal_error = "internal error" in (texto or '').lower()
    es_chrome = _es_chrome_ui(texto or '')
    # Cuota/rate-limit: TRES señales combinadas (criterio de Tomás 2026-06-01,
    # endurecido tras un FP detectado en prueba):
    #   (1) palabra de cuota/límite presente (CUOTA_MARKERS, bilingüe),
    #   (2) texto MUY corto (< UMBRAL_CUOTA_CHARS),
    #   (3) la fuente es un mensaje de ERROR, no transcripción.
    # La (3) es la que mata el falso positivo: una transcripción real —aun corta y
    # aunque mencione "cuota mensual"/"límite" (avisos de prensa del s.XIX)— llega
    # como 'chunk'/'chunk_revisar'/'turn', NUNCA como 'error_snackbar'/'error_turn'.
    # Sin ella, un aviso de 79 chars con "cuota mensual" daba CUOTA (→ cooldown 6h +
    # descarte). El snackbar real de cuota sí llega por 'error_snackbar'.
    es_fuente_error = fuente in ('error_snackbar', 'error_turn')
    cuota = (detectar_cuota_agotada(texto)
             and len(texto or '') < UMBRAL_CUOTA_CHARS
             and es_fuente_error)

    # Marcador concreto que disparó el bloqueo (sólo en bloqueo_turn). Sirve para
    # distinguir 'recitation' —causa YA entendida (el modelo cree estar recitando
    # prensa antigua): no hay nada que inspeccionar visualmente, se cierra el tab
    # aun en debug— del resto de safety blocks, que sí se dejan abiertos. La
    # detección de bloqueo ya depende de BLOQUEO_MARKERS (inglés), así que el texto
    # del turno trae el marcador en inglés y este match no sufre la traducción de Chrome.
    bloqueo_motivo = None
    if fuente == 'bloqueo_turn':
        low = (texto or '').lower()
        bloqueo_motivo = next((m for m in BLOQUEO_MARKERS if m in low), None)

    heur = {
        "chars": chars,
        "marcador_estructura": marcador,
        "token_repetition": rep,
        "prompt_echo": es_echo,
        "internal_error": es_internal_error,
        "respuesta_chrome": es_chrome,
        "cuota_agotada": cuota,
        "bloqueo_seguridad": fuente == 'bloqueo_turn',
        "bloqueo_motivo": bloqueo_motivo,
        "fuente_dom": fuente,
    }
    if cuota:
        return "CUOTA", heur
    if fuente == 'bloqueo_turn':
        return "BLOQUEO", heur
    # Respuesta colapsada a chrome de UI: el parser sólo agarró labels de botones
    # ('downloadfullscreen', job #6502). NO sabemos la causa; sólo que NO es
    # transcripción. Antes caía en DUDOSA y se guardaba como basura. Lo tratamos
    # como CUALQUIER error de transcripción: REINTENTO (como internal_error) y, si
    # se agota, error con el tab de Chrome abierto para inspección.
    if es_chrome:
        return "RESPUESTA_CHROME", heur
    # error_snackbar (permission denied / failed to generate) y error_turn (caja
    # en 'An internal error has occurred.') comparten política: REINTENTAR. Tomás
    # (2026-06-01): no sabemos la causa y no hay API call de por medio (no gasta
    # cuota), así que conviene reintentar antes que descartar. El texto del
    # snackbar/caja viaja en response_text y queda en el mensaje de error.
    if es_internal_error or fuente in ('error_turn', 'error_snackbar'):
        return "INTERNAL_ERROR", heur
    # chunk_revisar: hay transcripción usable PERO con un error co-presente
    # (snackbar permiso / internal error junto al chunk). Se guarda la
    # transcripción (ok=true) y se flaggea para revisión humana.
    if fuente == 'chunk_revisar':
        return "REVISAR", heur
    if fuente == 'sin_respuesta':
        return "SIN_RESPUESTA", heur
    if chars > 800 and marcador and not rep and not es_echo:
        return "OK_SANA", heur
    if es_echo or rep:
        return "DEGRADADA", heur
    return "DUDOSA", heur


# ============================================================
# Lectura de la respuesta del modelo
# ============================================================

# Lectura PASIVA del DOM en una sola pasada (un round-trip), sin la maquinaria de
# actionability/visibility/timeout de los locators de Playwright. Un solo
# `page.evaluate` recoge el texto de todos los ms-prompt-chunk y del último
# ms-chat-turn. Clave (pedido de Tomás 2026-06-01): NO clickeamos ni descartamos
# nada para leer — pulleamos el texto directo del árbol del DOM, así una respuesta
# buena tapada por un overlay/popup se recupera igual (un overlay pintado encima
# NO altera el innerText del chunk).
#
# Cada nodo se lee en TRES formas:
#   - innerText  : base de CLASIFICACIÓN (echo/chrome/internal_error/marcadores).
#                  Es el contrato histórico; toda la heurística sigue corriendo
#                  sobre él, así no se mueve nada de la lógica ya calibrada.
#   - textContent: fallback de salvataje si innerText viene vacío.
#   - markdown   : RECONSTRUCCIÓN del markdown desde el HTML renderizado. AI Studio
#                  pinta la respuesta del modelo como HTML (renderer CommonMark/GFM),
#                  así que innerText APLANA lo que el modelo emitió en markdown:
#                  una tabla `| a | b |` pasa a `<table>` y su innerText pierde los
#                  pipes y la fila separadora (caso job #6549). El walker recorre el
#                  DOM y reconstruye tablas, negrita/itálica, headers, listas y code;
#                  los marcadores #T#/#/B#/#/C# son texto literal y sobreviven intactos.
#                  Es lo que se GUARDA como response (ver ejecutar_flujo). Pasivo: todo
#                  sale de un único page.evaluate, sin clicks. Validado con jsdom en
#                  temp/_jsdom_test/ (tablas, pipes escapados, listas anidadas, marcadores).
_JS_LEER_RESPUESTA = r"""
() => {
  const norm = s => (s == null ? '' : String(s)).trim();

  // --- HTML -> Markdown: walker recursivo, transparente a wrappers custom
  // (ms-cmark-node, span). NO escapa caracteres: el modelo ya emite markdown y
  // hay que preservar los marcadores #T#/#/B#/#/C# tal cual.
  const inline = node => { let o=''; node.childNodes.forEach(ch=>{o+=render(ch,true);}); return o; };
  const block  = node => { let o=''; node.childNodes.forEach(ch=>{o+=render(ch,false);}); return o; };
  const cellText = el => inline(el).replace(/\n+/g,' ').replace(/\|/g,'\\|').trim();
  function table(el){
    // AI Studio interpone wrappers <ms-cmark-node> entre table/tr y tr/td, así
    // que tr.children NO son los <td> directos (job #6549 bis: edi 14695/14699
    // p4, tablas contables que salían vacías). Query PROFUNDO + closest para
    // atravesar el wrapper sin robar celdas/filas de una tabla anidada.
    const rows=Array.from(el.querySelectorAll('tr')).filter(r=>r.closest('table')===el);
    if(!rows.length) return '';
    const toCells=tr=>Array.from(tr.querySelectorAll('td,th')).filter(c=>c.closest('tr')===tr).map(cellText);
    let header=toCells(rows[0]); const body=rows.slice(1).map(toCells);
    const ncol=Math.max(header.length,...body.map(r=>r.length),1);
    const pad=r=>{while(r.length<ncol)r.push(''); return r;};
    header=pad(header); const sep=new Array(ncol).fill('---');
    const lines=['| '+header.join(' | ')+' |','| '+sep.join(' | ')+' |'];
    body.forEach(r=>lines.push('| '+pad(r).join(' | ')+' |'));
    return '\n\n'+lines.join('\n')+'\n\n';
  }
  function list(el,ordered){
    let i=0; const lines=[];
    // Mismo wrapper <ms-cmark-node> que en tablas: los <li> NO son hijos directos
    // de <ul>/<ol>. querySelectorAll + closest para tomar SOLO los items de ESTE
    // nivel (closest('ul,ol')===el excluye los de sublistas anidadas).
    Array.from(el.querySelectorAll('li')).filter(li=>li.closest('ul,ol')===el).forEach(li=>{
      i++; const marker=ordered?(i+'. '):'- '; let content='';
      li.childNodes.forEach(ch=>{
        if(ch.nodeType===1&&/^(ul|ol)$/i.test(ch.tagName)){
          const sub=list(ch,ch.tagName.toLowerCase()==='ol');
          content+='\n'+sub.split('\n').map(l=>l?'  '+l:l).join('\n');
        } else { content+=render(ch,true); }
      });
      lines.push(marker+content.trim());
    });
    return lines.join('\n');
  }
  function render(node,inlineCtx){
    if(node.nodeType===3) return node.textContent;   // texto
    if(node.nodeType!==1) return '';                 // comentarios, etc.
    const tag=node.tagName.toLowerCase();
    switch(tag){
      case 'br': return '\n';
      case 'hr': return '\n\n---\n\n';
      case 'strong': case 'b': return '**'+inline(node)+'**';
      case 'em': case 'i': return '*'+inline(node)+'*';
      case 'del': case 's': return '~~'+inline(node)+'~~';
      case 'code': return (node.closest&&node.closest('pre'))?node.textContent:'`'+node.textContent+'`';
      case 'pre': return '\n\n```\n'+node.textContent.replace(/\n+$/,'')+'\n```\n\n';
      case 'a': { const href=node.getAttribute('href')||''; const txt=inline(node); return href?'['+txt+']('+href+')':txt; }
      case 'h1': case 'h2': case 'h3': case 'h4': case 'h5': case 'h6':
        return '\n\n'+'#'.repeat(+tag[1])+' '+inline(node).trim()+'\n\n';
      case 'blockquote':
        return '\n\n'+block(node).trim().split('\n').map(l=>'> '+l).join('\n')+'\n\n';
      case 'ul': return '\n'+list(node,false)+'\n';
      case 'ol': return '\n'+list(node,true)+'\n';
      case 'li': return inline(node);   // normalmente lo maneja list()
      case 'table': return table(node);
      case 'p': case 'div': case 'section': case 'article':
        return '\n\n'+block(node).trim()+'\n\n';
      default: return inlineCtx?inline(node):block(node);   // wrapper custom: transparente
    }
  }
  const toMarkdown = el => {
    let md=block(el);
    md=md.replace(/[ \t]+\n/g,'\n').replace(/\n{3,}/g,'\n\n');
    return md.trim();
  };

  const leer = el => ({
    innerText: norm(el.innerText),
    textContent: norm(el.textContent),
    markdown: norm(toMarkdown(el)),
  });
  const chunks = Array.from(document.querySelectorAll('ms-prompt-chunk')).map(leer);
  const turnos = document.querySelectorAll('ms-chat-turn');
  const lastTurn = turnos.length ? leer(turnos[turnos.length - 1]) : null;
  return { chunks, lastTurn };
}
"""


def _texto_nodo(nodo: dict | None) -> str:
    """innerText si lo hay; si no, textContent (fallback de salvataje). Base de
    CLASIFICACIÓN: toda la heurística (echo/chrome/internal_error/marcadores) corre
    sobre esto, sin cambios respecto del contrato histórico."""
    if not isinstance(nodo, dict):
        return ""
    return (nodo.get('innerText') or '').strip() or (nodo.get('textContent') or '').strip()


def _md_nodo(nodo: dict | None) -> str:
    """Markdown reconstruido del HTML del nodo (preserva tablas/negrita/etc. que
    innerText aplana); cae a _texto_nodo si no hubo markdown. Es lo que se GUARDA
    como response —la clasificación sigue usando _texto_nodo—."""
    if not isinstance(nodo, dict):
        return ""
    return (nodo.get('markdown') or '').strip() or _texto_nodo(nodo)


async def _leer_respuesta_modelo(page, prompt_enviado: str) -> tuple[str, str, str, str]:
    """Devuelve (texto, fuente, error_sospechado, texto_md).

    fuente ∈ {'chunk','error_turn','bloqueo_turn','fallback_turn','sin_respuesta'}.
    texto: innerText, base de CLASIFICACIÓN (heurística sin cambios).
    texto_md: markdown reconstruido del MISMO nodo — lo que se GUARDA como response
    (preserva tablas/negrita que innerText aplana). Para fuentes que no son
    transcripción (error/bloqueo/fallback) coincide con `texto`.
    error_sospechado: texto de un indicio de error CO-PRESENTE ('internal error'
    en algún chunk o en el turno) cuando ADEMÁS hay un chunk usable de
    transcripción. Vacío si no hay chunk usable (ahí el error va por `fuente`) o
    si no hay indicio. Habilita el veredicto REVISAR: priorizar/guardar la
    transcripción y a la vez flaggear el warning para revisión humana.

    Lectura 100% pasiva (un page.evaluate, sin clicks ni descartes): pulleamos el
    texto crudo del DOM, recuperando incluso respuestas tapadas por overlays."""
    prompt_head = _normalizar(prompt_enviado)[:200]

    try:
        dom = await page.evaluate(_JS_LEER_RESPUESTA)
    except Exception:
        return "", 'sin_respuesta', '', ''
    if not isinstance(dom, dict):
        return "", 'sin_respuesta', '', ''

    chunks = dom.get('chunks') or []
    ultimo_turn = _texto_nodo(dom.get('lastTurn'))

    # Indicio de error co-presente: 'internal error' en cualquier chunk o en el
    # turno. Se calcula SIEMPRE (no corta la lectura). Si además hay un chunk
    # usable, viaja como error_sospechado → REVISAR.
    error_sosp = ''
    for cand in [_texto_nodo(ch) for ch in chunks] + ([ultimo_turn] if ultimo_turn else []):
        if 'internal error' in cand.lower():
            error_sosp = cand.strip()[:300]
            break

    # 1. Primer chunk USABLE (de atrás para adelante): no vacío, no echo, no
    #    thoughts y NO el chunk de error en sí (ese no es transcripción).
    for ch in reversed(chunks):
        t = _texto_nodo(ch)
        if not t:
            continue
        norm = _normalizar(t)[:200]
        if norm and norm == prompt_head:
            continue
        low = t.lower()
        if low.startswith('thoughts') or 'expand to view model thoughts' in low:
            continue
        if 'internal error' in low:
            continue   # es el error, no una transcripción → seguir buscando
        return t, 'chunk', error_sosp, _md_nodo(ch)

    # 2. Sin chunk usable → clasificar por el turno (error/bloqueo/fallback). Estas
    #    fuentes no son transcripción: texto_md == texto (no hay markdown que rescatar).
    if ultimo_turn:
        low = ultimo_turn.lower()
        if 'internal error' in low:
            return ultimo_turn, 'error_turn', '', ultimo_turn
        # Bloqueo de seguridad (recitation/safety/etc.): el turno sólo tiene el
        # chip de warning + chrome de la UI (botones edit/more_vert/thumb_up...).
        # No es transcripción — marcarlo como bloqueo para que NO se acepte como
        # respuesta dudosa.
        if any(m in low for m in BLOQUEO_MARKERS):
            return ultimo_turn, 'bloqueo_turn', '', ultimo_turn
        if 'Model' in ultimo_turn and len(ultimo_turn) > 30:
            return ultimo_turn, 'fallback_turn', '', ultimo_turn

    return "", 'sin_respuesta', '', ''


# Snackbars/toasts de AI Studio leídos en una pasada pasiva (sin clickear). Mismo
# set de contenedores que _describir_overlays. innerText preferido; textContent de
# fallback por si el snackbar está a punto de desaparecer.
_JS_SNACKBARS = r"""
() => Array.from(document.querySelectorAll(
  'mat-snack-bar-container, .mat-mdc-snack-bar-container, [matsnackbarlabel]'
)).map(el => ((el.innerText || el.textContent) || '').trim()).filter(Boolean)
"""


async def _leer_snackbar_error(page) -> str:
    """Devuelve el texto del primer snackbar de error de generación (PERMISO_MARKERS)
    visible, o '' si no hay. 100% pasivo: lee el texto, no clickea ni descarta."""
    try:
        snacks = await page.evaluate(_JS_SNACKBARS)
    except Exception:
        return ""
    if not isinstance(snacks, list):
        return ""
    for s in snacks:
        if any(m in (s or '').lower() for m in PERMISO_MARKERS):
            return (s or '').strip()
    return ""


def _resolver_chunk_con_error(texto: str, error_sosp: str, incompleto: bool,
                              texto_md: str = '') -> tuple[str, bool, str, str, str]:
    """Decide el destino de un chunk usable que llega CON un error co-presente.

    - chunk sustancial (>= MIN_CHARS_TRANSCRIPCION_REVISAR) → 'chunk_revisar':
      se guarda la transcripción (prioridad a la caja) y se flaggea el warning.
    - chunk demasiado corto → 'error_snackbar': casi nunca es transcripción real
      (chrome de UI tipo 'downloadfullscreen'); se trata como error puro para que
      reintente y, si no, re-transcriba. Devuelve el TEXTO DEL ERROR (no el chunk
      basura) para que el snackbar quede auditado en el mensaje de error.

    El piso de longitud se mide sobre `texto` (innerText, base de clasificación),
    no sobre el markdown. Devuelve la misma tupla que `esperar_respuesta`:
    (texto, incompleto, fuente, error_sospechado, texto_md)."""
    if len(texto) < MIN_CHARS_TRANSCRIPCION_REVISAR:
        # error puro: el markdown no aporta (no es transcripción) → md = error.
        return error_sosp, incompleto, 'error_snackbar', error_sosp, error_sosp
    return texto, incompleto, 'chunk_revisar', error_sosp, (texto_md or texto)


async def esperar_respuesta(page, prompt_enviado: str, timeout_s: int) -> tuple[str, bool, str, str, str]:
    """Devuelve (texto, incompleto, fuente, error_sospechado, texto_md).

    fuente extra 'chunk_revisar': hay transcripción usable Y ADEMÁS una señal de
    error (snackbar de permiso / internal error co-presente). En ese caso `texto`
    es la transcripción (se guarda) y `error_sospechado` el mensaje de error
    (se flaggea). Para errores sin transcripción, `error_sospechado` repite el
    texto del error; para OK normal va vacío.

    texto_md: markdown reconstruido (lo que se guarda como response); `texto` queda
    como innerText para la clasificación. Coinciden cuando no hay markdown que
    rescatar (errores/bloqueos)."""
    deadline = time.time() + timeout_s
    last_len = -1
    estable_count = 0
    last_text = ""
    last_text_md = ""
    last_fuente = 'sin_respuesta'
    last_error_sosp = ""
    snackbar_err = ""   # último snackbar de error de generación (permiso) visto

    # Instrumentación del popup de fin de turno: apenas detectamos un overlay/
    # diálogo visible, volcamos su DOM (texto + botones con aria-label) a stderr,
    # UNA sola vez. NO clickeamos nada — sólo identificar qué aparece al recoger
    # el resultado. El screenshot se removió (2026-06-01): en debug la pestaña
    # queda abierta y alcanza para inspección manual.
    popup_logueado = False

    while time.time() < deadline:
        await asyncio.sleep(2.0)

        # [DEBUG 2026-06-01] Cancel del diálogo de Drive COMENTADO a pedido de
        # Tomás. Hipótesis: este click (el único punto de mutación del DOM en el
        # camino de captura) es lo que colapsa el chunk bueno a 'internal error'
        # —no un popup que el usuario vería, sino que NUESTRA acción interactiva
        # reemplaza la respuesta completada por un chunk de error—. Lo dejamos
        # fuera para ver si el fallo intermitente desaparece. La función sigue
        # llamándose en el OPEN del chat (pre-Run), donde no puede pisar una
        # respuesta. Restaurar acá si la hipótesis se descarta.
        # try:
        #     await _descartar_dialogo_drive(page)
        # except Exception:
        #     pass

        try:
            stop_visible = await page.locator(SEL_STOP_BUTTON).first.is_visible()
        except Exception:
            stop_visible = False

        try:
            text, fuente, err_dom, text_md = await _leer_respuesta_modelo(page, prompt_enviado)
        except Exception:
            continue

        # Snackbar de error de generación (no-popup, arriba): "Failed to generate
        # content, permission denied". Suele preceder/acompañar al internal_error
        # de la caja. Lo capturamos por su texto (más señal).
        try:
            snk = await _leer_snackbar_error(page)
        except Exception:
            snk = ""
        if snk:
            snackbar_err = snk

        # Señal de error co-presente: snackbar (prioridad) o internal-error en el
        # DOM junto al chunk. Si NO hay chunk usable ni bloqueo, es un error puro
        # → cortar para reintentar (INTERNAL_ERROR). Si SÍ hay chunk usable, NO
        # cortamos acá: dejamos que el chunk estabilice y se devuelve REVISAR
        # abajo (prioridad a la caja de transcripción).
        error_sosp = snackbar_err or err_dom
        if error_sosp and fuente not in ('chunk', 'bloqueo_turn'):
            sys.stderr.write(f"\n[ais] error de generación sin transcripción → {error_sosp!r}\n")
            return error_sosp, False, 'error_snackbar', error_sosp, error_sosp

        if not popup_logueado:
            try:
                desc = await _describir_overlays(page)
            except Exception:
                desc = ""
            if desc:
                popup_logueado = True
                sys.stderr.write(f"\n[ais] OVERLAY al recoger el resultado (fuente={fuente}) → {desc}\n")
                sys.stderr.flush()

        actual_len = len(text)
        sys.stderr.write(
            f"\r[ais] ... {actual_len} chars (stop={'sí' if stop_visible else 'no'}, fuente={fuente})   "
        )
        sys.stderr.flush()

        if fuente == 'sin_respuesta':
            estable_count = 0
            last_len = -1
            continue

        if fuente in ('error_turn', 'bloqueo_turn'):
            sys.stderr.write("\n")
            return text, False, fuente, '', text_md

        if not stop_visible and actual_len == last_len and actual_len > 0:
            estable_count += 1
            if estable_count >= 2:
                sys.stderr.write("\n")
                # Chunk estable. Si hubo error co-presente → REVISAR (guardar la
                # transcripción + flaggear) SALVO chunk demasiado corto (chrome de
                # UI, no transcripción) → error puro. Si no hay error, OK/DUDOSA.
                if error_sosp:
                    t2, inc2, f2, e2, md2 = _resolver_chunk_con_error(text, error_sosp, False, text_md)
                    if f2 == 'chunk_revisar':
                        sys.stderr.write(f"[ais] transcripción + error co-presente → REVISAR ({error_sosp!r})\n")
                    else:
                        sys.stderr.write(f"[ais] chunk corto ({len(text)} chars) + error → ERROR (no REVISAR): {error_sosp!r}\n")
                    return t2, inc2, f2, e2, md2
                return text, False, fuente, '', text_md
        else:
            estable_count = 0

        last_len = actual_len
        last_text = text
        last_text_md = text_md
        last_fuente = fuente
        last_error_sosp = error_sosp

    sys.stderr.write("\n")
    sys.stderr.write(
        f"[ais] timeout {timeout_s}s — devuelvo lo último leído ({len(last_text)} chars, fuente={last_fuente})\n"
    )
    # Timeout con transcripción + error pendiente → REVISAR (o error si el chunk
    # es demasiado corto: misma regla que el chunk estable).
    if last_fuente == 'chunk' and last_error_sosp:
        return _resolver_chunk_con_error(last_text, last_error_sosp, True, last_text_md)
    return last_text, True, last_fuente, '', last_text_md


# ============================================================
# Token usage (contador de AI Studio)
# ============================================================

# Lee el token usage del tooltip del contador de AI Studio. El span visible
# (.v3-token-count-value) trae frames del count-up animation de Angular (p. ej.
# "2,551 2,584 2,584") y es poco fiable; el tooltip da los valores finales
# etiquetados. Bilingüe en/es: Chrome auto-traduce la UI y --disable-features=
# Translate NO la apaga (verificado 2026-06-01), así que las etiquetas pueden
# venir en español ('Tokens de entrada') o inglés ('Input tokens'). El `output`
# de AI Studio INCLUYE los thought tokens (no se pueden separar acá). Se saltean
# las filas de costo ('Costo del token de entrada' contiene 'entrada' y
# matchearía input). El tooltip se monta en .cdk-overlay-container al disparar
# mouseenter; se lee en el mismo evaluate. `norm` quita todo no-dígito → tolera
# separador de miles '.' (es) o ',' (en). Validado contra pestaña real
# (temp/tests/aistudio_token_usage/) el 2026-06-01.
_JS_LEER_TOKEN_USAGE = r"""
async () => {
  const span = document.querySelector('.v3-token-count-value');
  if (!span) return { ok: false, motivo: 'no_span' };
  span.dispatchEvent(new MouseEvent('mouseenter', {bubbles:true}));
  span.dispatchEvent(new MouseEvent('mouseover',  {bubbles:true}));
  span.dispatchEvent(new MouseEvent('mousemove',  {bubbles:true}));
  try { span.focus(); } catch (e) {}
  await new Promise(r => setTimeout(r, 700));
  const norm = s => { const d = String(s).replace(/[^\d]/g,''); return d ? parseInt(d,10) : null; };
  const out = { ok: false };
  const rows = document.querySelectorAll('.token-count-tooltip .tooltip-row');
  for (const row of rows) {
    const sp = row.querySelectorAll('span');
    if (sp.length < 2) continue;
    const label = (sp[0].innerText || '').toLowerCase();
    const val   = sp[1].innerText || '';
    if (/\$/.test(val) || /costo|coste|cost/.test(label)) continue;   // saltar costos
    if (/entrada|input/.test(label))                     out.input  = norm(val);
    else if (/salida|output/.test(label))                out.output = norm(val);
    else if (/total de tokens|total tokens/.test(label)) out.total  = norm(val);
    else if (/uso del token|token usage/.test(label)) {
      const m = val.match(/([\d.,]+)\s*\/\s*([\d.,]+)/);
      if (m) { out.used = norm(m[1]); out.context_max = norm(m[2]); }
    }
  }
  span.dispatchEvent(new MouseEvent('mouseleave', {bubbles:true}));   // cerrar (no destructivo)
  out.ok = (out.input != null || out.output != null || out.total != null);
  return out;
}
"""


async def leer_token_usage(page) -> dict | None:
    """Devuelve {ok, input, output, total, used, context_max} del tooltip de
    tokens, o None si no se pudo leer. 100% no fatal: cualquier excepción → None.

    Se invoca DESPUÉS de capturar la transcripción (out['response'] ya poblado),
    así que aunque el mouseenter tocara el DOM, no puede pisar la respuesta. Es
    la única interacción de lectura del usage; mantenerla acotada al éxito."""
    try:
        res = await page.evaluate(_JS_LEER_TOKEN_USAGE)
        return res if isinstance(res, dict) else None
    except Exception as e:
        sys.stderr.write(f"[ais] WARN no se pudo leer token usage: {e}\n")
        return None


# ============================================================
# Partición del prompt
# ============================================================

def partir_prompt(prompt_completo: str) -> tuple[str | None, str]:
    """
    Si el prompt contiene `<<<CHAT_INPUT>>>` en su propia línea, todo lo de
    arriba va a System Instructions y lo de abajo al chat input. Si no, todo va
    al chat (sys_prompt=None).
    """
    if CHAT_INPUT_MARKER not in prompt_completo:
        return None, prompt_completo

    parts = re.split(r'^\s*' + re.escape(CHAT_INPUT_MARKER) + r'\s*$', prompt_completo, maxsplit=1, flags=re.M)
    if len(parts) != 2:
        # marcador no estaba en línea propia — split flexible
        parts = prompt_completo.split(CHAT_INPUT_MARKER, 1)
    sys_part = parts[0].strip()
    chat_part = parts[1].strip() if len(parts) > 1 else ''
    if not chat_part:
        # marcador presente pero sin contenido abajo: degrada a all-in-chat
        return None, prompt_completo.replace(CHAT_INPUT_MARKER, '').strip()
    return sys_part, chat_part


# ============================================================
# Flujo principal (1 imagen)
# ============================================================

async def ejecutar_flujo(page, imagen: Path, prompt_completo: str, args, t0: float) -> dict:
    """Devuelve dict con todas las keys del shape esperado."""
    sys_prompt, chat_prompt = partir_prompt(prompt_completo)

    out = {
        "ok": False,
        "response": "",
        "duracion_seg": 0.0,
        "fuente_dom": None,
        "heuristicas": {},
        "veredicto": None,
        "modelo_verificado": None,
        "media_resolution": None,
        "thinking_level": None,
        "tab_cerrado": False,
        "incompleto": False,
        "modo_prompt": "split" if sys_prompt else "all-in-chat",
        "sys_prompt_chars": len(sys_prompt) if sys_prompt else 0,
        "chat_prompt_chars": len(chat_prompt),
        "error": None,
        "error_sospechado": None,   # poblado en REVISAR (warning junto a transcripción)
        "longitud_sospechosa": False,  # transcripción aceptada pero < UMBRAL_LONGITUD_SOSPECHOSA
        # Token usage leído del tooltip de AI Studio tras una respuesta aceptable
        # (null si no se pudo leer). output INCLUYE thoughts (AI Studio no los
        # separa). token_usage_raw guarda el dict completo (used/context_max) para
        # auditoría en salida.json.
        "tokens_input": None,
        "tokens_output": None,
        "tokens_total": None,
        "token_usage_raw": None,
    }

    url = f"https://aistudio.google.com/prompts/new_chat?model={args.modelo}"
    sys.stderr.write(f"[ais] navegando a {url}\n")
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)

    if "accounts.google.com" in page.url:
        out["error"] = "no_logueado: redirigió a login. El Chrome del CDP no tiene sesión activa en AI Studio."
        # Marca para el teardown: cerrar SIEMPRE este tab (aunque aistudio_debug
        # mantenga abiertos los tabs de error). Una pantalla de login no se
        # debuggea visualmente y deja un tab consumiendo recursos. (Fase 2)
        out["no_logueado"] = True
        return out

    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass

    # Cerrar ToS / popups de onboarding/promo (ej. "Start creating with media…").
    # Antes sólo se buscaba el botón "Dismiss"; ahora un acatador genérico en/es
    # que cubre las variantes y, si no, cierra por Escape/backdrop.
    cerrados = await _aceptar_popups(page, espera_seg=1.5)
    if cerrados:
        sys.stderr.write(f"[ais] popups cerrados al abrir: {cerrados}\n")

    # El diálogo de guardado en Drive lo excluye _aceptar_popups (su botón seguro
    # es 'Cancel', que ese acatador evita). En cuentas recién configuradas puede
    # estar pendiente ya al abrir el chat — descartarlo ACÁ, antes de transcribir,
    # evita caer en el internal error en el primer job de la cuenta nueva.
    if await _descartar_dialogo_drive(page):
        sys.stderr.write("[ais] diálogo de Drive pendiente descartado al abrir\n")

    # Verificar modelo. La card del selector muestra el modelo con su nombre
    # LOCALIZADO, no el slug: EN "Gemini 3 Flash Preview", ES "Vista previa de
    # Gemini 3 Flash". Sólo manejamos esos dos idiomas. El nombre base
    # ("gemini3flash", sin el qualifier 'preview') es substring de ambos una vez
    # normalizado (el ES antepone "vista previa", por eso se suelta 'preview' del
    # slug), así que comparamos contra esa base en lugar del slug literal.
    try:
        modelo_card_txt = await page.locator(SEL_MODEL_SELECTOR_CARD).first.inner_text(timeout=5000)
        modelo_base = _normalizar(re.sub(r'-?preview', '', args.modelo, flags=re.I))
        if modelo_base and modelo_base not in _normalizar(modelo_card_txt):
            out["error"] = (f"modelo_no_match: card {modelo_card_txt!r} no contiene "
                            f"el modelo {args.modelo} (base '{modelo_base}')")
            return out
        out["modelo_verificado"] = args.modelo
    except PWTimeout:
        pass

    # Defensivo: un popup puede aparecer tarde y tapar advanced settings (su
    # backdrop intercepta el click del botón "Expand", ver anomalía #146). Cerrar
    # de nuevo justo antes de interactuar con los settings.
    await _aceptar_popups(page, espera_seg=1.0)

    # Media resolution
    try:
        expand_btn = page.locator(SEL_ADV_SETTINGS_EXPAND).first
        if await expand_btn.is_visible(timeout=2000):
            media_select_visible = await page.locator(SEL_MEDIA_RES_SELECT).first.is_visible(timeout=500)
            if not media_select_visible:
                await expand_btn.click()
                await page.wait_for_timeout(500)
        media_select = page.locator(SEL_MEDIA_RES_SELECT).first
        if await media_select.is_visible(timeout=2000):
            await media_select.click()
            await page.wait_for_timeout(400)
            opts = page.locator('mat-option')
            n_opts = await opts.count()
            target_idx = None
            target_txt = None
            for i in range(n_opts):
                t = (await opts.nth(i).inner_text()).strip()
                if t.lower() == args.media_resolution.lower():
                    target_idx = i; target_txt = t; break
            if target_idx is None:
                for i in range(n_opts):
                    t = (await opts.nth(i).inner_text()).strip()
                    if args.media_resolution.lower() in t.lower():
                        target_idx = i; target_txt = t; break
            if target_idx is None and n_opts > 0:
                target_idx = n_opts - 1
                target_txt = (await opts.nth(target_idx).inner_text()).strip()
            if target_idx is not None:
                await opts.nth(target_idx).click()
                await page.wait_for_timeout(300)
                out["media_resolution"] = target_txt
            else:
                await page.keyboard.press("Escape")
    except Exception as e:
        sys.stderr.write(f"[ais] WARN media resolution: {e}\n")

    # Thinking level (opcional). El dropdown vive en advanced settings, ya
    # expandido por el bloque de media resolution. Vacío/omitido = no tocar (se
    # respeta el default de la cuenta). No es fatal si falla: sólo se registra,
    # para no matar el job por un cambio puntual del UI (el path es fail-fast).
    if args.thinking_level:
        try:
            aplicado = await _set_thinking_level(page, args.thinking_level.strip().lower())
            out["thinking_level"] = aplicado
            if aplicado:
                sys.stderr.write(f"[ais] thinking level = {aplicado}\n")
            else:
                sys.stderr.write(f"[ais] WARN no se pudo setear thinking level '{args.thinking_level}'\n")
        except Exception as e:
            sys.stderr.write(f"[ais] WARN thinking level: {e}\n")

    # Apagar Grounding
    try:
        chip_close = page.locator('button[aria-label*="Remove" i][aria-label*="Grounding" i]').first
        if await chip_close.is_visible(timeout=1500):
            await chip_close.click()
    except Exception:
        pass

    # System Instructions (modo split)
    if sys_prompt:
        sys.stderr.write(f"[ais] inyectando System Instructions ({len(sys_prompt)} chars)\n")
        try:
            card = page.locator(SEL_SYSTEM_INSTRUCTIONS_CARD).first
            await card.click()
            await page.wait_for_timeout(700)
        except Exception:
            pass
        sys_ta = page.locator(SEL_SYSTEM_INSTRUCTIONS_TEXTAREA).first
        try:
            await sys_ta.wait_for(state="visible", timeout=5000)
        except PWTimeout:
            out["error"] = "system_instructions_no_aparecio"
            return out
        await sys_ta.click()
        try:
            await sys_ta.fill(sys_prompt, timeout=15000)
        except Exception:
            await page.keyboard.type(sys_prompt, delay=0)
        cargado = (await sys_ta.input_value()).strip()
        if len(cargado) < len(sys_prompt) * 0.9:
            sys.stderr.write(f"[ais] WARN system instructions: cargado={len(cargado)} esperado={len(sys_prompt)}\n")
        # Cerrar overlay
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
            backdrop = page.locator('.cdk-overlay-backdrop.cdk-overlay-backdrop-showing').first
            if await backdrop.is_visible(timeout=500):
                await page.mouse.click(400, 400)
                await page.wait_for_timeout(300)
            await page.locator('.cdk-overlay-backdrop.cdk-overlay-backdrop-showing').first.wait_for(
                state="hidden", timeout=4000
            )
        except Exception:
            pass

    # Upload imagen
    sys.stderr.write(f"[ais] subiendo imagen: {imagen.name} ({imagen.stat().st_size} bytes)\n")
    file_set = False
    try:
        inputs = page.locator(SEL_FILE_INPUT)
        if await inputs.count() > 0:
            await inputs.first.set_input_files(str(imagen), timeout=8000)
            file_set = True
    except Exception:
        pass

    if not file_set:
        await page.locator(SEL_INSERT_BUTTON).first.click()
        try:
            await page.locator('[role="menuitem"]').first.wait_for(state="visible", timeout=5000)
        except PWTimeout:
            out["error"] = "menu_insert_no_aparecio"
            return out
        items = page.locator('[role="menuitem"]')
        n = await items.count()
        target_idx = None
        for i in range(n):
            txt = (await items.nth(i).inner_text()).strip().lower()
            if any(h in txt for h in MENU_ITEM_UPLOAD_HINTS):
                target_idx = i; break
        if target_idx is None:
            out["error"] = "menu_insert_sin_upload_item"
            return out
        async with page.expect_file_chooser(timeout=10000) as fc_info:
            await items.nth(target_idx).click()
        fc = await fc_info.value
        await fc.set_files(str(imagen))

    # El modal de copyright "Start creating with media… / Acknowledge" se dispara
    # al USAR media (este upload) y es modal: bloquea el chip, el token count y el
    # Run si no se acepta. Tarda ~3s en renderizar → pollear hasta 6s y clickear
    # "Acknowledge". (Causa raíz del fallo de cuenta F, #146.)
    cerrados = await _aceptar_popups(page, espera_seg=6.0)
    if cerrados:
        sys.stderr.write(f"[ais] popups cerrados tras upload: {cerrados}\n")

    try:
        await page.locator(SEL_ATTACHMENT_CHIP).first.wait_for(state="visible", timeout=15000)
    except PWTimeout:
        sys.stderr.write("[ais] WARN chip de imagen no visible\n")

    try:
        await page.locator(SEL_ATTACHMENT_READY).first.wait_for(state="visible", timeout=15000)
    except PWTimeout:
        sys.stderr.write("[ais] WARN token count no apareció en 15s\n")

    # Inyectar chat prompt + Run
    sys.stderr.write(f"[ais] inyectando chat prompt ({len(chat_prompt)} chars)\n")
    textarea = page.locator(SEL_PROMPT_TEXTAREA).first
    await textarea.click(timeout=10000)
    try:
        await textarea.fill(chat_prompt, timeout=15000)
    except Exception:
        await page.keyboard.type(chat_prompt, delay=0)

    # Defensivo: si el modal de copyright reapareció (o llegó tarde), cerrarlo
    # antes del Run para que no intercepte el Ctrl+Enter.
    await _aceptar_popups(page, espera_seg=1.5)

    # Snackbar de error de generación ANTES de Run (visto en cuenta F: "Failed to
    # generate content: permission denied", arriba, no-popup; deja la caja en
    # internal error). Si ya está, abortar como error capturando su texto — da más
    # señal que el internal_error genérico y evita disparar un Run condenado.
    snk_pre = await _leer_snackbar_error(page)
    if snk_pre:
        sys.stderr.write(f"[ais] snackbar de error PRE-Run → {snk_pre!r}\n")
        # veredicto INTERNAL_ERROR → lib_aistudio.php reintenta el subprocess
        # entero (re-navega), que puede limpiar un snackbar transitorio. No gasta
        # API call. El texto del snackbar queda en response/error para auditoría.
        out["error"] = f"internal_error (snackbar pre-Run): {snk_pre[:300]}"
        out["veredicto"] = "INTERNAL_ERROR"
        out["response"] = snk_pre
        out["fuente_dom"] = "error_snackbar_pre_run"
        return out

    sys.stderr.write("[ais] disparando Run (Ctrl+Enter)\n")
    t_run = time.time()
    await page.keyboard.press("Control+Enter")

    # ── Modo DEBUG fire-and-hold ──────────────────────────────────────────────
    # Disparamos Run y NO hacemos nada más: no leer, no clickear, no cerrar. El
    # tab queda abierto para que Tomás vea el popup de fin de turno en vivo,
    # le saque screenshot y copie el HTML. Holdeamos hold_seg conectados (para
    # que la generación complete sin race de teardown) y devolvemos sin tocar
    # nada; el teardown NO cierra el tab cuando out['hold'] es True.
    if getattr(args, 'hold_tras_run', False):
        sys.stderr.write(
            f"[ais] HOLD: Run disparado. Dejo el tab ABIERTO {args.hold_seg}s para "
            f"inspección manual. No leo, no clickeo, no cierro nada.\n"
        )
        sys.stderr.flush()
        await asyncio.sleep(max(1, args.hold_seg))
        out["hold"] = True
        out["error"] = "hold_debug: tab dejado abierto para inspección manual"
        out["duracion_seg"] = round(time.time() - t_run, 2)
        return out

    sys.stderr.write(f"[ais] esperando respuesta (timeout {args.timeout_respuesta}s)...\n")
    response_text, incompleto, fuente, error_sosp, response_md = await esperar_respuesta(
        page, chat_prompt, args.timeout_respuesta)
    duracion = time.time() - t_run

    # CLASIFICACIÓN sobre innerText (response_text): contrato histórico intacto.
    veredicto, heur = calcular_veredicto(response_text, chat_prompt, fuente)

    # Lo que se GUARDA es el markdown reconstruido (preserva tablas/negrita que
    # innerText aplana). Cae a innerText si no hubo markdown.
    response_final = response_md or response_text or ""

    # Una respuesta es ACEPTABLE (se guarda) si tiene marcadores #T#/#/B#/#/C# y no hay
    # degradación clara. OK_SANA vs DUDOSA es etiqueta de calidad (umbral de
    # chars). REVISAR también se guarda (prioridad a la caja) pero arrastra un
    # warning para revisión humana. DEGRADADA/INTERNAL_ERROR/SIN_RESPUESTA/CUOTA
    # son fallas reales.
    aceptable = veredicto in ("OK_SANA", "DUDOSA", "REVISAR")

    out.update({
        "ok": aceptable,
        "response": response_final,
        "duracion_seg": round(duracion, 2),
        "fuente_dom": fuente,
        "heuristicas": heur,
        "veredicto": veredicto,
        "incompleto": incompleto,
        # error_sospechado: el warning que acompaña a una transcripción REVISAR
        # (el worker lo loguea en api_errorlog y dispara el bit QA AI Studio).
        "error_sospechado": (error_sosp or None) if veredicto == "REVISAR" else None,
    })

    # Auditoría de rollout: si el markdown reconstruido difiere del innerText (hubo
    # tabla/negrita/etc. que se habría perdido), guardar el innerText crudo para
    # poder comparar en salida.json. Sólo cuando difieren → no bloatea páginas de
    # texto plano. PHP ignora esta key (lee 'response'); removible tras validar.
    if response_text and response_text != response_final:
        out["response_innertext"] = response_text

    # Token usage del tooltip: SÓLO sobre respuestas aceptables (hay un turno
    # completado con su contador), DESPUÉS de capturar la transcripción. No fatal:
    # si falla, los tokens quedan en null y el job sigue igual. output incluye
    # thoughts (AI Studio no los separa).
    if aceptable:
        usage = await leer_token_usage(page)
        if usage and usage.get('ok'):
            out["tokens_input"]  = usage.get('input')
            out["tokens_output"] = usage.get('output')
            out["tokens_total"]  = usage.get('total')
            out["token_usage_raw"] = usage
            sys.stderr.write(
                f"[ais] token usage: in={usage.get('input')} out={usage.get('output')} "
                f"total={usage.get('total')} (out incluye thoughts)\n"
            )
        else:
            sys.stderr.write(f"[ais] token usage no leído (usage={usage})\n")

    # "OK dudoso" por longitud: la transcripción se aceptó (ok=true) pero quedó por
    # debajo del piso fijo → probable truncado a mitad del stream. No la
    # descartamos (ya se guardó), pero marcamos para dejar el tab abierto en debug
    # y poder ver en vivo si el stream se cortó. Sólo sobre respuestas aceptadas:
    # un error/bloqueo ya trae su propio warning.
    if aceptable and len(response_text or "") < UMBRAL_LONGITUD_SOSPECHOSA:
        out["longitud_sospechosa"] = True
        sys.stderr.write(
            f"[ais] longitud sospechosa: {len(response_text or '')} chars "
            f"< {UMBRAL_LONGITUD_SOSPECHOSA} (posible truncado de stream) → tab queda abierto en debug\n"
        )

    if veredicto == "CUOTA":
        out["error"] = "cuota_agotada: AI Studio devolvió un mensaje de cuota/rate limit"
    elif veredicto == "REVISAR":
        # NO es error: se guarda la transcripción. El detalle del warning va en
        # error_sospechado (arriba), no en error.
        out["error"] = None
    elif veredicto == "BLOQUEO":
        out["error"] = "bloqueo_seguridad: AI Studio bloqueó la respuesta (recitation/safety). Sin transcripción usable."
    elif veredicto == "INTERNAL_ERROR":
        detalle = (response_text or "").strip()
        out["error"] = (f"internal_error: {detalle[:300]}" if detalle
                        else "internal_error: AI Studio devolvió 'An internal error has occurred.'")
    elif veredicto == "RESPUESTA_CHROME":
        detalle = (response_text or "").strip()[:80]
        out["error"] = (f"respuesta_chrome: el parser sólo obtuvo chrome de UI "
                        f"('{detalle}'), sin transcripción usable. Causa desconocida.")
    elif veredicto == "SIN_RESPUESTA":
        out["error"] = f"sin_respuesta: timeout {args.timeout_respuesta}s sin respuesta del modelo"
    elif veredicto == "DEGRADADA":
        out["error"] = f"degradada: {heur.get('token_repetition') or 'prompt_echo'} detectado"
    elif veredicto == "DUDOSA":
        # No es error — solo etiqueta de auditoría (chars bajos). El orquestador
        # PHP parsea normalmente y decide si necesita reintento por sin_marcador.
        out["error"] = None

    return out


# ============================================================
# Entry point async
# ============================================================

async def amain():
    args = parse_args()
    t0 = time.time()

    imagen = Path(args.imagen).resolve()
    prompt_path = Path(args.prompt).resolve()
    salida_json = Path(args.salida_json).resolve()

    if not imagen.is_file():
        salida_json.parent.mkdir(parents=True, exist_ok=True)
        salida_json.write_text(json.dumps({
            "ok": False, "error": f"imagen_no_existe: {imagen}",
            "veredicto": "ERROR_PREFLIGHT", "duracion_seg": 0,
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        sys.exit(3)

    if not prompt_path.is_file():
        salida_json.parent.mkdir(parents=True, exist_ok=True)
        salida_json.write_text(json.dumps({
            "ok": False, "error": f"prompt_no_existe: {prompt_path}",
            "veredicto": "ERROR_PREFLIGHT", "duracion_seg": 0,
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        sys.exit(3)

    prompt_completo = prompt_path.read_text(encoding='utf-8')
    salida_json.parent.mkdir(parents=True, exist_ok=True)

    out = {
        "ok": False, "error": None, "veredicto": None, "duracion_seg": 0.0,
        "response": "", "heuristicas": {}, "fuente_dom": None,
        "modelo_verificado": None, "media_resolution": None,
        "tab_cerrado": False, "incompleto": False,
        "modo_prompt": None, "sys_prompt_chars": 0, "chat_prompt_chars": 0,
        "fecha_iso": datetime.now().isoformat(timespec='seconds'),
        "imagen_path": str(imagen), "prompt_path": str(prompt_path),
        "modelo_pedido": args.modelo, "cdp": args.cdp,
    }

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(args.cdp, timeout=10000)
        except Exception as e:
            out["error"] = f"cdp_unreachable: {e}"
            out["veredicto"] = "ERROR_CDP"
            out["duracion_seg"] = round(time.time() - t0, 2)
            salida_json.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
            sys.exit(2)

        if not browser.contexts:
            out["error"] = "cdp_sin_contexts"
            out["veredicto"] = "ERROR_CDP"
            out["duracion_seg"] = round(time.time() - t0, 2)
            salida_json.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
            sys.exit(2)

        ctx = browser.contexts[0]
        page = await ctx.new_page()
        page.set_default_timeout(30000)

        # Forzar viewport amplio via CDP. La ventana del Chrome lanzado por
        # iniciar_sistema.bat suele estar minimizada, y AI Studio aplica un
        # layout responsive que colapsa el panel "Run settings" (donde vive
        # System instructions) cuando el viewport es angosto. Override de
        # devtools sobrevive a estado minimizado/restaurado de la ventana.
        try:
            cdp = await ctx.new_cdp_session(page)
            await cdp.send("Emulation.setDeviceMetricsOverride", {
                "width": 1600, "height": 1000,
                "deviceScaleFactor": 1, "mobile": False,
            })
        except Exception as e:
            sys.stderr.write(f"[ais] WARN no se pudo forzar viewport: {e}\n")

        try:
            res = await ejecutar_flujo(page, imagen, prompt_completo, args, t0)
            out.update(res)
        except Exception as e:
            out["error"] = f"excepcion_flujo: {e}"
            out["veredicto"] = "ERROR_FLUJO"
            out["traceback"] = traceback.format_exc()
            sys.stderr.write(f"\n[ais] excepcion en flujo: {e}\n")
            sys.stderr.write(out["traceback"])
        finally:
            out["duracion_seg"] = round(time.time() - t0, 2)

            # Modo hold (debug): no tocar nada — dejar el tab tal cual para que
            # Tomás inspeccione el popup. Saltar cierre.
            es_hold = bool(out.get("hold"))

            # ¿Hay algo que valga la pena inspeccionar VISUALMENTE en el tab?
            #   - un error real (ok=false), o un REVISAR (ok=true + error_sospechado).
            # Se deja abierto SÓLO en debug y SÓLO si es inspeccionable. NO lo es:
            #   - permission denied PRE-Run (fuente_dom='error_snackbar_pre_run'): el
            #     snackbar ya estaba antes de disparar Run → NO hubo generación, nada
            #     que inspeccionar (rechazo de auth de Drive ya entendido) → cerrar aun
            #     en debug. El permission denied POST-Run SÍ se deja abierto: un Run
            #     corrió y el modelo pudo generar transcripción antes de colapsar al
            #     snackbar (caso 92s visto por Tomás 2026-06-01: se vio la transcripción
            #     escupida y luego "permission denied"). Para cuando el parser pollea,
            #     esa transcripción ya colapsó a ~chrome de UI, así que el tab abierto
            #     es la única vía de inspección. (Antes se cerraba TODO permission
            #     denied; revertido a pre-Run-only por pedido de Tomás 2026-06-01.)
            #   - recitation block: causa ya entendida (el modelo cree recitar prensa
            #     antigua); el turno sólo trae el chip de warning, nada inspeccionable
            #     → cerrar aun en debug (pedido de Tomás 2026-06-01). Otros safety
            #     blocks (blocked/safety/prohibited) SÍ se dejan abiertos.
            #   - no_logueado: una pantalla de login no se debuggea y come recursos.
            #   - cuota agotada: el snackbar de cuota ("user has exceeded quota") es
            #     un mensaje breve sin nada que inspeccionar visualmente; además la
            #     cuenta queda en cooldown ~6h, así que no tiene sentido dejar su
            #     Chrome con un tab colgado → cerrar aun en debug (pedido de Tomás
            #     2026-06-01).
            # NUNCA cerrar en modo hold (el objetivo es dejarlo abierto).
            permiso_pre_run = (out.get("fuente_dom") == 'error_snackbar_pre_run')
            recitation_block = (out.get("heuristicas") or {}).get("bloqueo_motivo") == 'recitation'
            cuota_block = (out.get("veredicto") == 'CUOTA')
            hay_warning = ((not out.get("ok"))
                           or bool(out.get("error_sospechado"))
                           or bool(out.get("longitud_sospechosa")))
            inspeccionable = (hay_warning and not permiso_pre_run
                              and not recitation_block
                              and not cuota_block
                              and not out.get("no_logueado"))
            cerrar = (not es_hold) and (not inspeccionable or not args.no_cerrar_tab_error)
            if cerrar:
                try:
                    await page.close()
                    out["tab_cerrado"] = True
                except Exception:
                    pass

            try:
                # No cerrar el browser entero (es del usuario por CDP).
                # Solo desconectamos.
                await browser.close()  # cierra la conexión, no el Chrome real
            except Exception:
                pass

    salida_json.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    sys.exit(0)


def main():
    try:
        asyncio.run(amain())
    except SystemExit:
        raise
    except Exception as e:
        sys.stderr.write(f"FATAL: {e}\n")
        sys.stderr.write(traceback.format_exc())
        sys.exit(4)


if __name__ == "__main__":
    main()
