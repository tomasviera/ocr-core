#!/usr/bin/env python
"""
scripts/aistudio_check_login.py

Probe LIVIANO de login de una cuenta AI Studio web. NO transcribe, NO sube
imagen, NO gasta cuota: se conecta al Chrome por CDP, navega a AI Studio y
responde dos cosas:

  1. ¿Hay sesión de Google activa? (si AI Studio redirige a accounts.google.com
     → deslogueada).
  2. ¿Con qué email? (best-effort: lee el aria-label / texto del chip de cuenta
     de Google en el DOM, incluyendo iframes del OneGoogle bar).

Pensado como análogo barato al probe OAuth del CLI (cli_probe_perfil.php), pero
para el pool de Chromes/CDP de AI Studio. Lo invoca PHP (lib_aistudio.php
::aistudioCheckLoginCDP) y, en background al arrancar, el supervisor.

Es un script SEPARADO de transcribir_aistudio.py a propósito: el wrapper de
transcripción es un pipeline que ya funciona y no se toca. La única lógica
compartida es el boilerplate de connect_over_cdp (~20 líneas), duplicación
aceptada para aislar el path delicado.

Uso:
  python scripts/aistudio_check_login.py --cdp http://localhost:9223
  python scripts/aistudio_check_login.py --cdp http://localhost:9223 \\
      --email-esperado donsarasa2998@gmail.com \\
      --salida-json <path>

Salida: SIEMPRE imprime el JSON del resultado a stdout. Si se pasa --salida-json,
también lo escribe ahí. Shape:
  {
    "ok": bool,              # el probe pudo correr (CDP respondió y navegó)
    "logueado": bool|null,   # true=sesión activa, false=redirigió a login, null=no se pudo determinar
    "email": str|null,       # email detectado (best-effort), o null si no se extrajo
    "email_esperado": str|null,
    "email_coincide": bool|null,  # null si no se pudo comparar (email no extraído o sin esperado)
    "email_raw": str|null,   # el aria-label/texto crudo de donde salió el email (para auditar el selector)
    "url_final": str|null,
    "detalle": str,
    "duracion_seg": float
  }

Exit codes:
  0 = el probe corrió y escribió el JSON (logueado puede ser true/false/null adentro)
  2 = no se pudo conectar a CDP (Chrome no responde en ese puerto)
  4 = excepción inesperada
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time

try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.stderr.write("ERROR: falta playwright. Instalá con:  pip install playwright\n")
    sys.exit(4)


# Regex de email "razonable" (no RFC completo; alcanza para gmail/workspace).
EMAIL_RE = re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')

# URL de AI Studio. new_chat es la misma que usa el wrapper de transcripción;
# si hay sesión NO redirige; si no la hay, Google manda a accounts.google.com.
AISTUDIO_URL = "https://aistudio.google.com/prompts/new_chat"


def parse_args():
    p = argparse.ArgumentParser(description="Probe de login AI Studio via CDP (no transcribe)")
    p.add_argument("--cdp", required=True, help="Endpoint CDP del Chrome de la cuenta (http://localhost:PUERTO).")
    p.add_argument("--email-esperado", default=None, dest="email_esperado",
                   help="Email que se espera logueado (para comparar). Opcional.")
    p.add_argument("--salida-json", default=None, dest="salida_json",
                   help="Si se pasa, escribe el JSON del resultado ahí además de stdout.")
    p.add_argument("--timeout", type=int, default=20,
                   help="Segundos máximo para navegar/asentar la página (default 20).")
    return p.parse_args()


# JS liviano: SOLO escanea [aria-label] + mailto (NO document.querySelectorAll
# de todos los div/span, que sobre el DOM pesado de AI Studio era lento). El
# email confiable vive en el aria-label del chip de cuenta del OneGoogle bar:
# "Cuenta de Google: Nombre (email@gmail.com)" / "Google Account: Name (email)".
_JS_EMAIL = r"""
() => {
  const out = [];
  const emailRe = /[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}/;
  for (const el of document.querySelectorAll('[aria-label]')) {
    const lbl = el.getAttribute('aria-label') || '';
    if (!emailRe.test(lbl)) continue;
    const esCuenta = /google account|cuenta de google|account:/i.test(lbl);
    out.push({ label: lbl, score: esCuenta ? 100 : 40 });
  }
  for (const a of document.querySelectorAll('a[href^="mailto:"]')) {
    out.push({ label: a.getAttribute('href') || '', score: 60 });
  }
  return out;
}
"""


async def _escanear_email_una_vez(page) -> tuple[str | None, str | None]:
    """Un barrido (documento + iframes). Devuelve (email, raw_label) o (None, None)."""
    candidatos: list[dict] = []
    for frame in page.frames:
        try:
            res = await frame.evaluate(_JS_EMAIL)
            if isinstance(res, list):
                candidatos.extend(res)
        except Exception:
            continue
    if not candidatos:
        return None, None
    candidatos.sort(key=lambda c: c.get("score", 0), reverse=True)
    for c in candidatos:
        m = EMAIL_RE.search(c.get("label", "") or "")
        if m:
            return m.group(0).lower(), c.get("label")
    return None, None


async def _extraer_email(page, espera_seg: float = 12.0) -> tuple[str | None, str | None]:
    """
    Poll acotado: reintenta el barrido cada 600ms hasta `espera_seg` o hasta que
    aparezca el chip de cuenta. El OneGoogle bar carga un toque después del shell;
    poll en vez de wait_for_load_state('networkidle') (AI Studio nunca queda
    idle — mantiene websockets — y esa espera comía 8s fijos por cuenta logueada).
    """
    deadline = time.time() + espera_seg
    while True:
        email, raw = await _escanear_email_una_vez(page)
        if email:
            return email, raw
        if time.time() >= deadline:
            return None, None
        await page.wait_for_timeout(600)


# Botones para cerrar/aceptar popups de onboarding/promo (en/es). Mismo criterio
# que transcribir_aistudio.py::_aceptar_popups; duplicado a propósito para no
# acoplar el probe (liviano) al wrapper de transcripción (ver docstring).
_POPUP_ACCEPT = {
    'got it', 'got it!', 'dismiss', 'ok', 'okay', 'acknowledge', 'no thanks',
    'no, thanks', 'close', 'done', 'continue', 'maybe later',
    'entendido', 'aceptar', 'cerrar', 'omitir', 'no, gracias', 'listo',
    'continuar', 'de acuerdo', 'más tarde', 'mas tarde',
}


async def _aceptar_popups(page, rondas: int = 2) -> int:
    """Best-effort: cierra overlays de onboarding al abrir la cuenta, para que el
    primer job no choque con el backdrop. No fatal. Devuelve cuántos cerró."""
    cerrados = 0
    for _ in range(rondas):
        algo = False
        try:
            botones = page.locator(
                'mat-dialog-container button, [role="dialog"] button, .cdk-overlay-pane button'
            )
            for i in range(await botones.count()):
                b = botones.nth(i)
                try:
                    if not await b.is_visible():
                        continue
                    txt = (await b.inner_text() or '').strip().lower()
                    aria = (await b.get_attribute('aria-label') or '').strip().lower()
                except Exception:
                    continue
                if txt in _POPUP_ACCEPT or aria in _POPUP_ACCEPT:
                    try:
                        await b.click(timeout=2000)
                        cerrados += 1
                        algo = True
                        await page.wait_for_timeout(400)
                        break
                    except Exception:
                        pass
        except Exception:
            pass
        if not algo:
            try:
                if await page.locator('.cdk-overlay-backdrop').first.is_visible(timeout=500):
                    await page.keyboard.press('Escape')
                    await page.wait_for_timeout(300)
                    algo = True
                    cerrados += 1
            except Exception:
                pass
        if not algo:
            break
    return cerrados


async def amain():
    args = parse_args()
    t0 = time.time()
    out = {
        "ok": False, "logueado": None, "email": None,
        "email_esperado": (args.email_esperado.lower() if args.email_esperado else None),
        "email_coincide": None, "email_raw": None,
        "url_final": None, "detalle": "", "duracion_seg": 0.0,
    }

    def _emitir(exit_code: int):
        out["duracion_seg"] = round(time.time() - t0, 2)
        blob = json.dumps(out, ensure_ascii=False, indent=2)
        if args.salida_json:
            try:
                from pathlib import Path
                Path(args.salida_json).parent.mkdir(parents=True, exist_ok=True)
                Path(args.salida_json).write_text(blob, encoding='utf-8')
            except Exception as e:
                sys.stderr.write(f"[login] no se pudo escribir salida-json: {e}\n")
        print(blob)
        sys.exit(exit_code)

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(args.cdp, timeout=10000)
        except Exception as e:
            out["detalle"] = f"cdp_unreachable: {e}"
            _emitir(2)

        if not browser.contexts:
            out["detalle"] = "cdp_sin_contexts"
            _emitir(2)

        ctx = browser.contexts[0]
        page = await ctx.new_page()
        page.set_default_timeout(args.timeout * 1000)

        # Forzar viewport amplio via CDP (igual que el wrapper de transcripción):
        # la ventana del Chrome suele estar minimizada y eso throttlea timers/
        # render; el override de devtools lo mitiga y sobrevive al minimizado.
        try:
            cdp = await ctx.new_cdp_session(page)
            await cdp.send("Emulation.setDeviceMetricsOverride", {
                "width": 1600, "height": 1000, "deviceScaleFactor": 1, "mobile": False,
            })
        except Exception as e:
            sys.stderr.write(f"[login] WARN no se pudo forzar viewport: {e}\n")

        try:
            await page.goto(AISTUDIO_URL, wait_until="domcontentloaded", timeout=args.timeout * 1000)
            out["url_final"] = page.url

            if "accounts.google.com" in page.url:
                out["ok"] = True
                out["logueado"] = False
                out["detalle"] = "redirigió a accounts.google.com: no hay sesión activa en este Chrome."
            else:
                out["ok"] = True
                out["logueado"] = True
                out["detalle"] = "sesión activa (no redirigió a login)."
                # Cerrar popups de onboarding/promo apenas se abre la cuenta, así
                # el primer job no choca con el backdrop (lo que pediste para el
                # arranque del sistema). Best-effort, no afecta el veredicto.
                try:
                    cerrados = await _aceptar_popups(page)
                    if cerrados:
                        out["popups_cerrados"] = cerrados
                        sys.stderr.write(f"[login] popups cerrados: {cerrados}\n")
                except Exception as e:
                    sys.stderr.write(f"[login] WARN aceptar_popups: {e}\n")
                # Poll acotado al chip de cuenta (sin networkidle, ver _extraer_email).
                email, raw = await _extraer_email(page, espera_seg=12.0)
                out["email"] = email
                out["email_raw"] = raw
                out["url_final"] = page.url
                if email and out["email_esperado"]:
                    out["email_coincide"] = (email == out["email_esperado"])
        except PWTimeout as e:
            out["ok"] = False
            out["detalle"] = f"timeout navegando a AI Studio: {e}"
        except Exception as e:
            out["ok"] = False
            out["detalle"] = f"excepcion: {e}"
        finally:
            # Cerrar SIEMPRE el tab del probe (no es el del usuario; lo abrimos nosotros).
            try:
                await page.close()
            except Exception:
                pass
            try:
                await browser.close()  # cierra la conexión CDP, no el Chrome real
            except Exception:
                pass

    _emitir(0)


def main():
    try:
        asyncio.run(amain())
    except SystemExit:
        raise
    except Exception as e:
        sys.stderr.write(f"FATAL: {e}\n")
        import traceback
        sys.stderr.write(traceback.format_exc())
        sys.exit(4)


if __name__ == "__main__":
    main()
