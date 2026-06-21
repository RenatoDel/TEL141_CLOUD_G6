-- ============================================================================
-- 003_coach_per_course.sql
--
-- Asignación de coaches a cursos en relación M:N.
--
-- Antes de esta migración el rol "coach" era read-only GLOBAL: cualquier coach
-- veía todos los slices del sistema. Eso ya no encaja: queremos que cada coach
-- audite solo los cursos que le fueron asignados (análogo al profesor, pero
-- sin permiso de mutación).
--
-- Cambios:
--   - Nueva tabla curso_coach (curso_id, coach_id) con UNIQUE en el par.
--   - Idempotente (CREATE TABLE IF NOT EXISTS, INSERT IGNORE).
--
-- Aplicación:
--   sudo docker exec -i pucp_mariadb mariadb -u pucp -ppucp_pass pucp_cloud \
--     < sql/migrations/003_coach_per_course.sql
-- ============================================================================

USE pucp_cloud;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Tabla curso_coach (M:N coach ↔ curso)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS curso_coach (
  curso_id INT NOT NULL,
  coach_id INT NOT NULL,
  asignado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (curso_id, coach_id),
  CONSTRAINT fk_cc_curso
    FOREIGN KEY (curso_id) REFERENCES curso(id) ON DELETE CASCADE,
  CONSTRAINT fk_cc_coach
    FOREIGN KEY (coach_id) REFERENCES usuario(id) ON DELETE CASCADE
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Seed: coach1 → TEL141 (para que el demo del curso TEL141 muestre coach)
-- ─────────────────────────────────────────────────────────────────────────────
INSERT IGNORE INTO curso_coach (curso_id, coach_id)
  SELECT c.id, u.id
    FROM curso c
    JOIN usuario u ON u.username = 'coach1'
   WHERE c.codigo = 'TEL141';
