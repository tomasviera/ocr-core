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
      [--cmd-mode interactive|print]   # print = -p (markdown crudo, sin inflar tablas)

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

# Comando -i corto (plan §D + smoke validado): referencia @prompt.md + @imagen.jpg.
# Evita el límite de 8191 chars del cmdline; el prompt completo está en el
# archivo dentro del sandbox.
CMD_I_DEFAULT = ("Transcribí la imagen @imagen.jpg siguiendo al pie de la letra "
                 "las instrucciones de @prompt.md. No uses búsqueda web.")

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
    estado: str                       # OK_FIN | QUIESCENT_NO_MARKER | TIMEOUT | PROC_EXIT | ERROR_SPAWN
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

            if verbose and (now - last_tick) >= progress_seg:
                last_tick = now
                cand = "sí" if candidato_at is not None else "no"
                print(f"  [capturar] t={int(now - t0)}s bytes={total_bytes} "
                      f"candidato={cand} alive={proc.isalive()}",
                      file=sys.stderr, flush=True)

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
    return res


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
      - `db_path`     : path de la .db elegida (para copiar al bundle), o None
      - `tool_calls`  : list[{"name":str,"args":str,"step_idx":int}]
      - `web_signals` : list[str] — patrones canónicos matcheados
      - `web_urls`    : list[str] — hosts URL http(s) no-google encontrados

    Best-effort: cualquier excepción → dict con listas vacías y db_path=None.
    """
    out = {"db_path": None, "tool_calls": [], "web_signals": [], "web_urls": []}

    # ── Vía 1 (preferida): UUID exacto del logfile ──
    steps_strings = None  # [(idx, printable_str_blob), ...]
    chosen_db = None
    if uuid_conversacion:
        cand = os.path.join(conv_dir, f"{uuid_conversacion}.db")
        if os.path.isfile(cand):
            rows = _leer_steps_db(cand)
            if rows is not None:
                chosen_db = cand
                steps_strings = _steps_a_strings(rows)

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
        best = None  # (db_path, steps_strings)
        for db in sorted(dbs, key=os.path.getmtime, reverse=True)[:10]:
            rows = _leer_steps_db(db)
            if rows is None:
                continue
            ss = _steps_a_strings(rows)
            if best is None or len(ss) > len(best[1]):
                best = (db, ss)
        if best is None:
            return out
        chosen_db, steps_strings = best[0], best[1]

    out["db_path"] = chosen_db

    seen_signals = set()
    seen_hosts = set()
    prev_call = None  # (canon_name, args_snippet_head) para dedupar plan+ejec

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
            if hl not in seen_hosts:
                seen_hosts.add(hl)
                out["web_urls"].append(hl)
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

      OK    : response no vacío (cualquier fuente).
      ERROR : spawn falló, o todas las fuentes vacías.
      CUOTA : reservado (no se detecta auto todavía; handoff #4).
    """
    if res.estado == "ERROR_SPAWN":
        return ("ERROR", "", res.error or "spawn_agy_fallo", False, "vacio")

    # 0) Cuota agotada (HTTP 429): si `_detectar_cuota_en_conversacion` halló
    #    el RESOURCE_EXHAUSTED en la .db de esta corrida, devolvemos CUOTA
    #    antes que cualquier otra cosa. Con esto el wrapper PHP (lib_agy +
    #    worker) ya no ve `agy_exit_sin_datos` enmascarando un 429, sino el
    #    veredicto correcto → cooldown + rotación. El `cuota_reset_seg` lo
    #    leva shape_salida al dict de salida.
    if getattr(res, "cuota_detectada", False):
        return ("CUOTA", "",
                "cuota_agotada: 429 RESOURCE_EXHAUSTED (Individual quota reached)",
                False, "cuota")

    # 1) Camino preferido: hubo INICIO_MARKER en el history.
    partial = (res.partial_from_ini or "").strip()
    if partial and len(partial) >= MIN_CONTENT_LEN:
        fuente = "ini_fin" if res.fin_visto else "ini_only"
        return ("OK", partial, None, bool(res.fin_visto), fuente)

    # 2) Sin INICIO: caemos al history limpio (de-renderizado por pyte). PHP
    #    discrimina con QA bits según `fuente_response`.
    history = (res.history_text or "").strip()
    if history and len(history) >= MIN_CONTENT_LEN:
        return ("OK", history, None, False, "history")

    # 3) Último recurso: snapshot de la pantalla visible.
    screen = (res.screen_snapshot or "").strip()
    if screen and len(screen) >= MIN_CONTENT_LEN:
        return ("OK", screen, None, False, "screen")

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
    # Estos chequeos van ANTES del de exploracion_agy, así el original queda
    # como último resort para el caso conversacional real. Retrocompat:
    # - Un caso que hoy cae en exploracion_agy y no matchea A/B/C sigue
    #   cayendo en exploracion_agy con la misma etiqueta.
    # - El shape no cambia claves ni tipos.
    # - veredicto=TRANSITORIO ya está mapeado por lib_worker_policy.php
    #   (agyAccionResultado → RETRY → AgyTransitorioException).
    resp_stripped = (response or "").strip()
    firma_transitorio_motivo_forzado = None
    firma_detectada = None
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
        firma_detectada = "B_executor_terminated"
        ok = False
        veredicto = "TRANSITORIO"
        firma_transitorio_motivo_forzado = "executor_terminated_prearranque"
        error = (
            "agy_terminado_prearranque: response literal 'Error: Agent execution "
            "terminated due to error.' (agy salió antes de generar). Sub-causas "
            "habituales en debug/agy_logfile.log: 'neither PlanModel nor RequestedModel', "
            "UNAUTHENTICATED (401), 'model unreachable' (red), HTTP 502. Reintentable"
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
        "cuota_agotada": (veredicto == "CUOTA"),
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
                        "en \\n real al reconstruir desde el grid de pyte).")
    p.add_argument("--rows", type=int, default=100,
                   help="Filas (default 100; generoso para evitar truncado del viewport).")
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
                        "sin-imagen (postproceso) para no pedir transcribir la imagen dummy.")
    p.add_argument("--cmd-mode", default="interactive", dest="cmd_mode",
                   choices=["interactive", "print"],
                   help="interactive=-i (TUI legacy, default por compat); print=-p "
                        "(no-interactivo: agy imprime markdown crudo SIN inflar tablas y "
                        "cierra solo). prensa opta a 'print' via lib_agy; v2 sigue en -i.")
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
    agy_log_path: Optional[Path] = None
    if args.cmd_mode == "print":
        argv = [args.agy_bin, "-p", args.cmd_i, "--print-timeout", f"{int(args.timeout)}s"]
    else:
        argv = [args.agy_bin, "-i", args.cmd_i]
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        agy_log_path = debug_dir / "agy_logfile.log"
        argv.extend(["--log-file", str(agy_log_path)])
    except Exception as e:
        sys.stderr.write(f"[agy] WARN no se pudo preparar debug_dir: {e}\n")
        agy_log_path = None

    # ── Captura ──
    pids_prev = _pids_agy_actuales()
    t_epoch = time.time()
    sys.stderr.write(
        f"[agy] lanzando agy bajo ConPTY (cwd={sandbox_dir}, "
        f"timeout={args.timeout}s, cols={args.cols}, rows={args.rows}, "
        f"grace={args.grace}s)\n"
    )

    res = capturar(
        argv,
        cwd=str(sandbox_dir),
        env=env,
        cols=args.cols, rows=args.rows,
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
    try:
        _trans, _motivo = _detectar_arranque_transitorio(res, agy_log_path)
        if _trans:
            res.transitorio_detectado = True
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
    _db_info = {"db_path": None, "tool_calls": [], "web_signals": [], "web_urls": []}
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
            f"uuid={_uuid or 'N/A'}\n"
        )
    except Exception as _e:
        sys.stderr.write(f"[agy] WARN _parsear_conversacion_db: {_e}\n")

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
            },
            "tokens": tokens,
            "veredicto": out["veredicto"],
            "fuente_response": out["fuente_response"],
            "ok": out["ok"],
            "longitud_sospechosa": out["longitud_sospechosa"],
            "stdout_largo_sospechoso": out["stdout_largo_sospechoso"],
            "len_response": len(out["response"] or ""),
            "len_extracted_screen": len(res.extracted_screen or ""),
            "len_extracted_history": len(res.extracted_history or ""),
            "len_partial_from_ini": len(res.partial_from_ini or ""),
            "len_console_raw": len(res.console_raw or ""),
            "cols": args.cols, "rows": args.rows,
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
        f"tools={len(tools_used)} statusln={out['statusline_disponible']} "
        f"tok_in={out['tokens_input']} tok_out={out['tokens_output']} "
        f"len_resp={len(out['response'] or '')} dur={out['duracion_seg']}s "
        f"zombis={zombis}\n"
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
