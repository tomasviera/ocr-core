#!/usr/bin/env python
"""
scripts/transcribir_agy.py

Wrapper de producción que transcribe UNA imagen vía Antigravity CLI (`agy`),
lanzándolo bajo una pseudo-consola (ConPTY/pywinpty) y des-renderizando su
buffer con pyte para extraer texto limpio entre los marcadores
<<<INICIO_TRANSCRIPCION>>> y <<<FIN_TRANSCRIPCION>>> que el prompt instruye.

Producto del smoke #0 (`temp/tests/2026-06-04_164010_agy_smoke_conpty/`), que
validó la mecánica ConPTY+pyte contra agy real (1 imagen, OK_FIN, 7286 chars).
El núcleo (`capturar()` + `CaptureResult`) está embebido literal desde
`harness/capture_core.py` para que este `.py` sea autocontenido.

Lo invoca PHP (`web/includes/lib_agy.php`) desde un sandbox PRE-TRUSTED en
`agy.trustedWorkspaces` (setup manual, una vez por sandbox):

  python scripts/transcribir_agy.py \\
      --imagen <ruta>/page.b64 \\
      --prompt <ruta>/prompt.md \\
      --salida-json <sandbox>/salida.json \\
      --sandbox-dir <sandbox> \\
      [--home-dir C:/Users/Tomas/.gemini] \\
      [--modelo-agy "Gemini 3.5 Flash (Low)"] \\
      [--timeout 300] \\
      [--cmd-mode interactive|print|paste]
          # print = -p (markdown crudo, sin inflar tablas)
          # paste = TUI sin -i/-p con la imagen adjuntada por PORTAPAPELES
          #         (bump v34). WORKAROUND TEMPORAL del bug upstream agy #735
          #         (LS 1.1.10 manda `inline_data` de longitud 0 cuando el
          #         modelo abre una imagen con view_file → INVALID_ARGUMENT
          #         400): el paste adjunta la imagen como media del mensaje y
          #         evita el converter roto.
          #         https://github.com/google-antigravity/antigravity-cli/issues/735
          #         En este modo el texto se lee de la .db de conversación
          #         (`fuente_response="db"`), NO de la pantalla: el TUI
          #         renderiza el markdown y destruye los tags
          #         `<ilegible>`/`<dudoso>` que el prompt exige.
          #         Si el bug se arregla upstream: evaluar volver a 'print' y
          #         re-verificar fuente db, señal imagen_cargada, chip y cols.

El bundle forense (`console_raw`, `history_text`, `extracted_*`, `metrics.json`,
`agy_logfile.log`) se escribe SIEMPRE en `<workdir_efímero>/debug/`, ok o !ok.
El workdir queda intacto al terminar el subprocess; el caller PHP decide después
(post-QA) si borrarlo o moverlo a un archive_dir.

Filosofía one-shot estricta (plan §"Cambios por archivo"):
  - UNA transcripción y nada más. Toda la política de error / reintento /
    cooldown / requeue vive en PHP (lib_worker_policy + worker).
  - Sin reintento interno, sin sleep entre intentos, sin loops.

Shape de retorno del JSON (espejo de `ejecutarAiStudio()`; el wrapper
`lib_agy.php` agrega/pisa `intentos`, `exit_code`, `sandbox_path`,
`duracion_seg` de proc_open). Veredictos simplificados (plan #3 §2):
SIN_FIN ya no es veredicto del .py — PHP lo detecta con qaDetectarSinFin()
sobre `response`.

Persistencia previa a discriminación (2026-06-21): `response` SIEMPRE se
puebla con la mejor evidencia disponible y `fuente_response` reporta de
dónde salió. PHP decide post-hoc qué hacer (QA bits según `fuente_response`).
Caso histórico: si agy emitió la transcripción pero NO los marcadores (v2
con prompt `[tipo:]`, o prensa con instruction-following degradado), antes
se perdía todo + colgaba 300s; ahora viaja y agy se cierra por quiescencia.
  {
    "ok": bool,                 # true si `response` no está vacío (cualquier fuente)
    "response": str,            # mejor evidencia disponible. Jerarquía:
                                #   1) desde el ÚLTIMO INICIO hasta el final
                                #      del history (FIN incluido si estaba);
                                #      PHP recorta AMBOS sentinelas en
                                #      parseAndInsertEntradas
                                #   2) history_text completo (sin INICIO)
                                #   3) screen_snapshot
                                #   4) ""
    "fuente_response": str,     # "ini_fin" | "ini_only" | "history" | "screen" | "vacio"
                                # | "db" (v34, sólo cmd_mode=paste: el texto sale
                                #   de la conversation.db, con los tags intactos)
    "error": str|null,
    "veredicto": "OK"|"ERROR"|"CUOTA",
    "engine": "agy",
    "stats": [], "tools": null,
    # tokens: del side-channel statusLine si el setup manual está hecho
    # (~/.gemini/antigravity-cli/settings.json); si no, todos en 0.
    "tokens_input":0, "tokens_output":0, "tokens_thought":0,
    "tokens_cached":0, "tokens_total":0,
    "session_id": null,
    "stdout_raw": str,          # console_raw capado a STDOUT_CAP chars
    "stderr_raw": "",
    "cuota_agotada": bool,
    # extras agy
    "fin_presente": bool,       # → QA_BIT_SIN_FIN si false con response no vacío
    "websearch_detectado": bool,# → QA_BIT_AGY_WEBSEARCH (8192)
    "websearch_patrones": [str],
    "websearch_fuente": str,    # "db_steps" | "tools_used" | "heuristica" | "none"
                                # db_steps (v23+): grounding server-side + tools web
                                # leídos de la .db de conversación. Cubre `-p` donde
                                # el chrome del TUI no ecoa y captura el grounding
                                # `vertexaisearch` que nunca fue visible en `-i`.
    "tools_used": [{"name":str,"args":str}],  # v23+: si hubo tools en la .db,
                                # viene de ahí (nombres canónicos + args JSON
                                # snippet). Fallback al chrome del TUI en `-i`.
    "longitud_sospechosa": bool,# response < UMBRAL (espejo de aistudio)
    "stdout_largo_sospechoso": bool,  # console_raw > UMBRAL_STDOUT_SOSPECHOSO
    "estado_captura": str,      # OK_FIN | QUIESCENT_NO_MARKER | TIMEOUT | PROC_EXIT | ERROR_SPAWN
                                # | LOOP_DEGENERADO (v31: corte temprano por loop de salida)
                                # | PASTE_SIN_CHIP (v34: el adjunto no apareció en el
                                #   TUI ⇒ NO se envió el mensaje ⇒ cero cuota ⇒
                                #   veredicto TRANSITORIO, motivo `paste_sin_chip`)
    # extras del modo paste (v34). Siempre presentes, con default, para no romper
    # consumidores viejos; en interactive/print valen False/0/None.
    "paste_chip_detectado": bool,
    "paste_reintentos": int,
    "clipboard_restaurado": bool,
    "media_en_log": int|None,   # `media=N` del --log-file; ≥1 = la imagen viajó
                                # como media del mensaje del usuario
    "duracion_seg": float,
    "bytes_leidos": int,
    "zombis_barridos": int,
    "modelo_pedido": str,
    # forense statusLine (no a DB; útil en bundle debug)
    "statusline_disponible": bool,
    "context_window_size": int,
    "used_percentage": float,
    "plan_tier": str,
    "fecha_iso": str
  }

Exit codes (espejo de transcribir_aistudio.py):
  0 = pudo escribir `salida.json` (ok puede ser true o false adentro)
  3 = falla pre-flight (imagen/prompt no existe; sandbox no existe)
  4 = excepción inesperada antes de escribir el JSON

PRECONDICIÓN (setup manual, plan §Setup):
  El `--sandbox-dir` debe estar agregado UNA vez a `trustedWorkspaces` del
  settings.json global de agy
  (C:\\Users\\Tomas\\.gemini\\antigravity-cli\\settings.json), vía
  `/permissions` en una sesión interactiva. Tomás lo hace.
"""

from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# Windows: forzar stdout/stderr a utf-8 (mismo gesto que transcribir_aistudio.py)
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

try:
    import pyte
    import winpty
except ImportError as e:
    sys.stderr.write(f"ERROR: falta dependencia Python ({e}). Instalá con:\n"
                     "  pip install pyte pywinpty psutil\n")
    sys.exit(3)

try:
    import psutil
except ImportError:
    sys.stderr.write("ERROR: falta psutil. Instalá con: pip install psutil\n")
    sys.exit(3)


# ============================================================
# CONSTANTES
# ============================================================

INI_MARKER = "<<<INICIO_TRANSCRIPCION>>>"
FIN_MARKER = "<<<FIN_TRANSCRIPCION>>>"

# Largo mínimo del candidato entre INICIO/FIN para considerarlo transcripción
# real (filtra el eco "<<<INICIO>>> y <<<FIN>>>" del prompt).
MIN_CONTENT_LEN = 20

# Cap del console_raw que se devuelve en `stdout_raw` (la DB no lo necesita
# entero; el bundle .txt completo va a `<workdir>/debug/` cuando se vuelca).
# Subido 2026-06-04 (plan #3 §6): el smoke fue 61 KB, pero thoughts del modelo
# en consola podrían pasar de 100 KB.
STDOUT_CAP = 256 * 1024  # 256 KiB
UMBRAL_STDOUT_SOSPECHOSO = 180 * 1024  # forense: flag si console_raw se acerca al cap

# Umbral de longitud sospechosa (espejo de transcribir_aistudio.py: el
# parámetro fijo del Lucero como referencia). Bajado a 1000 por Tomás 2026-06-01.
UMBRAL_LONGITUD_SOSPECHOSA = 1000

# Cierre por quiescencia sin marcador (2026-06-21). Si bytes congelaron
# `quiescent_seg` segundos Y total_bytes >= MIN_BYTES_PLAUSIBLES Y no hubo
# INICIO/FIN, asumimos que agy terminó la respuesta y quedó ocioso esperando
# otro turno (TUI vivo). Evita el cuelgue de 300s del v2 cuando el prompt no
# emite los marcadores. Mientras agy trabaja, el spinner del TUI escribe
# bytes continuos → last_byte_at se patea y este fallback no dispara.
MIN_BYTES_PLAUSIBLES = 10 * 1024

# ── Corte temprano por LOOP degenerado de salida (bump v31, 2026-07-31) ──────
# agy (medido: SIEMPRE `Gemini 3.1 Pro (Low)`) entra a veces en un estado donde
# la generación YA TERMINÓ pero el CLI queda escupiendo una palabra de estado
# (`producing`) sin newline hasta que el timeout de 600s lo mata. Medición sobre
# 35 casos reales en prensa (2026-06-29 → 2026-07-31, 32 con bundle forense):
#   - el `agy_logfile.log` no tiene un solo `streamGenerateContent` después de
#     ~t=26s; los 574s restantes son puro CLI colgado (cero cuota adicional),
#   - el stream entero es la unidad repetida: sacándole "producing" al
#     `raw_stripped` de 134 KB quedan 6 chars,
#   - 30/35 clavaron el timeout; el `response` (fuente=history) se persistió como
#     transcripción vigente → 31 páginas con ~135 KB de basura en `entradas`.
#
# La detección NO matchea el literal "producing": mide PERIODICIDAD degenerada
# sobre la cola del stream (`_periodo_minimo`, función de fallo de KMP), así que
# cubre cualquier token que agy repita en el futuro. Umbrales calibrados contra
# los 278 bundles forenses archivados: 32/32 loops detectados, 0/246 corridas
# sanas tocadas (test `temp/tests/2026-07-31_agy_loop_detector/`).
LOOP_VENTANA_CHARS    = 20000   # cola del stream sobre la que se mide el período
LOOP_MIN_CHARS        = 2000    # nada por debajo de esto se evalúa
LOOP_MAX_PERIODO      = 48      # la unidad repetida tiene que ser CORTA
LOOP_MIN_REPETICIONES = 200     # ...y repetirse muchas veces (≥ 200 × ≤48 chars)
# Con estos valores el corte cae a ~t=45s (a t=45s hay ~4 KB acumulados) en vez
# de t=600s. La generación real terminó a t≈26s ⇒ no se pierde nada.

# Comando -i corto (plan §D + smoke validado): referencia @prompt.md + @imagen.jpg.
# Evita el límite de 8191 chars del cmdline; el prompt completo está en el
# archivo dentro del sandbox.
CMD_I_DEFAULT = ("Transcribí la imagen @imagen.jpg siguiendo al pie de la letra "
                 "las instrucciones de @prompt.md. No uses búsqueda web.")

# ── MODO `paste` (bump v34, 2026-08-05) ──────────────────────────────────────
# WORKAROUND TEMPORAL — bug upstream agy #735 (LS 1.1.10 manda inline_data
# vacío cuando el modelo abre una imagen con view_file → INVALID_ARGUMENT 400).
# https://github.com/google-antigravity/antigravity-cli/issues/735
# El paste del TUI adjunta la imagen como media del mensaje y evita el
# converter roto. Si el bug se arregla upstream: evaluar volver a cmd_mode
# 'print' (markdown crudo sin TUI, sin tocar el portapapeles) y re-verificar
# los subsistemas tocados (fuente db, señal imagen_cargada, chip, cols).
#
# Todas las constantes de abajo salen CALIBRADAS del test que validó la
# mecánica contra agy real: `temp/tests/2026-08-03_170000_agy_tui_paste_media/`
# (corrida `B_real_file_ctrlv`: media=1, 2,8 MB adjuntos, 26.808 chars).
PASTE_COLS, PASTE_ROWS = 220, 80        # el TUI pinta cajas: ancho realista, no 2000
PASTE_READY_QUIESCENT_SEG   = 5.0
PASTE_READY_TIMEOUT_SEG     = 150.0     # cold start de agy puede ir a ~80 s
PASTE_ESPERA_POST_TECLA_SEG = 5.0       # espera fija tras Ctrl+V antes de drenar
PASTE_CHIP_QUIESCENT_SEG    = 3.0
PASTE_CHIP_TIMEOUT_SEG      = 25.0
PASTE_RESP_QUIESCENT_SEG    = 30.0
PASTE_RESP_GRACIA_SEG       = 5.0       # tras ver FIN, esperar bytes estables
PASTE_CIERRE_QUIESCENT_SEG  = 3.0
PASTE_CIERRE_TIMEOUT_SEG    = 12.0
PASTE_DELAY_ANTES_ENTER_SEG = 0.8       # que el TUI pinte el texto antes del \r
PASTE_MAX_REINTENTOS_CHIP   = 1         # 1 reintento del par (clipboard, Ctrl+V)
PASTE_PS_TIMEOUT_SEG        = 60        # timeout de cada invocación del .ps1
# Presupuesto TOTAL de la captura. `capturar()` usa `timeout_seg` como total,
# pero en paste las fases previas (READY hasta 150 s + chip + reintento) son
# ADEMÁS del tiempo de generación. El wrapper PHP mata el subprocess a
# `timeout_respuesta_seg + 120` (lib_agy.php `$procTimeout`), y un taskkill de
# PHP deja el job sin `salida.json` (error genérico `proc_timeout`, sin
# veredicto ni bundle completo). Para que eso no pase, la fase de respuesta se
# recorta a lo que quede dentro de `timeout + PASTE_PRESUPUESTO_EXTRA_SEG`,
# reservando el cierre. En el camino feliz (READY ~15-80 s) no recorta nada.
PASTE_PRESUPUESTO_EXTRA_SEG = 90
PASTE_RESERVA_CIERRE_SEG    = 20
PASTE_RESP_TIMEOUT_MIN_SEG  = 30        # piso: nunca dejar la respuesta sin margen
PASTE_GATE_COLA_CHARS       = 256       # ventana deslizante del gate del marcador
PASTE_KEY_CTRL_V = "\x16"               # pegar
PASTE_KEY_CTRL_U = "\x15"               # limpiar la línea antes de reintentar
# Script de portapapeles: vive AL LADO de este .py (viaja con el vendor).
PASTE_CLIPBOARD_SCRIPT = "agy_clipboard.ps1"
# Firma del chip de adjunto en el snapshot pyte. La línea real medida es:
#   `  ▸ 📎 1 media attached (clipboard, 2.8 MB, image/png)  (ctrl+o to expand)`
# Se exigen los DOS tokens en la MISMA línea: "media attached" solo podría venir
# de texto pegado por el usuario, y "image/" solo aparece en cualquier mención
# de mime. Juntos identifican el chip del TUI.
PASTE_CHIP_TOKENS = ("media attached", "image/")
# Texto CORTO que se tipea después del paste (el prompt largo lo lee el modelo
# de disco). OJO: NADA de `@` — en el TUI la tecla `@` abre el menú interactivo
# de menciones y el Enter final confirmaría el popup en vez de enviar (medido).
PASTE_TEXTO_CORTO_TPL = (
    "Lee el archivo prompt.md del directorio de trabajo ({sandbox}) y "
    "transcribi la imagen adjunta siguiendo esas instrucciones."
)
# Línea del `--log-file` que dice cuántos medios viajaron con el mensaje del
# usuario. La real intercala `to conversation <uuid>` entre medio, así que el
# regex NO asume formato fijo ahí: `Forwarding user message to conversation
# <uuid> (items=1, media=1)`.
_RE_FORWARDING_MEDIA = re.compile(
    r"Forwarding user message\b.*?\(items=(\d+),\s*media=(\d+)\)")

# .agents/settings.json del sandbox (plan §D2 + smoke validado): SIN write_file
# (no escribimos output.txt; leemos consola), deny defensivo de WebSearch
# (NO confiable: por eso lo detectamos en console_raw).
#
# v27 (2026-07-17) — mitigación PROACTIVA del ritual `echo "Starting transcription"`
# que emite Gemini 3.1 Pro esporádicamente antes de transcribir en `-p`. Sin este
# allow, el `echo` chocaba con `command(*)` en deny → agy auto-deniega en headless
# → response literal "jetski: no output produced…" → job muere. Con v25 el jetski
# ya se etiqueta como `jetski_headless_deny: tool='command'` en vez de esconderse
# bajo `exploracion_agy`, pero seguía siendo terminal. Este allow lo previene.
#
# Sintaxis: `command(echo)`. La doc de agent-permissions dice "matches commands
# by exact word/token prefix"; interpretación esperada = prefix (todo command que
# arranque con `echo` matchea, incluyendo `echo "Starting transcription"`). A
# validar en próxima investigación de jobs — ver notas/motor_agy.md §Bump v27
# para el checklist: (a) allow gana sobre el `command(*)` en deny, (b) el patrón
# realmente cubre `echo <args con espacios/quotes>`, (c) no aparece uso malicioso.
# Si en (b) resulta que la interpretación es estricta por-token en vez de prefix,
# habrá que agregar variantes `command(echo .*)`, `command(echo .* .*)`, etc.
SANDBOX_SETTINGS = {
    "permissions": {
        "allow": [
            "tool(read_file)",
            "command(echo)",
        ],
        "deny": [
            "command(*)", "tool(run_terminal_cmd)", "tool(execute_command)",
            "tool(run_command)", "tool(shell)", "tool(Bash)", "tool(PowerShell)",
            "tool(web_search)", "tool(google_web_search)", "tool(web_fetch)",
            "tool(WebSearch)", "tool(WebFetch)", "tool(google_search)",
            "tool(fetch_url)",
            "tool(write_file)", "tool(create_file)", "tool(edit_file)",
            "tool(apply_patch)",
            "tool(list_dir)", "tool(glob)", "tool(grep)", "tool(grep_search)",
            "tool(delete_file)", "tool(open_url)", "tool(browser)",
        ],
    }
}

# Tool calls del TUI agy: formato fijo `● ToolName(args)` (smoke real, plan #3 §4).
# `\(.*?\)` no-greedy: matchea hasta el PRIMER `)`, así el sufijo
# `(ctrl+o to expand)` que agy a veces agrega queda fuera del grupo.
TOOL_CALL_RE = re.compile(r"^●\s+(\w+)\s*\((.*?)\)\s*(?:\(ctrl\+o.*?\))?\s*$",
                          re.MULTILINE)

# Nombres canónicos de tools de búsqueda web en agy (PascalCase, case-sensitive).
# Plan #3 §4: detección estructurada via tools_used; el fallback heurístico
# substring queda como red de seguridad por si agy cambia el formato del TUI.
WEBSEARCH_TOOL_NAMES = {
    "WebSearch", "WebFetch", "GoogleSearch", "GoogleWebSearch",
    "FetchUrl", "OpenUrl",
}

# Patrones substring (FALLBACK) para detectar WebSearch sobre console_raw /
# history / screen cuando el parseo estructurado no encuentra nada.
WEBSEARCH_PATRONES = [
    "WebSearch", "web_search", "google_web_search", "google_search",
    "Searching the web", "Buscando en la web", "web.run", "googleSearch",
]


# ============================================================
# capture_core EMBEBIDO (corazón validado en el smoke #0)
# ============================================================
# Copia literal de temp/tests/2026-06-04_164010_agy_smoke_conpty/harness/capture_core.py
# (rev. 2026-06-04). Embebido y no importado a propósito: este .py es
# autocontenido (el directorio temp/tests puede borrarse) y editarlo NO
# requiere reiniciar workers (invariante #1).
#
# Hallazgo empírico (smoke): bajo ConPTY el "crudo" leído NO son los bytes del
# hijo, es el VT que la ConPTY genera como diff de su pantalla interna →
# strip_ansi ingenuo da basura. Por eso la extracción va SIEMPRE sobre el grid
# de pyte (screen + history), nunca sobre el crudo. El crudo se guarda solo
# como forense.

_ANSI_RE = re.compile(
    r"""
    \x1b\][^\x07\x1b]*(?:\x07|\x1b\\)
  | \x1b[PX^_][^\x1b]*\x1b\\
  | \x1b\[[0-9;?!>=]*[ -/]*[@-~]
  | \x1b[@-Z\\-_]
    """,
    re.VERBOSE,
)


def strip_ansi(s: str) -> str:
    s = _ANSI_RE.sub("", s)
    out_lines = []
    for line in s.split("\n"):
        if "\r" in line:
            line = line.split("\r")[-1]
        out_lines.append(line)
    s = "\n".join(out_lines)
    s = "".join(ch for ch in s if ch >= " " or ch in "\n\t")
    return s


def _periodo_minimo(s: str) -> int:
    """Período mínimo de `s` vía la función de fallo de KMP.

    Para "abcabcabc" devuelve 3; para un texto no periódico devuelve len(s).
    O(n) en tiempo y memoria — con la ventana de 20k chars es despreciable
    incluso corriendo una vez cada `progress_seg`.
    """
    n = len(s)
    if n == 0:
        return 0
    fail = [0] * n
    k = 0
    for i in range(1, n):
        while k and s[i] != s[k]:
            k = fail[k - 1]
        if s[i] == s[k]:
            k += 1
        fail[i] = k
    return n - fail[n - 1]


def detectar_loop_degenerado(texto: str) -> dict:
    """¿La COLA de `texto` es una unidad corta repetida cientos de veces?

    Firma del bug de agy documentado en §"Bump v31": el CLI queda escupiendo
    una palabra de estado (`producing`) después de que la generación terminó.
    Se mide sobre la cola —no sobre todo el texto— para atrapar también el caso
    mixto (transcripción real y DESPUÉS el loop), que es donde `chars_loop`
    marca dónde termina lo rescatable.

    Devuelve siempre el mismo shape:
      {'detectado': bool, 'unidad': str, 'repeticiones': int, 'chars_loop': int}
    `unidad` es una ROTACIÓN cualquiera del token repetido (KMP no garantiza
    cuál) — es forense, no se matchea contra nada.
    """
    vacio = {"detectado": False, "unidad": "", "repeticiones": 0, "chars_loop": 0}
    if not texto or len(texto) < LOOP_MIN_CHARS:
        return vacio

    # Se mide sobre la proyección SIN SALTOS DE LÍNEA, con un mapa de índices de
    # vuelta al original. **Esto no es cosmético**: el stream crudo del PTY no
    # trae newlines, pero el `response` lo reconstruye pyte, que WRAPEA a `cols`
    # (2000) e inserta un '\n' — y ese newline rompe la periodicidad. Medido
    # sobre el job 65993 real: el history de 2381 chars queda como
    # [2000, 380] y su período mínimo salta de 9 a 2008 ⇒ sin esta proyección el
    # detector anda en `capturar()` pero NO en `shape_salida()`, y el recorte del
    # response no se aplica nunca. Ver `replay_real.py` del test.
    if "\n" in texto or "\r" in texto:
        compacto_chars, mapa = [], []
        for idx, ch in enumerate(texto):
            if ch not in "\r\n":
                compacto_chars.append(ch)
                mapa.append(idx)
        compacto = "".join(compacto_chars)
    else:
        compacto, mapa = texto, None

    if len(compacto) < LOOP_MIN_CHARS:
        return vacio

    cola = compacto[-LOOP_VENTANA_CHARS:]
    p = _periodo_minimo(cola)
    if p <= 0 or p > LOOP_MAX_PERIODO:
        return vacio
    reps_ventana = len(cola) // p
    if reps_ventana < LOOP_MIN_REPETICIONES:
        return vacio

    # Extender hacia atrás sobre el texto COMPLETO: el sufijo maximal con
    # período p. No se compara contra `unidad` porque el loop puede arrancar en
    # cualquier rotación — se compara char contra char a distancia p.
    i = len(compacto) - p
    while i > 0 and compacto[i - 1] == compacto[i - 1 + p]:
        i -= 1

    # `chars_loop` vuelve a coordenadas del ORIGINAL (incluye los newlines que
    # caen dentro del tramo), para que `texto[:len(texto)-chars_loop]` corte
    # justo donde arranca el loop.
    inicio_orig = mapa[i] if mapa is not None else i
    return {
        "detectado": True,
        "unidad": cola[:p],
        "repeticiones": (len(compacto) - i) // p,
        "chars_loop": len(texto) - inicio_orig,
    }


def _cola_para_loop(raw_parts: list, budget: Optional[int] = None) -> str:
    """Últimos `budget` chars del stream, juntados desde los chunks del PTY.

    Se recorre `raw_parts` de atrás para adelante acumulando por PRESUPUESTO DE
    CHARS, no por cantidad de chunks. Es la parte contraintuitiva: en el bug
    real agy escribe `producing` de a ~9 bytes, así que un `raw_parts[-N:]` con
    N fijo devolvería unos cientos de chars —por debajo de LOOP_MIN_CHARS— y el
    detector no dispararía NUNCA. Costo acotado: sólo se tocan los chunks
    necesarios para llenar el presupuesto, una vez cada `progress_seg`.
    """
    if budget is None:
        budget = 4 * LOOP_VENTANA_CHARS
    piezas, acum = [], 0
    for pedazo in reversed(raw_parts):
        piezas.append(pedazo)
        acum += len(pedazo)
        if acum >= budget:
            break
    piezas.reverse()
    return "".join(piezas)[-budget:]


def extract_between(text: str, ini: str, fin: str) -> Optional[str]:
    """Texto entre el ÚLTIMO `ini` y el ÚLTIMO `fin` posterior a ese `ini`."""
    i = text.rfind(ini)
    if i == -1:
        return None
    after = i + len(ini)
    j = text.rfind(fin)
    if j == -1 or j < after:
        return None
    return text[after:j].strip("\r\n")


def extract_from_last_ini(text: str, ini: str, fin: str = "") -> Optional[str]:
    """Desde el ÚLTIMO `ini` (inclusive) hasta el final del texto.

    Plan #3 §3: NO se corta en `fin`. El `response` que el .py entrega a PHP
    incluye AMBOS sentinelas si estaban presentes; PHP los recorta a los dos
    en parseAndInsertEntradas() (lib_api_caller.php:611-627: `strrpos` para
    INICIO descarta marker y todo lo previo; `strpos` para FIN descarta
    marker y todo lo posterior). Mantenerlos en el raw ayuda a la auditoría
    desde `api_RawResponse` (truncado real vs bug del parser). El argumento
    `fin` queda en la firma por compatibilidad con la API original de
    capture_core pero ya no se usa.
    """
    i = text.rfind(ini)
    if i == -1:
        return None
    return text[i:].strip("\r\n")


def _history_text(screen: "pyte.HistoryScreen") -> str:
    lines = []
    for buf in list(screen.history.top):
        if buf:
            width = max(buf.keys()) + 1
            lines.append("".join(buf[x].data if x in buf else " " for x in range(width)).rstrip())
        else:
            lines.append("")
    lines.extend(line.rstrip() for line in screen.display)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _screen_text(screen: "pyte.Screen") -> str:
    lines = [line.rstrip() for line in screen.display]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


@dataclass
class CaptureResult:
    estado: str                       # OK_FIN | QUIESCENT_NO_MARKER | TIMEOUT | PROC_EXIT
                                      # | ERROR_SPAWN | LOOP_DEGENERADO (v31)
    fin_visto: bool
    duracion_seg: float
    bytes_leidos: int
    console_raw: str = ""
    raw_stripped: str = ""
    screen_snapshot: str = ""
    history_text: str = ""
    extracted_screen: Optional[str] = None
    extracted_history: Optional[str] = None
    partial_from_ini: Optional[str] = None
    pid: Optional[int] = None
    exitstatus: Optional[int] = None
    error: Optional[str] = None
    notas: list = field(default_factory=list)
    # Detección de cuota agotada (HTTP 429) leída del .db de la conversación de
    # ESTE job (ver _detectar_cuota_en_conversacion). En `-p` el 429 no llega a
    # consola/history → vive sólo en SQLite. Si True, decidir_veredicto retorna
    # CUOTA y shape_salida marca `cuota_agotada=true`.
    cuota_detectada: bool = False
    cuota_reset_seg: Optional[int] = None  # parseado de "Resets in 13m27s"; None si no se pudo
    # Fallo de ARRANQUE anterior a cualquier llamado de generación al LLM (ver
    # _detectar_arranque_transitorio). Si True, agy murió antes de invocar el
    # endpoint `streamGenerateContent` (backend 500 al resolver modelo, keyring
    # /auth timeout, etc.) → NO consumió cuota → decidir_veredicto retorna
    # TRANSITORIO y el worker lo reintenta (cap) en vez de fail-fast a ERROR.
    transitorio_detectado: bool = False
    transitorio_motivo: str = ""
    # LOOP degenerado de salida (bump v31): agy quedó repitiendo una unidad
    # corta (`producing`) después de terminar de generar. Lo puebla el tick de
    # progreso de `capturar()` vía `detectar_loop_degenerado`; cuando dispara,
    # el estado de cierre es "LOOP_DEGENERADO" (no TIMEOUT).
    loop_detectado: bool = False
    loop_unidad: str = ""
    loop_repeticiones: int = 0
    loop_chars: int = 0
    # ── v37 ──────────────────────────────────────────────────────────────────
    # `spawn_ok`: el proceso arrancó. Es un HECHO observado, no el valor inicial
    # de `estado`. `estado` nace en "ERROR_SPAWN" y sólo se reasigna dentro del
    # try de las fases, así que hasta v36 una excepción en CUALQUIER fase
    # posterior (incluso después de que el modelo generó y escribió en la .db)
    # salía rotulada "falló el spawn" → ERROR terminal, fail-fast, sin mirar si
    # había texto. Con esto, `estado == "ERROR_SPAWN" and spawn_ok` significa
    # "excepción de fase", que es infraestructura y se trata distinto.
    spawn_ok: bool = False
    # Texto candidato leído de la .db de conversación. Hasta v36 lo poblaba
    # main() sólo en modo `paste`; desde v37 se intenta en los 3 modos como
    # RESCATE cuando la pantalla no dio nada.
    response_db: Optional[str] = None
    db_fuente: str = ""          # "db" (INICIO+FIN) | "db_parcial" (INICIO sin FIN) | ""
    db_rechazo: str = ""         # por qué NO se aceptó el candidato; "" si pasó
    db_candidato: Optional[str] = None   # mejor segmento hallado, PASE O NO las guardas
    db_marcas_pagina: int = 0    # marcas de estructura en líneas no-razonamiento
    db_razonamiento: bool = False  # el segmento trae thinking mezclado (etiqueta QA)
    # ¿La .db elegida es la de ESTA corrida? Tri-estado (True/False/None=no
    # verificable). Gatea el rescate de texto Y las señales derivadas (v37).
    db_identidad_ok: Optional[bool] = None
    # Forense del `--log-file`, leído una sola vez por main() (v33).
    hubo_generacion: Optional[bool] = None
    subcausa_log: str = ""
    # Camino de RESCATE por el que entró el texto (v37): viaja al shape como
    # `rescate_motivo` y prensa lo convierte en el QA grave `texto_rescatado`.
    # "" = camino normal.
    rescate_motivo: str = ""


def capturar(
    argv: list,
    *,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    cols: int = 2000,
    rows: int = 100,
    history_lines: int = 8000,
    timeout_seg: float = 240.0,
    ini_marker: str = INI_MARKER,
    fin_marker: str = FIN_MARKER,
    fin_grace_seg: float = 5.0,
    quiescent_seg: float = 30.0,
    read_size: int = 4096,
    on_chunk=None,
    verbose: bool = True,
    progress_seg: float = 10.0,
) -> CaptureResult:
    """Lanza `argv` bajo ConPTY; cierra al ver candidato válido + bytes estables.

    Caminos de cierre, en orden de prioridad:
      1) OK_FIN              — apareció FIN_MARKER + `fin_grace_seg` estables.
      2) PROC_EXIT           — agy cerró solo (raro: corona TUI ocioso).
      3) QUIESCENT_NO_MARKER — no hubo INICIO/FIN pero los bytes congelaron
                               `quiescent_seg` segundos con MIN_BYTES_PLAUSIBLES
                               ya leídos. Cubre v2 (prompt `[tipo:]` sin
                               marcadores) sin esperar al timeout.
      4) TIMEOUT             — `timeout_seg` total agotado.
    """
    res = CaptureResult(estado="ERROR_SPAWN", fin_visto=False, duracion_seg=0.0, bytes_leidos=0)

    plain = pyte.Screen(cols, rows)
    plain_stream = pyte.Stream(plain)
    hist = pyte.HistoryScreen(cols, rows, history=history_lines, ratio=0.5)
    hist_stream = pyte.Stream(hist)

    raw_parts: list = []
    total_bytes = 0

    try:
        proc = winpty.PtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(rows, cols))
    except Exception as e:
        res.error = f"spawn: {type(e).__name__}: {e}"
        return res

    res.pid = getattr(proc, "pid", None)
    res.spawn_ok = True   # v37: el spawn anduvo (ver el campo en CaptureResult)

    # Lector en thread daemon (pywinpty.read() BLOQUEA sin datos → si va en el
    # loop principal, un agy esperando input cuelga el timeout).
    q: "queue.Queue" = queue.Queue()
    stop_flag = threading.Event()

    def _reader():
        while not stop_flag.is_set():
            try:
                ch = proc.read(read_size)
            except EOFError:
                q.put(None)
                return
            except Exception as e:
                q.put(("__EXC__", f"{type(e).__name__}: {e}"))
                return
            if ch:
                q.put(ch)
            else:
                if not proc.isalive():
                    q.put(None)
                    return
                time.sleep(0.02)

    reader = threading.Thread(target=_reader, name="conpty-reader", daemon=True)
    reader.start()

    t0 = time.monotonic()
    last_byte_at = t0
    last_tick = t0
    candidato_at: Optional[float] = None
    best_len = 0
    proc_eof = False

    def evaluar_candidato():
        nonlocal best_len, candidato_at
        scr = _screen_text(plain)
        if fin_marker not in scr and fin_marker not in "".join(plain.display):
            htxt_quick = _history_text(hist)
            if fin_marker not in htxt_quick:
                return False
            htxt = htxt_quick
        else:
            htxt = _history_text(hist)

        cand_s = extract_between(scr, ini_marker, fin_marker)
        cand_h = extract_between(htxt, ini_marker, fin_marker)
        mejor = max(
            [(len(c), c, src) for c, src in [(cand_s, "screen"), (cand_h, "history")]
             if c and len(c) >= MIN_CONTENT_LEN],
            default=None,
        )
        if mejor is None:
            return False
        lng = mejor[0]
        if lng > best_len:
            best_len = lng
            if cand_s and len(cand_s) >= MIN_CONTENT_LEN:
                res.extracted_screen = cand_s
            if cand_h and len(cand_h) >= MIN_CONTENT_LEN:
                res.extracted_history = cand_h
            res.screen_snapshot = scr
            res.history_text = htxt
            res.partial_from_ini = extract_from_last_ini(htxt, ini_marker, fin_marker)
            res.fin_visto = True
        if candidato_at is None:
            candidato_at = time.monotonic()
        return True

    try:
        while True:
            now = time.monotonic()
            if now - t0 >= timeout_seg:
                res.estado = "TIMEOUT"
                break

            chunk = None
            try:
                item = q.get(timeout=0.2)
                if item is None:
                    proc_eof = True
                elif isinstance(item, tuple) and item and item[0] == "__EXC__":
                    res.notas.append(f"read-exc: {item[1]}")
                    proc_eof = True
                else:
                    chunk = item
            except queue.Empty:
                chunk = None

            if chunk:
                raw_parts.append(chunk)
                total_bytes += len(chunk)
                last_byte_at = now
                plain_stream.feed(chunk)
                hist_stream.feed(chunk)
                if on_chunk:
                    try:
                        on_chunk(chunk)
                    except Exception:
                        pass
                if fin_marker[-6:] in chunk or candidato_at is not None:
                    evaluar_candidato()

            if proc_eof and q.empty():
                evaluar_candidato()
                res.estado = "PROC_EXIT"
                break

            if candidato_at is not None and (now - last_byte_at) >= fin_grace_seg:
                res.estado = "OK_FIN"
                break

            # Fallback: agy en modo TUI no se autocierra al terminar la
            # respuesta — queda vivo esperando otro turno. Si bytes congelaron
            # quiescent_seg con MIN_BYTES_PLAUSIBLES ya leídos y nunca vimos
            # INICIO_MARKER, asumimos "respuesta terminada sin marcador" y
            # cerramos. Si vinieron marcadores, OK_FIN gana antes.
            if (candidato_at is None
                    and total_bytes >= MIN_BYTES_PLAUSIBLES
                    and (now - last_byte_at) >= quiescent_seg):
                res.estado = "QUIESCENT_NO_MARKER"
                break

            if (now - last_tick) >= progress_seg:
                last_tick = now
                if verbose:
                    cand = "sí" if candidato_at is not None else "no"
                    print(f"  [capturar] t={int(now - t0)}s bytes={total_bytes} "
                          f"candidato={cand} alive={proc.isalive()}",
                          file=sys.stderr, flush=True)

                # ── Corte temprano por LOOP degenerado (bump v31) ────────────
                # Mismo tick que el progreso: una sola pasada cada
                # `progress_seg`, sobre la COLA del stream (no sobre los 134 KB
                # completos) para que el costo no crezca con la duración.
                # `strip_ansi` sobre el último tramo puede cortar una secuencia
                # de escape por la mitad: inocuo (a lo sumo deja basura al
                # inicio de la ventana, que el período ignora).
                #
                # Gate `candidato_at is None`: si YA vimos FIN_MARKER, el cierre
                # por OK_FIN va a ganar en ≤ fin_grace_seg y no hay nada que
                # ahorrar — no le pisamos el camino feliz.
                if candidato_at is None and total_bytes >= LOOP_MIN_CHARS:
                    info_loop = detectar_loop_degenerado(
                        strip_ansi(_cola_para_loop(raw_parts)))
                    if info_loop["detectado"]:
                        res.loop_detectado    = True
                        res.loop_unidad       = info_loop["unidad"]
                        res.loop_repeticiones = info_loop["repeticiones"]
                        res.estado = "LOOP_DEGENERADO"
                        print(f"  [capturar] LOOP degenerado detectado a t={int(now - t0)}s "
                              f"(unidad={info_loop['unidad']!r} reps≥{info_loop['repeticiones']}) "
                              f"→ cierro agy sin esperar el timeout",
                              file=sys.stderr, flush=True)
                        break

        res.duracion_seg = round(time.monotonic() - t0, 2)
        if candidato_at is None:
            res.screen_snapshot = _screen_text(plain)
            res.history_text = _history_text(hist)
            res.partial_from_ini = extract_from_last_ini(res.history_text, ini_marker, fin_marker)

    finally:
        stop_flag.set()
        try:
            res.exitstatus = proc.exitstatus
        except Exception:
            pass
        try:
            if proc.isalive():
                proc.terminate(force=True)
        except Exception:
            pass

    res.console_raw = "".join(raw_parts)
    res.raw_stripped = strip_ansi(res.console_raw)
    res.bytes_leidos = total_bytes

    # Medición definitiva del loop sobre el stream COMPLETO (el corte de arriba
    # se decide sobre la cola, que es una muestra). Sólo forense: alimenta
    # metrics.json / el stderr. La decisión "¿queda algo rescatable?" NO se toma
    # acá sino en `shape_salida`, sobre el `response` que se iría a persistir.
    if res.loop_detectado:
        _info_final = detectar_loop_degenerado(res.raw_stripped)
        if _info_final["detectado"]:
            res.loop_unidad       = _info_final["unidad"]
            res.loop_repeticiones = _info_final["repeticiones"]
            res.loop_chars        = _info_final["chars_loop"]

    return res


# ============================================================
# MODO `paste` — captura TUI con la imagen adjuntada por portapapeles (v34)
# ============================================================
#
# WORKAROUND TEMPORAL — bug upstream agy #735 (LS 1.1.10 manda inline_data
# vacío cuando el modelo abre una imagen con view_file → INVALID_ARGUMENT 400).
# https://github.com/google-antigravity/antigravity-cli/issues/735
# El paste del TUI adjunta la imagen como media del mensaje y evita el
# converter roto. Si el bug se arregla upstream: evaluar volver a cmd_mode
# 'print' (markdown crudo sin TUI, sin tocar el portapapeles) y re-verificar
# los subsistemas tocados (fuente db, señal imagen_cargada, chip, cols).
#
# Diferencias con `capturar()`, todas medidas en el test que validó la mecánica:
#   - argv SIN `-i`/`-p`: si el mensaje se pasa por línea de comandos, agy lo
#     envía ANTES de que podamos pegar y el adjunto no llega a viajar.
#   - PTY 220×80: el TUI pinta cajas; con cols=2000 el chrome se deforma.
#   - el TEXTO se tipea después del paste y NO lleva `@` (la tecla abre el menú
#     de menciones y el Enter confirmaría el popup en vez de enviar).
#   - la transcripción NO sale de la pantalla: el TUI renderiza el markdown y
#     DESTRUYE los tags `<ilegible>`/`<dudoso>` (medido: 271 en la .db, 0 en el
#     grid pyte). El texto sale de la .db (`_extraer_transcripcion_db`); el
#     grid/history quedan sólo como evidencia forense en el bundle.


def _ps_clipboard(args_ps: list, timeout: float = PASTE_PS_TIMEOUT_SEG) -> tuple:
    """Invoca `agy_clipboard.ps1` (sibling de este .py) y devuelve
    (returncode, stdout_bytes, stderr_str).

    `-STA` es OBLIGATORIO: las APIs de portapapeles de WinForms sólo funcionan
    en un apartment single-threaded. Best-effort: cualquier excepción
    (timeout, powershell ausente) sale como rc=-1 con el detalle en stderr.
    """
    script = Path(__file__).resolve().parent / PASTE_CLIPBOARD_SCRIPT
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA",
           "-ExecutionPolicy", "Bypass", "-File", str(script)] + [str(a) for a in args_ps]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return (r.returncode, r.stdout or b"",
                (r.stderr or b"").decode("utf-8", "replace").strip())
    except Exception as e:
        return (-1, b"", f"{type(e).__name__}: {e}")


def _chip_media_presente(snapshot: str) -> bool:
    """¿El snapshot del TUI tiene la línea del chip de adjunto?

    Exige los dos tokens de `PASTE_CHIP_TOKENS` en la MISMA línea. Es la red
    anti-colisión con el usuario: si alguien copió algo en la ventana crítica
    (el portapapeles es global), el Ctrl+V pegó SU contenido y esta firma NO
    aparece → el .py reintenta y, si vuelve a fallar, cierra agy sin enviar
    nada (cero cuota consumida).
    """
    for linea in (snapshot or "").split("\n"):
        if all(tok in linea for tok in PASTE_CHIP_TOKENS):
            return True
    return False


def _texto_corto_paste(sandbox_abs: str) -> str:
    """Mensaje que se TIPEA en el TUI tras adjuntar la imagen. Sin ningún `@`
    (ver el comentario de PASTE_TEXTO_CORTO_TPL)."""
    return PASTE_TEXTO_CORTO_TPL.format(sandbox=sandbox_abs)


def capturar_paste(
    argv: list,
    *,
    imagen_path: str,
    texto_corto: str,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    debug_dir: Optional[Path] = None,
    cols: int = PASTE_COLS,
    rows: int = PASTE_ROWS,
    history_lines: int = 8000,
    timeout_seg: float = 300.0,
    ini_marker: str = INI_MARKER,
    fin_marker: str = FIN_MARKER,
    read_size: int = 4096,
    verbose: bool = True,
    progress_seg: float = 15.0,
) -> CaptureResult:
    """Abre el TUI de agy, pega `imagen_path` desde el portapapeles, tipea
    `texto_corto` + Enter y espera la respuesta. Devuelve un `CaptureResult`.

    Secuencia (calibrada en el test `2026-08-03_170000_agy_tui_paste_media`):
      1) READY      — quiescencia 5 s (timeout 150 s: el cold start va a ~80 s).
      2) CLIPBOARD  — backup del TEXTO previo → file-drop (CF_HDROP) → Ctrl+V
                      INMEDIATO (la ventana en que el portapapeles del usuario
                      está pisado tiene que ser mínima: es un recurso global y
                      NO hay lock — hoy hay 1 solo slot agy por host).
      3) CHIP       — espera fija 5 s + drenaje 3 s/25 s (`exigir_bytes=False`:
                      el paste puede no repintar nada) y se busca la firma del
                      adjunto. Verificado el chip se RESTAURA el portapapeles
                      en el acto (no al final del job). Sin chip: Ctrl+U y un
                      reintento; si vuelve a fallar → estado PASTE_SIN_CHIP y
                      se cierra agy SIN ENVIAR NADA (cero cuota → reintentable).
      4) ENVÍO      — texto corto + 0,8 s + Enter.
      5) RESPUESTA  — marcador FIN + 5 s estables, o quiescencia de 30 s, o
                      `timeout_seg`. Con el detector de loop degenerado (v32)
                      en el tick de progreso: el stream del TUI es un PTY igual
                      que el de `-p`.
      6) CIERRE     — Ctrl+U + `/exit` → Ctrl+C ×2 → `_kill_arbol`. El barrido
                      de zombis del LS detached lo hace `main()` después (mismo
                      helper que los otros modos: acá duplicarlo desincronizaría
                      el contador `zombis_barridos`). La corrida real cerró
                      siempre por `/exit`.

    Estados posibles: OK_FIN | QUIESCENT_NO_MARKER | TIMEOUT | PROC_EXIT |
    ERROR_SPAWN | LOOP_DEGENERADO | PASTE_SIN_CHIP.

    Atributos dinámicos que cuelga en el resultado (mismo gesto que
    `imagen_cargada_ok`): `paste_chip_detectado`, `paste_reintentos`,
    `clipboard_restaurado`, `paste_modo`.
    """
    res = CaptureResult(estado="ERROR_SPAWN", fin_visto=False,
                        duracion_seg=0.0, bytes_leidos=0)
    res.paste_modo = True
    res.paste_chip_detectado = False
    res.paste_reintentos = 0
    res.clipboard_restaurado = False

    plain = pyte.Screen(cols, rows)
    plain_stream = pyte.Stream(plain)
    hist = pyte.HistoryScreen(cols, rows, history=history_lines, ratio=0.5)
    hist_stream = pyte.Stream(hist)

    raw_parts: list = []
    total_bytes = 0
    t0 = time.monotonic()

    def _log(msg: str) -> None:
        if verbose:
            sys.stderr.write(f"[agy-paste] {msg}\n")
            sys.stderr.flush()

    def _dump(nombre: str, contenido: str) -> None:
        """Escribe un artefacto forense del paste al bundle. Best-effort."""
        if debug_dir is None:
            return
        try:
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / nombre).write_text(contenido or "", encoding="utf-8")
        except Exception as e:
            sys.stderr.write(f"[agy-paste] WARN debug {nombre}: {e}\n")

    try:
        proc = winpty.PtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(rows, cols))
    except Exception as e:
        res.error = f"spawn: {type(e).__name__}: {e}"
        return res
    res.pid = getattr(proc, "pid", None)
    res.spawn_ok = True   # v37: el spawn anduvo (ver el campo en CaptureResult)
    _log(f"spawn OK pid={res.pid} argv={argv}")

    q: "queue.Queue" = queue.Queue()
    stop_flag = threading.Event()

    def _reader():
        while not stop_flag.is_set():
            try:
                ch = proc.read(read_size)
            except EOFError:
                q.put(None)
                return
            except Exception as e:
                q.put(("__EXC__", f"{type(e).__name__}: {e}"))
                return
            if ch:
                q.put(ch)
            else:
                if not proc.isalive():
                    q.put(None)
                    return
                time.sleep(0.02)

    reader = threading.Thread(target=_reader, name="conpty-paste-reader", daemon=True)
    reader.start()

    def _drenar(quiescent_seg: float, total_timeout: float, label: str,
                exigir_bytes: bool = True, marcador: Optional[str] = None,
                gracia: float = PASTE_RESP_GRACIA_SEG,
                detectar_loop: bool = False) -> tuple:
        """Drena hasta quiescencia (o marcador + gracia, o timeout).

        Devuelve (ok, motivo) con motivo ∈ {QUIESCENT, MARCADOR, TIMEOUT,
        PROC_EXIT, READER_EXC, LOOP}. `exigir_bytes=False` permite cerrar por
        quiescencia aunque no haya llegado un solo byte nuevo — necesario tras
        el paste, porque el TUI puede no repintar nada al adjuntar.
        """
        nonlocal total_bytes
        local_t0 = time.monotonic()
        last_byte = local_t0
        last_tick = local_t0
        bytes_at_entry = total_bytes
        marcador_at = None
        # ── Gate barato del chequeo de marcador (bump v35) ───────────────────
        # Port del gate que `capturar()` ya tiene (`if fin_marker[-6:] in chunk
        # or candidato_at is not None`). Sin él, en modo paste el chequeo caro
        # (`_screen_text` + `_history_text`) corre en CADA chunk de 4 KB, y
        # `_history_text` recorre el historial entero celda por celda en Python
        # puro: con `history=8000` su costo por llamada crece de ~24 ms a
        # ~460 ms a medida que satura. Como el TUI repinta el mensaje mientras
        # crece, el stream se amplifica 31-85× contra 1,02× de `-p` ⇒ cientos de
        # chunks × cientos de ms = decenas de segundos de CPU pura por página.
        #
        # El gate busca el marcador COMPLETO en el chunk con los escapes ANSI
        # removidos (`_ANSI_RE.sub`, NO `strip_ansi()`: ése además se queda con
        # lo que sigue al último `\r` de cada línea y podría COMER el marcador).
        #
        # Dos decisiones que parecen detalles y no lo son, ambas medidas sobre
        # los 54 bundles paste de la sesión 419 (55,7 MB de `console_raw`):
        #
        #  1. NO usar `marcador[-6:]` como hace `capturar()`. En `-p` da igual,
        #     pero acá "ION>>>" es también el sufijo de
        #     `<<<INICIO_TRANSCRIPCION>>>` ⇒ el gate abriría en el chunk ~30 de
        #     ~350 y el ahorro caería al 2% (11.657 llamadas contra 77).
        #  2. Des-ANSI-ar antes de buscar. El TUI parte el marcador con un
        #     escape en medio (visto: `<<<FIN_TRANSCRIPCIO` + `ESC[4h` +
        #     `N>>>`); pyte lo re-arma pero un `in` sobre el crudo no. Con el
        #     crudo, 1 de 54 bundles detectaba 3 chunks tarde; des-ANSI-ado,
        #     54/54 detectan en el MISMO chunk que sin gate.
        #
        # `gate_cola` arrastra los últimos chars del chunk anterior para no
        # perder un marcador partido entre dos lecturas de `read_size`; se usan
        # 256 (no len(marcador)-1) porque tras quitar los escapes la ventana
        # útil se achica. `gate_visto` es sticky (equivalente al
        # `or candidato_at is not None` de `capturar()`). El chequeo caro queda
        # IDÉNTICO — el gate sólo saltea llamadas donde el marcador no puede
        # estar. Costo del gate: 0,69 s de regex sobre los 55,7 MB.
        gate_token = marcador or ""
        gate_cola = ""
        gate_visto = False
        while True:
            now = time.monotonic()
            if now - local_t0 >= total_timeout:
                _log(f"{label}: TIMEOUT t={int(now-local_t0)}s "
                     f"bytes_nuevos={total_bytes-bytes_at_entry}")
                return False, "TIMEOUT"
            try:
                item = q.get(timeout=0.2)
                if item is None:
                    _log(f"{label}: PROC_EXIT")
                    return False, "PROC_EXIT"
                if isinstance(item, tuple) and item and item[0] == "__EXC__":
                    res.notas.append(f"read-exc: {item[1]}")
                    _log(f"{label}: reader exc {item[1]}")
                    return False, "READER_EXC"
                raw_parts.append(item)
                total_bytes += len(item)
                last_byte = now
                plain_stream.feed(item)
                hist_stream.feed(item)
                if marcador and marcador_at is None:
                    if not gate_visto:
                        if gate_token in _ANSI_RE.sub("", gate_cola + item):
                            gate_visto = True
                        # La cola se recalcula sobre `cola+item`, no sobre
                        # `item`: el PTY devuelve "hasta read_size", no
                        # exactamente read_size, así que con lecturas cortas
                        # tomar sólo la cola del chunk perdería el arrastre.
                        gate_cola = (gate_cola + item)[-PASTE_GATE_COLA_CHARS:]
                    if gate_visto and (marcador in _screen_text(plain)
                                       or marcador in _history_text(hist)):
                        marcador_at = now
                        _log(f"{label}: marcador visto a t={int(now-local_t0)}s")
            except queue.Empty:
                pass

            nuevos = total_bytes - bytes_at_entry
            if marcador_at is not None and (now - last_byte) >= gracia:
                _log(f"{label}: MARCADOR + {gracia}s estables (bytes_nuevos={nuevos})")
                return True, "MARCADOR"
            if (now - last_byte) >= quiescent_seg and (nuevos > 0 or not exigir_bytes):
                _log(f"{label}: QUIESCENT t={int(now-local_t0)}s bytes_nuevos={nuevos}")
                return True, "QUIESCENT"

            if (now - last_tick) >= progress_seg:
                last_tick = now
                _log(f"{label}: t={int(now-local_t0)}s bytes_nuevos={nuevos} "
                     f"alive={proc.isalive()}")
                # ── Corte temprano por LOOP degenerado (bump v31/v32) ────────
                # Mismo gate que `capturar()`: si YA vimos el marcador, el
                # cierre por MARCADOR gana en ≤ gracia y no hay nada que
                # ahorrar. El stream del TUI es un PTY igual que el de `-p`, así
                # que la firma (unidad corta repetida cientos de veces) aplica.
                if (detectar_loop and marcador_at is None
                        and total_bytes >= LOOP_MIN_CHARS):
                    info_loop = detectar_loop_degenerado(
                        strip_ansi(_cola_para_loop(raw_parts)))
                    if info_loop["detectado"]:
                        res.loop_detectado    = True
                        res.loop_unidad       = info_loop["unidad"]
                        res.loop_repeticiones = info_loop["repeticiones"]
                        _log(f"LOOP degenerado detectado a t={int(now-local_t0)}s "
                             f"(unidad={info_loop['unidad']!r} "
                             f"reps≥{info_loop['repeticiones']}) → cierro agy")
                        return False, "LOOP"

    # ── Estado del portapapeles: se pisa lo MÍNIMO y se restaura SIEMPRE ──
    backup_path = (debug_dir / "clipboard_previo.txt") if debug_dir is not None else None
    clipboard_pisado = False

    def _guardar_clipboard() -> None:
        """Backup del TEXTO previo del portapapeles. Best-effort y sólo texto:
        una imagen/lista de archivos ajena NO se puede preservar (queda
        documentado en notas/motor_agy.md §Bump v34)."""
        nonlocal backup_path
        rc, out, err = _ps_clipboard(["-Mode", "get"])
        txt = out.decode("utf-8", "replace") if rc == 0 else ""
        if rc != 0:
            _log(f"WARN clipboard get falló ({err}); sigo con backup vacío")
        if backup_path is None:
            # Sin debug_dir no hay dónde dejarlo: el .ps1 restaura desde archivo
            # (evita quoting), así que cae a un temporal del sistema. NUNCA al
            # sandbox: es el cwd de agy y tiene que quedar sólo con lo canónico.
            backup_path = Path(tempfile.gettempdir()) / (
                f"agy_clipboard_previo_{os.getpid()}.txt")
        try:
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path.write_text(txt, encoding="utf-8")
        except Exception as e:
            _log(f"WARN no pude escribir el backup del portapapeles: {e}")
            backup_path = None
        _log(f"portapapeles previo guardado ({len(txt)} chars)")

    def _restaurar_clipboard() -> None:
        nonlocal clipboard_pisado
        if not clipboard_pisado:
            return
        if backup_path is None or not backup_path.is_file():
            _log("WARN no hay backup del portapapeles para restaurar")
            return
        rc, _o, err = _ps_clipboard(["-Mode", "text", "-TextFile", str(backup_path)])
        if rc == 0:
            clipboard_pisado = False
            res.clipboard_restaurado = True
            _log("portapapeles restaurado")
        else:
            _log(f"WARN restauración del portapapeles falló: {err}")

    def _pegar_imagen() -> bool:
        """Pone el file-drop y manda Ctrl+V INMEDIATAMENTE. False si el .ps1
        falló (en ese caso el portapapeles NO quedó pisado)."""
        nonlocal clipboard_pisado
        rc, _o, err = _ps_clipboard(["-Mode", "file", "-Path", imagen_path])
        if rc != 0:
            _log(f"WARN clipboard file-drop falló: {err}")
            return False
        clipboard_pisado = True
        proc.write(PASTE_KEY_CTRL_V)
        _log("file-drop puesto + Ctrl+V enviado")
        return True

    motivo_resp = None
    try:
        # ── FASE 1: TUI listo ──────────────────────────────────────────────
        ok_ready, motivo_ready = _drenar(
            PASTE_READY_QUIESCENT_SEG, PASTE_READY_TIMEOUT_SEG, "READY")
        res.notas.append(f"ready:{motivo_ready}")
        snap_pre = _screen_text(plain)
        _dump("snapshot_pre_paste.txt", snap_pre)

        if motivo_ready in ("PROC_EXIT", "READER_EXC"):
            # agy murió antes de que pudiéramos pegar: nada que enviar, nada de
            # cuota consumida. El estado lo interpreta decidir_veredicto.
            res.estado = "PROC_EXIT"
        else:
            # Un TIMEOUT del READY no aborta: el TUI puede estar listo con el
            # spinner de auth escribiendo. El chip es el que decide de verdad.
            if not ok_ready:
                _log(f"TUI no quedó quiescente ({motivo_ready}) — sigo igual, "
                     f"el chip decide")

            # ── FASE 2+3: portapapeles + Ctrl+V + verificación del chip ─────
            _guardar_clipboard()
            chip_ok = False
            for intento in range(PASTE_MAX_REINTENTOS_CHIP + 1):
                if intento > 0:
                    res.paste_reintentos = intento
                    _log(f"chip ausente → Ctrl+U y reintento {intento}/"
                         f"{PASTE_MAX_REINTENTOS_CHIP}")
                    try:
                        proc.write(PASTE_KEY_CTRL_U)  # limpia lo que se haya pegado
                    except Exception as e:
                        _log(f"WARN Ctrl+U falló: {e}")
                    time.sleep(0.5)
                if not _pegar_imagen():
                    continue
                time.sleep(PASTE_ESPERA_POST_TECLA_SEG)
                _drenar(PASTE_CHIP_QUIESCENT_SEG, PASTE_CHIP_TIMEOUT_SEG,
                        "PASTE", exigir_bytes=False)
                snap_post = _screen_text(plain)
                # El reintento va a un archivo aparte: si se pisara, el bundle
                # perdería la evidencia de POR QUÉ falló el primer paste.
                _dump("snapshot_post_paste.txt" if intento == 0
                      else f"snapshot_post_paste_reintento{intento}.txt", snap_post)
                if _chip_media_presente(snap_post):
                    chip_ok = True
                    res.paste_chip_detectado = True
                    _log("chip de adjunto DETECTADO")
                    # Restaurar YA: el portapapeles del usuario no tiene por qué
                    # quedar pisado durante los minutos que dura la generación.
                    _restaurar_clipboard()
                    break
                _restaurar_clipboard()

            if not chip_ok:
                # Ni el paste ni el reintento adjuntaron nada (p. ej. el usuario
                # copió algo en la ventana crítica y pegamos SU texto). Se cierra
                # agy SIN ENVIAR NADA → cero cuota consumida → re-encolable,
                # coherente con el invariante de v19.
                res.estado = "PASTE_SIN_CHIP"
                res.transitorio_detectado = True
                res.transitorio_motivo = "paste_sin_chip"
                _log("chip AUSENTE tras los reintentos → cierro sin enviar")
            else:
                # ── FASE 4: envío del texto corto ──────────────────────────
                proc.write(texto_corto)
                time.sleep(PASTE_DELAY_ANTES_ENTER_SEG)
                proc.write("\r")
                _log(f"mensaje enviado ({len(texto_corto)} chars)")

                # ── FASE 5: respuesta ──────────────────────────────────────
                # Presupuesto: `timeout_seg` completo salvo que las fases
                # previas (cold start largo + reintento del chip) ya se hayan
                # comido el margen que PHP nos da antes del taskkill. Ver
                # PASTE_PRESUPUESTO_EXTRA_SEG.
                _restante = (timeout_seg + PASTE_PRESUPUESTO_EXTRA_SEG
                             - (time.monotonic() - t0) - PASTE_RESERVA_CIERRE_SEG)
                _resp_timeout = max(PASTE_RESP_TIMEOUT_MIN_SEG,
                                    min(timeout_seg, _restante))
                if _resp_timeout < timeout_seg:
                    _log(f"presupuesto recortado: la respuesta tiene "
                         f"{int(_resp_timeout)}s (de {int(timeout_seg)}s) para no "
                         f"comerse el margen del proc_timeout de PHP")
                ok_resp, motivo_resp = _drenar(
                    PASTE_RESP_QUIESCENT_SEG, _resp_timeout, "RESPUESTA",
                    marcador=fin_marker, gracia=PASTE_RESP_GRACIA_SEG,
                    detectar_loop=True)
                res.notas.append(f"respuesta:{motivo_resp}")
                if motivo_resp == "MARCADOR":
                    res.estado = "OK_FIN"
                elif motivo_resp == "QUIESCENT":
                    res.estado = "QUIESCENT_NO_MARKER"
                elif motivo_resp == "LOOP":
                    res.estado = "LOOP_DEGENERADO"
                elif motivo_resp in ("PROC_EXIT", "READER_EXC"):
                    res.estado = "PROC_EXIT"
                else:
                    res.estado = "TIMEOUT"
    except Exception as e:
        res.error = f"paste: {type(e).__name__}: {e}"
        res.notas.append(f"excepcion: {type(e).__name__}: {e}")
        # v37 — Fix 5: el estado NO puede quedarse en el "ERROR_SPAWN" con el
        # que nació `res`. El spawn ya había andado (llegamos hasta acá), así
        # que declarar un fallo de arranque es una afirmación falsa que además
        # dispara fail-fast SIN mirar si el modelo ya había generado y escrito
        # la transcripción en la .db. Caso testigo: job 70926 (2026-08-06,
        # edi 5742 p4) — `EOFError: Pty is closed`, ERROR terminal, página sin
        # transcribir. Sólo se pisa el estado si NINGUNA fase alcanzó a
        # asignarlo: un TIMEOUT/OK_FIN previo describe mejor la corrida que la
        # excepción del cierre. Ver notas/motor_agy.md §"Bump v37".
        if res.estado == "ERROR_SPAWN" and res.spawn_ok:
            res.estado = "EXCEPCION_FASE"
        sys.stderr.write(f"[agy-paste] EXCEPCIÓN: {e}\n{traceback.format_exc()}")
    finally:
        # El portapapeles es global: restaurar pase lo que pase.
        try:
            _restaurar_clipboard()
        except Exception as e:
            _log(f"WARN restauración final falló: {e}")

        # ── FASE 6: cierre en escalera (/exit → Ctrl+C ×2 → kill → barrido) ──
        via = []
        try:
            if proc.isalive():
                try:
                    # Ctrl+U ANTES del /exit, SIEMPRE. En el camino feliz la
                    # línea quedó vacía tras el envío y es un no-op; pero en
                    # PASTE_SIN_CHIP puede haber quedado tipeado lo que el
                    # paste metió en el input (p. ej. el texto que el usuario
                    # tenía copiado) y entonces el Enter del `/exit` LO
                    # ENVIARÍA como mensaje ⇒ consumo de cuota justo en el
                    # camino que se declara "cero cuota". Barato y salva eso.
                    proc.write(PASTE_KEY_CTRL_U)
                    time.sleep(0.3)
                    proc.write("/exit\r")
                    via.append("ctrl-u+/exit")
                    _drenar(PASTE_CIERRE_QUIESCENT_SEG, PASTE_CIERRE_TIMEOUT_SEG,
                            "CIERRE", exigir_bytes=False)
                except Exception as e:
                    _log(f"WARN write /exit falló: {e}")
            if proc.isalive():
                try:
                    proc.write("\x03")
                    time.sleep(0.6)
                    proc.write("\x03")
                    via.append("ctrl-c x2")
                    _drenar(PASTE_CIERRE_QUIESCENT_SEG, PASTE_CIERRE_TIMEOUT_SEG,
                            "CIERRE2", exigir_bytes=False)
                except Exception as e:
                    _log(f"WARN ctrl-c falló: {e}")
            if proc.isalive() and res.pid:
                _kill_arbol(res.pid)
                via.append("taskkill")
        except Exception as e:
            _log(f"WARN cierre: {e}")
        finally:
            stop_flag.set()
            try:
                res.exitstatus = proc.exitstatus
            except Exception:
                pass
            try:
                if proc.isalive():
                    proc.terminate(force=True)
                    via.append("terminate")
            except Exception:
                pass
        res.notas.append("cierre:" + ("+".join(via) or "ya_muerto"))

    res.duracion_seg = round(time.monotonic() - t0, 2)
    res.console_raw  = "".join(raw_parts)
    res.raw_stripped = strip_ansi(res.console_raw)
    res.bytes_leidos = total_bytes
    res.screen_snapshot = _screen_text(plain)
    res.history_text    = _history_text(hist)
    # El grid NO es la fuente del texto en paste (el TUI destruye los tags), pero
    # se puebla igual: alimenta el bundle forense y deja comparable el modo paste
    # con los otros dos.
    res.partial_from_ini    = extract_from_last_ini(res.history_text, ini_marker, fin_marker)
    res.extracted_history   = extract_between(res.history_text, ini_marker, fin_marker)
    res.extracted_screen    = extract_between(res.screen_snapshot, ini_marker, fin_marker)
    res.fin_visto = (fin_marker in res.history_text or fin_marker in res.screen_snapshot)

    # Medición definitiva del loop sobre el stream COMPLETO (el corte se decide
    # sobre la cola, que es una muestra). Sólo forense.
    if res.loop_detectado:
        _info_final = detectar_loop_degenerado(res.raw_stripped)
        if _info_final["detectado"]:
            res.loop_unidad       = _info_final["unidad"]
            res.loop_repeticiones = _info_final["repeticiones"]
            res.loop_chars        = _info_final["chars_loop"]

    _dump("snapshot_final.txt", res.screen_snapshot)
    return res


# ══════════════════════════════════════════════════════════════════════════════
# LECTURA DE LA `.db` DE CONVERSACIÓN — identidad, contenido y errores (v37)
# ══════════════════════════════════════════════════════════════════════════════
#
# REGLA DEL SUBSISTEMA (medida sobre los 2.638 bundles de `temp/agy_debug/`):
# **la pantalla es prueba de identidad; la `.db` NO lo es.** agy toca las `.db`
# de la carpeta al arrancar, así que el fallback por mtime puede elegir la
# conversación de OTRO job: 11 bundles con `.db` ajena, 2 de ellos con una
# transcripción completa y perfecta — de otra página. Por contenido son
# indistinguibles de una transcripción buena. Por eso TODO lo que sale de la
# `.db` (texto y señales derivadas) pasa primero por `_db_es_de_esta_corrida`.

# Marcas de estructura que el prompt de producción EXIGE (`#/C#` por columna,
# como mínimo) + los tags de incertidumbre. Sirven como evidencia positiva de
# "esto es la página", no como filtro de calidad.
_RE_TAGS_PAGINA = re.compile(
    r"#T#|#/T#|#ST#|#/ST#|#B#|#/B#|#SEC#|#/SEC#|#C#|#/C#|<i>|</i>|<ilegible>|<dudoso>")
_RE_FILA_TABLA = re.compile(r"^\s*\|.+\|\s*$")
# Razonamiento del modelo: llega en inglés, con títulos en negrita y primera
# persona. NO se usa para descartar texto — sólo para NO contar esas líneas como
# evidencia de página (ver `_marcas_pagina_en_segmento`).
_RE_TITULO_RAZONAMIENTO = re.compile(r"^\s*\*\*[^\n*]{3,90}\*\*\s*$")
_RE_LINEA_RAZONAMIENTO = re.compile(
    r"\b(I'm|I've|I'll|I am|I have|I will|I need|I noticed|I plan|I should|I can|"
    r"Let's|I also|my focus|the user|next step|now focusing|transcribing the)\b",
    re.I)


def _marcas_pagina_en_segmento(seg: str) -> int:
    """Cuánta evidencia hay de que el segmento contiene PÁGINA y no sólo
    razonamiento del modelo.

    Cuenta marcas de estructura del prompt (`#T#`, `#/C#`, `<i>`, `<ilegible>`,
    …) y filas de tabla markdown, **descartando las líneas que son razonamiento**
    (el modelo menciona sus propios tags mientras piensa). NO es un filtro
    anti-thinking: un segmento con thinking mezclado y página adentro puntúa
    alto y se persiste igual — lo único que este número decide es si hay ALGO de
    página, porque persistir 0 chars de página deja la edición en `Completado`
    con razonamiento y nadie vuelve a mirarla (`actualizarEstadoEdicion` cuenta
    páginas con entradas, no mira QA).

    Medido sobre los 10 bundles testigo del proyecto `agy_texto_descartado`:
      - transcripciones reales ......... 308–378 marcas
      - fragmento real + thinking ......   4 marcas (job58451: 867 ch de página)
      - razonamiento puro ..............   0 marcas (job62023 6.396 ch,
                                            job70227 436 ch, job68254 56 ch)
    """
    n = 0
    for linea in (seg or "").splitlines():
        s = linea.strip()
        if not s or s == FIN_MARKER or s == INI_MARKER:
            continue
        if _RE_TITULO_RAZONAMIENTO.match(s) or _RE_LINEA_RAZONAMIENTO.search(s):
            continue
        n += len(_RE_TAGS_PAGINA.findall(s))
        if _RE_FILA_TABLA.match(s):
            n += 1
    return n


def _hay_razonamiento_en_segmento(seg: str) -> bool:
    """¿El segmento trae razonamiento del modelo mezclado con la página?

    Señal: al menos un título en negrita suelto (`**Examining Column 1 Data**`),
    la forma en que el modelo encabeza cada bloque de thinking. Es una ETIQUETA
    para el QA (`thinking_mezclado`), NO un filtro: el texto se persiste igual.
    Medido en los bundles testigo — transcripciones limpias: 0 títulos
    (job69869/53672/53681/68768); mezcladas: 1 (job58451, job70227) y 16
    (job62023).
    """
    for linea in (seg or "").splitlines():
        if _RE_TITULO_RAZONAMIENTO.match(linea.strip()):
            return True
    return False


def _leer_una_columna_db(db_path: Optional[str], sql: str) -> Optional[list]:
    """`SELECT` best-effort en modo read-only con reintentos. None si no se pudo
    leer (agy puede tener la .db lockeada con WAL)."""
    if not db_path:
        return None
    for attempt in range(3):
        try:
            uri = "file:" + str(db_path).replace("\\", "/") + "?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=2)
            try:
                return con.execute(sql).fetchall()
            finally:
                con.close()
        except Exception as e:
            if attempt == 2:
                sys.stderr.write(f"[agy] WARN lectura .db ({sql[:40]}…): "
                                 f"{type(e).__name__}: {e}\n")
                return None
            time.sleep(0.3)
    return None


def _uuid_de_conversacion_db(db_path: Optional[str]) -> Optional[str]:
    """UUID de la conversación según la PROPIA `.db`: `trajectory_meta.cascade_id`.

    Verificado en los 8 bundles testigo legítimos: `cascade_id` coincide exacto
    con el `Created conversation <uuid>` que agy escribe en su `--log-file`. Es
    la contraparte independiente del nombre del archivo (que en producción ya es
    el uuid, pero en un bundle forense se copió como `conversation.db`).
    None si la tabla no existe o no se pudo leer.
    """
    rows = _leer_una_columna_db(db_path, "SELECT cascade_id FROM trajectory_meta")
    if not rows:
        return None
    for (cid,) in rows:
        if cid:
            return str(cid).strip().lower()
    return None


def _db_es_de_esta_corrida(db_path: Optional[str],
                           uuid_log: Optional[str]) -> Optional[bool]:
    """¿La `.db` elegida es la conversación de ESTA corrida? Tri-estado.

    True  → el uuid del `--log-file` de esta corrida coincide con el de la .db
            (`cascade_id`) o, si la tabla no es legible, con el nombre del
            archivo.
    False → coinciden con algo distinto ⇒ es la conversación de OTRO job.
    None  → **no verificable** (sin log, sin línea `Created conversation`, o
            ninguna de las dos vías dio un uuid). No es lo mismo que False, pero
            los callers tratan ambos igual para el texto: sin identidad probada
            no se persiste nada como vigente. Es la lección del v33 aplicada acá
            (tri-estado, no booleano: colapsarlo esconde el "no sé").
    """
    u = (uuid_log or "").strip().lower()
    if not u:
        return None
    cascade = _uuid_de_conversacion_db(db_path)
    if cascade:
        return cascade == u
    try:
        stem = Path(str(db_path)).stem.strip().lower()
    except Exception:
        return None
    if len(stem) == 36 and stem.count("-") == 4:
        return stem == u
    return None


# Sub-causas que agy deja en los steps de error de la .db. Medidas sobre los
# bundles del proyecto `agy_texto_descartado` (nada inventado): el string va tal
# cual aparece en `step_payload` / `error_details` de los steps 17 y 21.
#   (patrón_lower, etiqueta, reintentable)
# `reintentable=None` ⇒ lo decide el gate de generación del caller.
_FIRMAS_ERROR_DB = (
    ("individual quota reached",            "cuota_individual",        False),
    ("eligibility check failed",            "eligibility_429",         True),
    ("unauthenticated",                     "401_unauthenticated",     True),
    ("code 401",                            "401_unauthenticated",     True),
    ("invalid_argument (code 400)",         "invalid_argument_400",    False),
    ("request contains an invalid argument", "invalid_argument_400",   False),
    ("media has no inline data",            "media_sin_inline_data",   False),
    ("unsupported mime type",               "mime_no_soportado",       False),
    ("user denied permission to run command", "tool_denegada_headless", False),
    ("model produced invalid output",       "model_output_invalido",   None),
    ("permission_denied",                   "permission_denied",       False),
    ("model unreachable",                   "model_unreachable",       True),
)
# Sólo texto imprimible: los payloads son protobuf crudo con los strings adentro.
_RE_ASCII_LEGIBLE = re.compile(rb"[\x20-\x7e]{6,}")


def _diagnostico_errores_db(db_path: Optional[str]) -> dict:
    """Etiqueta la causa raíz que agy dejó en la `.db` (v37).

    Hasta v36 nadie leía esto: el `UNAUTHENTICATED 401` y el `eligibility_429`
    del modo `paste` vivían en la .db y el veredicto salía siempre como el
    genérico `db_sin_texto_paste`, perdiendo el freno corto de cuenta y el
    desambigüe pre/post generación del v33.

    Devuelve {"etiqueta": str, "reintentable": bool|None, "detalle": str}.
    Etiqueta "" = no se encontró ninguna firma conocida.
    """
    out = {"etiqueta": "", "reintentable": None, "detalle": ""}
    rows = _leer_una_columna_db(
        db_path,
        "SELECT idx, step_type, step_payload, error_details FROM steps "
        "WHERE step_type IN (17, 21, 23) ORDER BY idx")
    if not rows:
        return out
    for idx, step_type, payload, error_details in rows:
        for blob in (error_details, payload):
            if not blob:
                continue
            if isinstance(blob, str):
                blob = blob.encode("utf-8", "replace")
            try:
                txt = b" ".join(_RE_ASCII_LEGIBLE.findall(bytes(blob))).decode(
                    "ascii", "replace")
            except Exception:
                continue
            bajo = txt.lower()
            for needle, etiqueta, reintentable in _FIRMAS_ERROR_DB:
                if needle in bajo:
                    pos = bajo.find(needle)
                    out["etiqueta"]     = etiqueta
                    out["reintentable"] = reintentable
                    out["detalle"]      = txt[max(0, pos - 40): pos + 200].strip()[:300]
                    return out
    return out


def _extraer_transcripcion_db(db_path: Optional[str]) -> dict:
    """Mejor candidato a transcripción dentro de la `.db` de conversación.

    v34 lo introdujo para el modo `paste` (donde la pantalla NO es fuente
    válida: el TUI renderiza el markdown y DESTRUYE los tags `<ilegible>`/
    `<dudoso>` — medido 271 vs 0). v37 lo generaliza a los 3 modos como camino
    de RESCATE y arregla dos defectos:

      1) **Leía sólo el ÚLTIMO `step_type=15`.** Cuando el error deja un step 15
         vacío al final (238 bytes en el job 70839), el par INICIO/FIN quedaba
         en un step anterior y se tiraba la transcripción entera. Ahora recorre
         en REVERSA y se queda con el primer candidato que tenga página adentro.
      2) **Exigía el par INICIO…FIN completo.** Un texto truncado se descartaba
         entero, cuando en `-p`/`-i` ese mismo caso se rescata como `ini_only` y
         PHP lo marca con `qaDetectarSinFin()`. Ahora el parcial vuelve como
         `fuente="db_parcial"` y PHP hace lo suyo.

    Devuelve SIEMPRE un dict (nunca None), porque el candidato viaja al raw
    aunque se rechace — el texto crudo que produjo agy no se tira nunca:
      - `texto`     : segmento aceptado (con ambos marcadores si los tenía), o None.
      - `fuente`    : "db" | "db_parcial" | "".
      - `candidato` : mejor segmento hallado, PASE O NO las guardas (para el raw).
      - `rechazo`   : motivo por el que no se aceptó ("" si se aceptó o no había nada).
      - `marcas`    : marcas de página del segmento elegido (ver
                      `_marcas_pagina_en_segmento`).
      - `step_idx`  : índice del step del que salió.

    OJO: la identidad de la .db NO se chequea acá (este helper no conoce el
    logfile). La gatea el caller con `_db_es_de_esta_corrida` — sin eso, esto
    devuelve felizmente la transcripción de otra página.
    """
    out = {"texto": None, "fuente": "", "candidato": None, "rechazo": "",
           "marcas": 0, "step_idx": None, "razonamiento": False}
    rows = _leer_una_columna_db(
        db_path,
        "SELECT idx, step_payload FROM steps WHERE step_type = 15 ORDER BY idx")
    if rows is None:
        out["rechazo"] = "db_ilegible"
        return out
    if not rows:
        out["rechazo"] = "sin_step_15"
        return out

    mejor_candidato = None       # (len, seg, idx, fuente, marcas)
    for idx, payload in reversed(rows):
        if payload is None:
            continue
        if isinstance(payload, str):
            payload = payload.encode("utf-8", "replace")
        try:
            txt = bytes(payload).decode("utf-8", errors="replace")
        except Exception:
            continue
        i = txt.find(INI_MARKER)
        if i == -1:
            continue
        j = txt.find(FIN_MARKER, i + len(INI_MARKER))
        if j != -1:
            seg    = txt[i: j + len(FIN_MARKER)]
            fuente = "db"
        else:
            # Parcial: cortar en el SEGUNDO INICIO si lo hay. El payload trae el
            # texto dos veces (medido en v34), así que sin este corte el rescate
            # de un truncado devolvería el arranque duplicado.
            k   = txt.find(INI_MARKER, i + len(INI_MARKER))
            seg = txt[i: k] if k != -1 else txt[i:]
            fuente = "db_parcial"
        seg = seg.strip()
        if len(seg) < MIN_CONTENT_LEN:
            continue
        marcas = _marcas_pagina_en_segmento(seg)
        if mejor_candidato is None or len(seg) > mejor_candidato[0]:
            mejor_candidato = (len(seg), seg, idx, fuente, marcas)
        if marcas >= 1:
            out.update({"texto": seg, "fuente": fuente, "candidato": seg,
                        "rechazo": "", "marcas": marcas, "step_idx": int(idx),
                        "razonamiento": _hay_razonamiento_en_segmento(seg)})
            return out

    if mejor_candidato is None:
        out["rechazo"] = "sin_par_inicio"
        return out
    # Hubo segmento pero sin una sola marca de página: es razonamiento del modelo
    # citando los marcadores. Viaja al raw, no a `entradas`.
    out.update({"candidato": mejor_candidato[1], "marcas": mejor_candidato[4],
                "step_idx": int(mejor_candidato[2]),
                "razonamiento": _hay_razonamiento_en_segmento(mejor_candidato[1]),
                "rechazo": "sin_contenido_de_pagina"})
    return out


def resolver_texto_db(db_path: Optional[str],
                      uuid_log: Optional[str],
                      hubo_generacion: Optional[bool]) -> dict:
    """Punto ÚNICO de decisión "¿este texto de la .db se puede persistir?" (v37).

    Junta el extractor con las dos guardas de AUTORÍA del §4 del handoff, para
    que `main()` y el harness de tests corran exactamente el mismo código (si el
    test replicara el gateo, validaría su propia copia, no el motor).

      guarda 1 — generación propia : `streamGenerateContent ≥ 1` en el
                 `--log-file` de ESTA corrida (`hubo_generacion`). Tri-estado.
      guarda 2 — identidad         : el uuid del log == el de la .db.
      (guarda 3 — que el segmento tenga página adentro — vive en el extractor,
       vía `_marcas_pagina_en_segmento`.)

    Las tres en conjunción. "No verificable" (None) pesa igual que "no" para
    persistir: sin poder probar de quién es el texto, no se escribe como
    transcripción vigente. El candidato vuelve igual en `candidato` para que el
    raw lo conserve.

    Devuelve dict: texto, fuente, candidato, rechazo, marcas, step_idx, identidad.
    """
    try:
        out = _extraer_transcripcion_db(db_path)
    except Exception as e:
        sys.stderr.write(f"[agy] WARN _extraer_transcripcion_db: {type(e).__name__}: {e}\n")
        out = {"texto": None, "fuente": "", "candidato": None,
               "rechazo": "extractor_excepcion", "marcas": 0, "step_idx": None}
    identidad = _db_es_de_esta_corrida(db_path, uuid_log)
    out["identidad"] = identidad
    if out.get("texto"):
        if identidad is not True:
            out["rechazo"] = ("db_de_otra_conversacion" if identidad is False
                              else "identidad_no_verificable")
            out["texto"], out["fuente"] = None, ""
        elif hubo_generacion is not True:
            out["rechazo"] = ("sin_generacion_propia" if hubo_generacion is False
                              else "generacion_no_verificable")
            out["texto"], out["fuente"] = None, ""
    return out


def _png_en_gen_metadata(db_path: Optional[str]) -> Optional[bool]:
    """¿Hay un PNG (magic `\\x89PNG`) en algún blob de `gen_metadata` (v34)?

    Es la señal FUERTE de que la imagen viajó al modelo: es el binario que el
    backend recibió, no el rastro del intento de la tool. Devuelve True/False,
    o None si la tabla no existe / la .db no es legible (formato viejo →
    "no verificable", y el caller cae al fallback).

    Se itera fila por fila (los blobs pueden ser de 3 MB): nada de `fetchall()`.
    """
    if not db_path:
        return None
    try:
        uri = "file:" + str(db_path).replace("\\", "/") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2)
    except Exception as e:
        sys.stderr.write(f"[agy] WARN _png_en_gen_metadata (open): {e}\n")
        return None
    try:
        cur = con.execute("SELECT data FROM gen_metadata")
        for (d,) in cur:
            if d is None:
                continue
            if isinstance(d, str):
                d = d.encode("utf-8", "replace")
            if b"\x89PNG" in bytes(d):
                return True
        return False
    except Exception as e:
        # Tabla ausente (formato viejo de .db) o lectura fallida → no verificable.
        sys.stderr.write(f"[agy] WARN _png_en_gen_metadata: {e}\n")
        return None
    finally:
        try:
            con.close()
        except Exception:
            pass


def _media_en_log(agy_log_path) -> Optional[int]:
    """Máximo `media=N` de las líneas `Forwarding user message …
    (items=N, media=M)` del `--log-file` (bump v34).

    M ≥ 1 ⇒ el mensaje del usuario viajó CON medios adjuntos (el camino del
    paste). None si el log no existe / no es legible / no tiene la línea.
    """
    try:
        if not agy_log_path or not Path(agy_log_path).is_file():
            return None
        txt = Path(agy_log_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    vals = [int(m) for _i, m in _RE_FORWARDING_MEDIA.findall(txt)]
    return max(vals) if vals else None


def _resolver_imagen_cargada(png_gen_metadata: Optional[bool],
                             tripleta_steps: bool,
                             media_en_log: Optional[int]) -> Optional[bool]:
    """Señal tri-estado "agy adjuntó la imagen al contexto multimodal" (v34).

    Reemplaza la señal de v28 (la tripleta `imagen.jpg`+`image/`+
    `tempmediaStorage` en un mismo blob de `steps`), que en `paste` da False y
    disparaba el falso positivo del QA grave NO_CARGO_IMAGEN.

    Medición sobre las 3 .db testigo (paste sano, `-p` sano 1.1.9, `-p` roto
    1.1.10 del #735):
                        tripleta   PNG gen_metadata   media= del log
      paste sano          NO            SÍ (1)             1
      `-p` sano           SÍ            SÍ (1)             0
      `-p` roto           SÍ            NO (0)             0

    ⇒ La tripleta NO PUEDE ir en la disyunción: en el caso roto describe el
    INTENTO del tool (`view_file` corrió y stageó el archivo) pero la imagen
    NO viajó (`inline_data` de longitud 0) — daría True donde la respuesta
    correcta es False. La semántica queda:
      señal FUERTE  = PNG en `gen_metadata`  OR  media ≥ 1 en el `--log-file`
      tripleta vieja= sólo FALLBACK cuando `gen_metadata` no existe o no es
                      legible (.db de formato viejo) — ahí es la única
                      evidencia disponible y vale más que un None.
    Nota: la señal del PNG habría detectado el #735 el primer día (el `-p` roto
    da 0 PNG en `gen_metadata` mientras la tripleta decía que todo bien).
    """
    if (media_en_log or 0) >= 1:
        return True
    if png_gen_metadata is True:
        return True
    if png_gen_metadata is False:
        return False
    # gen_metadata no verificable → última evidencia disponible.
    return True if tripleta_steps else None


# ============================================================
# /usage SCREEN — captura interactiva + parser
# ============================================================
#
# Branch del --modo=usage: lanza `agy` SIN args (abre TUI), espera quiescencia
# inicial (READY), escribe "/usage\r" al stdin del PTY, espera quiescencia
# post-comando (USAGE), parsea el bloque GEMINI MODELS del snapshot final y
# devuelve un dict con weekly_pct_usado, weekly_reset_seg, h5_pct_usado,
# h5_reset_seg, account_email, plan_tier + el raw_screen.
#
# Por qué reusar el motor de captura: capturar() está cableado a marcadores
# INICIO/FIN y a la lógica fin_grace. Para /usage necesitamos un pattern
# distinto (2 quiescencias + 1 write). Por eso esto es una función paralela
# simplificada, embebida acá (no duplica el lector ni el grid pyte; lo único
# duplicado es el loop de drenado por quiescencia). Validado en
# `scratchpad/probe_agy_usage/probe_interactive.py` (corrida real con
# `purusit@gmail.com`, snapshot completo de GEMINI MODELS + CLAUDE).

# Quiescencias / timeouts del modo usage. Cold start de agy puede ir hasta ~80s
# la primera vez del proceso (auth + experiments); en caliente es ~10s para que
# el TUI esté listo. El round del /usage en sí ronda ~6s.
USAGE_READY_QUIESCENT_SEG = 5.0
USAGE_READY_TIMEOUT_SEG   = 90.0
USAGE_POST_QUIESCENT_SEG  = 5.0
USAGE_POST_TIMEOUT_SEG    = 45.0

# Regex del header "35% remaining · Refreshes in 125h 28m" (para reset_seg) y
# del bar `[███░░░...] 5.65%` (para el pct con decimales). Historia:
#   2026-06-27: parseo del header sólo. Granularidad 1% alcanzaba para el
#     umbral de pausa (90%).
#   v20 (2026-07-08): el header redondea a entero y perdemos precisión visible
#     ("6% remaining" cuando el bar dice 5.65%). Ahora el bar manda para el
#     pct (2 decimales), el header sigue mandando para reset_seg + fallback si
#     el bar no matcheara. Ver notas/motor_agy.md §"Bump v20".
#   v21 (2026-07-09): al 100% weekly usado, el TUI OMITE el prefijo
#     "X% remaining · " y emite sólo "Refreshes in 95h 23m" en la línea del
#     header → el regex previo fallaba y `weekly_reset_seg` quedaba None (DB
#     grababa NULL). Hacemos opcional el prefijo `(\d+)% remaining · ` y el
#     fallback de pct por header queda condicionado a que `rem` haya matcheado.
#     Ver notas/motor_agy.md §"Bump v21".
_USAGE_REMAINING_RE = re.compile(
    r"(?:(?P<rem>\d+)%\s+remaining\s*·\s*)?Refreshes\s+in\s+"
    r"(?:(?P<h>\d+)h\s*)?(?:(?P<m>\d+)m)?", re.IGNORECASE)
_USAGE_BAR_RE = re.compile(
    r"\]\s+(?P<rem>\d+(?:\.\d+)?)\s*%", re.IGNORECASE)
_USAGE_QUOTA_AVAILABLE_RE = re.compile(r"\bQuota\s+available\b", re.IGNORECASE)
# El bloque GEMINI MODELS está delimitado por su header. El siguiente bloque
# ("CLAUDE AND GPT MODELS") corta el alcance del parser para no mezclarlos.
_USAGE_GEMINI_BLOCK_RE = re.compile(
    r"GEMINI\s+MODELS\s*\n(.*?)(?=\n\s*[A-Z][A-Z ]{3,}\s*MODELS\b|\Z)",
    re.IGNORECASE | re.DOTALL)
_USAGE_WEEKLY_LABEL_RE = re.compile(r"Weekly\s+Limit", re.IGNORECASE)
_USAGE_5H_LABEL_RE     = re.compile(r"Five\s+Hour\s+Limit", re.IGNORECASE)
_USAGE_ACCOUNT_RE      = re.compile(
    r"Account:\s*(?P<email>[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})",
    re.IGNORECASE)
# Plan tier viene en el corona del TUI, junto al email entre paréntesis.
# Ej: "purusit@gmail.com (Google AI Pro)".
_USAGE_PLAN_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\s*\(([^()\n]+)\)")


def _parse_usage_segmento(seg_text: str) -> tuple:
    """Parsea un segmento (Weekly o Five Hour) y devuelve (pct_usado, reset_seg)
    o (None, None) si no se pudo parsear. Granularidad: 0.01% (del bar); el
    header redondea a entero. `reset_seg` en segundos (=0 cuando dice
    "Quota available")."""
    if not seg_text:
        return (None, None)
    if _USAGE_QUOTA_AVAILABLE_RE.search(seg_text):
        return (0.0, 0)  # 100% remaining → 0% usado, reset_seg = 0

    # pct: bar (con decimales) prevalece; header (entero) es fallback.
    # v21: el header ahora puede matchear SIN el grupo `rem` (caso 100% weekly:
    # el TUI emite sólo "Refreshes in 95h 23m"). En ese caso el header sigue
    # sirviendo para `reset_seg` abajo, pero NO para `pct`.
    bar_m = _USAGE_BAR_RE.search(seg_text)
    hdr_m = _USAGE_REMAINING_RE.search(seg_text)
    hdr_rem_str = hdr_m.group("rem") if hdr_m is not None else None
    if bar_m is not None:
        rem = float(bar_m.group("rem"))
    elif hdr_rem_str is not None:
        rem = float(hdr_rem_str)
    else:
        return (None, None)
    pct_usado = max(0.0, min(100.0, 100.0 - rem))

    # reset_seg: sólo del header. Si el header no matcheó, dejamos None
    # (el UPSERT PHP lo persiste como weekly_reset_at=NULL).
    reset_seg = None
    if hdr_m is not None:
        horas = int(hdr_m.group("h") or 0)
        mins  = int(hdr_m.group("m") or 0)
        reset_seg = horas * 3600 + mins * 60
    return (pct_usado, reset_seg)


def parsear_usage_screen(snapshot: str) -> dict:
    """Parsea el snapshot del TUI tras `/usage` y devuelve un dict con los
    campos de cuota del grupo GEMINI MODELS (Flash + Pro). El grupo CLAUDE se
    ignora (prensa no lo consume). Devuelve campos None cuando no se puede
    parsear: el upsert PHP los persiste tal cual y el monitor los muestra
    "(sin dato)"."""
    out = {
        "account_email":    None,
        "plan_tier":        None,
        "weekly_pct_usado": None,
        "weekly_reset_seg": None,
        "h5_pct_usado":     None,
        "h5_reset_seg":     None,
        "gemini_block_found": False,
        "parser_notes":     [],
    }
    if not snapshot:
        out["parser_notes"].append("snapshot_vacio")
        return out

    em = _USAGE_ACCOUNT_RE.search(snapshot)
    if em:
        out["account_email"] = em.group("email")
    pm = _USAGE_PLAN_RE.search(snapshot)
    if pm:
        out["plan_tier"] = pm.group(1).strip()

    bm = _USAGE_GEMINI_BLOCK_RE.search(snapshot)
    if not bm:
        out["parser_notes"].append("bloque_gemini_no_encontrado")
        return out
    out["gemini_block_found"] = True
    bloque = bm.group(1)

    # Cortar el bloque en sub-bloques por las etiquetas Weekly / Five Hour.
    # La estructura real (probe 2026-06-27):
    #   Models within this group: ...
    #   Weekly Limit
    #     [bar] 34.59%
    #     35% remaining · Refreshes in 125h 28m
    #   Five Hour Limit
    #     [bar] 56.86%
    #     57% remaining · Refreshes in 1h 55m
    w_m = _USAGE_WEEKLY_LABEL_RE.search(bloque)
    h_m = _USAGE_5H_LABEL_RE.search(bloque)

    if w_m:
        # Segmento weekly = desde "Weekly Limit" hasta "Five Hour Limit" (o fin).
        ini = w_m.end()
        fin = h_m.start() if (h_m and h_m.start() > ini) else len(bloque)
        weekly_pct, weekly_reset = _parse_usage_segmento(bloque[ini:fin])
        out["weekly_pct_usado"] = weekly_pct
        out["weekly_reset_seg"] = weekly_reset
        if weekly_pct is None:
            out["parser_notes"].append("weekly_segmento_no_parseado")
    else:
        out["parser_notes"].append("weekly_label_no_encontrado")

    if h_m:
        ini = h_m.end()
        # El segmento 5h va hasta el fin del bloque GEMINI MODELS.
        h5_pct, h5_reset = _parse_usage_segmento(bloque[ini:])
        out["h5_pct_usado"] = h5_pct
        out["h5_reset_seg"] = h5_reset
        if h5_pct is None:
            out["parser_notes"].append("h5_segmento_no_parseado")
    else:
        out["parser_notes"].append("h5_label_no_encontrado")

    return out


def capturar_slash_command(
    argv: list,
    *,
    slash_cmd: str,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    cols: int = 220,
    rows: int = 80,
    ready_quiescent_seg: float = USAGE_READY_QUIESCENT_SEG,
    ready_timeout_seg:   float = USAGE_READY_TIMEOUT_SEG,
    post_quiescent_seg:  float = USAGE_POST_QUIESCENT_SEG,
    post_timeout_seg:    float = USAGE_POST_TIMEOUT_SEG,
    read_size: int = 4096,
    verbose: bool = True,
) -> dict:
    """Lanza `argv` bajo ConPTY; espera quiescencia (TUI listo) y manda
    `slash_cmd` + Enter al stdin del PTY; espera quiescencia post-comando;
    devuelve el snapshot pyte final + history + raw + métricas.

    Patrón distinto al de `capturar()` (sin marcadores INICIO/FIN): cierra
    SIEMPRE por quiescencia (`bytes congelados N segundos`). Sin
    MIN_BYTES_PLAUSIBLES — el TUI ya pintó el corona antes de la primera
    quiescencia, así que cualquier cantidad de bytes nuevos vale.

    Devuelve dict con: ok, snapshot, history, raw, bytes_total, exitstatus,
    pid, estado, error, duracion_seg, ready_ok (bool), post_ok (bool).
    """
    plain = pyte.Screen(cols, rows)
    plain_stream = pyte.Stream(plain)
    hist = pyte.HistoryScreen(cols, rows, history=4000, ratio=0.5)
    hist_stream = pyte.Stream(hist)

    raw_parts: list = []
    total_bytes = 0
    t0 = time.monotonic()
    estado = "ERROR_SPAWN"
    error: Optional[str] = None
    pid: Optional[int] = None
    exitstatus: Optional[int] = None
    ready_ok = False
    post_ok = False

    try:
        proc = winpty.PtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(rows, cols))
    except Exception as e:
        error = f"spawn: {type(e).__name__}: {e}"
        return {
            "ok": False, "snapshot": "", "history": "", "raw": "",
            "bytes_total": 0, "exitstatus": None, "pid": None,
            "estado": estado, "error": error,
            "duracion_seg": round(time.monotonic() - t0, 2),
            "ready_ok": False, "post_ok": False,
        }
    pid = getattr(proc, "pid", None)
    if verbose:
        sys.stderr.write(f"[agy-usage] spawn OK pid={pid} argv={argv}\n")

    q: "queue.Queue" = queue.Queue()
    stop_flag = threading.Event()

    def _reader():
        while not stop_flag.is_set():
            try:
                ch = proc.read(read_size)
            except EOFError:
                q.put(None)
                return
            except Exception as e:
                q.put(("__EXC__", f"{type(e).__name__}: {e}"))
                return
            if ch:
                q.put(ch)
            else:
                if not proc.isalive():
                    q.put(None)
                    return
                time.sleep(0.02)

    reader = threading.Thread(target=_reader, name="conpty-usage-reader", daemon=True)
    reader.start()

    def _drenar(quiescent_seg: float, total_timeout: float, label: str) -> bool:
        """Drena el queue hasta que pasen N segundos sin nuevos bytes (o
        total_timeout). Devuelve True si quiescente, False si TIMEOUT o PROC_EXIT."""
        nonlocal total_bytes
        local_t0 = time.monotonic()
        last_byte = local_t0
        bytes_at_entry = total_bytes
        last_tick = local_t0
        while True:
            now = time.monotonic()
            if now - local_t0 >= total_timeout:
                if verbose:
                    sys.stderr.write(
                        f"[agy-usage] {label}: TIMEOUT t={int(now-local_t0)}s "
                        f"bytes_nuevos={total_bytes-bytes_at_entry}\n")
                return False
            try:
                item = q.get(timeout=0.2)
                if item is None:
                    if verbose:
                        sys.stderr.write(f"[agy-usage] {label}: PROC_EXIT\n")
                    return False
                elif isinstance(item, tuple) and item and item[0] == "__EXC__":
                    if verbose:
                        sys.stderr.write(f"[agy-usage] {label}: reader exc {item[1]}\n")
                    return False
                else:
                    raw_parts.append(item)
                    total_bytes += len(item)
                    last_byte = now
                    plain_stream.feed(item)
                    hist_stream.feed(item)
            except queue.Empty:
                pass
            if ((now - last_byte) >= quiescent_seg
                    and (total_bytes - bytes_at_entry) > 0):
                if verbose:
                    sys.stderr.write(
                        f"[agy-usage] {label}: QUIESCENT t={int(now-local_t0)}s "
                        f"bytes_nuevos={total_bytes-bytes_at_entry}\n")
                return True
            if verbose and int(now - last_tick) >= 10:
                last_tick = now
                sys.stderr.write(
                    f"[agy-usage] {label}: t={int(now-local_t0)}s "
                    f"bytes_nuevos={total_bytes-bytes_at_entry} alive={proc.isalive()}\n")

    try:
        # FASE 1: esperar TUI listo
        ready_ok = _drenar(ready_quiescent_seg, ready_timeout_seg, "READY")
        if not ready_ok:
            estado = "READY_TIMEOUT"
            error = "tui_no_listo_dentro_de_timeout"
        else:
            # FASE 2: mandar slash command + Enter
            try:
                proc.write(slash_cmd + "\r")
                if verbose:
                    sys.stderr.write(f"[agy-usage] write OK: {slash_cmd!r}\n")
            except Exception as e:
                estado = "WRITE_FAIL"
                error = f"write_fail: {type(e).__name__}: {e}"
                ready_ok = False  # tratado como fallido
            if estado != "WRITE_FAIL":
                # FASE 3: esperar respuesta
                post_ok = _drenar(post_quiescent_seg, post_timeout_seg, "USAGE")
                if not post_ok:
                    estado = "POST_TIMEOUT"
                    error = "respuesta_no_quiescent_dentro_de_timeout"
                else:
                    estado = "OK_QUIESCENT"
    finally:
        stop_flag.set()
        try:
            exitstatus = proc.exitstatus
        except Exception:
            pass
        try:
            if proc.isalive():
                proc.terminate(force=True)
        except Exception:
            pass

    snapshot = _screen_text(plain)
    history  = _history_text(hist)
    raw      = "".join(raw_parts)
    duracion = round(time.monotonic() - t0, 2)

    return {
        "ok": (estado == "OK_QUIESCENT") and post_ok,
        "snapshot": snapshot,
        "history": history,
        "raw": raw,
        "bytes_total": total_bytes,
        "exitstatus": exitstatus,
        "pid": pid,
        "estado": estado,
        "error": error,
        "duracion_seg": duracion,
        "ready_ok": ready_ok,
        "post_ok": post_ok,
    }


def main_usage(args, t0_total: float) -> int:
    """Branch del --modo=usage. Espejo simplificado de main(): no toca scratch,
    no prepara sandbox, no setea modelo global, no levanta agy con prompt; sólo
    abre la TUI, manda /usage, parsea y escribe el JSON.

    El sandbox tiene que existir (se pasa a Popen como cwd; sin él el ConPTY
    falla con "directorio no válido"). Pero NO se escribe nada adentro."""
    salida_json = Path(args.salida_json).resolve()
    sandbox_dir = Path(args.sandbox_dir).resolve()
    home_dir = Path(args.home_dir).resolve() if args.home_dir else None
    # Bundle forense (mismo modelo que main(): dentro del workdir efímero).
    debug_dir = salida_json.parent / "debug"

    # Pre-flight mínimo: el sandbox debe existir (cwd del PTY).
    if not sandbox_dir.is_dir():
        _escribir_salida_temprana(salida_json, {
            "ok": False, "engine": "agy", "modo": "usage",
            "veredicto": "ERROR",
            "error": f"sandbox_dir_no_existe: {sandbox_dir}",
            "account_email": None, "plan_tier": None,
            "weekly_pct_usado": None, "weekly_reset_seg": None,
            "h5_pct_usado": None, "h5_reset_seg": None,
            "raw_screen": "",
            "duracion_seg": round(time.time() - t0_total, 2),
            "fecha_iso": datetime.now().isoformat(timespec='seconds'),
        })
        return 3

    # Env: si --home-dir está, pisamos USERPROFILE/HOME (mismo gesto que
    # main() — los keyrings/auth no se afectan porque viven en el Credential
    # Manager por SID; ver motor_agy.md §"Multi-slot same-cuenta"). NO
    # tocamos APPDATA/LOCALAPPDATA (agy.exe no los usa, validado por probe).
    env = None
    if home_dir is not None:
        env = dict(os.environ)
        env["USERPROFILE"] = str(home_dir)
        env["HOME"] = str(home_dir)

    # NO llamamos _limpiar_estado_agy NI _stagear_en_scratch. Justificación:
    # (1) /usage no necesita @imagen.jpg ni @prompt.md; vaciar el scratch lo
    # único que haría es destruir el estado de la próxima transcripción real
    # (vendría con _limpiar_estado_agy igual, pero gratis); (2) NO escribimos
    # nada al sandbox tampoco (preparar_sandbox no se llama). Después de esta
    # corrida el scratch queda con lo que sea que hubiera + algún subdir nuevo
    # de la conversación efímera que abrió agy bajo el SID actual. Eso es OK:
    # la próxima transcripción seguirá su pipeline y lo limpiará.

    argv_usage = [args.agy_bin]  # SIN args = abre TUI (con autenticación normal)
    cap = capturar_slash_command(
        argv_usage,
        slash_cmd="/usage",
        cwd=str(sandbox_dir),
        env=env,
        cols=220, rows=80,
        ready_quiescent_seg=USAGE_READY_QUIESCENT_SEG,
        ready_timeout_seg=float(args.timeout),
        post_quiescent_seg=USAGE_POST_QUIESCENT_SEG,
        post_timeout_seg=USAGE_POST_TIMEOUT_SEG,
        verbose=True,
    )

    # Si el subprocess de agy NO cerró solo, agy quedó vivo (TUI esperando otro
    # turno). Por las dudas barrer su árbol — agy interactivo a veces deja un
    # language server detached.
    if cap.get("pid"):
        try:
            _kill_arbol(cap["pid"])
        except Exception as _e:
            sys.stderr.write(f"[agy-usage] WARN _kill_arbol: {_e}\n")
        time.sleep(0.3)
        try:
            _barrido_zombis(_pids_agy_actuales(), t0_total)
        except Exception as _e:
            sys.stderr.write(f"[agy-usage] WARN _barrido_zombis: {_e}\n")

    parsed = parsear_usage_screen(cap.get("snapshot", ""))

    # Determinar veredicto. Hay 3 niveles:
    #   ERROR: la captura no llegó al post-quiescent o el bloque GEMINI MODELS
    #          no se encontró. La cuenta no se upsertea (ultimo_check_ok=false
    #          en PHP).
    #   OK   : se encontró el bloque GEMINI MODELS y SE PUDO parsear el segmento
    #          weekly (el dato crítico para el umbral). El segmento 5h es
    #          opcional (puede que no se haya emitido por alguna razón).
    #   OK pero incompleto: bloque encontrado pero NO se pudo parsear weekly.
    #          Tratamos como ERROR (el operador puede ver los notes para
    #          diagnosticar) porque el dato weekly es el que decide el pausado.
    cap_ok = bool(cap.get("ok"))
    gemini_ok = bool(parsed.get("gemini_block_found"))
    weekly_ok = parsed.get("weekly_pct_usado") is not None
    veredicto_ok = cap_ok and gemini_ok and weekly_ok
    if not veredicto_ok:
        if not cap_ok:
            error_msg = f"captura_fallo: estado={cap.get('estado')} err={cap.get('error')}"
        elif not gemini_ok:
            error_msg = (f"bloque_gemini_no_encontrado en snapshot ({len(cap.get('snapshot') or '')} chars); "
                         f"parser_notes={parsed.get('parser_notes')}")
        else:
            error_msg = (f"weekly_no_parseado; parser_notes={parsed.get('parser_notes')}")
    else:
        error_msg = None

    out = {
        "ok": veredicto_ok,
        "engine": "agy",
        "modo": "usage",
        "veredicto": "OK" if veredicto_ok else "ERROR",
        "error": error_msg,
        "account_email":    parsed.get("account_email"),
        "plan_tier":        parsed.get("plan_tier"),
        "weekly_pct_usado": parsed.get("weekly_pct_usado"),
        "weekly_reset_seg": parsed.get("weekly_reset_seg"),
        "h5_pct_usado":     parsed.get("h5_pct_usado"),
        "h5_reset_seg":     parsed.get("h5_reset_seg"),
        "raw_screen":       cap.get("snapshot", ""),
        "duracion_seg":     round(time.time() - t0_total, 2),
        "estado_captura":   cap.get("estado"),
        "bytes_total":      cap.get("bytes_total", 0),
        "ready_ok":         cap.get("ready_ok", False),
        "post_ok":          cap.get("post_ok", False),
        "parser_notes":     parsed.get("parser_notes", []),
        "fecha_iso":        datetime.now().isoformat(timespec='seconds'),
    }

    try:
        salida_json.parent.mkdir(parents=True, exist_ok=True)
        salida_json.write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as e:
        sys.stderr.write(f"FATAL: no pude escribir salida JSON: {e}\n")
        sys.stderr.write(traceback.format_exc())
        return 4

    # Debug dump: SIEMPRE (v18+). Mismo modelo que main() — PHP decide post-QA.
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / "usage_snapshot.txt").write_text(out["raw_screen"], encoding='utf-8')
        (debug_dir / "usage_history.txt").write_text(cap.get("history", ""), encoding='utf-8')
        (debug_dir / "usage_raw.txt").write_text(cap.get("raw", ""), encoding='utf-8', errors='replace')
        (debug_dir / "usage_metrics.json").write_text(json.dumps({
            "ok": out["ok"], "veredicto": out["veredicto"], "estado": out["estado_captura"],
            "bytes_total": out["bytes_total"], "duracion_seg": out["duracion_seg"],
            "ready_ok": out["ready_ok"], "post_ok": out["post_ok"],
            "error": out["error"], "parser_notes": out["parser_notes"],
        }, indent=2, ensure_ascii=False), encoding='utf-8')
    except Exception as _e:
        sys.stderr.write(f"[agy-usage] WARN debug dump: {_e}\n")

    sys.stderr.write(
        f"[agy-usage] veredicto={out['veredicto']} ok={out['ok']} "
        f"wk_pct={out['weekly_pct_usado']} wk_reset={out['weekly_reset_seg']} "
        f"h5_pct={out['h5_pct_usado']} h5_reset={out['h5_reset_seg']} "
        f"email={out['account_email']} tier={out['plan_tier']} "
        f"dur={out['duracion_seg']}s bytes={out['bytes_total']}\n"
    )
    return 0


# ============================================================
# SANDBOX (espejo de preparar_sandbox del smoke + decode .b64 al estilo aistudio)
# ============================================================

def _resolver_imagen_al_sandbox(imagen_src: Path, dst_jpg: Path) -> Optional[str]:
    """Copia o decodifica la imagen al sandbox como `imagen.jpg`.

    Soporta el formato canónico de la cola del WEB (.b64) y archivos de imagen
    reales (.jpg/.png). Espejo del bloque de lib_aistudio.php:510-537.
    Devuelve None si OK, o un string de error.
    """
    if not imagen_src.is_file():
        return f"imagen_no_existe: {imagen_src}"

    if imagen_src.suffix.lower() == ".b64":
        try:
            b64_data = imagen_src.read_text(encoding='utf-8', errors='replace')
        except Exception as e:
            return f"imagen_b64_no_leible: {e}"
        # Tolerar prefijo data:URL
        if ',' in b64_data:
            b64_data = b64_data[b64_data.index(',') + 1:]
        try:
            decoded = base64.b64decode(b64_data.strip(), validate=True)
        except Exception as e:
            return f"imagen_b64_decode_fallo: {e}"
        if not decoded:
            return f"imagen_b64_vacia: {imagen_src}"
        try:
            dst_jpg.write_bytes(decoded)
        except Exception as e:
            return f"imagen_b64_escritura_fallo: {e}"
    else:
        try:
            shutil.copy2(imagen_src, dst_jpg)
        except Exception as e:
            return f"imagen_copia_fallo: {e}"
    return None


# Entradas del sandbox que NO se borran entre corridas: `.agents` (config de
# permisos). `imagen.jpg`/`prompt.md` los reescribe preparar_sandbox a
# continuación, así que NO hace falta preservarlos. Todo lo demás es residuo de
# una corrida anterior y se elimina.
_SANDBOX_KEEP = {".agents"}


def _limpiar_estado_agy(sandbox_dir: Path, scratch_dir: Optional[Path]) -> None:
    """Borra el estado escribible de agy ANTES de cada corrida (fix freeze 2026-06-25).

    Causa raíz del freeze: en `-p`, agy stagea/lee `@imagen.jpg`/`@prompt.md`
    desde su propio `scratch` (`<home>/.gemini/antigravity-cli/scratch`), NO
    desde el `--sandbox-dir`. Si una corrida deja una copia ahí, las corridas
    siguientes (conversación nueva — UUID distinto — pero MISMO filesystem) la
    releen y devuelven la transcripción de una página vieja, congelada. Además
    agy escribe basura en el sandbox (`crop_*.py`, `inspection/`, `{cwd}/`,
    `image.jpg`) cuando decide "explorar/programar" en vez de transcribir.

    Vaciar ambos antes de cada corrida garantiza que un fallo en una corrida no
    influya en las posteriores y que `@imagen.jpg` (cwd=sandbox) resuelva al
    archivo fresco. Best-effort: loguea a stderr pero nunca aborta el job.
    """
    def _borrar(entry: Path) -> None:
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink()
        except Exception as e:
            sys.stderr.write(f"[agy] WARN no pude limpiar {entry}: {e}\n")

    # 1) Vaciar el scratch de agy (la fuente real del freeze).
    if scratch_dir is not None and scratch_dir.is_dir():
        for entry in scratch_dir.iterdir():
            _borrar(entry)

    # 2) Dejar el sandbox sólo con lo canónico (.agents); preparar_sandbox
    #    reescribe imagen.jpg + prompt.md inmediatamente después.
    if sandbox_dir.is_dir():
        for entry in sandbox_dir.iterdir():
            if entry.name in _SANDBOX_KEEP:
                continue
            _borrar(entry)


def _stagear_en_scratch(scratch_dir: Optional[Path], sandbox_dir: Path) -> None:
    """Copia la imagen+prompt FRESCOS del job al scratch de agy (fix freeze 2026-06-25).

    Empíricamente agy en `-p` resuelve `@imagen.jpg`/`@prompt.md` de forma
    NO determinística: a veces los lee del cwd (=sandbox), a veces sale a
    "buscarlos en el user directory" y termina mirando su `scratch`. Vaciar el
    scratch (paso previo) mata el contenido stale pero deja el caso "no los
    encuentra → explora → no transcribe". La solución robusta es servirle la
    copia FRESCA del job TAMBIÉN en el scratch: lea de donde lea (sandbox o
    scratch), siempre obtiene la imagen correcta de ESTE job, nunca una vieja.

    Se llama DESPUÉS de preparar_sandbox (que ya validó/escribió los archivos en
    el sandbox). Best-effort: loguea a stderr pero no aborta.
    """
    if scratch_dir is None:
        return
    try:
        scratch_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sandbox_dir / "imagen.jpg", scratch_dir / "imagen.jpg")
        shutil.copy2(sandbox_dir / "prompt.md", scratch_dir / "prompt.md")
    except Exception as e:
        sys.stderr.write(f"[agy] WARN no pude stagear en scratch ({scratch_dir}): {e}\n")


def preparar_sandbox(sandbox_dir: Path, imagen_src: Path, prompt_src: Path) -> Optional[str]:
    """Asegura sandbox_dir/{imagen.jpg,prompt.md,.agents/settings.json}.

    El sandbox NO se borra ni recrea: debe existir (es PRE-TRUSTED, paso de
    setup manual). Sólo se sobreescriben los 3 archivos del job actual.
    Devuelve None si OK, o string de error.

    Plan #3 §1: ya NO borramos un output.txt defensivo —
    SANDBOX_SETTINGS["permissions"]["deny"] incluye tool(write_file),
    tool(create_file) y tool(edit_file), así que agy físicamente no puede
    crearlo. La paranoia heredada del otro proyecto no aplica acá.

    Plan #3 §5.4: limpiamos `.agy_last_status.json` del run anterior, así
    los tokens reportados no quedan contaminados si el statusLine no se
    dispara en este run (degrade limpio a tokens_*=0).
    """
    if not sandbox_dir.is_dir():
        return f"sandbox_dir_no_existe: {sandbox_dir}"

    err = _resolver_imagen_al_sandbox(imagen_src, sandbox_dir / "imagen.jpg")
    if err:
        return err

    if not prompt_src.is_file():
        return f"prompt_no_existe: {prompt_src}"
    try:
        shutil.copy2(prompt_src, sandbox_dir / "prompt.md")
    except Exception as e:
        return f"prompt_copia_fallo: {e}"

    agents = sandbox_dir / ".agents"
    try:
        agents.mkdir(exist_ok=True)
        (agents / "settings.json").write_text(
            json.dumps(SANDBOX_SETTINGS, indent=2), encoding='utf-8')
    except Exception as e:
        return f"settings_sandbox_fallo: {e}"

    status_file = sandbox_dir / ".agy_last_status.json"
    if status_file.exists():
        try:
            status_file.unlink()
        except Exception:
            pass
    return None


# ============================================================
# MODELO GLOBAL (settings.json del HOME de agy)
# ============================================================

def setear_modelo_global(home_dir: Optional[Path], modelo: str) -> Optional[str]:
    """Mergea `model` en `<home_dir>/.gemini/antigravity-cli/settings.json`.

    PRE: PHP ya tomó el advisory lock `agy_slot:<perfil>` (plan §Concurrencia).
    Si `modelo` es vacío, no toca nada (default cuenta). Si el archivo no
    existe, NO lo crea (es setup manual del usuario, plan §Setup paso 2).
    Devuelve None si OK o no aplica; string de error si el merge falló.
    """
    if not modelo:
        return None
    base = home_dir if home_dir is not None else Path.home()
    settings_path = base / ".gemini" / "antigravity-cli" / "settings.json"
    if not settings_path.is_file():
        # No es nuestro trabajo crearlo. Lo crea el usuario al loguearse.
        sys.stderr.write(f"[agy] WARN settings.json global no existe: {settings_path}\n")
        return None
    try:
        raw = settings_path.read_text(encoding='utf-8')
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            return f"settings_global_invalido: no es objeto JSON"
        if data.get("model") == modelo:
            return None  # ya está
        data["model"] = modelo
        # Escritura atómica
        tmp = settings_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
        tmp.replace(settings_path)
    except Exception as e:
        return f"settings_global_merge_fallo: {type(e).__name__}: {e}"
    return None


# ============================================================
# KILL del árbol agy + barrido del language server detached
# (espejo de _kill_arbol / _barrido_zombis del smoke)
# ============================================================

def _pids_agy_actuales() -> set:
    pids = set()
    for p in psutil.process_iter(["name"]):
        try:
            nm = (p.info["name"] or "").lower()
            if nm.startswith("agy"):
                pids.add(p.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


def _kill_arbol(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, timeout=8, check=False,
        )
    except Exception:
        pass


def _barrido_zombis(pids_previos: set, t_start_epoch: float) -> int:
    """Mata procesos `agy*` creados durante el run (LS detached del padre)."""
    barridos = 0
    cutoff = t_start_epoch - 5
    for p in psutil.process_iter(["name", "create_time"]):
        try:
            nm = (p.info["name"] or "").lower()
            if not nm.startswith("agy"):
                continue
            if p.pid in pids_previos:
                continue
            if p.info["create_time"] < cutoff:
                continue
            p.kill()
            barridos += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return barridos


# ============================================================
# PARSEO ESTRUCTURADO DE TOOL CALLS (plan #3 §4)
# ============================================================

def parsear_tool_calls(history_text: str) -> list:
    """Parsea los tool calls que agy lista en el TUI (`● ToolName(args)`).

    Aplica el regex sólo a la zona PRE-INICIO del history: después del último
    INICIO, el carácter `●` deja de ser delimitador de tool y se vuelve
    ambiguo con el contenido transcripto. Validado contra el smoke real
    (`outputs/run_01/history_text.txt`).
    """
    if not history_text:
        return []
    idx = history_text.find(INI_MARKER)
    zona = history_text[:idx] if idx != -1 else history_text
    tools = []
    seen = set()
    for m in TOOL_CALL_RE.finditer(zona):
        name = m.group(1)
        args = (m.group(2) or "").strip()
        # Recorte defensivo: si el regex no atrapó el sufijo, lo sacamos acá.
        if ") (ctrl" in args:
            args = args.split(") (ctrl", 1)[0]
        clave = (name, args)
        if clave in seen:
            continue
        seen.add(clave)
        tools.append({"name": name, "args": args})
    return tools


# ============================================================
# DETECCIÓN DE WEBSEARCH
# ============================================================

def detectar_websearch(res: CaptureResult, tools_used: list) -> dict:
    """Detección de WebSearch en dos pasos (plan #3 §4):

    1. Estructurada: si alguno de los tool calls parseados es una tool de
       búsqueda web conocida (PascalCase exacto). Confiable, viene de lo que
       agy reporta haber hecho.
    2. Fallback heurístico por substring sobre el blob crudo, sólo si el
       parseo estructurado no encontró nada — por si agy cambia el formato
       del TUI y el parser queda silencioso.

    Política (plan macro §A): nunca dispara error ni retranscripción
    automática. Resultado → QA_BIT_AGY_WEBSEARCH=8192 → estado Revisar +
    sospecha manual.
    """
    hits_tool = [t["name"] for t in (tools_used or [])
                 if t.get("name") in WEBSEARCH_TOOL_NAMES]
    if hits_tool:
        return {"detectado": True, "patrones": hits_tool, "fuente": "tools_used"}

    blob = ((res.console_raw or "") + "\n" +
            (res.raw_stripped or "") + "\n" +
            (res.history_text or ""))
    low = blob.lower()
    hits_heur = [p for p in WEBSEARCH_PATRONES if p.lower() in low]
    if hits_heur:
        return {"detectado": True, "patrones": hits_heur, "fuente": "heuristica"}
    return {"detectado": False, "patrones": [], "fuente": "none"}


# ============================================================
# DETECCIÓN DE CUOTA AGY (HTTP 429) — SQLite de la conversación
# ============================================================
# En `-p` el 429 RESOURCE_EXHAUSTED NO llega a consola/history (vive sólo en
# `~/.gemini/antigravity-cli/conversations/<uuid>.db`, tabla `steps`, columnas
# `step_payload`/`error_details`). Sin esto el wrapper veía exit 0 + stdout
# vacío y reportaba el genérico `agy_exit_sin_datos`, sin poder distinguir
# "cuota agotada" de "agy se rompió". Detector validado 2026-06-25 contra 109
# .db reales (cero falsos positivos: hits limpios en las conversaciones de la
# franja de cuota agotada; sin hits en las previas que sí completaron).
#
# agy es serial por usuario Windows (tope 1 por cuenta) → la .db modificada
# durante esta corrida es la de ESTE job. Filtramos por mtime >= t_launch-2s
# para no leer .db de un job previo.

_PRINTABLE = re.compile(rb"[\x20-\x7e]{4,}")
_PAT_CUOTA = re.compile(
    r"RESOURCE_EXHAUSTED|Individual quota reached|quota reached|HTTP 429|code[ \"]*:?\s*429",
    re.IGNORECASE,
)
_PAT_RESET = re.compile(
    r"Resets?\s+in\s+(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?",
    re.IGNORECASE,
)


def _detectar_cuota_en_conversacion(conv_dir: str, t_launch: float) -> tuple:
    """Busca `RESOURCE_EXHAUSTED` / "Individual quota reached" / HTTP 429 en la
    .db de la conversación de ESTE job.

    Devuelve `(cuota: bool, reset_seg: int|None)` — `reset_seg` parseado de
    "Resets in 13m27s" cuando agy lo trae adosado. Si no aparece, devuelve
    `(False, None)` o `(True, None)` según el match.

    Lectura `mode=ro` con timeout corto: best-effort, nunca aborta el job (el
    .py sigue con el flujo normal aunque esto falle).
    """
    try:
        dbs = [
            p for p in glob.glob(os.path.join(conv_dir, "*.db"))
            if os.path.getmtime(p) >= t_launch - 2
        ]
    except Exception:
        return (False, None)
    dbs.sort(key=os.path.getmtime, reverse=True)
    for db in dbs[:3]:
        try:
            uri = "file:" + db.replace("\\", "/") + "?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=2)
            try:
                try:
                    rows = con.execute(
                        "SELECT step_payload, error_details FROM steps"
                    ).fetchall()
                except Exception:
                    rows = con.execute("SELECT step_payload FROM steps").fetchall()
            finally:
                con.close()
        except Exception:
            continue
        parts = []
        for row in rows:
            for b in row:
                if b is None:
                    continue
                if isinstance(b, str):
                    b = b.encode("utf-8", "replace")
                parts += [m.group().decode("ascii", "replace")
                          for m in _PRINTABLE.finditer(b)]
        blob = "\n".join(parts)
        if _PAT_CUOTA.search(blob):
            reset_seg = None
            m = _PAT_RESET.search(blob)
            if m and any(m.groups()):
                reset_seg = (int(m.group(1) or 0) * 3600
                             + int(m.group(2) or 0) * 60
                             + int(m.group(3) or 0))
            return (True, reset_seg)
    return (False, None)


# ============================================================
# DETECTOR .db STEPS: tool calls + web signals (bump v23, 2026-07-16)
# ============================================================
# En `-p` el chrome del TUI NO se ecoa a consola → `parsear_tool_calls` sobre
# `history_text` queda ciego (`tools_used=[]` siempre) y `detectar_websearch`
# cae al fallback heurístico substring sobre un blob vacío. Peor todavía: el
# **grounding server-side** (Google Search dentro de `streamGenerateContent`)
# NUNCA fue visible ni en `-i` — no pasa por permisos ni deja línea en el log.
# Sí queda rastro en `~/.gemini/antigravity-cli/conversations/<uuid>.db`,
# tabla `steps`, columnas BLOB `step_payload`/`error_details` (strings ASCII
# legibles intercalados en el protobuf; probado empíricamente 2026-07-16 con la
# .db del test de web-search de Tomás: `search_web` + args JSON, `vertexaisearch`,
# `read_url_content`, URLs a duckduckgo, `run_command`, `RunCommand`).
#
# Este detector reusa el mismo mecanismo del detector de cuota (SQLite ro,
# mtime filter). Es best-effort: cualquier excepción cae al comportamiento
# heredado (tools_used del TUI + heurística substring).

# Clave canónica que agy escribe en step_payload por cada TOOL CALL real.
# Ejemplos observados 2026-07-16: "toolAction":"Viewing file",
# "toolAction":"Running command", "toolAction":"Searching the web",
# "toolAction":"Reading URL", "toolAction":"Editing file",
# "toolAction":"Writing file". Va acompañada de "toolSummary" (label humano
# corto) y de un args JSON. Los DEMÁS nombres que aparecen en el step
# (`search_web`, `grep`, etc.) son chain-of-thought del modelo mencionando
# los nombres de otras tools — NO son ejecuciones reales. **Sólo mirar
# toolAction filtra ese ruido.**
_TOOL_ACTION_RE = re.compile(r'"toolAction"\s*:\s*"([^"]+)"')
# Mapeo human-readable ("Running command") → nombre canónico ("run_command")
# para la salida. Si no matchea, devolver el valor crudo (fallback tolerante a
# labels futuros o localizados).
_TOOL_ACTION_TO_CANON = {
    "Viewing file":                    "view_file",
    "Viewing prompt.md":               "view_file",   # skill agy-customizations (local)
    "Viewing image":                   "view_file",   # skill agy-customizations (local)
    "Reading file":                    "read_file",
    "Reading prompt instructions":     "read_file",   # esperado en cada job (@prompt.md)
    "Viewing image for transcription": "view_file",   # esperado en cada job (@imagen.jpg)
    "Writing file":                    "write_file",
    "Creating file":                   "create_file",
    "Editing file":                    "edit_file",
    "Deleting file":                   "delete_file",
    "Listing directory":               "list_dir",
    "Searching files":                 "grep_search",
    "Searching for files":             "glob",
    "Running command":                 "run_command",
    "Searching the web":               "search_web",
    "Reading URL":                     "read_url_content",
    "Fetching URL":                    "web_fetch",
    "Browsing":                        "browser",
    "Applying patch":                  "apply_patch",
}
# Regex para el UUID de la conversación de ESTA corrida: agy lo escribe en la
# 1ª línea del --log-file ("Created conversation <uuid>"). Usarlo es 100%
# determinístico — evita depender de heurísticas de mtime que fallan cuando
# corren varias transcripciones en ventana chica (multi-host, o post-reinicio
# del worker con .db abiertas por procesos zombie).
_UUID_CONV_RE = re.compile(
    r'Created conversation ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
    re.IGNORECASE,
)


def _uuid_conversacion_desde_log(agy_log_path) -> Optional[str]:
    """Extrae el UUID de la conversación del `agy_logfile.log` de ESTA corrida.
    Determinístico: agy escribe una línea `Created conversation <uuid>` cerca
    del arranque. Best-effort: si el archivo no existe / está corrupto,
    devuelve None y el detector cae al mecanismo heurístico (por mtime)."""
    if not agy_log_path:
        return None
    try:
        p = str(agy_log_path)
        if not os.path.isfile(p):
            return None
        with open(p, encoding='utf-8', errors='replace') as f:
            head = f.read(64 * 1024)  # el "Created conversation" cae en los 1ros KB
        m = _UUID_CONV_RE.search(head)
        return m.group(1) if m else None
    except Exception:
        return None
# Set canónico de tools que se consideran "web" (para poblar el bit
# websearch_detectado). Grounding server-side es aparte (via _DB_WEB_MARKERS).
_DB_WEB_TOOL_NAMES = frozenset({
    "search_web", "read_url_content", "web_fetch", "browser",
})
# Patrones (substring) de GROUNDING server-side y URL scraping que NO son
# tool calls con nombre pero sí evidencia de web. `vertexaisearch` es el
# grounding real de Google; el resto suele acompañarlo o venir de tool web.
_DB_WEB_MARKERS = (
    "vertexaisearch", "groundingMetadata", "grounding_metadata",
    "webSearchQueries", "web_search_queries",
)
# URLs no-google (excluir hosts propios de google para no marcar falsos hits
# por links a docs/config/etc. que agy pueda mencionar).
_URL_RE = re.compile(r"https?://([A-Za-z0-9.\-]+)", re.IGNORECASE)
_URL_GOOGLE_HOSTS_SUFFIX = (
    "google.com", "googleusercontent.com", "gstatic.com", "googleapis.com",
    "youtube.com", "ggpht.com", "chromium.org",
)
# ── Citations del checker de atribución/recitación (bump v30, 2026-07-24) ────
# Cuando el texto generado matchea material indexado, el backend adjunta al
# candidato un `CitationMetadata` con N × `CitationSource`:
#     message CitationSource {
#       optional int32  start_index = 1;   # \x08 <varint>
#       optional int32  end_index   = 2;   # \x10 <varint>
#       optional string uri         = 3;   # \x1a <len> <bytes>
#       optional string license     = 4;
#     }
# En la `.db` eso queda como bytes crudos en el `step_payload` del step de
# respuesta (`step_type=15`), inmediatamente después del texto truncado.
# **NO es evidencia de web**: el modelo no navegó. Verificado 2026-07-24 sobre
# los 12 bundles de `temp/agy_debug/` que traen citations (hemerotecas
# digitalizadas: cdigital.dgb.uanl.mx, bibliotecavirtualmadrid, prensahistorica
# .mcu.es, archivos.juridicas.unam.mx): **cero** marcadores de grounding
# (`vertexaisearch`/`groundingMetadata`/`webSearchQueries`) y **cero**
# toolActions de web en los 12. Es el checker de recitación de Google marcando
# que el modelo reprodujo material memorizado — la contracara del corte de
# stream (los 12 son `fuente_response=ini_only`, y ninguno de los `ini_fin`
# trae citations).
# Se extraen para dos cosas: (a) NO contarlas como `web_urls` — prendían el bit
# WEBSEARCH sin que hubiera búsqueda; (b) exponerlas como evidencia de la
# recitación enmascarada que documenta `notas/bloqueo_qa_recurrente.md`.
_PB_VARINT = rb"(?:[\x80-\xff]{0,4}[\x00-\x7f])"
_CITATION_URI_RE = re.compile(
    rb"\x08" + _PB_VARINT +          # start_index
    rb"\x10" + _PB_VARINT +          # end_index
    rb"\x1a" + _PB_VARINT +          # len(uri)
    rb"(https?://[\x21-\x7e]{4,300})"
)


def _citation_urls_de_rows(rows: list) -> list:
    """URLs de `CitationSource` en los BLOBs CRUDOS de `steps` (bump v30).

    Trabaja sobre bytes, no sobre los strings imprimibles de
    `_steps_a_strings`: la firma que discrimina una citation de una URL
    cualquiera es estructural (los dos varints `start_index`/`end_index`
    inmediatamente antes del `uri`), y esos bytes no sobreviven la extracción
    ASCII. No parsea el protobuf completo — matchea la firma del submensaje.
    Best-effort: cualquier excepción la absorbe el caller."""
    vistas, out = set(), []
    for row in rows:
        for b in row[1:]:
            if b is None:
                continue
            if isinstance(b, str):
                b = b.encode("utf-8", "replace")
            for m in _CITATION_URI_RE.finditer(bytes(b)):
                u = m.group(1).decode("ascii", "replace")
                if u not in vistas:
                    vistas.add(u)
                    out.append(u)
    return out
# Snippet corto de un JSON de args para mostrar en tools_used[i].args
# (primeros ~200 chars del primer bloque `{"...":...}` que aparezca en el step).
_ARGS_JSON_RE = re.compile(r"\{[^{}]{2,600}\}")


def _steps_a_strings(rows: list) -> list:
    """Convierte las rows de la tabla `steps` a [(idx, blob_ascii_concat), ...].
    Extrae strings ASCII imprimibles (regex `[\\x20-\\x7e]{4,}`) del BLOB
    protobuf de cada step. Es el mismo gesto que `strings` de Unix — no
    parsea el protobuf, sólo saca los strings legibles que quedaron
    intercalados (nombres de tool, args JSON, URLs, etc.)."""
    out = []
    for row in rows:
        idx = row[0]
        parts = []
        for b in row[1:]:
            if b is None:
                continue
            if isinstance(b, str):
                b = b.encode("utf-8", "replace")
            parts += [m.group().decode("ascii", "replace")
                      for m in _PRINTABLE.finditer(b)]
        out.append((idx, "\n".join(parts)))
    return out


def _leer_steps_db(db_path: str, retries: int = 3, sleep_s: float = 0.3) -> Optional[list]:
    """Abre la .db en mode=ro y devuelve rows[(idx, payload, err)] o None.
    Reintenta si sqlite3 falla con "database is locked" o "unable to open" —
    agy puede tener la .db abierta (WAL activo) al momento del check y una
    lectura ro con timeout=2 puede fallar la 1ª vez pero pasar la 2ª."""
    last_err = None
    for attempt in range(retries):
        try:
            uri = "file:" + db_path.replace("\\", "/") + "?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=2)
            try:
                try:
                    rows = con.execute(
                        "SELECT idx, step_payload, error_details FROM steps ORDER BY idx"
                    ).fetchall()
                except Exception:
                    rows = con.execute(
                        "SELECT idx, step_payload FROM steps ORDER BY idx"
                    ).fetchall()
                return rows
            finally:
                con.close()
        except Exception as e:
            last_err = e
            if attempt + 1 < retries:
                time.sleep(sleep_s)
    sys.stderr.write(f"[agy] WARN _leer_steps_db({os.path.basename(db_path)}) tras {retries} intentos: {last_err}\n")
    return None


def _parsear_conversacion_db(conv_dir: str, t_launch: float,
                             uuid_conversacion: Optional[str] = None) -> dict:
    """Extrae tool calls + web signals de la .db de conversación de ESTE job.

    Prioridades para elegir la .db:
      1) **`uuid_conversacion` explícito** — el UUID lo saca `main()` del
         `agy_logfile.log` de ESTA corrida (línea `Created conversation <uuid>`).
         Es determinístico → cero ambigüedad, cero riesgo de agarrar la .db
         de una corrida concurrente en otro slot. **Es la vía preferida.**
      2) Fallback heurístico por mtime: .db con `mtime >= t_launch - 2` de la
         carpeta, procesadas por mtime desc (cap 10). Se elige la que más steps
         tenga. Sólo aplica si (1) no dio resultado (log corrupto, agy no logueó
         el UUID, etc.).

    Retorna dict con:
      - `db_path`         : path de la .db elegida (para copiar al bundle), o None
      - `tool_calls`      : list[{"name":str,"args":str,"step_idx":int}]
      - `web_signals`     : list[str] — patrones canónicos matcheados
      - `web_urls`        : list[str] — hosts URL http(s) no-google encontrados,
                            EXCLUIDOS los hosts que sólo aparecen como
                            `CitationSource` (v30: no son evidencia de web)
      - `citation_urls`   : list[str] — URLs completas de `CitationSource` del
                            checker de atribución/recitación de Google. Evidencia
                            de recitación enmascarada, NO de acceso web
      - `imagen_cargada`  : bool|None — ¿agy adjuntó la imagen al contexto
                            multimodal? Tri-estado (True/False/None=no
                            verificable). **v34: la señal cambió** — ver
                            `_resolver_imagen_cargada`. Fuerte = PNG en
                            `gen_metadata`; la tripleta de `steps` (v28) quedó
                            como fallback porque en el `-p` roto del #735 daba
                            True con la imagen sin viajar. La señal del
                            `--log-file` (`media=N`) la suma el caller, que es
                            el que tiene el path del log.
      - `imagen_tripleta` : bool — señal vieja (v28) cruda, para forense.
      - `imagen_png_media`: bool|None — PNG en `gen_metadata`, cruda.
      - `db_via`          : "uuid" | "mtime" | "" — cómo se eligió la .db (v37).
      - `db_identidad_ok` : bool|None — ¿la .db es de ESTA corrida? (v37).

    ── v37: las señales derivadas también se gatean por identidad ──────────────
    Hasta v36, si la vía (2) elegía la .db de otro job, `imagen_cargada`,
    `websearch` y `citation_urls` viajaban al shape igual — y `imagen_cargada`
    dispara el QA grave `no_cargo_imagen`. Medido: 11 bundles con .db ajena.
    Ahora, si la identidad no da True, este helper devuelve las señales en
    NEUTRO (listas vacías, tri-estados en None = "no verificable", que es lo que
    PHP ya sabe interpretar sin disparar QA) y deja `db_path` poblado para que
    el bundle forense igual se lleve la .db.

    Best-effort: cualquier excepción → dict con listas vacías, db_path=None,
    imagen_cargada=None.
    """
    out = {"db_path": None, "tool_calls": [], "web_signals": [], "web_urls": [],
           "citation_urls": [], "imagen_cargada": None,
           "imagen_tripleta": False, "imagen_png_media": None,
           "db_via": "", "db_identidad_ok": None}

    # ── Vía 1 (preferida): UUID exacto del logfile ──
    steps_strings = None  # [(idx, printable_str_blob), ...]
    chosen_db = None
    chosen_rows = None    # rows CRUDAS de la .db elegida (bump v30: citations)
    if uuid_conversacion:
        cand = os.path.join(conv_dir, f"{uuid_conversacion}.db")
        if os.path.isfile(cand):
            rows = _leer_steps_db(cand)
            if rows is not None:
                chosen_db = cand
                chosen_rows = rows
                steps_strings = _steps_a_strings(rows)
                out["db_via"] = "uuid"

    # ── Vía 2 (fallback): scan por mtime, elegir la de más steps ──
    if steps_strings is None:
        try:
            dbs = [
                p for p in glob.glob(os.path.join(conv_dir, "*.db"))
                if os.path.getmtime(p) >= t_launch - 2
            ]
        except Exception:
            return out
        if not dbs:
            return out
        best = None  # (db_path, steps_strings, rows_crudas)
        for db in sorted(dbs, key=os.path.getmtime, reverse=True)[:10]:
            rows = _leer_steps_db(db)
            if rows is None:
                continue
            ss = _steps_a_strings(rows)
            if best is None or len(ss) > len(best[1]):
                best = (db, ss, rows)
        if best is None:
            return out
        chosen_db, steps_strings, chosen_rows = best[0], best[1], best[2]
        out["db_via"] = "mtime"

    out["db_path"] = chosen_db

    # ── Guarda de IDENTIDAD (v37) ────────────────────────────────────────────
    # Sin esto, la vía (2) puede haber elegido la conversación de otro job y
    # todo lo que sigue describiría a esa otra corrida. `db_path` queda poblado
    # a propósito: el bundle forense se lleva la .db igual, para poder auditar.
    out["db_identidad_ok"] = _db_es_de_esta_corrida(chosen_db, uuid_conversacion)
    if out["db_identidad_ok"] is not True:
        sys.stderr.write(
            f"[agy] .db SIN identidad probada (via={out['db_via']}, "
            f"identidad={out['db_identidad_ok']}, "
            f"db={os.path.basename(chosen_db) if chosen_db else 'None'}) → "
            f"señales derivadas en neutro (no verificable)\n")
        return out

    # ── Citations del checker de atribución/recitación (bump v30) ──
    # Se extraen ANTES del loop de URLs porque sus hosts se excluyen de
    # `web_urls`. Un host que además tenga actividad web real sigue prendiendo
    # el bit por `web_signals` (toolAction web / marcador de grounding), que
    # este filtro no toca — por eso excluir el host acá no puede tapar una
    # búsqueda real.
    citation_hosts = set()
    try:
        out["citation_urls"] = _citation_urls_de_rows(chosen_rows or [])
        for _u in out["citation_urls"]:
            _m = _URL_RE.match(_u)
            if _m:
                citation_hosts.add(_m.group(1).lower())
    except Exception:
        out["citation_urls"] = []

    seen_signals = set()
    seen_hosts = set()
    prev_call = None  # (canon_name, args_snippet_head) para dedupar plan+ejec
    # Señal HISTÓRICA (v28) de "imagen.jpg adjuntada al contexto multimodal":
    # un mismo step de tool result con los 3 markers concurrentes `imagen.jpg`
    # + `image/` + `tempmediaStorage` (el blob `<home>/.gemini/antigravity-cli/
    # brain/<conv>/.tempmediaStorage/media_<conv>_<epoch>.png` donde agy stagea
    # la imagen resuelta).
    # v34: **ya no es la señal principal**. Describe el INTENTO del `view_file`,
    # no que el binario haya viajado: en el `-p` roto del bug #735 la tripleta
    # está presente y la imagen NO llegó (`inline_data` de longitud 0). Queda
    # como FALLBACK para .db sin `gen_metadata` legible. Ver
    # `_resolver_imagen_cargada`.
    imagen_tripleta = False

    for idx, blob in steps_strings:
        # Los tool calls reales de agy vienen SÓLO en "toolAction". Cada tool
        # aparece 2× (step "plan" con type=15 + step "ejec" con type≠15). No
        # tenemos el step_type acá — dedupamos por (canon, primeros 80 chars de
        # args) consecutivos: si el step anterior tuvo el mismo tool con los
        # mismos args, saltar (es el par plan+ejec).
        actions = _TOOL_ACTION_RE.findall(blob)
        for action_label in actions:
            canon = _TOOL_ACTION_TO_CANON.get(action_label, action_label)
            m = _ARGS_JSON_RE.search(blob)
            args = m.group(0)[:200] if m else ""
            key = (canon, args[:80])
            if key == prev_call:
                prev_call = key
                continue
            prev_call = key
            out["tool_calls"].append(
                {"name": canon, "args": args, "step_idx": int(idx)}
            )
            if canon in _DB_WEB_TOOL_NAMES and canon not in seen_signals:
                seen_signals.add(canon)
                out["web_signals"].append(canon)
        # Markers de grounding server-side (aparecen incluso sin toolAction).
        # `vertexaisearch` = grounding real de Google en `streamGenerateContent`,
        # invisible en `-i` y sin línea de log — sólo se ve acá.
        for mk in _DB_WEB_MARKERS:
            if mk in blob and mk not in seen_signals:
                seen_signals.add(mk)
                out["web_signals"].append(mk)
        # URLs no-google (excluir hosts propios de google para no marcar falsos
        # hits por links a docs/config/etc. que agy pueda mencionar).
        for host in _URL_RE.findall(blob):
            hl = host.lower()
            if any(hl == s or hl.endswith("." + s) for s in _URL_GOOGLE_HOSTS_SUFFIX):
                continue
            # v30: host que viene de un `CitationSource` no es evidencia de web.
            if hl in citation_hosts:
                continue
            if hl not in seen_hosts:
                seen_hosts.add(hl)
                out["web_urls"].append(hl)
        # Señal vieja (v28), hoy fallback: los 3 markers en el mismo blob.
        if (not imagen_tripleta
                and "imagen.jpg" in blob
                and "image/" in blob
                and "tempmediaStorage" in blob):
            imagen_tripleta = True

    # ── Señal v34: PNG crudo en `gen_metadata` + resolución tri-estado ────────
    # La del `--log-file` (`media=N`) la suma el caller (main), que es el que
    # tiene el path del log; acá se resuelve con lo que da la .db sola, así el
    # helper sigue siendo usable/testeable de forma independiente.
    out["imagen_tripleta"]  = imagen_tripleta
    out["imagen_png_media"] = _png_en_gen_metadata(chosen_db)
    out["imagen_cargada"]   = _resolver_imagen_cargada(
        out["imagen_png_media"], imagen_tripleta, None)
    return out


def _copiar_conv_db_al_bundle(db_path: str, debug_dir: Path) -> None:
    """Copia consistente de la .db de conversación al bundle forense.

    Usa `sqlite3.Connection.backup()` en vez de `shutil.copy2` porque agy puede
    tener la .db abierta con WAL (`.db-wal`/`.db-shm` presentes) en el momento
    en que copiamos → `shutil.copy2` daría una snapshot potencialmente
    inconsistente. `backup()` copia página-por-página bajo un lock corto,
    aplicando el WAL pendiente al destino. Best-effort: si falla, warning y
    seguimos.
    """
    dst = debug_dir / "conversation.db"
    try:
        src_uri = "file:" + db_path.replace("\\", "/") + "?mode=ro"
        src = sqlite3.connect(src_uri, uri=True, timeout=2)
        try:
            dstcon = sqlite3.connect(str(dst))
            try:
                src.backup(dstcon)
            finally:
                dstcon.close()
        finally:
            src.close()
    except Exception as e:
        sys.stderr.write(f"[agy] WARN debug conversation.db: {e}\n")


# Firmas de fallo de ARRANQUE de agy anteriores a cualquier generación (cada
# una muere antes de llamar al endpoint `streamGenerateContent` → NO consume
# cuota agy). El texto vive en el `--log-file` de agy y/o en la consola (pyte).
# Agregar una firma nueva acá alcanza para que el worker la reintente.
# Casos observados (agy_debug 2026-06-28..07-02): cluster A (backend 500 al
# bajar la lista de modelos → no resuelve el override → "neither PlanModel…"),
# cluster C (Credential Manager no responde en 5s → cae a OAuth interactivo →
# "authentication timed out"). NO incluye la exploración (cluster B): esa SÍ
# llega a generar (streamGenerateContent) → se queda ERROR y conserva el debug.
_FIRMAS_ARRANQUE_TRANSITORIO = (
    ("neither planmodel nor requestedmodel", "modelo_no_resuelto_backend"),
    ("failed to retrieve user quota summary", "quota_summary_500"),
    ("internal (code 500)",                   "backend_500_code_assist"),
    ("authentication timed out",              "auth_timeout"),
    ("authentication failed or timed out",    "auth_timeout"),
    ("keyringauth: timed out",                "keyring_timeout"),
)
# Endpoint de GENERACIÓN. Si aparece en el log, agy llamó al LLM → hubo posible
# consumo de cuota → NO es reintento seguro (gate duro de la regla de Tomás:
# "reintentar sólo lo que falla ANTES de que agy haga llamados al LLM").
_MARCA_GENERACION = "streamgeneratecontent"


def _detectar_arranque_transitorio(res: "CaptureResult",
                                   agy_log_path) -> tuple:
    """¿El fallo fue de ARRANQUE, anterior a cualquier generación (→ sin cuota
    consumida → reintentable)? Devuelve (True, motivo) sólo si (a) NO hubo
    `streamGenerateContent` en el log (gate duro) y (b) matchea una firma
    pre-LLM conocida. Best-effort: ante cualquier duda (log ilegible, sin firma,
    hubo generación) → (False, "") = se mantiene ERROR fail-fast (conserva
    debug, re-encolado manual = status quo). Nunca marca transitorio un job que
    llegó a llamar al LLM."""
    console = (res.console_raw or "").lower()
    log_txt = ""
    try:
        if agy_log_path and Path(agy_log_path).is_file():
            log_txt = Path(agy_log_path).read_text(
                encoding="utf-8", errors="replace").lower()
    except Exception:
        log_txt = ""  # logfile lockeado/ausente → gate no verificable → conservador
    # Gate duro: si agy llegó a generar, NO es reintentable (posible cuota).
    if _MARCA_GENERACION in log_txt:
        return (False, "")
    blob = console + "\n" + log_txt
    for needle, motivo in _FIRMAS_ARRANQUE_TRANSITORIO:
        if needle in blob:
            return (True, motivo)
    return (False, "")


# ── Forense del `--log-file` para el desambigüe de `executor_terminated` (v33) ─
# Patrones de sub-causa: agy deja la causa raíz en el log, pero hasta v32 no
# viajaba a ningún lado. El `error` que compone shape_salida enumeraba
# sub-causas "habituales" a mano, así que `api_errorlog` no decía cuál de ellas
# había ocurrido y había que abrir el bundle para saberlo.
_RE_SUBCAUSA_GENERADOR = re.compile(r"error in generator:\s*(.+)", re.I)
_RE_SUBCAUSA_PLANNER   = re.compile(r"processing planner output:\s*(.+)", re.I)
_RE_SUBCAUSA_PRINTMODE = re.compile(r"run ended with error[^:]*:\s*(.+)", re.I)
# Pistas complementarias: solas no explican el fallo, pero acotan mucho el
# diagnóstico. La de `media has no inline data` fue la firma del incidente del
# 2026-08-03 (INVALID_ARGUMENT 400 determinístico del backend).
_PISTAS_LOG = (
    "media has no inline data",
    "unauthenticated",
    "permission_denied",
    "model unreachable",
    "resource_exhausted",
)


def _forense_arranque(agy_log_path) -> dict:
    """Lee el `--log-file` UNA vez y devuelve lo que necesitan el gate de
    reintento y el mensaje de error.

    - `hubo_generacion`: True si el log tiene `streamGenerateContent` — agy
      llamó al LLM ⇒ consumió cuota ⇒ el fallo NO es "de arranque". **None** si
      el log es ilegible o no existe: no se puede afirmar nada, y los callers
      tratan None como "no sé" (conservador = comportamiento histórico).
    - `subcausa`: la causa raíz que agy dejó en el log, recortada, para que
      viaje dentro del `error` hasta `api_errorlog`.
    """
    out = {"hubo_generacion": None, "subcausa": ""}
    try:
        if not agy_log_path or not Path(agy_log_path).is_file():
            return out
        txt = Path(agy_log_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return out  # lockeado/ausente → gate no verificable

    out["hubo_generacion"] = (_MARCA_GENERACION in txt.lower())

    partes = []
    for rx in (_RE_SUBCAUSA_GENERADOR, _RE_SUBCAUSA_PLANNER, _RE_SUBCAUSA_PRINTMODE):
        m = rx.search(txt)
        if m:
            partes.append(m.group(1).strip()[:200])
            break
    bajo = txt.lower()
    pistas = [p for p in _PISTAS_LOG if p in bajo]
    if pistas:
        partes.append("pistas: " + ", ".join(pistas))
    out["subcausa"] = " | ".join(partes)[:400]
    return out


# ============================================================
# TOKEN USAGE — side-channel statusLine (plan #3 §5)
# ============================================================
# El "Thought for Xs, Yk tokens" del TUI NO desagrega input/output/cache.
# agy expone el contador real via statusLine: cada vez que cambia el agent
# state, agy ejecuta `statusLine.command` (~/.gemini/antigravity-cli/settings.json)
# y le pipea por stdin un JSON con la metadata de sesión. Nuestro mini-script
# (scripts/agy_statusline_dump.py) vuelca ese JSON a `<cwd>/.agy_last_status.json`.
# Como cwd de agy = --sandbox-dir, cada slot tiene su propio archivo: cero race.
# Doc: https://www.antigravity.google/docs/cli-statusline

def leer_token_usage(sandbox_dir: Path) -> dict:
    """Lee `<sandbox>/.agy_last_status.json` y mapea token counters.

    Degrada limpio (plan #3 §5 fallback):
      - Sin setup manual del statusLine → archivo no existe → todos los
        tokens en 0 + log a stderr `agy_statusline_no_configurado`. El
        wrapper sigue funcionando con la misma calidad que sin estos datos.
      - Archivo malformado / shape diferente → lectura defensiva con .get(),
        cualquier campo faltante queda en 0.

    Mapeo (shape oficial):
      context_window.current_usage.input_tokens         → tokens_input
      context_window.current_usage.output_tokens        → tokens_output
      context_window.current_usage.cache_read_input_tokens → tokens_cached
      total_input_tokens + total_output_tokens          → tokens_total
      tokens_thought                                    → 0 (agy/Gemini parecen
        incluir los thoughts dentro de output_tokens; corroborar en corrida real).
    """
    out = {
        "tokens_input": 0, "tokens_output": 0, "tokens_cached": 0,
        "tokens_thought": 0, "tokens_total": 0,
        "context_window_size": 0, "used_percentage": 0.0, "plan_tier": "",
        "statusline_disponible": False,
        # Cuota del bloque `quota` del statusLine (gemini-weekly / gemini-5h).
        # None = no vino el dato (sin statusLine, o shape sin `quota`) → el
        # consumidor PHP gatea con !== null y NO toca cuota. 0.0 sería "0% usado".
        "quota_weekly_pct_usado": None, "quota_weekly_reset_seg": None,
        "quota_h5_pct_usado": None, "quota_h5_reset_seg": None,
        "account_email": None,
    }
    path = sandbox_dir / ".agy_last_status.json"
    if not path.is_file():
        sys.stderr.write(
            "[agy] agy_statusline_no_configurado: "
            f"{path} no existe (tokens en 0)\n"
        )
        return out
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception as e:
        sys.stderr.write(f"[agy] WARN .agy_last_status.json no parseable: {e}\n")
        return out
    if not isinstance(data, dict):
        return out
    cw = data.get("context_window") or {}
    cu = (cw.get("current_usage") or {}) if isinstance(cw, dict) else {}

    def _i(v):
        try:
            return int(v or 0)
        except Exception:
            return 0

    def _f(v):
        try:
            return float(v or 0.0)
        except Exception:
            return 0.0

    out["tokens_input"]  = _i(cu.get("input_tokens"))
    out["tokens_output"] = _i(cu.get("output_tokens"))
    out["tokens_cached"] = _i(cu.get("cache_read_input_tokens"))
    out["tokens_total"]  = _i(cw.get("total_input_tokens")) + _i(cw.get("total_output_tokens"))
    out["context_window_size"] = _i(cw.get("context_window_size"))
    out["used_percentage"]     = _f(cw.get("used_percentage"))
    out["plan_tier"]           = str(data.get("plan_tier") or "")
    out["statusline_disponible"] = True

    # Cuota (bloque `quota`): sólo el grupo GEMINI que consume prensa
    # (gemini-weekly/gemini-5h; las `3p-*` son de Antigravity, no las usamos).
    # `remaining_fraction` (0..1) → pct USADO = round((1-frac)*100, 2); el mismo
    # shape que produce parsear_usage_screen para el /usage, así el feed en PHP
    # no discrimina origen. Defensivo con .get() y `if key in`: campo faltante
    # queda en None (base dict), degradación limpia.
    q = data.get("quota") or {}
    gw = (q.get("gemini-weekly") or {}) if isinstance(q, dict) else {}
    g5 = (q.get("gemini-5h") or {}) if isinstance(q, dict) else {}

    def _pct_usado(frac):
        try:
            return round((1.0 - float(frac)) * 100.0, 2)
        except Exception:
            return None

    if "remaining_fraction" in gw:
        out["quota_weekly_pct_usado"] = _pct_usado(gw.get("remaining_fraction"))
    if "reset_in_seconds" in gw:
        out["quota_weekly_reset_seg"] = _i(gw.get("reset_in_seconds"))
    if "remaining_fraction" in g5:
        out["quota_h5_pct_usado"] = _pct_usado(g5.get("remaining_fraction"))
    if "reset_in_seconds" in g5:
        out["quota_h5_reset_seg"] = _i(g5.get("reset_in_seconds"))
    out["account_email"] = str(data.get("email") or "") or None
    return out


# ============================================================
# DEBUG DUMP (plan §"Modo debug de captura forense")
# ============================================================

def volcar_debug_bundle(debug_dir: Path, res: CaptureResult, metrics: dict,
                        agy_log_path: Optional[Path]) -> None:
    """Vuelca el bundle .txt del smoke al `debug_dir` (`<workdir>/debug/`).

    v18+: el caller (main / main_usage) llama esto SIEMPRE; el lifecycle
    del workdir (borrar / archivar) lo decide PHP post-QA.
    """
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        sys.stderr.write(f"[agy] WARN debug_dir no se pudo crear: {e}\n")
        return

    def _w(name: str, content: str) -> None:
        try:
            (debug_dir / name).write_text(content or "", encoding='utf-8')
        except Exception as e:
            sys.stderr.write(f"[agy] WARN debug {name}: {e}\n")

    _w("console_raw.txt", res.console_raw)
    _w("raw_stripped.txt", res.raw_stripped)
    _w("screen_snapshot.txt", res.screen_snapshot)
    _w("history_text.txt", res.history_text)
    _w("extracted_screen.txt", res.extracted_screen or "")
    _w("extracted_history.txt", res.extracted_history or "")
    _w("partial_from_ini.txt", res.partial_from_ini or "")
    # v34 (paste): el texto que SÍ se persiste sale de la .db, no del grid.
    # Se vuelca sólo si existe, para no sembrar un archivo vacío en los bundles
    # de `print`/`interactive`.
    _resp_db = getattr(res, "response_db", None)
    if _resp_db:
        _w("extracted_db.txt", _resp_db)
    try:
        (debug_dir / "metrics.json").write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as e:
        sys.stderr.write(f"[agy] WARN debug metrics.json: {e}\n")
    # Si agy escribió su --log-file, copiarlo al debug_dir también.
    if agy_log_path and agy_log_path.is_file():
        try:
            shutil.copy2(agy_log_path, debug_dir / "agy_logfile.log")
        except Exception as e:
            sys.stderr.write(f"[agy] WARN debug agy_logfile.log: {e}\n")


# ============================================================
# DECISIÓN DE VEREDICTO Y SHAPE DE SALIDA
# ============================================================

def _mejor_fuente(res: CaptureResult) -> tuple:
    """Jerarquía de fuentes del `response`: (texto, fuente, fin_presente).

    Un solo lugar que responde "¿qué texto tenemos?", para que los caminos de
    fallo puedan preguntarlo ANTES de decidir que no hay nada (v37). Hasta v36
    la jerarquía vivía inline en `decidir_veredicto` y los bloques de cuota /
    excepción retornaban antes de llegar a ella: por eso una corrida que
    terminaba bien y recibía el 429 DESPUÉS devolvía `response=""`.

      paste : SÓLO la .db. La pantalla no es fallback válido — el TUI renderiza
              el markdown y destruye los tags `<ilegible>`/`<dudoso>` (medido:
              271 vs 0). Invariante del v34, no tocar.
      -p/-i : partial_from_ini → **.db (v37)** → history → screen.
              La .db entra como RESCATE por encima de history porque cuando el
              grid quedó vacío pero el modelo generó (job68768: TIMEOUT a los
              600 s con 19.316 ch en la .db) el texto está ahí y sólo ahí.

    `res.response_db` ya viene gateado por identidad + generación desde main():
    lo que llega acá es texto cuya autoría está probada.
    """
    if getattr(res, "paste_modo", False):
        db_txt = (getattr(res, "response_db", None) or "").strip()
        if db_txt and len(db_txt) >= MIN_CONTENT_LEN:
            return (db_txt, getattr(res, "db_fuente", "") or "db",
                    FIN_MARKER in db_txt)
        return ("", "vacio", False)

    partial = (res.partial_from_ini or "").strip()
    if partial and len(partial) >= MIN_CONTENT_LEN:
        return (partial, "ini_fin" if res.fin_visto else "ini_only",
                bool(res.fin_visto))
    db_txt = (getattr(res, "response_db", None) or "").strip()
    if db_txt and len(db_txt) >= MIN_CONTENT_LEN:
        return (db_txt, getattr(res, "db_fuente", "") or "db",
                FIN_MARKER in db_txt)
    history = (res.history_text or "").strip()
    if history and len(history) >= MIN_CONTENT_LEN:
        return (history, "history", False)
    screen = (res.screen_snapshot or "").strip()
    if screen and len(screen) >= MIN_CONTENT_LEN:
        return (screen, "screen", False)
    return ("", "vacio", False)


# Fuentes que cuentan como RESCATE. La lista es corta a propósito: son las que
# llevan los marcadores del prompt (o salen de la .db, que es la fuente
# estructurada). `history`/`screen` NO entran — un mensaje de chrome del TUI de
# 47 chars ("Error: Agent execution terminated due to error.") pasa el piso de
# MIN_CONTENT_LEN=20 y no es una transcripción: tratarlo como rescate convertía
# un veredicto CUOTA legítimo en un OK-con-texto-basura que después las firmas
# de `shape_salida` degradaban a ERROR, perdiendo la etiqueta de cuota.
# (Medido: job69869 intento 1, `history_text` de 47 chars.)
_FUENTES_RESCATABLES = ("ini_fin", "ini_only", "db", "db_parcial")


def _motivo_rescate(camino: str, fuente: str, res: "CaptureResult" = None) -> str:
    """Etiqueta del camino de rescate → `rescate_motivo` del shape → QA grave
    `texto_rescatado` en prensa. `camino` vacío + texto limpio ⇒ "" (sin QA).

    Se compone con `+`: además del camino, marca las dos propiedades del texto
    que un humano tiene que confirmar — que vino truncado y que trae
    razonamiento del modelo mezclado. Ojo: `thinking_mezclado` es una ETIQUETA,
    no un filtro; el texto se persiste igual (el fragmento de página real vale
    más que la prolijidad) y el QA grave lo manda a revisión.
    """
    partes = [p for p in (camino,) if p]
    if fuente == "db_parcial":
        partes.append("parcial_sin_fin")
    if res is not None and getattr(res, "db_razonamiento", False) and \
            str(fuente).startswith("db"):
        partes.append("thinking_mezclado")
    return "+".join(partes)


def decidir_veredicto(res: CaptureResult) -> tuple:
    """Mapea CaptureResult → (veredicto, response, error, fin_presente, fuente_response).

    Plan #3 §2: set simplificado OK | ERROR | CUOTA. `SIN_FIN` ya NO es
    veredicto del .py — PHP detecta truncado con qaDetectarSinFin() sobre
    `response` (que incluye FIN literal si estaba). El .py sigue usando FIN
    internamente para cerrar agy antes del timeout (evaluar_candidato +
    fin_grace_seg), pero eso no se refleja como veredicto.

    Persistir antes de discriminar (2026-06-21): `response` se puebla con la
    mejor evidencia disponible, no sólo cuando hay INICIO. Jerarquía:
      1) partial_from_ini (desde el ÚLTIMO INICIO hasta el final del history;
         FIN incluido si estaba) → fuente "ini_fin" o "ini_only" según fin_visto.
         PHP lo recorta en parseAndInsertEntradas(). Camino feliz prensa
         (idéntico al pre-cambio).
      2) history_text completo → fuente "history". Cubre: v2 con prompt
         `[tipo:]` sin marcadores; prensa con instruction-following degradado.
         PHP decide post-hoc (QA bit por "sin marcador").
      3) screen_snapshot → fuente "screen". Fallback si el history quedó vacío
         (p.ej. spawn falló muy temprano).
      4) "" → fuente "vacio". Único caso genuino de fallo de captura.

    v34 (modo `paste`): esa jerarquía NO aplica. El texto sale de la .db
    (fuente "db") y no hay fallback a pantalla — ver el bloque 0.5.

      OK    : response no vacío (cualquier fuente).
      ERROR : spawn falló, o todas las fuentes vacías.
      CUOTA : reservado (no se detecta auto todavía; handoff #4).

    v37 (proyecto `agy_texto_descartado`): NINGUNA señal de error vacía el
    `response` sin antes preguntar si ya había texto capturado. La jerarquía se
    evalúa UNA vez arriba (`_mejor_fuente`) y todos los caminos de fallo la
    consultan. Cuando el texto entra por un camino de rescate se cuelga
    `res.rescate_motivo`, que viaja al shape y prensa convierte en QA grave.
    """
    # ── Fix 5 (v37) — fallo de spawn GENUINO ─────────────────────────────────
    # `estado == "ERROR_SPAWN"` sin `spawn_ok` es el único caso en que podemos
    # AFIRMAR que agy no arrancó. Con `spawn_ok` es una excepción de fase (el
    # proceso corrió y pudo haber generado): se trata más abajo.
    if res.estado == "ERROR_SPAWN" and not getattr(res, "spawn_ok", False):
        return ("ERROR", "", res.error or "spawn_agy_fallo", False, "vacio")

    excepcion_fase = (
        res.estado == "EXCEPCION_FASE"
        or (res.estado == "ERROR_SPAWN" and getattr(res, "spawn_ok", False)))

    texto, fuente, fin_presente = _mejor_fuente(res)
    hay_texto = bool(texto)
    hubo_gen  = getattr(res, "hubo_generacion", None)

    # 0) Cuota agotada (HTTP 429): si `_detectar_cuota_en_conversacion` halló
    #    el RESOURCE_EXHAUSTED en la .db de esta corrida.
    #
    #    v37 — Fix 1: el 429 puede llegar DESPUÉS de que la corrida terminó bien
    #    (job69869: `estado=OK_FIN`, 21.812 ch completos, y el 429 en el step de
    #    error posterior). Hasta v36 esto devolvía `response=""` y el worker no
    #    tenía con qué reparar: la página se perdía y el job volvía a la cola.
    #    Ahora, si hay texto, se devuelve OK **sin perder la señal de cuota**:
    #    `shape_salida` marca `cuota_agotada=true` mirando `res.cuota_detectada`,
    #    no el veredicto, así el cooldown y la rotación siguen disparando. Son
    #    dos efectos independientes, no un if/else.
    if getattr(res, "cuota_detectada", False):
        if hay_texto and fuente in _FUENTES_RESCATABLES:
            res.rescate_motivo = _motivo_rescate("cuota_post_generacion", fuente, res)
            return ("OK", texto, None, fin_presente, fuente)
        return ("CUOTA", "",
                "cuota_agotada: 429 RESOURCE_EXHAUSTED (Individual quota reached)",
                False, "cuota")

    # 0.2) EXCEPCIÓN DE FASE (v37 — Fix 5). El proceso arrancó y una fase
    #      posterior tiró excepción (`EOFError: Pty is closed` del job 70926).
    #      Es INFRAESTRUCTURA: no depende del contenido de la página, así que
    #      es reintentable. Pero primero se rescata: si el PTY murió durante el
    #      drenaje, el modelo pudo haber terminado y escrito la .db.
    if excepcion_fase:
        if hay_texto and fuente in _FUENTES_RESCATABLES:
            res.rescate_motivo = _motivo_rescate("excepcion_fase", fuente, res)
            return ("OK", texto, None, fin_presente, fuente)
        # El sufijo pre/post generación NO cambia el veredicto (el cap de 3
        # reintentos acota el gasto y el fallo es del PTY, no del backend), pero
        # queda en `api_errorlog` para que un patrón postgen sea visible.
        sufijo = "postgen" if hubo_gen else "pregen"
        return ("TRANSITORIO", "",
                (f"excepcion_fase_{sufijo}: agy arrancó y una fase posterior tiró "
                 f"excepción ({res.error or 'sin detalle'}); no quedó texto "
                 f"rescatable [estado={res.estado} dur={res.duracion_seg}s]. "
                 f"Es infraestructura (PTY/portapapeles), reintentable"),
                False, "vacio")

    # 0.5) MODO `paste` (bump v34): la fuente del texto es la .db, NO la pantalla.
    #      WORKAROUND TEMPORAL — bug upstream agy #735 (LS 1.1.10 manda
    #      inline_data vacío cuando el modelo abre una imagen con view_file →
    #      INVALID_ARGUMENT 400).
    #      https://github.com/google-antigravity/antigravity-cli/issues/735
    #      El TUI renderiza el markdown y DESTRUYE los tags `<ilegible>`/
    #      `<dudoso>` (medido: 271 vs 0), así que el grid NO es fallback válido:
    #      persistirlo metería texto sin tags en la BD en silencio. Por eso este
    #      bloque devuelve SIEMPRE (no cae a las ramas 1-3 de pantalla).
    #      `res.response_db` lo puebla main() con `_extraer_transcripcion_db`.
    if getattr(res, "paste_modo", False):
        if res.estado == "PASTE_SIN_CHIP":
            return ("TRANSITORIO", "",
                    ("paste_sin_chip: el adjunto no apareció en el TUI tras el "
                     "paste (ni en el reintento) — agy se cerró SIN ENVIAR el "
                     "mensaje ⇒ cero cuota consumida. Causa típica: algo pisó el "
                     "portapapeles en la ventana crítica. Reintentable"),
                    False, "vacio")
        if hay_texto:
            # Camino normal del modo paste. Sólo se etiqueta como rescate si el
            # texto vino truncado (`parcial_sin_fin`) o con razonamiento
            # mezclado (`thinking_mezclado`): ahí PHP suma el QA grave para que
            # un humano lo confirme. Texto limpio ⇒ motivo "" ⇒ sin QA extra.
            res.rescate_motivo = _motivo_rescate("", fuente, res)
            return ("OK", texto, None, fin_presente, fuente)
        if getattr(res, "loop_detectado", False):
            # Excepción explícita: si el detector de loop disparó, el motivo
            # correcto es el de siempre (el CLI se colgó repitiendo), no el de
            # la .db vacía. Mismo camino que la rama 3.6.
            return ("TRANSITORIO", "",
                    (f"loop_salida_agy: agy quedó repitiendo "
                     f"{(getattr(res, 'loop_unidad', '') or '?')!r} y la .db no "
                     f"dejó transcripción [estado={res.estado} "
                     f"dur={res.duracion_seg}s]"),
                    False, "vacio")

        # ── Fix 3 (v37): dejar de colapsar TODO fallo de paste en un solo
        #    rótulo. Hasta v36, cualquier corrida sin texto salía como
        #    `db_sin_texto_paste` + TRANSITORIO, con dos consecuencias medidas:
        #    (a) un `eligibility_429` perdía el freno corto de cuenta y el
        #        worker martillaba un backend que decía RESOURCE_EXHAUSTED;
        #    (b) un fallo POSTERIOR a la generación se reintentaba a ciegas —
        #        el patrón exacto que el v33 arregló en la otra rama y que el
        #        2026-08-03 quemó 724 jobs contra un INVALID_ARGUMENT 400
        #        determinístico.
        #    Ahora se consultan las dos evidencias que ya existían y nadie leía:
        #    la sub-causa de la .db (`_diagnostico_errores_db`) y el gate de
        #    generación del `--log-file` (tri-estado: log ilegible → TRANSITORIO,
        #    conservador; colapsarlo a booleano reintroduce el bug del v33).
        diag         = getattr(res, "diag_db", None) or {}
        etiqueta     = str(diag.get("etiqueta") or "")
        reintentable = diag.get("reintentable")
        detalle      = str(diag.get("detalle") or "")[:200]
        det_txt      = f" Detalle de la .db: {detalle}" if detalle else ""
        ctx          = (f"[estado={res.estado} dur={res.duracion_seg}s "
                        f"generacion={hubo_gen}]")

        if etiqueta == "cuota_individual":
            # La .db dice cuota agotada aunque `_detectar_cuota_en_conversacion`
            # no la haya visto (p.ej. no pudo parsear el "Resets in …"). Vale
            # más el cooldown que un ERROR: el job vuelve a la cola sin consumir
            # intento y la cuenta descansa.
            return ("CUOTA", "",
                    f"cuota_agotada: firma 'Individual quota reached' en la .db "
                    f"de esta corrida.{det_txt}", False, "cuota")
        if reintentable is True:
            return ("TRANSITORIO", "",
                    (f"{etiqueta}: agy no dejó transcripción en la .db y la causa "
                     f"raíz es reintentable (sin consumo de generación o auth "
                     f"stale). {ctx}{det_txt}"),
                    False, "vacio")
        if reintentable is False:
            return ("ERROR", "",
                    (f"{etiqueta}: agy no dejó transcripción en la .db y la causa "
                     f"raíz es DETERMINÍSTICA — reintentar repite el gasto sin "
                     f"cambiar el resultado. {ctx}{det_txt}"),
                    False, "vacio")
        # Sin firma conocida (o firma ambigua) → manda el gate de generación.
        if hubo_gen is True:
            return ("ERROR", "",
                    (f"db_sin_texto_paste_postgen: agy llegó a llamar a "
                     f"streamGenerateContent (hubo consumo) y la .db no dejó par "
                     f"INICIO/FIN. NO es un fallo de arranque: reintentar repite "
                     f"el gasto. Terminal — mirar el bundle forense. "
                     f"{ctx}{det_txt}"),
                    False, "vacio")
        return ("TRANSITORIO", "",
                (f"db_sin_texto_paste: no se pudo extraer la transcripción de la "
                 f"conversation.db (sin step 15, sin par INICIO/FIN, .db ilegible "
                 f"o sin identidad probada) y en modo paste la pantalla NO es "
                 f"fallback válido (el TUI destruye los tags <ilegible>/<dudoso>). "
                 f"Sin generación registrada ⇒ reintentable. {ctx}{det_txt}"),
                False, "vacio")

    # 1-3) Jerarquía de fuentes (pantalla + .db) ya resuelta arriba por
    #      `_mejor_fuente`. La .db como fuente en `-p`/`-i` es camino de RESCATE
    #      (v37): el grid quedó vacío y el texto sobrevivió sólo en SQLite.
    if hay_texto:
        if fuente.startswith("db"):
            if res.estado == "TIMEOUT":
                camino = "timeout_con_db"
            elif res.estado in ("PROC_EXIT", "LOOP_DEGENERADO"):
                camino = "exit_con_db"
            else:
                camino = "db_rescate"
            res.rescate_motivo = _motivo_rescate(camino, fuente, res)
        return ("OK", texto, None, fin_presente, fuente)

    # 3.5) Fallo de ARRANQUE transitorio (pre-generación → sin cuota consumida):
    #      backend 500 al resolver el modelo, auth/keyring timeout, etc. Sólo
    #      llega acá si NO hubo response utilizable (agy murió antes de
    #      transcribir). El gate duro (sin `streamGenerateContent`) ya se validó
    #      en _detectar_arranque_transitorio. El worker lo reintenta (cap) en
    #      vez de fail-fast a ERROR; si agotó el cap, cae a ERROR terminal.
    if getattr(res, "transitorio_detectado", False):
        return ("TRANSITORIO", "",
                (f"arranque_transitorio: {res.transitorio_motivo or 'pre_generacion'} "
                 f"(agy no llegó a generar → sin cuota consumida) "
                 f"[estado={res.estado} exit={res.exitstatus}]"),
                False, "vacio")

    # 3.6) LOOP degenerado que además dejó el grid VACÍO (bump v31). El camino
    #      normal del loop no pasa por acá: el history trae el stream repetido
    #      (≥ MIN_CONTENT_LEN) y sale por la rama 2, que es donde `shape_salida`
    #      lo procesa. Esta rama es la defensiva — si pyte no reconstruyó nada,
    #      el resultado sigue siendo "agy se colgó repitiendo", que es
    #      REINTENTABLE, no un ERROR terminal. Sin esto caería al `else` de la
    #      rama 4 como `sin_datos_utiles` y la página moriría sin reintento.
    if getattr(res, "loop_detectado", False):
        return ("TRANSITORIO", "",
                (f"loop_salida_agy: agy quedó repitiendo "
                 f"{(getattr(res, 'loop_unidad', '') or '?')!r} y no dejó nada "
                 f"utilizable en el grid [estado={res.estado} "
                 f"dur={res.duracion_seg}s]"),
                False, "vacio")

    # 4) Genuino fallo: nada utilizable en el grid.
    err_parts = []
    if res.estado == "TIMEOUT":
        err_parts.append(f"timeout_sin_datos_utiles (dur={res.duracion_seg}s)")
    elif res.estado == "PROC_EXIT":
        err_parts.append(f"agy_exit_sin_datos (exitstatus={res.exitstatus})")
    elif res.estado == "QUIESCENT_NO_MARKER":
        err_parts.append(f"quiescent_sin_datos_utiles (dur={res.duracion_seg}s)")
    else:
        err_parts.append(f"sin_datos_utiles (estado_captura={res.estado})")
    if res.notas:
        err_parts.append("notas=" + "|".join(res.notas))
    return ("ERROR", "", "; ".join(err_parts), False, "vacio")


def shape_salida(
    res: CaptureResult,
    args,
    t0_total: float,
    ws_info: dict,
    tools_used: list,
    tokens: dict,
    zombis: int,
    modelo_pedido: str,
) -> dict:
    """Compone el JSON que escribe `--salida-json`. Shape espejo de
    `_aistudioShapeRespuesta()` (lib_aistudio.php:611-671) + extras agy.

    Cambios plan #3:
      §2: veredictos = OK | ERROR | CUOTA (sin SIN_FIN).
      §3: response = partial_from_ini (FIN incluido si existe; PHP recorta).
      §4: tools_used parseado del TUI; websearch_fuente expone tools_used/heurística.
      §5: tokens_* desde statusLine si está configurado (sino 0).
      §6: cap 256 KiB; stdout_largo_sospechoso si supera 180 KiB.

    Cambio 2026-06-21 (persistir antes de discriminar):
      `response` cae a history_text/screen_snapshot si no hubo INICIO;
      `fuente_response` reporta el origen para que PHP decida qué QA bits
      flaggear sin necesidad de re-inferir desde el contenido.
    """
    veredicto, response, error, fin_presente, fuente_response = decidir_veredicto(res)
    ok = (veredicto == "OK")

    # ── EL RAW NUNCA SE TIRA (v37) ───────────────────────────────────────────
    # Si el veredicto no es OK y no hay `response`, pero la .db SÍ tenía un
    # segmento que no pasó las guardas (razonamiento del modelo citando los
    # marcadores, o una .db que no es de esta corrida), ese texto viaja igual —
    # con un encabezado que dice por qué no se persistió. El worker lo escribe
    # en `api_rawresponse` (rama !ok) y NUNCA llega a `entradas`: la rama de
    # persistencia es la de ok=true. Sin esto, el único lugar donde sobrevivía
    # era el bundle forense, y sólo si el bundle llegaba a archivarse.
    if not ok and not (response or "").strip():
        _cand = (getattr(res, "db_candidato", None) or "").strip()
        if _cand:
            _why = getattr(res, "db_rechazo", "") or "no_persistido"
            response = (
                f"[texto_no_persistido: {_why}] Segmento hallado en la .db de "
                f"conversación que NO se persistió como transcripción. "
                f"Auditoría únicamente — verificar el bundle forense antes de "
                f"reinyectarlo a mano.\n{_cand}"
            )

    # ── LOOP DEGENERADO DE SALIDA (bump v31) ──────────────────────────────────
    # `capturar()` cortó porque agy quedó repitiendo una unidad corta. Acá se
    # decide qué hacer con lo capturado, sobre el `response` REAL (el que se
    # persistiría), no sobre el stream crudo:
    #
    #   1) Se le saca al `response` el sufijo periódico. Es chrome del CLI, no
    #      output del modelo: en los 35 casos medidos el `agy_logfile.log` no
    #      tiene un `streamGenerateContent` posterior al arranque del loop.
    #   2) Si lo que queda NO llega a UMBRAL_LONGITUD_SOSPECHOSA ⇒ no hay nada
    #      rescatable (35/35 de los casos observados: quedaban ≤ 6 chars) ⇒
    #      veredicto=TRANSITORIO y el worker RE-ENCOLA. Evidencia de que sirve:
    #      las 2 páginas que se re-encolaron a mano salieron OK al reintento
    #      (edi 4404 p1 y 4445 p2, 175s y 144s).
    #   3) Si queda una transcripción de largo plausible ⇒ se persiste SIN el
    #      loop, con `loop_detectado=true` para que el worker inyecte el QA
    #      grave `looping`. Que salten los demás motivos que tengan que saltar.
    #
    # EXCEPCIÓN EXPLÍCITA AL INVARIANTE de v19 ("sólo se reintenta lo que NO
    # consumió cuota"): acá agy SÍ llamó al LLM (4 `streamGenerateContent` en el
    # caso testigo, 408 tokens de output). Se reintenta igual porque (a) es
    # esporádico y el reintento funciona, (b) el consumo es marginal frente a
    # los ~9,5 min de slot que se ahorran, (c) el cap de 3 acota el peor caso.
    # Si tocás `_agy_reintentos` o `agyAccionResultado`, tené presente que este
    # motivo NO cumple el gate de `streamGenerateContent`.
    #
    # Sólo se recorta el sufijo periódico: es la forma observada (generación
    # primero, loop después). Un loop EN EL MEDIO sobreviviría al recorte, pero
    # `loop_detectado` viaja igual ⇒ el QA grave lo marca y la página va a
    # revisión. Ver notas/motor_agy.md §"Bump v31".
    loop_detectado    = bool(getattr(res, "loop_detectado", False))
    loop_chars_resp   = 0
    loop_unidad       = str(getattr(res, "loop_unidad", "") or "")
    firma_loop        = None   # → firma_transitorio_motivo_forzado, ver abajo
    if ok and loop_detectado:
        _info_resp = detectar_loop_degenerado(response)
        if _info_resp["detectado"]:
            loop_chars_resp = _info_resp["chars_loop"]
            loop_unidad     = _info_resp["unidad"] or loop_unidad
            response = response[: len(response) - loop_chars_resp].rstrip()
        if len(response.strip()) < UMBRAL_LONGITUD_SOSPECHOSA:
            # Nada rescatable. NO devolvemos el stream de basura como
            # `response`: el worker lo escribiría tal cual en
            # `api_calls.api_rawresponse` (una vez por reintento). El raw
            # completo queda en `stdout_raw` y en el bundle forense archivado.
            response = (
                f"[loop_salida_agy] {loop_chars_resp} chars de la unidad "
                f"{loop_unidad!r} repetida; sin contenido rescatable. "
                f"Raw completo en el bundle debug de esta corrida."
            )
            ok = False
            veredicto = "TRANSITORIO"
            error = (
                f"loop_salida_agy: agy quedó repitiendo {loop_unidad!r} "
                f"({getattr(res, 'loop_repeticiones', 0)} repeticiones, "
                f"{getattr(res, 'loop_chars', 0)} chars) tras terminar de generar; "
                f"captura cortada a los {res.duracion_seg}s sin esperar el timeout. "
                f"Sin nada rescatable → reintentable"
            )
        else:
            error = None

    # Etiqueta del reintento. Cubre las DOS vías por las que un loop llega a
    # TRANSITORIO: el bloque de arriba (había stream pero nada rescatable) y la
    # rama 3.6 defensiva de `decidir_veredicto` (el grid quedó vacío). Sin esto
    # la segunda vía llegaría al worker con `transitorio_motivo` vacío.
    if loop_detectado and veredicto == "TRANSITORIO":
        firma_loop = "loop_salida_agy"

    longitud_sospechosa = (
        ok and 0 < len(response) < UMBRAL_LONGITUD_SOSPECHOSA
    )

    # ── DESAMBIGÜE DE `exploracion_agy` (bump v25) ────────────────────────────
    # Contexto: hasta v24 el único check post-decidir_veredicto era el de
    # "exploracion_agy" (fuente=history + !fin + longitud_sospechosa). Ese
    # check nació para la firma del job 7123 (response conversacional "Estoy
    # buscando los archivos…"). Pero en producción el mismo check terminó
    # etiquetando bajo `exploracion_agy` al menos 3 firmas ajenas — todas con
    # response corto sin marcadores → matchean los mismos 3 flags:
    #   A. jetski headless deny  → agy en `-p` auto-deniega una tool (read_file,
    #      command, …) y emite en el response el mensaje literal
    #      "jetski: no output produced — a tool required the \"<tool>\" permission…".
    #      TERMINAL: no vale reintentar sin cambiar SANDBOX_SETTINGS o el prompt.
    #   B. executor terminated → agy muere pre-arranque con `printmode.go`
    #      "run ended with error and no response: Agent execution terminated
    #      due to error." (response literal 47 chars). Sub-causas del
    #      agy_logfile.log: "neither PlanModel nor RequestedModel specified"
    #      (bug del CLI), UNAUTHENTICATED 401 (auth stale), "model unreachable"
    #      (red), HTTP 502. TRANSITORIO: reintento suele andar (el harness
    #      del bump v19 ya lo soporta).
    #   C. auth expired → response arranca con "Authentication required." o
    #      contiene "Please visit the URL to log in". TERMINAL: la cuenta
    #      necesita relogin manual; reintentar loopea.
    #   E. eligibility 429 (bump v29) → response literal "Error: Eligibility check
    #      failed: RESOURCE_EXHAUSTED (code 429): …". Rate limit del backend de
    #      code assist en el arranque (fetchUserInfo/loadCodeAssist), sin llamada
    #      al LLM. TRANSITORIO: reintento con backoff corto. Detalle abajo.
    # Estos chequeos van ANTES del de exploracion_agy, así el original queda
    # como último resort para el caso conversacional real. Retrocompat:
    # - Un caso que hoy cae en exploracion_agy y no matchea A/B/C sigue
    #   cayendo en exploracion_agy con la misma etiqueta.
    # - El shape no cambia claves ni tipos.
    # - veredicto=TRANSITORIO ya está mapeado por lib_worker_policy.php
    #   (agyAccionResultado → RETRY → AgyTransitorioException).
    # ── Qué de este desambigüe aplica en modo `paste` (bump v34) ─────────────
    # El `response` de paste viene de la .db, no de la pantalla. Repaso guard
    # por guard, para que no queden checks corriendo "de casualidad":
    #   A jetski / B executor terminated / C auth expired / E eligibility 429 →
    #     SÍ aplican. Son matches sobre el LITERAL del response; si agy murió
    #     temprano no hay step 15 con par INICIO/FIN ⇒ en paste esos casos salen
    #     antes por `db_sin_texto_paste` (TRANSITORIO). Se dejan igual: cuestan
    #     nada y cubren el caso hipotético de que agy escriba una de esas
    #     firmas DENTRO de los marcadores.
    #   D exploracion_agy → NO aplica nunca: está gateado a
    #     `fuente_response == "history"` y en paste la fuente es "db". Es
    #     correcto que no aplique — la exploración conversacional se detectaría
    #     como ausencia de par INICIO/FIN, o sea `db_sin_texto_paste`.
    #   longitud_sospechosa → SÍ aplica (mide el response real que se persiste).
    #   loop degenerado (bloque de arriba) → SÍ aplica: `loop_detectado` viaja
    #     al worker para el QA grave `looping`, pero el recorte del sufijo
    #     periódico no va a encontrar nada en un texto que salió de la .db.
    resp_stripped = (response or "").strip()
    # v31: el bloque de loop degenerado corre ANTES que este desambigüe y puede
    # haber forzado ya veredicto=TRANSITORIO. Se siembran las dos variables con
    # su resultado (None si no hubo loop) en vez de pisarlas con None — si no,
    # el motivo se perdería y el worker reintentaría sin etiqueta.
    firma_transitorio_motivo_forzado = firma_loop
    firma_detectada = "F_loop_salida_agy" if firma_loop else None
    if ok and resp_stripped.startswith("jetski:"):
        m = re.search(r'required the "([^"]+)" permission', resp_stripped)
        tool_denegada = m.group(1) if m else "desconocida"
        firma_detectada = "A_jetski"
        ok = False
        veredicto = "ERROR"
        error = (
            f"jetski_headless_deny: agy en -p auto-denegó la tool '{tool_denegada}' "
            f"(headless no puede pedir confirmación interactiva). No reintentable "
            f"sin agregar 'command(...)' u otro allow en SANDBOX_SETTINGS, o reforzar "
            f"el prompt para que el modelo no invoque esa tool"
        )
    elif ok and resp_stripped == "Error: Agent execution terminated due to error.":
        # ── B: executor terminated. SE PARTE EN DOS (bump v33, 2026-08-03) ────
        # Hasta v32 esta rama forzaba TRANSITORIO a secas, saltándose el gate
        # duro de v19 ("sólo se reintenta lo que NO llamó al LLM"). La etiqueta
        # `prearranque` era una AFIRMACIÓN no verificada, y el 2026-08-03 se
        # demostró falsa: el backend empezó a devolver INVALID_ARGUMENT (400)
        # DESPUÉS de 2-3 `streamGenerateContent` (403 tokens de output, 31 k
        # totales por corrida). Como el fallo era determinístico, el reintento
        # repetía el gasto exacto: 724 jobs × 3 = 2176 corridas idénticas, ~15 h
        # de slot y la cola entera quemada.
        #
        # El gate ahora se consulta de verdad:
        #   - sin `streamGenerateContent`  → TRANSITORIO (comportamiento v25).
        #   - CON `streamGenerateContent`  → ERROR terminal: hubo consumo, y un
        #     fallo posterior a la generación no se arregla reintentando.
        #   - log ilegible (None)          → TRANSITORIO (conservador = status quo).
        _hubo_gen = getattr(res, "hubo_generacion", None)
        _subcausa = (getattr(res, "subcausa_log", "") or "").strip()
        _sub_txt  = f" Sub-causa del log: {_subcausa}." if _subcausa else ""
        if _hubo_gen:
            firma_detectada = "B2_executor_terminated_postgen"
            ok = False
            veredicto = "ERROR"
            error = (
                "executor_terminated_postgen: agy llegó a llamar a "
                "streamGenerateContent (hubo generación y consumo de cuota) y "
                "RECIÉN DESPUÉS murió con 'Agent execution terminated due to "
                "error.'. NO es un fallo de arranque: reintentar repite el gasto "
                "sin cambiar el resultado. Terminal — mirar la sub-causa y el "
                "bundle forense." + _sub_txt
            )
        else:
            firma_detectada = "B_executor_terminated"
            ok = False
            veredicto = "TRANSITORIO"
            firma_transitorio_motivo_forzado = "executor_terminated_prearranque"
            error = (
                "agy_terminado_prearranque: response literal 'Error: Agent execution "
                "terminated due to error.' y el log NO tiene streamGenerateContent "
                "(agy salió antes de generar → sin consumo de cuota). Sub-causas "
                "habituales: 'neither PlanModel nor RequestedModel', UNAUTHENTICATED "
                "(401), 'model unreachable' (red), HTTP 502. Reintentable." + _sub_txt
            )
    elif ok and ("eligibility check failed" in resp_stripped[:200].lower()
                 and "resource_exhausted" in resp_stripped.lower()):
        # E. eligibility check 429 (bump v29). agy muere en el chequeo de
        #    elegibilidad del print mode — ANTES de cualquier generación:
        #      http_helpers.go: Failed to make code assist backend request
        #        (…v1internal:fetchUserInfo): 429 RESOURCE_EXHAUSTED
        #      printmode.go: Print mode: eligibility check failed: …
        #    El response literal es una línea de ~111 chars, así que caía en
        #    `exploracion_agy` (fuente=history + !fin + corto) y terminaba
        #    ERROR fail-fast → la página moría sin reintento y con un
        #    diagnóstico falso ("agy exploró con run_command").
        #
        #    Evidencia (prensa, agy_debug 2026-07-24, 9 casos): `streamGenerateContent`
        #    ausente en el agy_logfile.log en los 9 → agy NUNCA llamó al LLM →
        #    cuota de generación NO consumida → reintentable (misma regla que B).
        #
        #    NO confundir con cuota agotada real: ésa la detecta
        #    `_detectar_cuota_en_conversacion` sobre la .db y sale por
        #    veredicto=CUOTA mucho antes (texto "Individual quota reached",
        #    reset de horas/días). Acá el proceso ni siquiera llegó a abrir
        #    conversación. Por eso el match exige RESOURCE_EXHAUSTED explícito:
        #    un "eligibility check failed" con OTRO código (401/403 = cuenta
        #    sin elegibilidad, suscripción vencida) NO es reintentable y sigue
        #    cayendo en el `exploracion_agy` de siempre (status quo conservador).
        firma_detectada = "E_eligibility_429"
        ok = False
        veredicto = "TRANSITORIO"
        firma_transitorio_motivo_forzado = "eligibility_429"
        error = (
            "eligibility_429: el chequeo de elegibilidad del print mode falló con "
            "RESOURCE_EXHAUSTED (429) — rate limit del backend de code assist en el "
            "ARRANQUE, sin llamada al LLM (sin consumo de cuota de generación). "
            "Reintentable con backoff corto; NO es cuota agotada (ésa sale por "
            "veredicto=CUOTA con 'Individual quota reached')"
        )
    elif ok and (resp_stripped.startswith("Authentication required")
                 or "Please visit the URL to log in" in resp_stripped[:400]):
        firma_detectada = "C_auth_expired"
        ok = False
        veredicto = "ERROR"
        error = (
            "auth_agy_expirada: la cuenta agy pide re-login interactivo "
            "(response arranca con 'Authentication required' o contiene 'Please visit "
            "the URL to log in'). Requiere /logout + relogin manual de la cuenta"
        )
    # ── Firma original de exploración conversacional (bump v12) ───────────────
    # Sólo llega acá si NINGUNA firma específica arriba matcheó. Los 3 flags
    # concurrentes identifican unívocamente el mensaje conversacional del
    # modelo ("Estoy buscando los archivos imagen.jpg y prompt.md…") vs una
    # transcripción real (incluso página casi vacía lleva INICIO/FIN):
    #   - fuente_response == "history": no hubo INICIO/FIN
    #   - not fin_presente: tampoco el cierre suelto
    #   - longitud_sospechosa: response < UMBRAL_LONGITUD_SOSPECHOSA
    # Caso 2026-06-25 (job 7123 prensa, edi 2961 p4): response="Estoy
    # buscando los archivos imagen.jpg y prompt.md en tu sistema…" (198
    # chars). Ver notas/motor_agy.md §Permisos (los denies de tool() son
    # no-op; único lever real es prompt+modelo). Forzar ERROR acá protege a
    # TODOS los consumidores del core (prensa + v3); response queda en el
    # dict para que PHP lo logue en api_rawresponse y el operador audite.
    if ok and fuente_response == "history" and not fin_presente and longitud_sospechosa:
        firma_detectada = "D_exploracion_conversacional"
        ok = False
        veredicto = "ERROR"
        error = (
            f"exploracion_agy: response cae a history (sin INICIO/FIN), "
            f"len={len(response)} < UMBRAL={UMBRAL_LONGITUD_SOSPECHOSA}; "
            f"agy probablemente exploró con run_command en vez de transcribir"
        )

    stdout_raw_full = res.console_raw or ""
    stdout_largo_sospechoso = len(stdout_raw_full) > UMBRAL_STDOUT_SOSPECHOSO
    stdout_capado = stdout_raw_full
    if len(stdout_capado) > STDOUT_CAP:
        # Conservar inicio + cola (lo último es lo más informativo en console_raw)
        head = stdout_capado[: STDOUT_CAP // 2]
        tail = stdout_capado[-STDOUT_CAP // 2:]
        stdout_capado = (
            head + f"\n…[truncado {len(stdout_raw_full)-STDOUT_CAP} bytes]…\n" + tail
        )

    return {
        "ok": ok,
        "response": response,
        "error": error,
        "veredicto": veredicto,
        "engine": "agy",
        # Shape espejo aistudio: campos vacíos pero presentes
        "stats": [], "tools": None,
        "tokens_input":   int(tokens.get("tokens_input", 0)),
        "tokens_output":  int(tokens.get("tokens_output", 0)),
        "tokens_thought": int(tokens.get("tokens_thought", 0)),
        "tokens_cached":  int(tokens.get("tokens_cached", 0)),
        "tokens_total":   int(tokens.get("tokens_total", 0)),
        "session_id": None,
        "stdout_raw": stdout_capado,
        "stderr_raw": "",
        # v37 — Fix 1: la cuota es un HECHO de la corrida, no un veredicto. Una
        # corrida puede terminar OK y recibir el 429 después (job69869): ahí el
        # texto se persiste Y la cuenta entra en cooldown. Son dos efectos
        # independientes; leer esto como `veredicto === 'CUOTA'` los ata y hace
        # perder uno de los dos. `agyAccionResultado` sigue mapeando a CUOTA en
        # la rama de fallo, y el camino OK lo consume el worker aparte.
        "cuota_agotada": (veredicto == "CUOTA"
                          or bool(getattr(res, "cuota_detectada", False))),
        # Segundos hasta el reset de cuota (parseado de "Resets in 13m27s" en el
        # 429 de la .db). 0 si no se pudo parsear o no aplica → el worker usará
        # el default `agy_cooldown_seg` como fallback.
        "cuota_reset_seg": int(getattr(res, "cuota_reset_seg", 0) or 0),
        # Motivo del fallo de arranque transitorio (pre-generación) cuando
        # veredicto==TRANSITORIO; "" si no aplica. Forense (viaja al bundle
        # debug). El worker rutea el reintento por veredicto, no por este campo.
        # v25: si el desambigüe de arriba forzó veredicto=TRANSITORIO por firma
        # específica (executor_terminated_prearranque), gana esa etiqueta sobre
        # el motivo default de _detectar_arranque_transitorio.
        "transitorio_motivo": (
            firma_transitorio_motivo_forzado
            or str(getattr(res, "transitorio_motivo", "") or "")
        ),
        # Extras agy (consumidos por lib_agy.php + worker)
        "fuente_response": fuente_response,
        "fin_presente": bool(fin_presente),
        "websearch_detectado": bool(ws_info.get("detectado")),
        "websearch_patrones": ws_info.get("patrones") or [],
        "websearch_fuente": ws_info.get("fuente") or "none",
        "tools_used": tools_used or [],
        "longitud_sospechosa": bool(longitud_sospechosa),
        "stdout_largo_sospechoso": bool(stdout_largo_sospechoso),
        "estado_captura": res.estado,
        "duracion_seg": round(time.time() - t0_total, 2),
        "bytes_leidos": res.bytes_leidos,
        "zombis_barridos": zombis,
        "modelo_pedido": modelo_pedido or "",
        # Forense statusLine (no se guarda en api_calls; sí va al bundle debug)
        "statusline_disponible": bool(tokens.get("statusline_disponible", False)),
        "context_window_size": int(tokens.get("context_window_size", 0)),
        "used_percentage": float(tokens.get("used_percentage", 0.0)),
        "plan_tier": str(tokens.get("plan_tier", "")),
        # Cuota en tiempo real desde el statusLine (feed que reemplaza el
        # agy_usage_check scheduled). None si no vino → PHP gatea !== null.
        "quota_weekly_pct_usado": tokens.get("quota_weekly_pct_usado"),
        "quota_weekly_reset_seg": tokens.get("quota_weekly_reset_seg"),
        "quota_h5_pct_usado":     tokens.get("quota_h5_pct_usado"),
        "quota_h5_reset_seg":     tokens.get("quota_h5_reset_seg"),
        "account_email":          tokens.get("account_email"),
        # Chequeo fáctico "agy adjuntó imagen.jpg al contexto multimodal"
        # (bump v28, 2026-07-23). Firma unívoca en `steps` de la .db de
        # conversación (ver `_parsear_conversacion_db`). Valores:
        #   True  → imagen efectivamente cargada al contexto.
        #   False → DB legible pero sin la firma → agy transcribió (o dijo
        #           que no pudo) SIN haber visto la imagen → posible
        #           alucinación → worker inyecta QA grave NO_CARGO_IMAGEN.
        #   None  → DB no legible / no encontrada → unknown → worker NO
        #           inyecta QA (evita falso positivo).
        # Ver notas/motor_agy.md §"Bump v28".
        "imagen_cargada_ok": (
            None if getattr(res, "imagen_cargada_ok", None) is None
            else bool(res.imagen_cargada_ok)
        ),
        # Citations del checker de atribución/recitación de Google (bump v30,
        # 2026-07-24). Lista de URIs de `CitationSource` halladas en la .db de
        # ESTA corrida (ver `_CITATION_URI_RE`). Semántica para el consumidor:
        #   []      → no hubo citations (o no se pudo leer la .db). Sin acción.
        #   [urls…] → el backend atribuyó tramos del texto generado a material
        #             indexado ⇒ el modelo recitó de memoria. Combinado con un
        #             corte de stream (`fin_presente=false`) es la firma de la
        #             recitación enmascarada. **NO es evidencia de acceso web.**
        # Ver notas/bloqueo_qa_recurrente.md §"Evidencia forense".
        "citation_urls": list(getattr(res, "citation_urls", None) or []),
        # LOOP degenerado de salida (bump v31, 2026-07-31). agy quedó repitiendo
        # una unidad corta después de terminar de generar (ver §"Bump v31").
        #   loop_detectado=False → camino normal, el resto de las claves en 0/"".
        #   loop_detectado=True + veredicto=TRANSITORIO → no quedaba nada
        #     rescatable; el worker re-encola (motivo `loop_salida_agy`).
        #   loop_detectado=True + ok=True → SÍ quedaba transcripción: `response`
        #     ya viene SIN el sufijo del loop y el worker inyecta el QA grave
        #     `looping`. `loop_chars_response` dice cuánto se recortó.
        # Consumidor viejo (lib_agy.php ≤ v30) ignora estas claves → no-op.
        "loop_detectado":        bool(loop_detectado),
        "loop_unidad":           loop_unidad,
        "loop_repeticiones":     int(getattr(res, "loop_repeticiones", 0) or 0),
        "loop_chars":            int(getattr(res, "loop_chars", 0) or 0),
        "loop_chars_response":   int(loop_chars_resp),
        # ── MODO `paste` (bump v34, 2026-08-05) ───────────────────────────────
        # WORKAROUND TEMPORAL — bug upstream agy #735 (LS 1.1.10 manda
        # inline_data vacío cuando el modelo abre una imagen con view_file →
        # INVALID_ARGUMENT 400).
        # https://github.com/google-antigravity/antigravity-cli/issues/735
        # Estas claves viajan SIEMPRE (con default) para no romper consumidores
        # viejos; en `interactive`/`print` valen False/0/None.
        #   paste_chip_detectado → el TUI mostró el chip `📎 N media attached`.
        #     False + estado_captura="PASTE_SIN_CHIP" ⇒ NO se envió el mensaje
        #     (cero cuota) y el veredicto es TRANSITORIO/`paste_sin_chip`.
        #   paste_reintentos     → cuántas veces hubo que re-pegar (0 ó 1).
        #   clipboard_restaurado → el portapapeles del usuario volvió a su texto
        #     previo. False sin haber pegado nunca es lo normal; False DESPUÉS
        #     de pegar es un problema operativo a mirar en el bundle.
        #   media_en_log         → `media=N` del `--log-file`; ≥1 confirma que
        #     la imagen viajó como media del mensaje. None = no verificable.
        "paste_chip_detectado": bool(getattr(res, "paste_chip_detectado", False)),
        "paste_reintentos":     int(getattr(res, "paste_reintentos", 0) or 0),
        "clipboard_restaurado": bool(getattr(res, "clipboard_restaurado", False)),
        "media_en_log": (
            None if getattr(res, "media_en_log", None) is None
            else int(res.media_en_log)
        ),
        # ── RESCATE DE TEXTO (v37, proyecto `agy_texto_descartado`) ───────────
        # `rescate_motivo` != "" ⇒ el texto que se está persistiendo NO vino por
        # el camino normal: entró por un camino que hasta v36 lo tiraba. Valores
        # observables (se combinan con `+`):
        #   cuota_post_generacion → la corrida terminó y el 429 llegó después.
        #   timeout_con_db / exit_con_db → el grid quedó vacío y el texto estaba
        #     sólo en la .db de la conversación.
        #   excepcion_fase        → una fase posterior al spawn tiró excepción.
        #   parcial_sin_fin       → el texto vino truncado (INICIO sin FIN).
        # prensa lo convierte en el QA grave `texto_rescatado` ⇒ la página cae en
        # revisión humana en vez de mezclarse con las transcripciones sanas, y el
        # bundle forense se archiva en vez de borrarse.
        "rescate_motivo": str(getattr(res, "rescate_motivo", "") or ""),
        # Forense de la .db (v37). `db_identidad_ok` tri-estado: True = la .db es
        # de ESTA corrida (cascade_id == uuid del logfile); False = es de otra
        # conversación; None = no verificable. Con != True no se persiste texto
        # NI se propagan las señales derivadas (imagen_cargada, websearch,
        # citations): viajan en neutro.
        "db_identidad_ok": (
            None if getattr(res, "db_identidad_ok", None) is None
            else bool(res.db_identidad_ok)
        ),
        "db_rechazo":       str(getattr(res, "db_rechazo", "") or ""),
        "db_marcas_pagina": int(getattr(res, "db_marcas_pagina", 0) or 0),
        "fecha_iso": datetime.now().isoformat(timespec='seconds'),
    }


# ============================================================
# CLI
# ============================================================

def parse_args():
    p = argparse.ArgumentParser(
        description="Wrapper Antigravity CLI (agy) via ConPTY + pyte"
    )
    p.add_argument("--modo", default="transcribir", dest="modo",
                   choices=["transcribir", "usage"],
                   help="'transcribir' (default) = corre el pipeline OCR de imagen. "
                        "'usage' = abre la TUI de agy, manda `/usage` por stdin del PTY, "
                        "parsea el snapshot resultante para extraer cuota weekly/5h del "
                        "grupo GEMINI MODELS y devuelve un JSON con el snapshot. NO toca "
                        "scratch ni sandbox (skipea limpieza+staging) para no contaminar "
                        "la próxima transcripción. NO requiere --imagen/--prompt.")
    p.add_argument("--imagen", required=False, default=None,
                   help="Ruta absoluta a la imagen (.jpg/.png/.b64). "
                        "Requerido sólo en --modo=transcribir.")
    p.add_argument("--prompt", required=False, default=None,
                   help="Ruta absoluta al prompt.md completo (con addenda agy). "
                        "Requerido sólo en --modo=transcribir.")
    p.add_argument("--salida-json", required=True, dest="salida_json",
                   help="Ruta absoluta donde escribir el JSON con el resultado.")
    p.add_argument("--sandbox-dir", required=True, dest="sandbox_dir",
                   help="Sandbox PRE-TRUSTED en agy.trustedWorkspaces (cwd de agy).")
    p.add_argument("--home-dir", default=None, dest="home_dir",
                   help="Override del HOME para agy (lee/escribe ~/.gemini de acá). "
                        "v1: omitir (usa HOME del usuario que ejecuta).")
    p.add_argument("--modelo-agy", default="", dest="modelo_agy",
                   help='Modelo exacto para settings.json["model"] (lo escribe '
                        'el .py bajo el advisory lock del slot). En PRODUCCIÓN '
                        'el wiring PHP SIEMPRE lo pasa. Vacío = modo manual/'
                        'smoke (no toca el settings global; se usa lo que ya '
                        'estaba).')
    p.add_argument("--timeout", type=int, default=300,
                   help="Timeout total de captura (s). Default 300.")
    p.add_argument("--cols", type=int, default=2000,
                   help="Columnas del pseudo-terminal. Default 2000 para "
                        "minimizar wrap visual del TUI (cada wrap se convierte "
                        "en \\n real al reconstruir desde el grid de pyte). "
                        "EXCEPCIÓN --cmd-mode=paste: si viene el default se usa "
                        "220 (el TUI pinta cajas y con 2000 el chrome se "
                        "deforma); un valor explícito distinto del default se "
                        "respeta tal cual.")
    p.add_argument("--rows", type=int, default=100,
                   help="Filas (default 100; generoso para evitar truncado del "
                        "viewport). EXCEPCIÓN --cmd-mode=paste: el default se "
                        "sustituye por 80; un valor explícito se respeta.")
    p.add_argument("--grace", type=float, default=5.0,
                   help="Segundos de estabilidad tras candidato (default 5).")
    p.add_argument("--quiescent-seg", type=float, default=30.0, dest="quiescent_seg",
                   help="Segundos de bytes congelados SIN candidato para cerrar "
                        "agy (default 30). Cubre prompts que no emiten INICIO/FIN "
                        "(p.ej. familia `[tipo:]` de manuscritos-v3). Mientras agy "
                        "trabaja el spinner del TUI emite bytes, así que este "
                        "fallback no dispara prematuramente.")
    # (v18+: el bundle se vuelca siempre, no hay flag de gating. El lifecycle
    # del workdir lo decide PHP post-QA.)
    p.add_argument("--launch-mode", default="conpty", dest="launch_mode",
                   choices=["conpty"],
                   help="Modo de captura. Solo 'conpty' por ahora (plan B output.txt "
                        "no implementado: la mecánica ConPTY+pyte quedó validada).")
    p.add_argument("--agy-bin", default="agy", dest="agy_bin",
                   help="Ejecutable de agy (default: 'agy' en PATH).")
    p.add_argument("--cmd-i", default=CMD_I_DEFAULT, dest="cmd_i",
                   help="Mensaje del prompt que se envia a agy (con -i o -p). Default: "
                        "transcripcion de imagen. lib_agy.php lo override-ea en modo "
                        "sin-imagen (postproceso) para no pedir transcribir la imagen dummy. "
                        "SE IGNORA en --cmd-mode=paste: ahi el mensaje es un texto corto "
                        "fijo (PASTE_TEXTO_CORTO_TPL) que NO puede llevar '@' porque en el "
                        "TUI esa tecla abre el menu de menciones.")
    p.add_argument("--cmd-mode", default="interactive", dest="cmd_mode",
                   choices=["interactive", "print", "paste"],
                   help="interactive=-i (TUI legacy, default por compat); print=-p "
                        "(no-interactivo: agy imprime markdown crudo SIN inflar tablas y "
                        "cierra solo); paste (bump v34) = TUI sin -i/-p, con la imagen "
                        "adjuntada como media del mensaje via paste del portapapeles "
                        "(workaround del bug upstream #735; el texto se lee de la .db, "
                        "no de la pantalla). prensa opta a 'print'/'paste' via lib_agy; "
                        "v2 sigue en -i.")
    return p.parse_args()


def _escribir_salida_temprana(salida_json: Path, payload: dict) -> None:
    """Fallback de pre-flight (espejo del bloque de imagen/prompt no existe en
    transcribir_aistudio.py:1569-1583)."""
    try:
        salida_json.parent.mkdir(parents=True, exist_ok=True)
        salida_json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as e:
        sys.stderr.write(f"FATAL: no pude escribir salida temprana: {e}\n")


def main() -> int:
    args = parse_args()
    t0_total = time.time()

    # Branch del --modo=usage: pipeline corto (TUI + /usage + parser). NO
    # comparte path con transcripción (no toca scratch/sandbox/modelo global).
    if args.modo == "usage":
        return main_usage(args, t0_total)

    # Para --modo=transcribir, --imagen y --prompt son obligatorios.
    if not args.imagen or not args.prompt:
        sys.stderr.write("ERROR: --modo=transcribir requiere --imagen y --prompt.\n")
        return 3

    imagen = Path(args.imagen).resolve()
    prompt_path = Path(args.prompt).resolve()
    salida_json = Path(args.salida_json).resolve()
    sandbox_dir = Path(args.sandbox_dir).resolve()
    home_dir = Path(args.home_dir).resolve() if args.home_dir else None
    # Bundle forense: SIEMPRE dentro del workdir efímero (la mkdir se hace abajo
    # antes de lanzar agy, junto con `--log-file`). El lifecycle del workdir
    # (borrar / archivar a OneDrive) lo decide PHP post-QA — v18+.
    debug_dir = salida_json.parent / "debug"

    # ── Pre-flight (escribe veredicto ERROR y exit 3 sin tocar agy) ──
    if not imagen.is_file():
        _escribir_salida_temprana(salida_json, {
            "ok": False, "error": f"imagen_no_existe: {imagen}",
            "veredicto": "ERROR", "engine": "agy", "duracion_seg": 0.0,
            "response": "", "stdout_raw": "", "stderr_raw": "",
            "fin_presente": False, "websearch_detectado": False,
            "websearch_fuente": "none", "tools_used": [],
            "longitud_sospechosa": False, "stdout_largo_sospechoso": False,
            "estado_captura": "PREFLIGHT", "fuente_response": "vacio",
            "cuota_agotada": False,
            "tokens_input": 0, "tokens_output": 0, "tokens_thought": 0,
            "tokens_cached": 0, "tokens_total": 0,
            "statusline_disponible": False,
            "modelo_pedido": args.modelo_agy or "",
            "fecha_iso": datetime.now().isoformat(timespec='seconds'),
        })
        return 3
    if not prompt_path.is_file():
        _escribir_salida_temprana(salida_json, {
            "ok": False, "error": f"prompt_no_existe: {prompt_path}",
            "veredicto": "ERROR", "engine": "agy", "duracion_seg": 0.0,
            "response": "", "stdout_raw": "", "stderr_raw": "",
            "fin_presente": False, "websearch_detectado": False,
            "websearch_fuente": "none", "tools_used": [],
            "longitud_sospechosa": False, "stdout_largo_sospechoso": False,
            "estado_captura": "PREFLIGHT", "fuente_response": "vacio",
            "cuota_agotada": False,
            "tokens_input": 0, "tokens_output": 0, "tokens_thought": 0,
            "tokens_cached": 0, "tokens_total": 0,
            "statusline_disponible": False,
            "modelo_pedido": args.modelo_agy or "",
            "fecha_iso": datetime.now().isoformat(timespec='seconds'),
        })
        return 3

    # ── Limpieza de estado escribible de agy (fix freeze por scratch stale, 2026-06-25) ──
    # agy en -p lee @imagen.jpg desde <home>/.gemini/antigravity-cli/scratch, no
    # del sandbox; sin esto, una corrida que dejó copias ahí congela a todas las
    # siguientes. Se limpia ANTES de stagear los archivos frescos.
    _base_home = home_dir if home_dir is not None else Path.home()
    _scratch_dir = _base_home / ".gemini" / "antigravity-cli" / "scratch"
    # Conversaciones (.db SQLite) de agy: las usa _detectar_cuota_en_conversacion
    # post-captura para leer el 429 RESOURCE_EXHAUSTED que en `-p` no llega a
    # consola/history.
    _conv_dir = _base_home / ".gemini" / "antigravity-cli" / "conversations"
    _limpiar_estado_agy(sandbox_dir, _scratch_dir)

    # ── Sandbox: copia/decodifica imagen y prompt + .agents/settings.json ──
    err_sand = preparar_sandbox(sandbox_dir, imagen, prompt_path)
    if err_sand:
        _escribir_salida_temprana(salida_json, {
            "ok": False, "error": err_sand, "veredicto": "ERROR",
            "engine": "agy", "duracion_seg": round(time.time() - t0_total, 2),
            "response": "", "stdout_raw": "", "stderr_raw": "",
            "fin_presente": False, "websearch_detectado": False,
            "websearch_fuente": "none", "tools_used": [],
            "longitud_sospechosa": False, "stdout_largo_sospechoso": False,
            "estado_captura": "PREFLIGHT", "fuente_response": "vacio",
            "cuota_agotada": False,
            "tokens_input": 0, "tokens_output": 0, "tokens_thought": 0,
            "tokens_cached": 0, "tokens_total": 0,
            "statusline_disponible": False,
            "modelo_pedido": args.modelo_agy or "",
            "fecha_iso": datetime.now().isoformat(timespec='seconds'),
        })
        return 3

    # Servir la copia FRESCA del job también en el scratch de agy: lea del cwd
    # (=sandbox) o salga a buscar a su scratch, siempre obtiene la imagen de
    # ESTE job, nunca una vieja (fix freeze 2026-06-25).
    _stagear_en_scratch(_scratch_dir, sandbox_dir)

    # ── Rutas absolutas para @imagen.jpg y @prompt.md (fix exploración 2026-06-28) ──
    # La addenda agy en BD viene genérica (`@imagen.jpg`, `@prompt.md`), y agy en `-p`
    # las resuelve de forma NO determinística: a veces cwd (=sandbox), a veces scratch,
    # a veces "se va a explorar" con run_command (job 7123, Bump v12). Reemplazamos a
    # rutas absolutas para que el modelo NO tenga que elegir dónde buscar — el path
    # absoluto al sandbox es trusted (vía trustedWorkspaces), agy hace view_file
    # directo. Reemplazamos en TRES lugares:
    #   1) prompt.md del sandbox (lo lee agy desde el cwd).
    #   2) prompt.md del scratch (la copia que stagea _stagear_en_scratch).
    #   3) args.cmd_i (el comando que pasa lib_agy.php a agy con la mención a
    #      @prompt.md o @imagen.jpg).
    # Path absoluto = sandbox_dir.resolve() (distinto en PC vs laptop; por eso no
    # podemos hardcodear en la addenda de BD).
    #
    # v34: en `paste` esta reescritura se SALTEA entera. No se tipea ningún `@`
    # en el TUI — la tecla abre el menú interactivo de menciones y el Enter final
    # confirmaría el popup en vez de enviar el mensaje (medido). La imagen llega
    # adjunta por portapapeles y el prompt.md lo abre el modelo por su nombre.
    if args.cmd_mode == "paste":
        sys.stderr.write("[agy] cmd_mode=paste → salteo la reescritura de @paths "
                         "absolutos (en el TUI '@' abre el menú de menciones)\n")
    else:
        try:
            abs_sandbox = str(sandbox_dir.resolve())
            ref_imagen  = f"@{abs_sandbox}{os.sep}imagen.jpg"
            ref_prompt  = f"@{abs_sandbox}{os.sep}prompt.md"

            prompt_disk = sandbox_dir / "prompt.md"
            contenido = prompt_disk.read_text(encoding="utf-8")
            contenido = contenido.replace("@imagen.jpg", ref_imagen)
            contenido = contenido.replace("@prompt.md",  ref_prompt)
            prompt_disk.write_text(contenido, encoding="utf-8")

            if _scratch_dir is not None and (_scratch_dir / "prompt.md").exists():
                try:
                    (_scratch_dir / "prompt.md").write_text(contenido, encoding="utf-8")
                except Exception as e:
                    sys.stderr.write(f"[agy] WARN no pude reescribir scratch prompt.md: {e}\n")

            args.cmd_i = args.cmd_i.replace("@imagen.jpg", ref_imagen) \
                                   .replace("@prompt.md",  ref_prompt)
        except Exception as e:
            # Best-effort: si el reemplazo falla por alguna razón (archivo locked,
            # permisos), seguimos con las refs genéricas. agy a lo sumo cae al
            # comportamiento histórico (resolución no determinística por el scratch
            # resolver), que sigue cubierto por _limpiar_estado_agy + _stagear_en_scratch.
            sys.stderr.write(f"[agy] WARN no pude reescribir refs absolutas: {e}\n")

    # ── Modelo global (NO-OP si --modelo-agy vacío) ──
    err_mod = setear_modelo_global(home_dir, args.modelo_agy)
    if err_mod:
        sys.stderr.write(f"[agy] WARN setear_modelo_global: {err_mod}\n")
        # No abortamos: el modelo que esté en settings.json se usará tal cual;
        # el worker puede comparar después si quiere.

    # ── Env para el subprocess agy ──
    env = None
    if home_dir is not None:
        import os as _os
        env = dict(_os.environ)
        env["USERPROFILE"] = str(home_dir)
        env["HOME"] = str(home_dir)

    # ── Comando agy: -i (interactivo, TUI) o -p (print, no-interactivo) ──
    # cmd_mode='print' (-p): agy imprime el markdown CRUDO del modelo y cierra solo
    # (PROC_EXIT) → NO infla tablas (el TUI de -i las paddea a ancho de terminal, lo
    # que con cols=2000 inflaba las tablas markdown) y no necesita el taskkill. La
    # auth IGUAL exige el TTY de ConPTY: un pipe normal a `agy -p` sale vacío. El
    # resto del pipeline (ConPTY, pyte, extracción INICIO/FIN, kill/barrido) queda
    # idéntico. cmd_mode='interactive' (-i) es el legacy y el DEFAULT por compat
    # (transcriptor-manuscritos-v3 sigue en -i, no setea cmd_mode; prensa opta vía lib_agy).
    # Validado 2026-06-24: prensadelplata/WEB/temp/tests/2026-06-24_agy_print_{AB,C}.
    # `--log-file` de agy siempre activo: el logfile va al workdir efímero
    # (descartable si OK; viaja al archive_dir si !OK). Más caro en disco local
    # por job pero garantiza evidencia forense en TODOS los fallos sin depender
    # de un flag externo (regla: persistir toda info útil de los casos que
    # fallan, ver feedback memory + §"Bump v17" en motor_agy.md).
    #
    # cmd_mode='paste' (bump v34) — WORKAROUND TEMPORAL, bug upstream agy #735
    # (LS 1.1.10 manda inline_data vacío cuando el modelo abre una imagen con
    # view_file → INVALID_ARGUMENT 400).
    # https://github.com/google-antigravity/antigravity-cli/issues/735
    # El paste del TUI adjunta la imagen como media del mensaje y evita el
    # converter roto. argv SIN `-i` y SIN `-p`: si el mensaje se pasa por línea
    # de comandos, agy lo ENVÍA antes de que podamos pegar y el adjunto no viaja
    # (medido). Mismo gesto que `main_usage`, que abre el TUI con argv pelado.
    # Si el bug se arregla upstream: evaluar volver a 'print' (markdown crudo
    # sin TUI, sin tocar el portapapeles) y re-verificar los subsistemas
    # tocados (fuente db, señal imagen_cargada, chip, cols).
    agy_log_path: Optional[Path] = None
    if args.cmd_mode == "print":
        argv = [args.agy_bin, "-p", args.cmd_i, "--print-timeout", f"{int(args.timeout)}s"]
    elif args.cmd_mode == "paste":
        argv = [args.agy_bin]
    else:
        argv = [args.agy_bin, "-i", args.cmd_i]
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        agy_log_path = debug_dir / "agy_logfile.log"
        argv.extend(["--log-file", str(agy_log_path)])
    except Exception as e:
        sys.stderr.write(f"[agy] WARN no se pudo preparar debug_dir: {e}\n")
        agy_log_path = None

    # ── Geometría del PTY ──
    # En `paste` el TUI pinta cajas: con cols=2000 el chrome se deforma y el chip
    # del adjunto no queda legible. Si el caller dejó los defaults del CLI
    # (2000/100) usamos 220×80 (los del test que validó la mecánica); si pasó
    # valores explícitos distintos del default, los respetamos.
    cols_efectivo, rows_efectivo = args.cols, args.rows
    if args.cmd_mode == "paste":
        if args.cols == 2000:
            cols_efectivo = PASTE_COLS
        if args.rows == 100:
            rows_efectivo = PASTE_ROWS

    # ── Captura ──
    pids_prev = _pids_agy_actuales()
    t_epoch = time.time()
    sys.stderr.write(
        f"[agy] lanzando agy bajo ConPTY (cwd={sandbox_dir}, modo={args.cmd_mode}, "
        f"timeout={args.timeout}s, cols={cols_efectivo}, rows={rows_efectivo}, "
        f"grace={args.grace}s)\n"
    )

    if args.cmd_mode == "paste":
        # `args.cmd_i` NO se usa acá: el mensaje es el texto corto fijo, sin
        # ningún `@` (en el TUI la tecla abre el menú de menciones y el Enter
        # final confirmaría el popup en vez de enviar). Ver PASTE_TEXTO_CORTO_TPL.
        res = capturar_paste(
            argv,
            imagen_path=str(sandbox_dir / "imagen.jpg"),
            texto_corto=_texto_corto_paste(str(sandbox_dir.resolve())),
            cwd=str(sandbox_dir),
            env=env,
            debug_dir=debug_dir,
            cols=cols_efectivo, rows=rows_efectivo,
            timeout_seg=float(args.timeout),
            ini_marker=INI_MARKER, fin_marker=FIN_MARKER,
            verbose=True, progress_seg=15.0,
        )
    else:
        res = capturar(
            argv,
            cwd=str(sandbox_dir),
            env=env,
            cols=cols_efectivo, rows=rows_efectivo,
            timeout_seg=float(args.timeout),
            ini_marker=INI_MARKER, fin_marker=FIN_MARKER,
            fin_grace_seg=float(args.grace),
            quiescent_seg=float(args.quiescent_seg),
            verbose=True, progress_seg=15.0,
        )

    # ── Kill agresivo + barrido del LS detached ──
    if res.pid:
        _kill_arbol(res.pid)
    time.sleep(1.5)
    zombis = _barrido_zombis(pids_prev, t_epoch)
    if zombis:
        time.sleep(0.3)

    # ── Detección de cuota agotada (HTTP 429) en la .db de esta corrida ──
    # En `-p` el 429 RESOURCE_EXHAUSTED no llega a consola/history; vive sólo
    # en SQLite. Sin esto, una corrida agotada por cuota reportaría el genérico
    # `agy_exit_sin_datos` y el worker no podría rotar/cooldownear la cuenta.
    # Best-effort: si falla la lectura, sigue el flujo normal.
    try:
        _cuota, _reset_seg = _detectar_cuota_en_conversacion(str(_conv_dir), t_epoch)
        if _cuota:
            res.cuota_detectada = True
            res.cuota_reset_seg = _reset_seg
            sys.stderr.write(
                f"[agy] cuota_agotada detectada en .db (reset_seg={_reset_seg})\n"
            )
    except Exception as _e:
        sys.stderr.write(f"[agy] WARN _detectar_cuota_en_conversacion: {_e}\n")

    # ── Detección de fallo de arranque transitorio (pre-generación) ──
    # Si agy murió antes de llamar al LLM (backend 500 al resolver modelo,
    # auth/keyring timeout, etc.) NO consumió cuota → el worker lo reintenta
    # (cap) en vez de fail-fast a ERROR. Gate duro dentro del helper: si hubo
    # `streamGenerateContent`, NO es transitorio (posible cuota → ERROR). No
    # aplica si ya se detectó cuota (decidir_veredicto prioriza CUOTA).
    # Forense del log (v33): se lee UNA vez y se cuelga de `res` para que
    # `shape_salida` pueda distinguir un fallo pre-generación de uno
    # post-generación sin volver a abrir el archivo.
    try:
        _forense = _forense_arranque(agy_log_path)
        res.hubo_generacion = _forense["hubo_generacion"]
        res.subcausa_log    = _forense["subcausa"]
    except Exception as _e:
        res.hubo_generacion = None
        res.subcausa_log    = ""
        sys.stderr.write(f"[agy] WARN _forense_arranque: {_e}\n")

    try:
        _trans, _motivo = _detectar_arranque_transitorio(res, agy_log_path)
        if _trans:
            res.transitorio_detectado = True
            # v34: NO pisar un motivo ya puesto por la captura. Hoy el único que
            # llega acá con motivo previo es `paste_sin_chip` (agy quedó sano,
            # simplemente no adjuntamos), y esa etiqueta describe mejor el caso
            # que una firma de arranque que pueda haber quedado en el log.
            if not (getattr(res, "transitorio_motivo", "") or ""):
                res.transitorio_motivo = _motivo
            sys.stderr.write(
                f"[agy] arranque_transitorio detectado (motivo={_motivo}) "
                f"— sin generación → reintentable\n"
            )
    except Exception as _e:
        sys.stderr.write(f"[agy] WARN _detectar_arranque_transitorio: {_e}\n")

    # ── Parseo de la .db de conversación: tool calls + web signals (bump v23) ──
    # En `-p` el chrome del TUI no ecoa → `parsear_tool_calls` sobre history_text
    # queda ciego, y el grounding server-side (`vertexaisearch`) nunca se ve ni
    # en `-i`. La .db tiene todo. Best-effort; si falla, cae al comportamiento
    # heredado (tools_used del TUI + heurística substring). Ver
    # `notas/agy_1.1.3_permisos_read_file.md §5ª ronda` para el contexto.
    _db_info = {"db_path": None, "tool_calls": [], "web_signals": [], "web_urls": [],
                "citation_urls": [], "imagen_cargada": None,
                "imagen_tripleta": False, "imagen_png_media": None,
                "db_via": "", "db_identidad_ok": None}
    # Inicializado FUERA del try: si la lectura del log revienta, `_uuid` tiene
    # que existir igual — lo consume `resolver_texto_db` más abajo (sin esto,
    # un NameError tumbaría el job entero en vez de degradar a "no verificable").
    _uuid: Optional[str] = None
    try:
        # UUID de conversación del `agy_logfile.log` de ESTA corrida (v24) —
        # 100% determinístico. Si no hay log (agy no arrancó / --log-file
        # falló al preparar debug_dir), cae al fallback por mtime.
        _uuid = _uuid_conversacion_desde_log(agy_log_path)
        _db_info = _parsear_conversacion_db(str(_conv_dir), t_epoch, _uuid)
        sys.stderr.write(
            f"[agy] db_steps: db={os.path.basename(_db_info['db_path']) if _db_info['db_path'] else 'None'} "
            f"tools={len(_db_info['tool_calls'])} "
            f"web_signals={_db_info['web_signals']} "
            f"web_urls={_db_info['web_urls'][:5]} "
            f"citations={len(_db_info.get('citation_urls') or [])} "
            f"imagen_cargada_db={_db_info.get('imagen_cargada')} "
            f"uuid={_uuid or 'N/A'}\n"
        )
    except Exception as _e:
        sys.stderr.write(f"[agy] WARN _parsear_conversacion_db: {_e}\n")

    # ── Señal de imagen cargada: se cierra acá con la 3ª evidencia (v34) ──────
    # `_parsear_conversacion_db` ya resolvió con lo que da la .db (PNG en
    # `gen_metadata` como señal fuerte, tripleta de `steps` como fallback). Acá
    # se suma el `media=N` del `--log-file`, que es la señal propia del paste
    # (la imagen viaja como media del MENSAJE, no como resultado de un tool).
    # Ver `_resolver_imagen_cargada` para la evidencia medida y el porqué de que
    # la tripleta vieja NO entre en la disyunción.
    _media_log = None
    try:
        _media_log = _media_en_log(agy_log_path)
    except Exception as _e:
        sys.stderr.write(f"[agy] WARN _media_en_log: {_e}\n")
    res.media_en_log = _media_log

    # Propagar al `res` para que shape_salida lo lea (mismo patrón que
    # cuota_reset_seg / transitorio_motivo). None = no se pudo leer la DB
    # (unknown → PHP NO dispara el QA para no generar falso positivo).
    res.imagen_cargada_ok = _resolver_imagen_cargada(
        _db_info.get("imagen_png_media"),
        bool(_db_info.get("imagen_tripleta")),
        _media_log,
    )
    sys.stderr.write(
        f"[agy] imagen_cargada: png_gen_metadata={_db_info.get('imagen_png_media')} "
        f"tripleta_steps={_db_info.get('imagen_tripleta')} "
        f"media_en_log={_media_log} → {res.imagen_cargada_ok}\n"
    )

    # ── MODO `paste`: la transcripción sale de la .db, NO de la pantalla ──────
    # WORKAROUND TEMPORAL — bug upstream agy #735 (LS 1.1.10 manda inline_data
    # vacío cuando el modelo abre una imagen con view_file → INVALID_ARGUMENT
    # 400). https://github.com/google-antigravity/antigravity-cli/issues/735
    # El TUI renderiza el markdown y DESTRUYE los tags `<ilegible>`/`<dudoso>`
    # que el prompt de producción exige (medido: 271 en la .db vs 0 en el grid),
    # y cols=220 wrapea la prosa. Si la .db no da texto NO se cae a la pantalla:
    # `decidir_veredicto` devuelve TRANSITORIO/`db_sin_texto_paste`.
    # v37: el texto de la .db se busca en LOS TRES MODOS. En `paste` es la
    # fuente (v34); en `-p`/`-i` es camino de RESCATE, para los casos donde el
    # grid quedó vacío y la transcripción sobrevivió sólo en SQLite (job68768:
    # TIMEOUT de 600 s con 19.316 ch en la .db, hoy ERROR terminal).
    # Las guardas de autoría viven en `resolver_texto_db` (mismo código que
    # corre el harness de tests). El candidato viaja SIEMPRE aunque se rechace:
    # shape_salida lo manda al `response` cuando el veredicto no es OK, y de ahí
    # a `api_rawresponse`. El texto crudo que produjo agy no se tira nunca; lo
    # que se decide acá es sólo si además se puede PERSISTIR como vigente.
    _extr = resolver_texto_db(_db_info.get("db_path"), _uuid, res.hubo_generacion)
    res.db_identidad_ok  = _extr.get("identidad")
    res.db_candidato     = _extr.get("candidato")
    res.db_marcas_pagina = int(_extr.get("marcas") or 0)
    res.db_razonamiento  = bool(_extr.get("razonamiento"))
    res.db_rechazo       = str(_extr.get("rechazo") or "")
    _txt_db              = _extr.get("texto")
    res.response_db      = _txt_db
    res.db_fuente        = str(_extr.get("fuente") or "") if _txt_db else ""
    sys.stderr.write(
        f"[agy] texto de la .db: {len(_txt_db) if _txt_db else 0} chars "
        f"aceptados (candidato={len(res.db_candidato or '')} ch, "
        f"fuente={res.db_fuente or '-'}, marcas_pagina={res.db_marcas_pagina}, "
        f"step={_extr.get('step_idx')}, rechazo={res.db_rechazo or '-'}, "
        f"identidad={res.db_identidad_ok}, generacion={res.hubo_generacion})\n"
    )

    if args.cmd_mode == "paste":
        sys.stderr.write(
            f"[agy] paste: chip={getattr(res, 'paste_chip_detectado', False)} "
            f"reintentos={getattr(res, 'paste_reintentos', 0)} "
            f"clipboard_restaurado={getattr(res, 'clipboard_restaurado', False)}\n"
        )
        if not _txt_db and res.db_identidad_ok is True:
            # Sub-causa de la .db para el desambigüe del Fix 3 (v37). Dos gates:
            #   - sólo cuando NO hay texto (en el camino feliz sería una consulta
            #     SQLite de más por job — lección de performance del v35);
            #   - sólo con identidad probada: el error de OTRA conversación
            #     etiquetaría mal esta corrida, que es el mismo pecado que el
            #     texto ajeno. Sin identidad, decide el gate de generación (que
            #     sale del `--log-file` propio y no de la .db).
            try:
                res.diag_db = _diagnostico_errores_db(_db_info.get("db_path"))
            except Exception as _e:
                res.diag_db = {}
                sys.stderr.write(f"[agy] WARN _diagnostico_errores_db: {_e}\n")
            sys.stderr.write(f"[agy] paste sin texto → diagnostico .db: "
                             f"{(res.diag_db or {}).get('etiqueta') or '(sin firma)'}\n")

        if (not _txt_db
                and res.estado != "PASTE_SIN_CHIP"
                and not getattr(res, "loop_detectado", False)
                and not (getattr(res, "transitorio_motivo", "") or "")):
            # Etiqueta del reintento: sin esto el worker recibiría TRANSITORIO
            # con `transitorio_motivo` vacío. Los otros caminos ya traen la suya
            # (`paste_sin_chip` lo pone capturar_paste; el loop lo fuerza
            # shape_salida vía `firma_transitorio_motivo_forzado`; un fallo de
            # arranque ya etiquetado por `_detectar_arranque_transitorio` es más
            # específico que éste y gana).
            # v37: si la .db nombró una causa raíz reintentable (`eligibility_429`,
            # `401_unauthenticated`), ESA es la etiqueta — es la que dispara el
            # freno corto de cuenta en prensa (`agyFrenarCuentaPorRateLimit`).
            _diag = getattr(res, "diag_db", None) or {}
            res.transitorio_detectado = True
            res.transitorio_motivo = (
                str(_diag.get("etiqueta") or "") if _diag.get("reintentable") is True
                else "db_sin_texto_paste"
            )
    # v30: citations del checker de atribución/recitación → shape → PHP.
    res.citation_urls = _db_info.get("citation_urls") or []

    # ── tools_used: en `-p` viene de la .db; en `-i` del chrome del TUI ──
    # El chrome del TUI aparece en `history_text` sólo en `-i` (agy interactivo).
    # En `-p` es siempre "" → el fallback a la .db recupera lo perdido.
    tools_used_tui = parsear_tool_calls(res.history_text or "")
    if _db_info["tool_calls"]:
        tools_used = [
            {"name": t["name"], "args": t.get("args", "")}
            for t in _db_info["tool_calls"]
        ]
    else:
        tools_used = tools_used_tui

    # ── Detección WebSearch: .db (fuerte) > tools_used TUI > heurística ──
    if _db_info["web_signals"] or _db_info["web_urls"]:
        _pats = list(_db_info["web_signals"]) + [
            f"url:{h}" for h in _db_info["web_urls"][:8]
        ]
        ws_info = {"detectado": True, "patrones": _pats, "fuente": "db_steps"}
    else:
        ws_info = detectar_websearch(res, tools_used_tui)

    # ── Token usage real via statusLine side-channel (plan #3 §5) ──
    # Degrada limpio a tokens_*=0 si el setup manual del statusLine no está hecho.
    tokens = leer_token_usage(sandbox_dir)

    # ── Compose salida + escribir JSON ──
    out = shape_salida(
        res, args, t0_total, ws_info, tools_used, tokens, zombis, args.modelo_agy,
    )

    try:
        salida_json.parent.mkdir(parents=True, exist_ok=True)
        salida_json.write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as e:
        sys.stderr.write(f"FATAL: no pude escribir salida JSON: {e}\n")
        sys.stderr.write(traceback.format_exc())
        return 4

    # ── Debug dump: SIEMPRE (v18+) ──
    # El bundle va al workdir efímero (debug_dir = <workdir>/debug/). PHP corre
    # el QA post-corrida y decide ahí si borrar el workdir (ok + QA limpia) o
    # archivarlo (!ok o cualquier qa_sospecha). El bundle también permite
    # diagnosticar OKs sospechosos: SIN_FIN, EXPLORACION_AGY (subsume v13),
    # CAPTURA_TIMEOUT, WEBSEARCH no esperado, etc.
    if True:
        metrics = {
            "estado_captura": res.estado,
            "fin_visto": res.fin_visto,
            "duracion_seg": res.duracion_seg,
            "duracion_total_seg": out["duracion_seg"],
            "bytes_leidos": res.bytes_leidos,
            "pid": res.pid,
            "exitstatus": res.exitstatus,
            "error": res.error,
            "notas": res.notas,
            "zombis_barridos": zombis,
            "websearch": ws_info,
            "tools_used": tools_used,
            "db_steps": {
                "db_path": _db_info.get("db_path"),
                "tool_calls": _db_info.get("tool_calls") or [],
                "web_signals": _db_info.get("web_signals") or [],
                "web_urls": _db_info.get("web_urls") or [],
                # v30: evidencia de recitación enmascarada (NO de web). Ver
                # `_CITATION_URI_RE` y `notas/bloqueo_qa_recurrente.md`.
                "citation_urls": _db_info.get("citation_urls") or [],
                # v34: las 3 señales POR SEPARADO + la resuelta. Sin esto, un
                # falso positivo/negativo de `imagen_cargada` no se puede
                # diagnosticar desde el bundle sin reabrir la .db.
                "imagen_cargada": out["imagen_cargada_ok"],
                "imagen_tripleta_steps": bool(_db_info.get("imagen_tripleta")),
                "imagen_png_gen_metadata": _db_info.get("imagen_png_media"),
                "imagen_media_en_log": out["media_en_log"],
            },
            "tokens": tokens,
            "veredicto": out["veredicto"],
            "fuente_response": out["fuente_response"],
            "ok": out["ok"],
            "longitud_sospechosa": out["longitud_sospechosa"],
            "stdout_largo_sospechoso": out["stdout_largo_sospechoso"],
            # v31: loop degenerado de salida (ver §"Bump v31"). `chars_response`
            # es lo que se le recortó al `response` persistido.
            "loop": {
                "detectado":      out["loop_detectado"],
                "unidad":         out["loop_unidad"],
                "repeticiones":   out["loop_repeticiones"],
                "chars":          out["loop_chars"],
                "chars_response": out["loop_chars_response"],
            },
            "len_response": len(out["response"] or ""),
            "len_extracted_screen": len(res.extracted_screen or ""),
            "len_extracted_history": len(res.extracted_history or ""),
            "len_partial_from_ini": len(res.partial_from_ini or ""),
            "len_console_raw": len(res.console_raw or ""),
            # v34: `cmd_mode` no viajaba al bundle (deuda conocida) — sin él no
            # se puede saber, mirando un bundle archivado, con qué modo corrió.
            # `cols`/`rows` son los EFECTIVOS (en paste el default se sustituye).
            "cmd_mode": args.cmd_mode,
            "paste": {
                "chip_detectado":       out["paste_chip_detectado"],
                "reintentos":           out["paste_reintentos"],
                "clipboard_restaurado": out["clipboard_restaurado"],
                "len_response_db":      len(getattr(res, "response_db", None) or ""),
            },
            "cols": cols_efectivo, "rows": rows_efectivo,
            "timeout_seg": args.timeout, "grace_seg": args.grace,
            "launch_mode": args.launch_mode,
            "modelo_pedido": args.modelo_agy or "",
            "home_dir": str(home_dir) if home_dir else None,
            "sandbox_dir": str(sandbox_dir),
            "fecha_iso": out["fecha_iso"],
        }
        volcar_debug_bundle(debug_dir, res, metrics, agy_log_path)
        # v23: copia consistente de la .db de conversación al bundle (evidencia
        # cruda para auditar grounding web / tool calls / cuota que sólo viven
        # en SQLite bajo `-p`). Best-effort.
        if _db_info.get("db_path"):
            try:
                _copiar_conv_db_al_bundle(_db_info["db_path"], debug_dir)
            except Exception as _e:
                sys.stderr.write(f"[agy] WARN copia conversation.db: {_e}\n")

    sys.stderr.write(
        f"[agy] veredicto={out['veredicto']} ok={out['ok']} "
        f"fuente={out['fuente_response']} estado={out['estado_captura']} "
        f"fin={out['fin_presente']} ws={out['websearch_detectado']} "
        f"img={out['imagen_cargada_ok']} "
        f"tools={len(tools_used)} statusln={out['statusline_disponible']} "
        f"tok_in={out['tokens_input']} tok_out={out['tokens_output']} "
        f"len_resp={len(out['response'] or '')} dur={out['duracion_seg']}s "
        f"zombis={zombis}"
        + (f" LOOP(unidad={out['loop_unidad']!r} reps={out['loop_repeticiones']} "
           f"chars={out['loop_chars']} recortados={out['loop_chars_response']})"
           if out["loop_detectado"] else "")
        + (f" PASTE(chip={out['paste_chip_detectado']} "
           f"reint={out['paste_reintentos']} "
           f"clip_restaurado={out['clipboard_restaurado']} "
           f"media_log={out['media_en_log']})"
           if args.cmd_mode == "paste" else "")
        + "\n"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        sys.stderr.write(f"FATAL: {e}\n")
        sys.stderr.write(traceback.format_exc())
        sys.exit(4)
