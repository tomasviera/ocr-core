-- prompts/schema_compartido.sql — NÚCLEO COMPARTIDO del sistema de prompts.
--
-- DDL de las 3 tablas REUTILIZABLES (sin discriminadores de dominio) que el
-- render del core lee: prompt_bases, prompt_fragmentos, prompt_modelo_addenda.
-- Idéntico entre proyectos consumidores (prensadelplata, transcriptor v3) →
-- garantía dura anti-drift: la fuente de verdad de este DDL es el core.
--
-- Lo aplica el init_db.php de CADA proyecto (idempotente: IF NOT EXISTS en todo).
-- El core NO lo aplica: no abre conexión; sólo provee el archivo.
--
-- Notas de portabilidad:
--   - `*_proposito` (en bases) queda como TEXT/VARCHAR LIBRE (sin CHECK) a
--     propósito: cada proyecto usa su propio vocabulario
--     (v3: 'transcripcion'|'postproc'; prensa: 'transcripcion'|'revision_qa').
--     Mantener el CHECK fuera del schema compartido conserva el DDL idéntico.
--   - `*_usuariocreacion` / `*_usuarioarchivo` son INT NULL SIN FK (igual que
--     prensa): proyectos sin tabla `usuarios` (v3, single-user) los dejan en NULL.
--   - Los cargadores del core (cargarBaseActiva/Fragmento/Addenda) leen con
--     fetchColumn() por posición → funcionan bajo PDO::CASE_LOWER de cualquier
--     proyecto sin depender del casing de las columnas.

-- ─────────────────────────────────────────────────────────────────────────────
-- prompt_bases — bases reutilizables (texto componible con placeholders).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS prompt_bases (
    bas_codigo          BIGSERIAL   PRIMARY KEY,
    bas_nombre          VARCHAR     NOT NULL,
    bas_texto           TEXT        NOT NULL,
    bas_notas           TEXT        NULL,
    bas_linaje          BIGINT      NULL,
    bas_version         INTEGER     NOT NULL DEFAULT 1,
    bas_activo          BOOLEAN     NOT NULL DEFAULT TRUE,
    bas_fechacreacion   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    bas_fechaarchivo    TIMESTAMPTZ NULL,
    bas_usuariocreacion INTEGER     NULL,
    bas_usuarioarchivo  INTEGER     NULL,
    bas_proposito       VARCHAR     NOT NULL DEFAULT 'transcripcion'
);
CREATE INDEX IF NOT EXISTS idx_bases_linaje
    ON prompt_bases (bas_linaje, bas_activo);
CREATE INDEX IF NOT EXISTS idx_bases_proposito_activas
    ON prompt_bases (bas_proposito) WHERE bas_activo = TRUE;

-- ─────────────────────────────────────────────────────────────────────────────
-- prompt_fragmentos — piezas reutilizables incluibles por slug ({{> slug}}).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS prompt_fragmentos (
    fra_codigo          BIGSERIAL   PRIMARY KEY,
    fra_slug            VARCHAR     NOT NULL,
    fra_texto           TEXT        NOT NULL,
    fra_notas           TEXT        NULL,
    fra_linaje          BIGINT      NULL,
    fra_version         INTEGER     NOT NULL DEFAULT 1,
    fra_activo          BOOLEAN     NOT NULL DEFAULT TRUE,
    fra_fechacreacion   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    fra_fechaarchivo    TIMESTAMPTZ NULL,
    fra_usuariocreacion INTEGER     NULL,
    fra_usuarioarchivo  INTEGER     NULL
);
CREATE INDEX IF NOT EXISTS idx_fragmentos_linaje
    ON prompt_fragmentos (fra_linaje, fra_activo);
-- Unicidad del slug ACTIVO: el render hace LIMIT 1, esto lo vuelve determinista.
CREATE UNIQUE INDEX IF NOT EXISTS idx_fragmentos_slug_activo
    ON prompt_fragmentos (fra_slug) WHERE fra_activo = TRUE;

-- ─────────────────────────────────────────────────────────────────────────────
-- prompt_modelo_addenda — addenda específica por familia de modelo.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS prompt_modelo_addenda (
    mad_codigo          BIGSERIAL   PRIMARY KEY,
    mad_familia         VARCHAR     NOT NULL,
    mad_texto           TEXT        NOT NULL,
    mad_notas           TEXT        NULL,
    mad_linaje          BIGINT      NULL,
    mad_version         INTEGER     NOT NULL DEFAULT 1,
    mad_activo          BOOLEAN     NOT NULL DEFAULT TRUE,
    mad_fechacreacion   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    mad_fechaarchivo    TIMESTAMPTZ NULL,
    mad_usuariocreacion INTEGER     NULL,
    mad_usuarioarchivo  INTEGER     NULL
);
CREATE INDEX IF NOT EXISTS idx_addenda_linaje
    ON prompt_modelo_addenda (mad_linaje, mad_activo);
-- Unicidad de la familia ACTIVA (el render hace LIMIT 1).
CREATE UNIQUE INDEX IF NOT EXISTS idx_addenda_familia_activa
    ON prompt_modelo_addenda (mad_familia) WHERE mad_activo = TRUE;
